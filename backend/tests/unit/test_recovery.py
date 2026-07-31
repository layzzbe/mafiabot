"""Tests for stale active_game_id recovery feature."""

import json
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from app.db.models import User
from app.services.game_service import recover_stale_active_games
from tortoise import Tortoise


@pytest.fixture(scope="module", autouse=True)
async def _db():
    await Tortoise.init(
        db_url="sqlite://:memory:",
        modules={
            "models": [
                "app.db.models.user",
                "app.db.models.group",
                "app.db.models.game",
                "app.db.models.statistics",
                "app.db.models.transaction",
                "app.db.models.audit",
                "app.db.models.system_settings",
                "app.db.models.role_config",
                "app.db.models.emoji_config",
                "app.db.models.sandbox",
            ]
        },
    )
    await Tortoise.generate_schemas(safe=True)
    yield
    await Tortoise.close_connections()


@pytest.fixture
def mock_backend():
    with patch("app.services.game_service.get_state_backend") as mock:
        backend = AsyncMock()
        mock.return_value = backend
        yield backend


@pytest.mark.asyncio
async def test_valid_active_game_remains_unchanged(mock_backend):
    game_id = uuid4()
    mock_backend.scan_match.return_value = ["mafia:game:-1001"]
    mock_backend.get.return_value = json.dumps(
        {"id": str(game_id), "group_id": -1001, "chat_id": -1001}
    )

    user = await User.create(id=1, first_name="ValidUser", active_game_id=game_id)

    await recover_stale_active_games()

    await user.refresh_from_db()
    assert user.active_game_id == game_id
    await user.delete()


@pytest.mark.asyncio
async def test_stale_active_game_is_cleared(mock_backend):
    game_id = uuid4()
    mock_backend.scan_match.return_value = []

    user = await User.create(id=2, first_name="StaleUser", active_game_id=game_id)

    await recover_stale_active_games()

    await user.refresh_from_db()
    assert user.active_game_id is None
    await user.delete()


@pytest.mark.asyncio
async def test_null_active_game_remains_unchanged(mock_backend):
    mock_backend.scan_match.return_value = []

    user = await User.create(id=3, first_name="NullUser", active_game_id=None)

    await recover_stale_active_games()

    await user.refresh_from_db()
    assert user.active_game_id is None
    await user.delete()


@pytest.mark.asyncio
async def test_storage_failure_clears_nothing(mock_backend):
    game_id = uuid4()
    mock_backend.scan_match.side_effect = Exception("Redis error")

    user = await User.create(id=4, first_name="FailUser", active_game_id=game_id)

    await recover_stale_active_games()

    await user.refresh_from_db()
    assert user.active_game_id == game_id
    await user.delete()


@pytest.mark.asyncio
async def test_corrupted_payload_aborts_recovery(mock_backend):
    game_id = uuid4()
    mock_backend.scan_match.return_value = ["mafia:game:-1002"]
    # Missing required fields like group_id, chat_id will fail GameState validation
    mock_backend.get.return_value = json.dumps({"id": str(game_id)})

    user = await User.create(id=5, first_name="CorruptedUser", active_game_id=game_id)

    await recover_stale_active_games()

    await user.refresh_from_db()
    # It must abort, leaving the game_id untouched
    assert user.active_game_id == game_id
    await user.delete()
