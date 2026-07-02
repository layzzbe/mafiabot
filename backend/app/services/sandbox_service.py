"""Super-admin sandbox orchestration.

Builds + drives a synthetic Mafia game inside Redis using fake users,
without ever touching real Telegram or the real users / games tables.
The engine code is reused verbatim; only the `Bot` instance is swapped
for `SandboxBot` (which captures every send into a transcript).

Lifecycle:
  create_sandbox  → SandboxSession + GameState(WAITING) in Redis
  start_sandbox   → assign roles + transition to NIGHT + start PhaseManager
  inject_callback → synthetic CallbackQuery → existing handler logic
  stop_sandbox    → snapshot transcript to DB + clear Redis

This module is the only one allowed to allocate sandbox IDs and the
only one that knows how to glue the engine's `Bot` parameter to a
`SandboxBot`. Everything else stays oblivious.
"""

from __future__ import annotations

import asyncio
import random
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from uuid import UUID

from loguru import logger

from app.core.sandbox_ids import (
    alloc_group_id,
    alloc_user_id,
    is_sandbox_user_id,
)
from app.core.state import GameState, NightAction, Phase, PlayerState, Team, Vote
from app.db.models import (
    AdminAccount,
    SandboxAutoPlayMode,
    SandboxSession,
    SandboxStatus,
    SandboxTimingPreset,
)
from app.db.models.group import (
    DEFAULT_AFK,
    DEFAULT_DISPLAY,
    DEFAULT_GAMEPLAY,
    DEFAULT_ITEMS_ALLOWED,
    DEFAULT_PERMISSIONS,
    DEFAULT_ROLES_ENABLED,
    DEFAULT_SILENCE,
    DEFAULT_TIMINGS,
)
from app.services import game_service, transcript_store
from app.services.recording_bot import SandboxBot, SandboxBotRegistry
from app.services.transcript_store import FakeUser

if TYPE_CHECKING:
    from aiogram import Bot


class SandboxError(Exception):
    """Domain error from sandbox service."""


# === Timing presets ===========================================================
# Override the group's default timings so test games run in seconds, not
# minutes. The numbers are tuned for "watch a full game in under two
# minutes" — admin can override per-session via `timings` in the config.

_TIMING_PRESETS: dict[str, dict[str, int]] = {
    "fast": {
        "registration": 5,
        "night": 15,
        "day": 10,
        "mafia_vote": 10,
        "hanging_vote": 10,
        "hanging_confirm": 8,
        "last_words": 5,
        "afsungar_carry": 10,
    },
    "normal": dict(DEFAULT_TIMINGS),  # same as production
    "slow": {k: v * 2 for k, v in DEFAULT_TIMINGS.items()},
    # Manual mode disables auto-advance: every timer is huge so the
    # PhaseManager only progresses when sandbox_service.advance_phase is
    # called explicitly.
    "manual": dict.fromkeys(DEFAULT_TIMINGS, 10**9),
}

# Default fake-player names, cycled by player_idx % len. Two-syllable
# Uzbek/Russian/English first names so the dashboard reads cleanly.
_FAKE_NAMES = [
    "Asad",
    "Lola",
    "Olim",
    "Dilshod",
    "Nigora",
    "Karim",
    "Madina",
    "Bekzod",
    "Saida",
    "Otabek",
    "Zarina",
    "Rustam",
    "Gulnoza",
    "Sherzod",
    "Malika",
    "Jasur",
    "Feruza",
    "Bobur",
    "Sevara",
    "Akmal",
    "Munira",
    "Doniyor",
    "Kamola",
    "Ulug'bek",
    "Mehrinoz",
    "Anvar",
    "Shahnoza",
    "Komron",
    "Dilnoza",
    "Sardor",
]


@dataclass
class SandboxCreateConfig:
    """Inputs accepted by `create_sandbox`."""

    n_players: int
    language: str = "uz"
    mafia_ratio: str = "low"  # "low" | "high"
    auto_play_mode: SandboxAutoPlayMode = SandboxAutoPlayMode.PAUSED
    timing_preset: SandboxTimingPreset = SandboxTimingPreset.FAST
    # Optional role-enabled overrides; merged on top of DEFAULT_ROLES_ENABLED.
    roles_enabled: dict[str, bool] | None = None
    # Optional per-N role-distribution override. Keys are player-count strings
    # ("8"), values are exact role rosters of that length. Matches the group
    # `role_distribution` setting and the engine `override` path.
    role_distribution: dict[str, list[str]] | None = None
    # Optional explicit timings override (otherwise derived from preset).
    timings: dict[str, int] | None = None
    seed: int | None = None  # reserved for deterministic role distribution
    # Custom names — falls back to `_FAKE_NAMES` cycle if missing/short.
    custom_names: list[str] = field(default_factory=list)


