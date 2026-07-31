"""Game lifecycle service — create, register, start, finish.

Stateless module functions. Uses Redis (or memory) for live state,
DB only on game finish (Game.history JSON + GameResult rows).
"""

from __future__ import annotations

import time
from datetime import UTC
from typing import TYPE_CHECKING, cast
from uuid import UUID

from loguru import logger

if TYPE_CHECKING:
    from aiogram import Bot

from app.core.distribution import distribute_mvp_roles
from app.core.redis_state import get_state_backend
from app.core.roles import get_role
from app.core.sandbox_ids import is_sandbox_state, is_sandbox_user_id
from app.core.state import (
    GameState,
    Phase,
    PlayerState,
    Team,
    game_state_key,
)
from app.db.models import Game, GameStatus, Group, User, WinnerTeam


class GameError(Exception):
    """Domain error from game service (i18n key as message)."""


# === State persistence ===


async def load_state(group_id: int) -> GameState | None:
    backend = get_state_backend()
    raw = await backend.get(game_state_key(group_id))
    if raw is None:
        return None
    return GameState.from_redis(raw)


async def save_state(state: GameState) -> None:
    backend = get_state_backend()
    await backend.set(game_state_key(state.group_id), state.to_redis())
    # Notify any open spectator socket that state has changed. The payload
    # is intentionally minimal — clients refetch /live to get the full
    # snapshot. Phase-specific events (phase_change) are still emitted
    # separately by the phase manager for richer UI cues.
    try:
        from app.services.ws_broker import emit_game_event

        await emit_game_event(
            "state_changed",
            group_id=state.group_id,
            phase=state.phase.value,
            round_num=state.round_num,
        )
    except Exception:
        pass


async def delete_state(group_id: int) -> None:
    backend = get_state_backend()
    await backend.delete(game_state_key(group_id))
    try:
        from app.services.ws_broker import emit_game_event

        await emit_game_event("game_cleared", group_id=group_id)
    except Exception:
        pass


# === User active game tracking ===


async def get_user_active_game_id(user_id: int) -> UUID | None:
    user = await User.get_or_none(id=user_id)
    if user is None or user.active_game_id is None:
        return None
    return user.active_game_id


async def set_user_active_game(user_id: int, game_id: UUID | None) -> None:
    # Sandbox fake users have no DB row — and the `users.id > 0` CHECK
    # constraint would loudly reject any INSERT into the negative range.
    # Treat as a no-op so the engine code stays branch-free.
    if is_sandbox_user_id(user_id):
        return
    user = await User.get_or_none(id=user_id)
    if user is None:
        return
    user.active_game_id = game_id
    await user.save(update_fields=["active_game_id"])


# === Game creation ===


async def create_game(
    group: Group, host_user: User, bounty_per_winner: int | None = None
) -> GameState:
    """Create a new game in WAITING phase."""
    if not group.onboarding_completed:
        raise GameError("game-onboarding-required")

    existing = await load_state(group.id)
    if existing is not None and existing.phase != Phase.FINISHED:
        raise GameError("game-already-running")

    await group.fetch_related("settings")
    settings = group.settings

    state = GameState(
        group_id=group.id,
        chat_id=group.id,
        phase=Phase.WAITING,
        settings=_settings_snapshot(settings),
        bounty_per_winner=bounty_per_winner,
        bounty_pool=bounty_per_winner * 10 if bounty_per_winner else None,
        bounty_initiator_id=host_user.id if bounty_per_winner else None,
        creator_user_id=host_user.id,
    )

    # Set phase end time (registration timeout)
    reg_secs = settings.timings.get("registration", 120) if settings else 120  # type: ignore[attr-defined,var-annotated,arg-type]
    state.phase_ends_at = int(time.time()) + reg_secs

    await save_state(state)
    logger.info(f"Game created in group {group.id} by user {host_user.id}")
    return state


