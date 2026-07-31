"""Redis state I/O — fakeredis fallback for memory FSM dev mode."""

from __future__ import annotations

from typing import Protocol


class StateBackend(Protocol):
    """Async key-value backend for game state."""

    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def scan_match(self, match: str) -> list[str]: ...


class MemoryBackend:
    """In-memory backend (for dev mode without Redis)."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value: str) -> None:
        self._store[key] = value

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    async def scan_match(self, match: str) -> list[str]:
        import fnmatch

        return [k for k in self._store if fnmatch.fnmatch(k, match)]


class RedisBackend:
    """Real Redis backend (production)."""

    def __init__(self, url: str) -> None:
        from redis.asyncio import from_url

        self._client = from_url(url, decode_responses=True)

    async def get(self, key: str) -> str | None:
        return await self._client.get(key)

    async def set(self, key: str, value: str) -> None:
        await self._client.set(key, value)

    async def delete(self, key: str) -> None:
        await self._client.delete(key)

    async def scan_match(self, match: str) -> list[str]:
        return [k async for k in self._client.scan_iter(match=match, count=100)]


_backend: StateBackend | None = None


def get_state_backend() -> StateBackend:
    global _backend
    if _backend is not None:
        return _backend

    from app.config import settings

    if settings.fsm_storage == "memory":
        _backend = MemoryBackend()
    else:
        _backend = RedisBackend(settings.redis_url)
    return _backend