def _settings_snapshot(config: SandboxCreateConfig) -> dict:
    """Build a `GameState.settings` dict from a sandbox config + defaults."""
    timings = config.timings or _TIMING_PRESETS[config.timing_preset.value]
    roles = dict(DEFAULT_ROLES_ENABLED)
    if config.roles_enabled:
        roles.update(config.roles_enabled)
    gameplay = dict(DEFAULT_GAMEPLAY)
    gameplay["mafia_ratio"] = config.mafia_ratio
    return {
        "language": config.language,
        "timings": timings,
        "silence": dict(DEFAULT_SILENCE),
        "items_allowed": dict(DEFAULT_ITEMS_ALLOWED),
        "permissions": dict(DEFAULT_PERMISSIONS),
        "gameplay": gameplay,
        "display": dict(DEFAULT_DISPLAY),
        "afk": dict(DEFAULT_AFK),
        "messages": {},
        "roles": roles,
        # Per-N role roster override. Empty/absent → `distribute_roles` falls
        # back to the default table + enabled filter (see start_game).
        "role_distribution": dict(config.role_distribution or {}),
    }


async def _allocate_session_seq() -> int:
    """Stable per-process counter for ID allocation.

    The DB row count is monotonic enough for our needs — collisions are
    impossible thanks to the 64-bit ID range, and even if the counter
    reused a value, the only thing keyed by `session_seq` is the fake
    user/group ID which is then bound to a UUID `sandbox_id` for all
    lookups. Counting is O(rows) but that's fine: sessions are rare and
    creation is admin-driven, not user-driven.
    """
    return await SandboxSession.all().count()


async def create_sandbox(admin: AdminAccount, config: SandboxCreateConfig) -> SandboxSession:
    """Build a fresh sandbox: fake users + GameState(WAITING) + DB row."""
    if not (4 <= config.n_players <= 30):
        raise SandboxError(f"n_players must be 4..30, got {config.n_players}")

    session_seq = await _allocate_session_seq()
    fake_group_id = alloc_group_id(session_seq)

    # Build the fake roster.
    fake_users: list[FakeUser] = []
    for i in range(config.n_players):
        uid = alloc_user_id(session_seq, i)
        name = (
            config.custom_names[i]
            if i < len(config.custom_names) and config.custom_names[i]
            else _FAKE_NAMES[i % len(_FAKE_NAMES)]
        )
        fake_users.append(
            FakeUser(
                user_id=uid,
                first_name=name,
                username=f"sandbox_p{i + 1}",
                language_code=config.language,
            )
        )

    settings = _settings_snapshot(config)
    creator_uid = fake_users[0].user_id

    # Create the DB row first so the UUID is durable even if Redis writes
    # fail downstream — at worst we get a stuck-in-CREATED row that the
    # dashboard can clean up.
    session = await SandboxSession.create(
        created_by=admin,
        status=SandboxStatus.CREATED,
        fake_group_id=fake_group_id,
        n_players=config.n_players,
        auto_play_mode=config.auto_play_mode,
        timing_preset=config.timing_preset,
        settings_snapshot=settings,
        fake_users_snapshot=[
            {
                "user_id": u.user_id,
                "first_name": u.first_name,
                "username": u.username,
                "language_code": u.language_code,
                # Filled by `start_sandbox` once roles are distributed.
                "role": None,
                "team": None,
            }
            for u in fake_users
        ],
    )

    # Persist the fake roster + meta to Redis so subsequent handlers
    # (especially the user-loader middleware) can resolve fake IDs.
    await transcript_store.set_fake_users(session.id, fake_users)
    await transcript_store.set_meta(
        session.id,
        {
            "sandbox_id": str(session.id),
            "fake_group_id": fake_group_id,
            "creator_user_id": creator_uid,
            "auto_play_mode": config.auto_play_mode.value,
            "timing_preset": config.timing_preset.value,
            "language": config.language,
            "n_players": config.n_players,
        },
    )

    # Build the WAITING-phase GameState and seed all players directly.
    state = GameState(
        group_id=fake_group_id,
        chat_id=fake_group_id,
        phase=Phase.WAITING,
        settings=settings,
        creator_user_id=creator_uid,
    )
    reg_secs = settings["timings"].get("registration", 5)
    state.phase_ends_at = int(time.time()) + reg_secs

    for i, fu in enumerate(fake_users, start=1):
        state.players.append(
            PlayerState(
                user_id=fu.user_id,
                username=fu.username,
                first_name=fu.first_name,
                join_order=i,
                role="citizen",  # placeholder — set in start_sandbox
                team=Team.CITIZENS,
            )
        )

    await game_service.save_state(state)
    logger.info(
        f"Sandbox {session.id} created with {config.n_players} fake players "
        f"(group={fake_group_id}, mode={config.auto_play_mode.value}, "
        f"timing={config.timing_preset.value})"
    )
    return session