def _settings_snapshot(settings: object) -> dict:
    """Extract settings JSON for game snapshot."""
    if settings is None:
        return {}
    return {
        "language": getattr(settings, "language", "uz"),
        "timings": getattr(settings, "timings", {}),
        "silence": getattr(settings, "silence", {}),
        "items_allowed": getattr(settings, "items_allowed", {}),
        "permissions": getattr(settings, "permissions", {}),
        "gameplay": getattr(settings, "gameplay", {}),
        "display": getattr(settings, "display", {}),
        "afk": getattr(settings, "afk", {}),
        "messages": getattr(settings, "messages", {}),
        "roles": getattr(settings, "roles", {}),
    }


# === Player registration ===


async def register_player(state: GameState, user: User) -> PlayerState:
    """Add user to game (registration phase)."""
    if state.phase != Phase.WAITING:
        raise GameError("join-no-active-registration")

    # Check if already in this game
    if state.get_player(user.id) is not None:
        raise GameError("join-already-in-this-game")

    max_players = state.settings.get("gameplay", {}).get("max_players", 30)
    if len(state.players) >= max_players:
        raise GameError("join-game-full")

    join_order = len(state.players) + 1
    player = PlayerState(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        join_order=join_order,
        role="citizen",  # placeholder — assigned at game start
        team=Team.CITIZENS,
    )
    state.players.append(player)
    await save_state(state)
    await set_user_active_game(user.id, state.id)
    logger.info(f"Player {user.id} joined game {state.id} (order={join_order})")
    return player


async def unregister_player(state: GameState, user_id: int) -> bool:
    """Remove player from registration (only WAITING phase)."""
    if state.phase != Phase.WAITING:
        return False
    state.players = [p for p in state.players if p.user_id != user_id]
    # Reorder
    for idx, p in enumerate(state.players, start=1):
        p.join_order = idx
    await save_state(state)
    await set_user_active_game(user_id, None)
    logger.info(f"Player {user_id} unregistered from game {state.id}")
    return True


# === Game start ===


async def start_game(state: GameState) -> GameState:
    """Transition WAITING → NIGHT (round 1). Assign roles."""
    if state.phase != Phase.WAITING:
        raise GameError("game-cannot-start-not-waiting")

    min_players = state.settings.get("gameplay", {}).get("min_players", 4)
    if len(state.players) < min_players:
        raise GameError("game-not-enough-players")

    # Assign roles
    user_ids = [p.user_id for p in state.players]
    mafia_ratio = state.settings.get("gameplay", {}).get("mafia_ratio", "low")
    enabled = state.settings.get("roles", {})
    # Admin-provided role list for this exact N — settings JSON keys are
    # strings, hence str(n). If missing/length-mismatched, the engine
    # falls back to the algorithm.
    role_dist_override = state.settings.get("role_distribution") or {}
    override_list = role_dist_override.get(str(len(user_ids)))
    assignments = distribute_mvp_roles(
        user_ids,
        mafia_ratio=mafia_ratio,
        enabled_roles=enabled,
        override=override_list,
    )

    role_map = {a.user_id: a.role for a in assignments}
    for p in state.players:
        p.role = role_map[p.user_id]
        role_obj = get_role(p.role)
        p.team = role_obj.team

    # Honor 🃏 special_role picks. For each player who pre-paid a
    # special_role credit and picked a specific role, swap their assigned
    # role with whichever player got that role in random distribution.
    # If the picked role isn't in THIS game's distribution, the credit is
    # NOT consumed — the pick carries over to a future game.
    # Sandbox games skip this — fake users have no inventory rows.
    sandbox = is_sandbox_state(state)
    if not sandbox:
        await _apply_special_role_picks(state)

    # Activate items from inventory (settings.enabled + items_allowed in group).
    # Sandbox players carry no items by default; the dashboard can add them
    # later via the override path.
    if not sandbox:
        items_allowed = state.settings.get("items_allowed", {})
        for p in state.players:
            p.items_active = await _resolve_active_items(p.user_id, items_allowed)
            # Mark premium players so the action resolver can grant the
            # 2x-shield bonus and protect them from the Hooker. The flag is
            # captured at start-time so a premium expiring mid-game does not
            # silently strip the privileges they paid for this game.
            is_premium = await _is_user_premium(p.user_id)
            if is_premium:
                p.extra["is_premium"] = True
                # Pre-seed shield_uses_left so _consume_shield can debit a
                # premium-bonus use before stripping the item slot.
                p.extra["shield_uses_left"] = {
                    "shield": 1,
                    "killer_shield": 1,
                    "vote_shield": 1,
                }

    # Persist Game record (status=running) in DB. Sandbox games live
    # entirely in Redis + the SandboxSession row managed by sandbox_service.
    if not sandbox:
        await Game.create(
            id=state.id,
            group_id=state.group_id,
            status=GameStatus.RUNNING,
            bounty_per_winner=state.bounty_per_winner,
            bounty_pool=state.bounty_pool,
            bounty_initiator_id=state.bounty_initiator_id,
            settings_snapshot=state.settings,
            history={},
        )
    state.started_at = int(time.time())

    # Move to night #1
    state.phase = Phase.NIGHT
    state.round_num = 1
    night_secs = state.settings.get("timings", {}).get("night", 60)
    state.phase_ends_at = int(time.time()) + night_secs

    await save_state(state)
    logger.info(f"Game {state.id} started with {len(state.players)} players")
    return state


