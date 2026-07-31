"""Application entry point: FastAPI + aiogram bot.

Two modes:
    - webhook (production): bot updates via FastAPI /webhook
    - polling (local dev): bot polls via long polling, parallel with FastAPI

FSM storage:
    - redis (production): RedisStorage
    - memory (local dev): MemoryStorage

Database:
    - DATABASE_URL env var overrides postgres_dsn (e.g., sqlite://./mafia.db for local dev)
"""

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import sentry_sdk
import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from fastapi import FastAPI
from loguru import logger
from sentry_sdk.integrations.fastapi import FastApiIntegration
from tortoise import Tortoise

from app.config import settings
from app.db.tortoise_config import TORTOISE_ORM


def setup_logging() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG" if settings.debug else "INFO",
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}:{function}:{line}</cyan> - <level>{message}</level>",
        backtrace=settings.debug,
        diagnose=settings.debug,
    )


def setup_sentry() -> None:
    if not settings.sentry_dsn:
        return
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.sentry_environment,
        integrations=[FastApiIntegration()],
        traces_sample_rate=0.1,
        profiles_sample_rate=0.1,
    )
    logger.info("Sentry initialized")


def _build_storage() -> BaseStorage:
    """FSM storage tanlash: memory (local dev) yoki Redis (production)."""
    if settings.fsm_storage == "memory":
        logger.info("FSM storage: MemoryStorage (local dev)")
        return MemoryStorage()
    logger.info(f"FSM storage: RedisStorage ({settings.redis_url})")
    return RedisStorage.from_url(settings.redis_url)


async def init_db() -> None:
    await Tortoise.init(config=TORTOISE_ORM)
    # generate_schemas(safe=True) is idempotent — creates tables only if missing.
    # Works for both SQLite (dev) and PostgreSQL (production fresh deploy).
    # For migrating an existing DB with old schema, use `aerich upgrade` instead.
    await Tortoise.generate_schemas(safe=True)
    if settings.database_url and settings.database_url.startswith("sqlite"):
        logger.info(f"Database connected: {settings.database_url} (schema ensured)")
    else:
        logger.info(f"Database connected: {settings.db_url.split('@')[-1]} (schema ensured)")

    # Seed achievements
    try:
        from app.services.achievement_service import seed_achievements

        await seed_achievements()
    except Exception as e:
        logger.warning(f"Achievement seed failed: {e}")

    # Seed default admin
    try:
        from app.api.routers.auth import seed_default_admin

        await seed_default_admin()
    except Exception as e:
        logger.warning(f"Default admin seed failed: {e}")

    # Seed SystemSettings (prices/rewards/exchange) defaults if missing
    try:
        from app.services.pricing_service import seed_default_settings

        await seed_default_settings()
    except Exception as e:
        logger.warning(f"SystemSettings seed failed: {e}")

    # Seed RoleConfig (per-role names/emojis) on first boot; warms cache.
    try:
        from app.services.role_config_service import seed_default_role_configs

        await seed_default_role_configs()
    except Exception as e:
        logger.warning(f"RoleConfig seed failed: {e}")

    # Seed EmojiConfig (scene/item/action/currency emojis) on first boot.
    try:
        from app.services.emoji_config_service import seed_default_emoji_configs

        await seed_default_emoji_configs()
    except Exception as e:
        logger.warning(f"EmojiConfig seed failed: {e}")


async def close_db() -> None:
    await Tortoise.close_connections()
    logger.info("Database disconnected")


# Bot/dispatcher singletons
bot: Bot | None = None
dp: Dispatcher | None = None


async def setup_bot() -> tuple[Bot, Dispatcher]:
    global bot, dp

    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=_build_storage())

    from app.bot.dispatcher import setup_routers

    setup_routers(dp)
    logger.info(f"Bot initialized: mode={settings.bot_mode}, env={settings.environment}")
    return bot, dp