async def start_sandbox(session_id: UUID) -> SandboxSession:
    """Assign roles + transition to NIGHT + start PhaseManager.

    Returns the updated `SandboxSession`. After this call the dashboard
    will start seeing transcript entries flow from the engine: role-reveal
    DMs, first night prompts, atmospheric messages.

    Idempotent on RUNNING — a double-click in the dashboard (stale React
    Query cache showing the old "created" status) returns the session
    as-is instead of raising. Terminal states still error so the caller
    knows the action was a no-op.
    """
    session = await SandboxSession.get(id=session_id).prefetch_related("created_by")
    if session.status == SandboxStatus.RUNNING:
        return session
    if session.status != SandboxStatus.CREATED:
        raise SandboxError(f"sandbox {session_id} is already {session.status.value}, cannot start")

    state = await game_service.load_state(session.fake_group_id)
    if state is None:
        raise SandboxError(
            f"sandbox {session_id} has no Redis state (likely TTL'd) — destroy + recreate"
        )

    # Wire the recording bot before any engine code runs so every send
    # (role-reveal DM, first-night prompts) lands in the transcript.
    bot = SandboxBot(
        sandbox_id=session.id,
        fake_group_id=session.fake_group_id,
        creator_fake_user_id=state.creator_user_id or 0,
    )
    SandboxBotRegistry.register(bot)

    try:
        await game_service.start_game(state)
    except Exception:
        SandboxBotRegistry.unregister(session.fake_group_id)
        session.status = SandboxStatus.ERRORED
        await session.save(update_fields=["status"])
        raise

    # Capture assigned roles into the session row for the dashboard list.
    role_by_uid = {p.user_id: (p.role, p.team.value) for p in state.players}
    for entry in session.fake_users_snapshot:
        role, team = role_by_uid.get(entry["user_id"], (None, None))
        entry["role"] = role
        entry["team"] = team

    session.status = SandboxStatus.RUNNING
    session.started_at = datetime.now(UTC)
    await session.save(update_fields=["status", "started_at", "fake_users_snapshot"])

    # Kick off the timer loop. The `_on_phase_change` import is local to
    # break a circular import (handlers/group/game imports services).
    from app.bot.handlers.group.game import _on_phase_change
    from app.core.phases.manager import PhaseManager

    # SandboxBot is a duck-typed stand-in for aiogram.Bot (see recording_bot
    # module docstring); this module is the sole glue layer, so we cast at
    # the engine boundary rather than teach the engine about SandboxBot.
    engine_bot = cast("Bot", bot)

    async def _hook(s: GameState) -> None:
        await _on_phase_change(engine_bot, s)

    PhaseManager.start_for(bot=engine_bot, group_id=session.fake_group_id, on_phase_change=_hook)
    _start_auto_loop_if_enabled(session)

    logger.info(
        f"Sandbox {session.id} started — {len(state.players)} players, "
        f"first night at round {state.round_num}, auto_mode={session.auto_play_mode.value}"
    )
    return session


# === Callback injection =======================================================
#
# Dashboard button-click → REST endpoint → this function → aiogram dispatcher
# → existing CallbackQuery handler. The handler's `bot` parameter is the
# SandboxBot, so every reply/edit it issues lands in the transcript.


_update_seq = 0


def _next_update_id() -> int:
    """Monotonic synthetic update_id. aiogram doesn't actually validate the
    value, but having something unique makes debug logs sortable."""
    global _update_seq
    _update_seq += 1
    return 10**9 + _update_seq


async def _ensure_runtime(session: SandboxSession) -> Bot:
    """Rebuild the in-memory runtime (bot, phase loop, auto loop) for a
    RUNNING sandbox whose memory state was lost — typically after the
    backend process restarted.

    Without this, every control + callback request would fail with
    "no live bot instance" after a deploy, even though the Redis state
    and DB row are intact. Re-registering the SandboxBot and restarting
    the loops is idempotent and cheap.

    Returns the SandboxBot typed as `Bot` — callers only pass it to the
    engine / dispatcher, which speak `Bot` (see recording_bot docstring).
    """
    existing = SandboxBotRegistry.get(session.fake_group_id)
    if existing is not None:
        return cast("Bot", existing)

    if session.status != SandboxStatus.RUNNING:
        raise SandboxError(f"sandbox {session.id} is {session.status.value} — start_sandbox first?")

    state = await game_service.load_state(session.fake_group_id)
    if state is None:
        # Redis TTL'd while we were down. Mark the session ERRORED so the
        # dashboard stops trying to drive a corpse.
        session.status = SandboxStatus.ERRORED
        await session.save(update_fields=["status"])
        raise SandboxError(
            f"sandbox {session.id} lost its Redis state (TTL or eviction); destroy + recreate"
        )

    bot = SandboxBot(
        sandbox_id=session.id,
        fake_group_id=session.fake_group_id,
        creator_fake_user_id=state.creator_user_id or 0,
    )
    SandboxBotRegistry.register(bot)

    # Restart the timer + auto-play loops. PhaseManager.start_for is a
    # no-op if a task already exists, so calling it again is safe.
    from app.bot.handlers.group.game import _on_phase_change
    from app.core.phases.manager import PhaseManager

    engine_bot = cast("Bot", bot)

    async def _hook(s: GameState) -> None:
        await _on_phase_change(engine_bot, s)

    PhaseManager.start_for(bot=engine_bot, group_id=session.fake_group_id, on_phase_change=_hook)
    _start_auto_loop_if_enabled(session)
    logger.info(
        f"Sandbox {session.id} runtime rebuilt — bot+loops re-registered "
        f"(likely after backend restart)"
    )
    return engine_bot