# === Game finish ===


async def finish_game(state: GameState, winner: Team | None) -> None:
    """Persist final state to DB, run stats finalization, clean up Redis.

    Sandbox games take a separate path: the engine work (game-end DMs via
    SandboxBot, state finalization in Redis) still runs, but DB-writing
    paths (`Game` row update, stats finalization, ELO/achievements,
    bounty payout) are skipped. Sandbox snapshot + transcript dump are
    handled by `sandbox_service.on_game_finish` (task #12).
    """
    from datetime import datetime

    state.phase = Phase.FINISHED
    state.winner_team = winner
    state.finished_at = int(time.time())

    if winner is not None and not state.winner_user_ids:
        # Phase manager normally fills winner_user_ids via winner_user_ids().
        # Fallback only fires when finish_game is called directly (rare):
        # team wins → alive members only, dead are not winners.
        from app.core.win_conditions import winner_user_ids as _wuids

        state.winner_user_ids = _wuids(state, winner)

    sandbox = is_sandbox_state(state)

    # Persist to DB (skipped for sandbox — there's no Game row in the
    # first place since start_game's create was guarded).
    if not sandbox:
        db_game = await Game.get_or_none(id=state.id)
        if db_game is not None:
            db_game.status = GameStatus.FINISHED
            db_game.finished_at = datetime.now(UTC)
            db_game.winner_team = (
                WinnerTeam(winner.value) if winner is not None else None  # type: ignore[arg-type]
            )
            db_game.history = state.to_history_dict()
            await db_game.save()

    # Stats finalization writes GameResult / UserStats / GroupUserStats /
    # ELO — none of which apply to sandbox runs.
    if not sandbox:
        try:
            from app.services.stats_service import finalize_game_stats

            await finalize_game_stats(state)
        except Exception as e:
            logger.exception(f"Stats finalization failed for game {state.id}: {e}")

    # Per-player game-end DM (role, won/lost, XP/ELO/$). Best-effort —
    # any failure here MUST NOT block bounty payout or state cleanup.
    # For sandbox, the bot is the SandboxBot so DMs land in the transcript.
    try:
        if sandbox:
            from app.services.recording_bot import SandboxBotRegistry

            bot_inst = SandboxBotRegistry.get(state.group_id)
        else:
            from app.main import bot as bot_inst  # type: ignore[no-redef,assignment]
        from app.services.game_end_dm import send_per_player_game_end_dm

        if bot_inst is not None:
            await send_per_player_game_end_dm(cast("Bot", bot_inst), state)
    except Exception as e:
        logger.exception(f"Game-end DM dispatch failed for game {state.id}: {e}")

    # Bounty payout (real-money / diamond settlement) — never for sandbox.
    if not sandbox and state.bounty_pool and state.bounty_per_winner and state.winner_user_ids:
        try:
            from app.services.giveaway_service import payout_bounty

            await payout_bounty(
                pool=state.bounty_pool,
                per_winner=state.bounty_per_winner,
                winner_user_ids=state.winner_user_ids,
                initiator_id=state.bounty_initiator_id,
                game_id=state.id,
            )
        except Exception as e:
            logger.exception(f"Bounty payout failed for game {state.id}: {e}")

    # Clear active_game for all participants. `set_user_active_game` is
    # itself sandbox-aware (no-ops for negative IDs), so this loop is
    # safe to run unconditionally.
    for p in state.players:
        await set_user_active_game(p.user_id, None)

    # Sandbox cleanup: snapshot transcript to DB + mark session finished.
    # Lives in sandbox_service so the finish hook can be implemented
    # alongside the rest of stop/restart/destroy (task #12).
    if sandbox:
        try:
            from app.services import sandbox_service

            hook = getattr(sandbox_service, "on_game_finish", None)
            if hook is not None:
                await hook(state, winner)
        except Exception as e:
            logger.exception(f"Sandbox finish hook failed for game {state.id}: {e}")

    # Remove Redis state (works for real + sandbox; harmless on either).
    await delete_state(state.group_id)

    logger.info(
        f"{'Sandbox ' if sandbox else ''}Game {state.id} finished, winner={winner}, "
        f"duration={state.finished_at - (state.started_at or state.finished_at)}s"
    )