async def _start_webhook(bot_instance: Bot, dp_instance: Dispatcher) -> None:
    """Production: webhook orqali Telegram update'lar qabul qilish.

    Strict mode — if anything fails (network error, invalid token, SSL issue,
    Telegram rejects the URL), the exception propagates out of lifespan and
    the FastAPI app fails to start. Operator must fix the config.

    Pre-flight checks:
      - webhook_base_url must be HTTPS (Telegram requirement)
      - webhook_secret must not be the development default
    """
    target_url = settings.webhook_url

    if not settings.webhook_base_url.startswith("https://"):
        raise RuntimeError(
            f"BOT_MODE=webhook requires HTTPS for webhook_base_url, got: "
            f"{settings.webhook_base_url!r}. Telegram refuses non-HTTPS webhooks."
        )
    if settings.webhook_secret.get_secret_value() in (
        "dev-secret",
        "",
        "change_me_to_random_string",
    ):
        raise RuntimeError(
            "BOT_MODE=webhook requires a non-default WEBHOOK_SECRET in .env. "
            "Generate one: `openssl rand -hex 32`"
        )

    # Show current state for diagnostics
    current = await bot_instance.get_webhook_info()
    logger.info(
        f"Webhook current state: url={current.url!r}, "
        f"pending_update_count={current.pending_update_count}, "
        f"last_error_date={current.last_error_date}, "
        f"last_error_message={current.last_error_message!r}"
    )

    # Always (re)set the webhook on startup — cheap call, ensures Telegram is in
    # sync with our config even after token rotations or URL changes.
    set_ok = await bot_instance.set_webhook(
        url=target_url,
        allowed_updates=dp_instance.resolve_used_update_types(),
        drop_pending_updates=True,
        secret_token=None,  # secret is part of URL path; alternative scheme not used
    )
    if not set_ok:
        raise RuntimeError(f"Telegram rejected setWebhook for {target_url}")

    # Verify Telegram acknowledged our URL
    verified = await bot_instance.get_webhook_info()
    if verified.url != target_url:
        raise RuntimeError(
            f"Webhook verification failed: Telegram reports url={verified.url!r}, "
            f"expected {target_url!r}"
        )

    logger.info(f"✅ Webhook registered with Telegram: {target_url}")
    if verified.last_error_message:
        logger.warning(
            f"⚠️  Telegram reports last webhook error: {verified.last_error_message} "
            f"(at {verified.last_error_date}). New deliveries may still succeed."
        )


async def _start_polling_task(bot_instance: Bot, dp_instance: Dispatcher) -> asyncio.Task:
    """Local dev: long polling parallel asyncio task."""
    # Delete webhook first (Telegram requires polling without webhook)
    try:
        await bot_instance.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook deleted (polling mode)")
    except Exception as e:
        logger.warning(f"Could not delete webhook: {e}")

    task = asyncio.create_task(dp_instance.start_polling(bot_instance, handle_signals=False))
    logger.info("Bot polling started")
    return task


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    setup_sentry()
    await init_db()

    from app.services.game_service import recover_stale_active_games

    await recover_stale_active_games()

    bot_instance, dp_instance = await setup_bot()

    polling_task: asyncio.Task | None = None
    if settings.bot_mode == "webhook":
        await _start_webhook(bot_instance, dp_instance)
    else:
        polling_task = await _start_polling_task(bot_instance, dp_instance)

    # Set bot commands menu (private + group scopes)
    from app.bot.handlers.common.help import setup_bot_commands

    await setup_bot_commands(bot_instance)

    # Start scheduler (cron: stats rollup)
    from app.workers.scheduler import shutdown_scheduler, start_scheduler

    start_scheduler()

    # Heal any broadcast rows the previous backend left in RUNNING — the
    # task is gone now, so the row would sit there forever without help.
    # The SA can manually re-run from the dashboard if needed.
    try:
        from app.services.broadcast_service import recover_interrupted_runs

        await recover_interrupted_runs()
    except Exception as e:  # pragma: no cover — never block startup on cleanup
        logger.warning(f"recover_interrupted_runs failed: {e}")

    logger.info(f"Application startup complete (mode={settings.bot_mode})")
    yield

    shutdown_scheduler()
    if polling_task is not None:
        polling_task.cancel()
        with suppress(asyncio.CancelledError):
            await polling_task
    await bot_instance.session.close()
    await close_db()
    logger.info("Application shutdown complete")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Mafia Bot API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs" if settings.debug else None,
        redoc_url=None,
    )

    from app.api.routers import (
        admin,
        health,
        public,
        sandbox,
        super_admin,
        webhook,
        ws,
    )
    from app.api.routers import (
        auth as auth_router,
    )
    from app.api.routers import (
        group as group_router,
    )

    app.include_router(health.router, tags=["health"])
    app.include_router(public.router)  # /api/public/* — no auth, read-only
    app.include_router(webhook.router, prefix="/webhook", tags=["webhook"])
    app.include_router(auth_router.router, prefix="/api", tags=["auth"])
    app.include_router(admin.router, prefix="/api", tags=["admin"])
    app.include_router(sandbox.router)  # /api/sa/sandbox/* — JWT super-admin
    app.include_router(super_admin.router)  # /api/sa/* — Telegram-ID auth
    app.include_router(group_router.router, prefix="/api", tags=["group"])
    app.include_router(ws.router, prefix="/ws", tags=["websocket"])

    # CORS for frontend dev server
    if settings.environment == "development":
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    return app


app = create_app()


def main() -> None:
    """Entry point for `python -m app.main`.

    Port resolution:
      - INTERNAL_PORT (if set) — used inside Docker container (always 8000)
      - settings.backend_port — local dev override (from BACKEND_PORT env)
      - 8000 — final fallback

    In production Docker setup, BACKEND_PORT is the HOST port (e.g. 8002)
    that docker-compose maps to the container's internal port (8000).
    Uvicorn must bind to the INTERNAL port, not the host port.
    """
    import os

    internal_port_env = os.getenv("INTERNAL_PORT")
    port = int(internal_port_env) if internal_port_env else (settings.backend_port or 8000)

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=port,
        reload=settings.debug,
        log_config=None,
    )


if __name__ == "__main__":
    main()