async def inject_callback(
    sandbox_id: UUID,
    fake_user_id: int,
    callback_data: str,
    message_id: int,
    chat_id: int | None = None,
) -> None:
    """Push a synthetic CallbackQuery through the bot dispatcher.

    The handler that owns `callback_data`'s prefix (registered in
    `app.bot.dispatcher.setup_routers`) runs with the SandboxBot as its
    `bot` parameter; any `bot.send_message` / `bot.edit_message_text` it
    issues lands in the transcript and emits a `transcript_append` WS
    event to the dashboard. The user-loader middleware patch (§task 6)
    hydrates the fake user without touching the DB.

    `chat_id` defaults to `fake_user_id` (the case for night-action
    prompts and other DM-only callbacks). Pass it explicitly only for
    group-chat callbacks (registration "Join", voting buttons, etc.).
    """
    if not is_sandbox_user_id(fake_user_id):
        raise SandboxError(f"inject_callback rejected: user_id {fake_user_id} is not a sandbox id")

    session = await SandboxSession.get(id=sandbox_id)
    bot = await _ensure_runtime(session)

    fake = await transcript_store.lookup_user(fake_user_id)
    if fake is None:
        raise SandboxError(f"fake user {fake_user_id} missing from sandbox {sandbox_id}")

    # Build aiogram types lazily — imports are heavy and they pull in
    # pydantic-aware model validation, which we only need at injection time.
    from aiogram.types import CallbackQuery, Chat, Message, Update
    from aiogram.types import User as AiogramUser

    if chat_id is None:
        chat_id = fake_user_id

    tg_user = AiogramUser(
        id=fake_user_id,
        is_bot=False,
        first_name=fake.get("first_name") or "Player",
        username=fake.get("username"),
        language_code=fake.get("language_code") or "uz",
    )
    chat_type = "private" if chat_id == fake_user_id else "supergroup"
    chat = Chat(id=chat_id, type=chat_type)
    msg = Message(message_id=message_id, date=datetime.now(UTC), chat=chat, from_user=tg_user)
    cb = CallbackQuery(
        id=f"sandbox-{_next_update_id()}",
        from_user=tg_user,
        chat_instance=str(chat_id),
        data=callback_data,
        message=msg,
    )
    update = Update(update_id=_next_update_id(), callback_query=cb)

    # Lazy import — main.py wires the dispatcher at app startup, and
    # importing it at module top would create a cycle.
    from app.main import dp

    if dp is None:
        raise SandboxError("dispatcher not initialized — is the bot running?")

    await dp.feed_update(bot=bot, update=update)
    logger.debug(
        f"sandbox {sandbox_id}: injected cb '{callback_data}' "
        f"for user {fake_user_id} on msg {message_id}"
    )