async def _apply_special_role_picks(state: GameState) -> None:
    """Swap roles for players who pre-paid a 🃏 special_role pick.

    Runs AFTER distribute_mvp_roles. For each picker:
      * If they already lucked into their picked role, just consume the
        credit silently.
      * Otherwise swap with whichever player got the picked role.
      * If the picked role isn't in this game's distribution at all, do
        nothing (the credit stays so the pick carries to a future game).
    """
    from app.db.models import UserInventory

    for player in state.players:
        inv = await UserInventory.get_or_none(user_id=player.user_id)
        if inv is None:
            continue
        sr_settings = (inv.settings or {}).get("special_role", {})
        forced_role = sr_settings.get("next_role")
        if not forced_role:
            continue

        if player.role == forced_role:
            await _consume_special_role_credit(player.user_id, inv)
            continue

        other = next(
            (p for p in state.players if p.role == forced_role and p.user_id != player.user_id),
            None,
        )
        if other is None:
            # Role missing from this distribution — keep the credit for
            # the next game.
            continue

        player.role, other.role = forced_role, player.role
        player.team = get_role(player.role).team
        other.team = get_role(other.role).team
        logger.info(
            f"special_role swap: user {player.user_id} forced into {forced_role} "
            f"(swapped with user {other.user_id})"
        )
        await _consume_special_role_credit(player.user_id, inv)


async def _consume_special_role_credit(user_id: int, inv: object) -> None:
    """Decrement special_role inventory + clear the picked role."""
    from app.db.models import UserInventory

    new_settings = dict(getattr(inv, "settings", None) or {})
    sr = dict(new_settings.get("special_role", {}))
    sr.pop("next_role", None)
    sr["enabled"] = False
    new_settings["special_role"] = sr
    new_count = max(0, getattr(inv, "special_role", 0) - 1)
    await UserInventory.filter(user_id=user_id).update(
        special_role=new_count, settings=new_settings
    )


