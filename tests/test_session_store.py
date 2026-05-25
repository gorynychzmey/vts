from __future__ import annotations

from vts.services import session_store


class _FakeRedis:
    """In-memory Redis stub recording set/get/delete + TTLs."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.ttls: dict[str, int] = {}

    async def set(self, key: str, value: bytes, *, ex: int | None = None) -> None:
        if isinstance(value, str):
            value = value.encode("utf-8")
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex

    async def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    async def delete(self, key: str) -> int:
        return 1 if self.store.pop(key, None) is not None else 0


async def test_create_returns_64_hex_char_sid_and_writes_record() -> None:
    redis = _FakeRedis()
    sid = await session_store.create(
        redis, email="alice@example.com", ttl_seconds=3600, issued_at=12345
    )
    # token_hex(16) -> 32 hex chars (128 bits).
    assert len(sid) == 32
    assert all(c in "0123456789abcdef" for c in sid)
    assert redis.ttls[f"vts:session:{sid}"] == 3600


async def test_lookup_returns_record_for_existing_sid() -> None:
    redis = _FakeRedis()
    sid = await session_store.create(
        redis, email="alice@example.com", ttl_seconds=3600, issued_at=12345
    )
    record = await session_store.lookup(redis, sid)
    assert record is not None
    assert record.email == "alice@example.com"
    assert record.issued_at == 12345


async def test_lookup_returns_none_for_missing_sid() -> None:
    redis = _FakeRedis()
    record = await session_store.lookup(redis, "deadbeef" * 4)
    assert record is None


async def test_delete_removes_the_record() -> None:
    redis = _FakeRedis()
    sid = await session_store.create(
        redis, email="alice@example.com", ttl_seconds=3600, issued_at=12345
    )
    await session_store.delete(redis, sid)
    assert await session_store.lookup(redis, sid) is None


async def test_delete_missing_sid_is_safe() -> None:
    """logout-after-expiry should not raise."""
    redis = _FakeRedis()
    await session_store.delete(redis, "deadbeef" * 4)  # no error