async def inject_message(
    sandbox_id: UUID,
    fake_user_id: int,
    text: str,
    chat_id: int | None = None,
) -> None:
    """Inject a synthetic text Message as if `fake_user_id` typed it.

    Mirrors `inject_callback` but for plain-text messages — the operator
    types in the DM panel (or the group panel), and the dispatcher routes
    the synthetic Update to whichever handler owns it. The two
    sandbox-relevant downstream cases are:

      * Private + alive mafia at night → `mafia_chat.relay_mafia_message`
        rebroadcasts to other mafia DMs, exactly like the real bot.
      * Private + dead within last-words window → `broadcast_last_words`
        posts to the group transcript.

    `chat_id` defaults to `fake_user_id` (the DM scope). Pass a group_id
    explicitly only for group-scope messages — most testing happens in DMs.
    """
    if not is_sandbox_user_id(fake_user_id):
        raise SandboxError(f"inject_message rejected: user_id {fake_user_id} is not a sandbox id")
    if not text or not text.strip():
        raise SandboxError("inject_message rejected: empty text")

    session = await SandboxSession.get(id=sandbox_id)
    bot = await _ensure_runtime(session)

    fake = await transcript_store.lookup_user(fake_user_id)
    if fake is None:
        raise SandboxError(f"fake user {fake_user_id} missing from sandbox {sandbox_id}")

    from aiogram.types import Chat, Message, Update
    from aiogram.types import User as AiogramUser

    if chat_id is None:
        chat_id = fake_user_id

    tg_user = AiogramUser(
        id=fake_user_id,
        is_bot=False,
        first_name=fake.get("first_name") or "Player",
        username=fake.get("username"),
        language_code=fake.get("language_code") or "uz",
    )
    chat_type = "private" if chat_id == fake_user_id else "supergroup"
    chat = Chat(id=chat_id, type=chat_type)
    msg = Message(
        message_id=await transcript_store.next_message_id(sandbox_id),
        date=datetime.now(UTC),
        chat=chat,
        from_user=tg_user,
        text=text,
    )
    update = Update(update_id=_next_update_id(), message=msg)

    # Also surface the operator's typed message inside the transcript so
    # they can see what they "said" in the chat panel — otherwise the
    # only visible output would be the bot's relay/forward.
    from app.services.transcript_store import TranscriptEntry

    safe_text = text.replace("<", "&lt;").replace(">", "&gt;").replace("&", "&amp;")
    display_name = fake.get("first_name") or f"#{fake_user_id}"
    sender_entry = TranscriptEntry(
        type="send",
        scope="dm" if chat_type == "private" else "group",
        chat_id=chat_id,
        target_user_id=chat_id if chat_type == "private" else None,
        message_id=msg.message_id,
        text=f"👤 <b>{display_name}</b>: {safe_text}",
        parse_mode="HTML",
        extra={"from_user_id": fake_user_id, "user_authored": True},
    )
    await transcript_store.append(sandbox_id, sender_entry)
    # Mirror the broadcast hook so the dashboard's WS picks it up. The
    # bot's `_persist` does the same for its own sends; we open-code here
    # because the user-authored entry doesn't go through SandboxBot.
    try:
        from dataclasses import asdict

        from app.services.ws_broker import emit_game_event

        await emit_game_event(
            "transcript_append",
            group_id=session.fake_group_id,
            sandbox_id=str(session.id),
            entry=asdict(sender_entry),
        )
    except Exception as e:  # pragma: no cover — never let WS break the loop
        logger.debug(f"transcript_append WS emit (user msg) failed: {e}")

    from app.main import dp

    if dp is None:
        raise SandboxError("dispatcher not initialized — is the bot running?")

    await dp.feed_update(bot=bot, update=update)
    logger.debug(f"sandbox {sandbox_id}: injected msg from user {fake_user_id} on chat {chat_id}")


async def advance_phase(sandbox_id: UUID) -> Phase | None:
    """Manually advance the sandbox by one phase.

    Used by the dashboard's "⏭ Skip phase" button and by manual_mode
    sandboxes whose timing preset disables the regular timer loop.
    Returns the new phase, or `None` if the game has already finished.

    Rebuilds the in-memory runtime if it was lost (typically after a
    backend restart) so the operator can still drive an existing RUNNING
    sandbox without recreating it.
    """
    session = await SandboxSession.get(id=sandbox_id)
    bot = await _ensure_runtime(session)

    from app.bot.handlers.group.game import _on_phase_change
    from app.core.phases.manager import PhaseManager

    async def _hook(s: GameState) -> None:
        await _on_phase_change(bot, s)

    return await PhaseManager.tick_once(
        bot=bot, group_id=session.fake_group_id, on_phase_change=_hook, force=True
    )


# === Finish / cancel / destroy ================================================
#
# `on_game_finish` and `on_game_cancel` are the engine-side hooks the
# game_service calls when the game terminates naturally (winner found)
# or is cancelled. `stop_sandbox` and `destroy_sandbox` are the
# operator-facing controls that the REST router exposes.


async def _resolve_session_from_state(state: GameState) -> SandboxSession | None:
    """Find the SandboxSession row for a running game.

    The link is `state.group_id == session.fake_group_id`. Returns None
    if no matching row — that's a race (session deleted while game was
    finishing) and the caller should fall back to best-effort cleanup.
    """
    return await SandboxSession.filter(fake_group_id=state.group_id).first()


