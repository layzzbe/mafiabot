"""Super-admin sandbox session models.

A `SandboxSession` records each test-game run by a super-admin: who
created it, which fake players took part, what settings were used, and —
on finish — a snapshot of the final game state. `SandboxTranscriptEntry`
rows are the durable copy of the Redis-resident bot-message transcript;
they're bulk-inserted at session finish so completed sessions can be
re-opened from the dashboard even after Redis TTL expiry.

Live runs read/write Redis (`mafia:sandbox:{sid}:*`); DB is history only.
Real users / games / stats are NEVER touched by sandbox lifecycle.
"""

from enum import StrEnum

from tortoise import fields
from tortoise.models import Model


class SandboxStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    FINISHED = "finished"
    DESTROYED = "destroyed"
    ERRORED = "errored"


class SandboxAutoPlayMode(StrEnum):
    PAUSED = "paused"  # admin clicks every action manually
    AUTO = "auto"  # sane defaults (doctor heals random alive, etc.)
    RANDOM_ACTIONS = "random_actions"  # totally random valid targets


class SandboxTimingPreset(StrEnum):
    FAST = "fast"
    NORMAL = "normal"
    SLOW = "slow"
    MANUAL = "manual"  # phases advance only on explicit /advance


class TranscriptEntryType(StrEnum):
    SEND = "send"
    EDIT = "edit"
    DELETE = "delete"
    TOAST = "toast"  # answer_callback_query
    PIN = "pin"
    UNPIN = "unpin"


class TranscriptScope(StrEnum):
    GROUP = "group"
    DM = "dm"
    MAFIA_CHAT = "mafia_chat"
    DEAD_CHAT = "dead_chat"


class SandboxSession(Model):
    """One test-game run by a super-admin."""

    id = fields.UUIDField(pk=True)
    created_by = fields.ForeignKeyField(
        "models.AdminAccount",
        related_name="sandbox_sessions",
        on_delete=fields.CASCADE,
    )  # type: ignore[var-annotated]

    created_at = fields.DatetimeField(auto_now_add=True)
    started_at = fields.DatetimeField(null=True)
    finished_at = fields.DatetimeField(null=True)

    status = fields.CharEnumField(SandboxStatus, default=SandboxStatus.CREATED)

    # Fake group/chat ID used by the engine (also keys Redis: mafia:game:{fake_group_id})
    fake_group_id = fields.BigIntField()

    n_players = fields.IntField()
    auto_play_mode = fields.CharEnumField(SandboxAutoPlayMode, default=SandboxAutoPlayMode.PAUSED)
    timing_preset = fields.CharEnumField(SandboxTimingPreset, default=SandboxTimingPreset.NORMAL)

    # Snapshot of group settings used to start the run (so we can replay
    # the exact distribution / timings later even if defaults move).
    settings_snapshot: dict = fields.JSONField(default=dict)

    # Fake players present in this session: [{user_id, first_name, language_code,
    # role, team, alive_at_end}]. Roles are filled in by `start_sandbox` once
    # distribute_roles runs.
    fake_users_snapshot: list = fields.JSONField(default=list)

    # GameState.to_history_dict() at finish — full rounds, votes, deaths.
    final_state: dict | None = fields.JSONField(null=True)

    winner_team = fields.CharField(max_length=32, null=True)

    # Counts for the dashboard list view (avoid scanning all entries).
    # {n_entries, group_msg_count, dm_msg_count, mafia_chat_count, dead_chat_count}
    transcript_summary: dict | None = fields.JSONField(null=True)

    class Meta:
        table = "sandbox_sessions"
        indexes = [("created_by_id", "created_at"), ("status",)]

    def __str__(self) -> str:
        return f"SandboxSession({self.id}, status={self.status}, n={self.n_players})"


class SandboxTranscriptEntry(Model):
    """A single bot send/edit/delete that would have hit Telegram.

    Mirrors the Redis-LIST entry shape; bulk-inserted on session finish.
    """

    id = fields.BigIntField(pk=True)
    session = fields.ForeignKeyField(
        "models.SandboxSession",
        related_name="transcript",
        on_delete=fields.CASCADE,
    )  # type: ignore[var-annotated]

    # Monotonic within session (matches Redis LRANGE index).
    seq = fields.IntField()
    ts = fields.DatetimeField()

    type = fields.CharEnumField(TranscriptEntryType)
    scope = fields.CharEnumField(TranscriptScope)

    chat_id = fields.BigIntField()
    # For DM scope, == chat_id; null for group scopes.
    target_user_id = fields.BigIntField(null=True)

    # Synthetic per-session message_id (allocated by SandboxBot._next_msg_id).
    message_id = fields.IntField()
    # For edit/delete: original message_id being modified.
    ref_message_id = fields.IntField(null=True)

    text: str | None = fields.TextField(null=True)
    parse_mode = fields.CharField(max_length=32, null=True)

    # Serialized inline keyboard: {"inline_keyboard": [[{text, callback_data, url?}, ...]]}
    reply_markup: dict | None = fields.JSONField(null=True)

    # Media: {type: "photo"/"animation"/"video", url, caption}
    media: dict | None = fields.JSONField(null=True)

    class Meta:
        table = "sandbox_transcript_entries"
        indexes = [("session_id", "seq")]
        unique_together = (("session", "seq"),)

    def __str__(self) -> str:
        return f"SandboxTranscript({self.session_id}, seq={self.seq}, type={self.type})"  # type: ignore[attr-defined]