async def _is_user_premium(user_id: int) -> bool:
    """True iff the user has an active premium subscription right now.

    Both `is_premium=True` AND a future `premium_expires_at` are required
    — a stale flag from before the cron reset is treated as inactive.
    """
    from datetime import UTC, datetime

    user = await User.get_or_none(id=user_id)
    if user is None:
        return False
    if not user.is_premium:
        return False
    if user.premium_expires_at is None:
        return False
    return user.premium_expires_at > datetime.now(UTC)


async def _resolve_active_items(user_id: int, items_allowed: dict) -> list[str]:
    """Determine which items the user has + activated + group allows.

    Pure read-only: the item is moved into `items_active` for the game,
    but the DB inventory stock is NOT decremented here. The earlier
    behaviour (decrementing on game start) penalised buyers whose
    shields never got triggered — the operator's report
    "himoyalar ishlamasida kuyib ketmoqda" was exactly this. Stock is
    now debited only when the engine actually consumes the slot
    (`actions.py:_consume_shield`, `phase_manager._consume_vote_shield`,
    `actions.py rifle pierce path`), surfaced through
    `NightOutcome.inventory_consumed` so the phase manager can persist
    one update at the end of each phase resolution.
    """
    from app.db.models import UserInventory

    inv = await UserInventory.get_or_none(user_id=user_id)
    if inv is None:
        return []

    settings = inv.settings or {}
    active: list[str] = []

    item_fields = [
        "shield",
        "killer_shield",
        "vote_shield",
        "rifle",
        "mask",
        "fake_document",
    ]

    for field in item_fields:
        # Group must allow it
        if not items_allowed.get(field, True):
            continue
        # User has at least 1
        qty = getattr(inv, field, 0)
        if qty <= 0:
            continue
        # User enabled it in settings
        item_settings = settings.get(field, {})
        if not item_settings.get("enabled", False):
            continue
        active.append(field)

    return active


async def sync_inventory_into_active_game(user_id: int, item_code: str) -> None:
    """Reconcile a single inventory item with the user's in-game items_active.

    Fires when the user toggles or buys an item mid-game so the change is
    felt immediately instead of next round / next game. The contract:

      * Item now enabled + stock > 0 + not yet active in game → consume 1
        from stock, append to items_active.
      * Item now disabled + currently active in game → strip from
        items_active, refund 1 to stock.

    No-op when the user isn't in a live game or the group disallows the
    item. Items not in the consumable item list (rifle, special_role) are
    handled elsewhere and ignored here.
    """
    from app.db.models import UserInventory

    consumables = {"shield", "killer_shield", "vote_shield", "mask", "fake_document"}
    if item_code not in consumables:
        return

    game_id = await get_user_active_game_id(user_id)
    if game_id is None:
        return

    from app.db.models import Game

    db_game = await Game.get_or_none(id=game_id)
    if db_game is None:
        return
    state = await load_state(db_game.group_id)  # type: ignore[attr-defined]
    if state is None or state.phase in (Phase.WAITING, Phase.FINISHED, Phase.CANCELLED):
        return
    player = state.get_player(user_id)
    if player is None or not player.alive:
        return

    items_allowed = state.settings.get("items_allowed", {})
    if not items_allowed.get(item_code, True):
        return

    inv = await UserInventory.get_or_none(user_id=user_id)
    if inv is None:
        return

    settings = inv.settings or {}
    enabled = settings.get(item_code, {}).get("enabled", False)
    in_game = item_code in player.items_active
    stock = getattr(inv, item_code, 0)

    # New "consume-on-use" contract: only flip the in-game flag here;
    # the DB stock is debited later (in the action resolver / phase
    # manager) only when the item is actually triggered by an attack /
    # hanging confirm. Toggling mid-game therefore costs nothing.
    if enabled and not in_game and stock > 0:
        player.items_active.append(item_code)
        await save_state(state)
        return

    if not enabled and in_game:
        player.items_active.remove(item_code)
        await save_state(state)
        return