async def _persist_final_snapshot(
    session: SandboxSession,
    state: GameState,
    *,
    status: SandboxStatus,
    winner_team: str | None,
) -> None:
    """Common DB write at terminal lifecycle events (finish/cancel/destroy).

    Captures the final GameState (full history JSON), dumps the Redis
    transcript into `sandbox_transcript_entries`, and updates summary
    + status fields. Idempotent against re-entry — once the session is
    in a terminal status we leave it alone.
    """
    if session.status in (
        SandboxStatus.FINISHED,
        SandboxStatus.DESTROYED,
        SandboxStatus.ERRORED,
    ):
        return

    summary = await transcript_store.dump_to_db(session.id, session.id)

    session.final_state = state.to_history_dict()
    session.winner_team = winner_team
    session.status = status
    session.finished_at = datetime.now(UTC)
    session.transcript_summary = summary
    await session.save(
        update_fields=[
            "final_state",
            "winner_team",
            "status",
            "finished_at",
            "transcript_summary",
        ]
    )


async def _teardown_runtime(session: SandboxSession) -> None:
    """Cancel timer task + unregister bot + clear Redis transcript keys.

    Safe to call multiple times — each step is independent and idempotent.
    """
    from app.core.phases.manager import PhaseManager

    PhaseManager.cancel_for(session.fake_group_id)
    _cancel_auto_loop(session.fake_group_id)
    SandboxBotRegistry.unregister(session.fake_group_id)
    user_ids = [u["user_id"] for u in (session.fake_users_snapshot or [])]
    await transcript_store.clear(session.id, user_ids=user_ids)


async def on_game_finish(state: GameState, winner) -> None:
    """Called by `game_service.finish_game` for sandbox games.

    `finish_game` will already have set state.phase=FINISHED and cleared
    the Redis GameState — we just need to snapshot the transcript +
    update the session row + clean up runtime registries.
    """
    session = await _resolve_session_from_state(state)
    if session is None:
        logger.warning(
            f"sandbox finish hook fired but no SandboxSession matched "
            f"fake_group_id={state.group_id}"
        )
        return
    winner_str = getattr(winner, "value", None) if winner is not None else None
    await _persist_final_snapshot(
        session, state, status=SandboxStatus.FINISHED, winner_team=winner_str
    )
    await _teardown_runtime(session)
    logger.info(f"Sandbox {session.id} finished (winner={winner_str})")


async def on_game_cancel(state: GameState, reason: str) -> None:
    """Called by `game_service.cancel_game` for sandbox games."""
    session = await _resolve_session_from_state(state)
    if session is None:
        return
    await _persist_final_snapshot(session, state, status=SandboxStatus.DESTROYED, winner_team=None)
    await _teardown_runtime(session)
    logger.info(f"Sandbox {session.id} cancelled: {reason}")


async def stop_sandbox(sandbox_id: UUID) -> SandboxSession:
    """Operator-initiated termination. Engine sees this as a `cancel_game`."""
    session = await SandboxSession.get(id=sandbox_id)
    if session.status in (
        SandboxStatus.FINISHED,
        SandboxStatus.DESTROYED,
        SandboxStatus.ERRORED,
    ):
        return session
    state = await game_service.load_state(session.fake_group_id)
    if state is not None:
        # Lets game_service.cancel_game drive the cleanup so the engine's
        # invariants hold (Phase.CANCELLED set, registration message wiped,
        # etc.) — it will reach back into us via the on_game_cancel hook.
        await game_service.cancel_game(state, reason="sandbox-stop")
    else:
        # State already vanished (TTL or race) — finalize directly so the
        # session row doesn't stay in RUNNING forever.
        await _persist_final_snapshot(
            session,
            GameState(group_id=session.fake_group_id, chat_id=session.fake_group_id),
            status=SandboxStatus.DESTROYED,
            winner_team=None,
        )
        await _teardown_runtime(session)
    # Reload to pick up the status changed by the hook.
    return await SandboxSession.get(id=sandbox_id)


async def destroy_sandbox(sandbox_id: UUID) -> None:
    """Operator-initiated destroy — same as stop but the session row is
    deleted afterwards so it disappears from the dashboard list.

    Use sparingly — `stop_sandbox` is the friendlier default since it
    keeps the post-mortem snapshot accessible.
    """
    session = await stop_sandbox(sandbox_id)
    await session.delete()
    logger.info(f"Sandbox {sandbox_id} destroyed")


# === Auto-play loop ===========================================================
#
# Background task per sandbox that submits actions on behalf of fake
# players who haven't already been driven by the dashboard. Three
# modes (see SandboxAutoPlayMode):
#   - PAUSED         → loop never starts; admin must click every action
#   - AUTO           → role-aware "sane" choices
#   - RANDOM_ACTIONS → any valid target, no strategy
#
# Decoupled from injection: actions are written directly into
# state.current_actions / current_votes — no synthetic CallbackQuery
# round-trip. This is faster (no aiogram dispatch) and cleaner (auto
# actions don't need to render confirmation toasts in the transcript).

_auto_tasks: dict[int, asyncio.Task] = {}  # fake_group_id → task

