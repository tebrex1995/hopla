"""A raw store that fails on purpose (ADR-0003 fault tests): wraps a real one, injects errors."""

from collections.abc import Callable

from hopla.storage.raw_store import Listing, ObjectInfo, RawStore, StoreError

Trigger = Callable[[str, str], bool]  # (operation, key) -> fail this call?


class FaultyRawStore:
    """Delegates to `inner`, but a call for which `fail(op, key)` is true raises `error()`.

    With `land_then_fail`, a failing write happens first and the error comes after, like a
    PUT that times out on our side but lands on the server.
    """

    def __init__(
        self,
        inner: RawStore,
        *,
        fail: Trigger = lambda op, key: False,
        error: Callable[[str, str], StoreError] = lambda op, key: StoreError("server", op, key),
        land_then_fail: bool = False,
    ) -> None:
        self.inner = inner
        self.fail = fail
        self.error = error
        self.land_then_fail = land_then_fail
        self.calls: list[tuple[str, str]] = []

    def _check(self, op: str, key: str) -> bool:
        self.calls.append((op, key))
        return self.fail(op, key)

    async def head(self, key: str) -> ObjectInfo | None:
        if self._check("head", key):
            raise self.error("head", key)
        return await self.inner.head(key)

    async def get(self, key: str) -> bytes:
        if self._check("get", key):
            raise self.error("get", key)
        return await self.inner.get(key)

    async def put_if_absent(self, key: str, data: bytes, *, content_type: str) -> bool:
        failing = self._check("put_if_absent", key)
        if failing and not self.land_then_fail:
            raise self.error("put_if_absent", key)
        created = await self.inner.put_if_absent(key, data, content_type=content_type)
        if failing:
            raise self.error("put_if_absent", key)
        return created

    async def put(self, key: str, data: bytes, *, content_type: str) -> None:
        failing = self._check("put", key)
        if failing and not self.land_then_fail:
            raise self.error("put", key)
        await self.inner.put(key, data, content_type=content_type)
        if failing:
            raise self.error("put", key)

    async def list(self, prefix: str, *, delimiter: str | None = None) -> Listing:
        if self._check("list", prefix):
            raise self.error("list", prefix)
        return await self.inner.list(prefix, delimiter=delimiter)

    async def copy(self, src: str, dst: str) -> bool:
        failing = self._check("copy", dst)
        if failing and not self.land_then_fail:
            raise self.error("copy", dst)
        created = await self.inner.copy(src, dst)
        if failing:
            raise self.error("copy", dst)
        return created

    async def delete(self, key: str, *, if_match: str | None = None) -> None:
        failing = self._check("delete", key)
        if failing and not self.land_then_fail:
            raise self.error("delete", key)
        await self.inner.delete(key, if_match=if_match)
        if failing:
            raise self.error("delete", key)