async def cancel_game(state: GameState, reason: str = "cancelled") -> None:
    """Cancel game (admin /stop or auto). Refund bounty if applicable.

    Also unpins+deletes the registration message if one is still pinned —
    otherwise a stale "Join Game" card lingers in the chat forever after a
    failed-to-fill registration auto-cancel.

    Sandbox path: skip DB writes + bounty refund (no Game row, no real
    money), defer cleanup to `sandbox_service.on_game_cancel`.
    """
    from datetime import datetime

    state.phase = Phase.CANCELLED
    state.finished_at = int(time.time())

    sandbox = is_sandbox_state(state)

    if not sandbox:
        db_game = await Game.get_or_none(id=state.id)
        if db_game is not None:
            db_game.status = GameStatus.CANCELLED
            db_game.finished_at = datetime.now(UTC)
            db_game.history = state.to_history_dict()
            await db_game.save()

        # Refund bounty escrow
        if state.bounty_pool and state.bounty_initiator_id:
            try:
                from app.services.giveaway_service import payout_bounty

                await payout_bounty(
                    pool=state.bounty_pool,
                    per_winner=0,
                    winner_user_ids=[],  # empty → full refund
                    initiator_id=state.bounty_initiator_id,
                    game_id=state.id,
                )
            except Exception as e:
                logger.exception(f"Bounty refund failed for game {state.id}: {e}")

    for p in state.players:
        await set_user_active_game(p.user_id, None)

    # Wipe the registration message so the chat returns to a clean slate.
    # Best-effort: aiogram/Telegram failures here MUST NOT block the rest
    # of cleanup. Bot may also be unavailable during early-shutdown paths.
    if state.registration_message_id is not None:
        try:
            if sandbox:
                from app.services.recording_bot import SandboxBotRegistry

                bot = SandboxBotRegistry.get(state.group_id)
            else:
                from app.main import bot  # type: ignore[no-redef,assignment]

            if bot is not None:
                import contextlib

                with contextlib.suppress(Exception):
                    await bot.unpin_chat_message(
                        chat_id=state.chat_id, message_id=state.registration_message_id
                    )
                with contextlib.suppress(Exception):
                    await bot.delete_message(
                        chat_id=state.chat_id, message_id=state.registration_message_id
                    )
        except Exception as e:
            logger.debug(f"Registration message cleanup failed: {e}")
        state.registration_message_id = None

    if sandbox:
        try:
            from app.services import sandbox_service

            hook = getattr(sandbox_service, "on_game_cancel", None)
            if hook is not None:
                await hook(state, reason)
        except Exception as e:
            logger.exception(f"Sandbox cancel hook failed for game {state.id}: {e}")

    await delete_state(state.group_id)
    logger.info(f"{'Sandbox ' if sandbox else ''}Game {state.id} cancelled: {reason}")


async def recover_stale_active_games() -> None:
    """Clear active_game_id for users whose game is no longer in Redis.

    If storage access fails, or if any game payload is corrupted,
    swallows the error and does nothing.
    """

    from app.core.state import GameState
    from app.db.models import User

    try:
        backend = get_state_backend()
        keys = await backend.scan_match("mafia:game:*")
        active_uuids: set[UUID] = set()
        for k in keys:
            raw = await backend.get(k)
            if raw:
                try:
                    state = GameState.from_redis(raw)
                    active_uuids.add(state.id)
                except Exception as e:
                    logger.error(f"Corrupted game state found at key {k}: {e}. Aborting recovery.")
                    return
    except Exception as e:
        logger.error(f"Failed to scan active games for recovery: {e}")
        return

    try:
        if active_uuids:
            await (
                User.filter(active_game_id__not_isnull=True)
                .exclude(active_game_id__in=list(active_uuids))
                .update(active_game_id=None)
            )
        else:
            await User.filter(active_game_id__not_isnull=True).update(active_game_id=None)
        logger.info("Stale active_game_id values cleared successfully.")
    except Exception as e:
        logger.error(f"Failed to clear stale active_game_ids: {e}")