# How long after a NIGHT/VOTING/HANGING_CONFIRM transition we wait before
# submitting auto-actions. Gives the admin a window to click manually
# without having to race the bot.
_AUTO_DELAY_SECONDS = 4.0
# Poll interval while sleeping between events.
_POLL_INTERVAL_SECONDS = 1.5


async def _auto_play_loop(sandbox_id: UUID, fake_group_id: int, mode: str) -> None:
    """Owned by start_sandbox; cancelled by _teardown_runtime."""
    last_acted_round: tuple[int, str] | None = None
    while True:
        try:
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
            state = await game_service.load_state(fake_group_id)
            if state is None or state.phase in (Phase.FINISHED, Phase.CANCELLED):
                return
            if state.phase not in (Phase.NIGHT, Phase.VOTING, Phase.HANGING_CONFIRM):
                # WAITING/DAY have no per-player actions to auto-submit.
                continue
            # Only act once per (round, phase) — don't re-write actions
            # the dashboard may have overwritten manually after our first pass.
            phase_key = (state.round_num, state.phase.value)
            if last_acted_round == phase_key:
                continue
            # Wait the grace window AFTER the phase started so the admin
            # has time to click manually if they want.
            # Skip if we're very close to the deadline (last 1s).
            if state.phase_ends_at is not None and state.phase_ends_at - int(_now()) < 1:
                continue
            await asyncio.sleep(_AUTO_DELAY_SECONDS)
            # Re-load — admin may have advanced the phase during the delay.
            state = await game_service.load_state(fake_group_id)
            if state is None or state.phase in (Phase.FINISHED, Phase.CANCELLED):
                return
            if (state.round_num, state.phase.value) != phase_key:
                continue
            await _submit_auto_actions(state, mode)
            await game_service.save_state(state)
            last_acted_round = phase_key
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.exception(f"auto-play loop {sandbox_id} crashed: {e}")
            # Don't tight-loop on persistent errors.
            await asyncio.sleep(5)


def _now() -> float:
    return time.time()


async def _submit_auto_actions(state: GameState, mode: str) -> None:
    """Mutate state with auto-actions for players who haven't acted yet.

    Also surfaces the same atmospheric group messages the real handler
    chain produces ("🤵🏻 Don navbatdagi o'ljasini tanladi…" etc.) so the
    sandbox operator can see the AI actually moving instead of looking
    at a silent group chat until end-of-night resolution.
    """
    from app.core.roles import get_role

    bot = SandboxBotRegistry.get(state.group_id)
    submitted: list[tuple[PlayerState, str]] = []

    alive_players = state.alive_players()
    if state.phase == Phase.NIGHT:
        for actor in alive_players:
            if actor.user_id in state.current_actions:
                continue  # already acted (admin or previous auto pass)
            role = get_role(actor.role)
            if not role.has_night_action:
                continue
            target = _pick_night_target(role, actor, state, mode)
            if target is None:
                continue
            action_kind = _night_action_type_for(actor.role)
            state.current_actions[actor.user_id] = NightAction(
                actor_id=actor.user_id,
                role=actor.role,
                action_type=action_kind,
                target_id=target.user_id,
            )
            submitted.append((actor, action_kind))

        # Reuse the handler's atmosphere logic so the auto path and the
        # interactive path produce the same group-chat output. Imported
        # lazily because it pulls in aiogram types we don't need until
        # we actually have a SandboxBot to broadcast through.
        if bot is not None and submitted:
            from app.bot.handlers.private.role_actions import _broadcast_role_atmosphere

            engine_bot = cast("Bot", bot)
            for actor, action_kind in submitted:
                try:
                    await _broadcast_role_atmosphere(engine_bot, state, actor, action_kind)
                except Exception as e:  # pragma: no cover — never let UI break the loop
                    logger.debug(f"auto-play atmosphere broadcast failed for {actor.role}: {e}")
        return
    if state.phase == Phase.VOTING:
        for voter in alive_players:
            if voter.user_id in state.current_votes:
                continue
            target = _pick_vote_target(voter, state, mode)
            if target is None:
                continue
            weight = 2 if voter.role == "mayor" else 1
            state.current_votes[voter.user_id] = Vote(
                voter_id=voter.user_id, target_id=target.user_id, weight=weight
            )
        return
    if state.phase == Phase.HANGING_CONFIRM:
        # 50/50 yes/no, tally lives in current_round().extra
        round_log = state.current_round()
        confirm = round_log.extra.setdefault(
            "hanging_confirm", {"yes": {}, "no": {}, "target_id": None}
        )
        target_id = round_log.extra.get("pending_hang_target")
        if target_id is None:
            return
        confirm["target_id"] = target_id
        for voter in alive_players:
            uid_str = str(voter.user_id)
            if uid_str in confirm["yes"] or uid_str in confirm["no"]:
                continue
            bucket = "yes" if random.random() < 0.5 else "no"
            confirm[bucket][uid_str] = 2 if voter.role == "mayor" else 1
        # Recompute tallies so the engine can read them.
        round_log.extra["hang_yes_total"] = sum(confirm["yes"].values())
        round_log.extra["hang_no_total"] = sum(confirm["no"].values())


def _night_action_type_for(role: str) -> str:
    """Map role code → primary night action type for the NightAction record."""
    return {
        "don": "kill",
        "mafia": "vote",
        "killer": "kill",
        "lawyer": "protect",
        "journalist": "check",
        "detective": "check",
        "sergeant": "check",
        "doctor": "heal",
        "hooker": "sleep",
        "hobo": "visit",
        "maniac": "kill",
        "arsonist": "queue",
        "snitch": "check",
    }.get(role, "act")


def _pick_night_target(role, actor, state: GameState, mode: str):
    candidates = role.valid_targets(state, actor)
    if not candidates:
        return None
    if mode == SandboxAutoPlayMode.RANDOM_ACTIONS.value:
        return random.choice(candidates)
    # AUTO mode — light role-aware preferences.
    code = actor.role
    if code == "doctor":
        # Doctor avoids self unless every other choice is dead.
        non_self = [c for c in candidates if c.user_id != actor.user_id] or candidates
        return random.choice(non_self)
    if code in ("don", "mafia", "killer", "maniac"):
        # Killing roles prefer non-team targets.
        from app.core.state import Team

        non_team = [c for c in candidates if c.team != actor.team] or candidates
        return random.choice(non_team)
    if code in ("detective", "sergeant"):
        # Inspectors prefer mafia / singleton (i.e. non-citizens).
        from app.core.state import Team

        suspects = [c for c in candidates if c.team != Team.CITIZENS] or candidates
        return random.choice(suspects)
    return random.choice(candidates)


def _pick_vote_target(voter, state: GameState, mode: str):
    candidates = [p for p in state.alive_players() if p.user_id != voter.user_id]
    if not candidates:
        return None
    if mode == SandboxAutoPlayMode.RANDOM_ACTIONS.value:
        return random.choice(candidates)
    # AUTO mode — citizens vote for non-citizens, mafia votes for citizens.
    from app.core.state import Team

    if voter.team == Team.MAFIA:
        # Don't lynch your own.
        targets = [c for c in candidates if c.team != Team.MAFIA] or candidates
    else:
        targets = [c for c in candidates if c.team != voter.team] or candidates
    return random.choice(targets)


def _start_auto_loop_if_enabled(session: SandboxSession) -> None:
    """Spawn the auto-play loop for a freshly-started sandbox."""
    if session.auto_play_mode == SandboxAutoPlayMode.PAUSED:
        return
    if session.fake_group_id in _auto_tasks and not _auto_tasks[session.fake_group_id].done():
        return
    task = asyncio.create_task(
        _auto_play_loop(session.id, session.fake_group_id, session.auto_play_mode.value)
    )
    _auto_tasks[session.fake_group_id] = task


def _cancel_auto_loop(fake_group_id: int) -> None:
    task = _auto_tasks.pop(fake_group_id, None)
    if task is None or task.done():
        return
    task.cancel()
    # Suppress the cancellation propagating — the loop catches CancelledError.
    with suppress(BaseException):
        pass


async def restart_sandbox(sandbox_id: UUID) -> SandboxSession:
    """Stop + create-fresh with the same admin + config + same player names.

    Returns the NEW SandboxSession — the caller should redirect the
    dashboard to its detail page. The old session row stays around
    (marked DESTROYED) so its transcript is still browsable post-mortem.
    """
    old = await SandboxSession.get(id=sandbox_id).prefetch_related("created_by")
    if old.status not in (
        SandboxStatus.FINISHED,
        SandboxStatus.DESTROYED,
        SandboxStatus.ERRORED,
    ):
        await stop_sandbox(sandbox_id)

    config = SandboxCreateConfig(
        n_players=old.n_players,
        language=old.settings_snapshot.get("language", "uz"),
        mafia_ratio=old.settings_snapshot.get("gameplay", {}).get("mafia_ratio", "low"),
        auto_play_mode=SandboxAutoPlayMode(old.auto_play_mode),
        timing_preset=SandboxTimingPreset(old.timing_preset),
        roles_enabled=dict(old.settings_snapshot.get("roles", {})),
        role_distribution=dict(old.settings_snapshot.get("role_distribution", {})),
        timings=dict(old.settings_snapshot.get("timings", {})),
        custom_names=[u.get("first_name") for u in (old.fake_users_snapshot or [])],
    )
    return await create_sandbox(old.created_by, config)
