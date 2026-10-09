"""The raw store (ADR-0003): an object store for response bodies, run manifests and ops state.

One protocol, two backends: S3-compatible (Cloudflare R2, the T-R0-03 second part) and the
local file system (development and tests).

Write rules, the same on both backends:
- Bodies, manifest parts and audit records are **write-once** (`put_if_absent`; `copy` is
  create-only too and only targets `raw/`).
- Only `ops/flags.json` and `_preflight/` objects are overwritten (`put`), and only
  `ops/disabled/` objects are deleted, optionally only if unchanged since read (`if_match`).

Time: `head()` and `list()` report each object's LastModified in the **store's** clock. That
is the time base for staleness and run order (ADR-0004 S4, S8), so no runner clock decides
them. Writes return no time (S3's PutObject doesn't either): read it back with `head()`.
"""

import contextlib
import errno
import hashlib
import os
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from hopla.core.clock import Clock
from hopla.storage.keys import (
    DISABLED_PREFIX,
    FLAGS_KEY,
    OPS_ROOT,
    PREFLIGHT_ROOT,
    RAW_ROOT,
    RUNS_ROOT,
)

_KEY = re.compile(r"[A-Za-z0-9._/-]{1,512}")
ROOTS = (RAW_ROOT, RUNS_ROOT, OPS_ROOT, PREFLIGHT_ROOT)
_PUTTABLE = (FLAGS_KEY, PREFLIGHT_ROOT)
_DELETABLE = (DISABLED_PREFIX,)
_COPY_TARGETS = (RAW_ROOT,)
_TMP_DIR = ".tmp"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    size: int
    last_modified: datetime  # the store's clock, aware UTC
    etag: str | None = None


@dataclass(frozen=True)
class Listing:
    objects: tuple[ObjectInfo, ...]  # sorted by key
    prefixes: tuple[str, ...] = ()  # the "folders" one level down, with a delimiter


class StoreError(Exception):
    """A store call failed. `kind` is what the manifest records as `store:<kind>` (ADR-0003)."""

    KINDS = frozenset(
        {
            "denied",
            "server",
            "network",
            "throttled",
            "exists",
            "conflict",
            "invalid",
            "missing",
            "timeout",
        }
    )

    def __init__(self, kind: str, op: str, key: str | None = None, detail: str = "") -> None:
        if kind not in self.KINDS:
            raise ValueError(f"unknown store error kind {kind!r}")
        self.kind, self.op, self.key = kind, op, key
        super().__init__(f"{op} {key or ''}: {kind}{f' ({detail})' if detail else ''}".strip())

    def manifest_error(self) -> str:
        return "store_timeout" if self.kind == "timeout" else f"store:{self.kind}"


class StoreTimeout(StoreError):
    def __init__(self, op: str, key: str | None = None, detail: str = "") -> None:
        super().__init__("timeout", op, key, detail)


class ObjectMissing(StoreError):
    def __init__(self, op: str, key: str | None = None) -> None:
        super().__init__("missing", op, key)


class RawStore(Protocol):
    async def head(self, key: str) -> ObjectInfo | None: ...
    async def get(self, key: str) -> bytes:
        """The object's bytes; ObjectMissing if it doesn't exist (e.g. expired)."""
        ...

    async def put_if_absent(self, key: str, data: bytes, *, content_type: str) -> bool:
        """Create the object; False (and nothing changed) if it already exists."""
        ...

    async def put(self, key: str, data: bytes, *, content_type: str) -> None:
        """Create or overwrite; only `ops/flags.json` and `_preflight/` objects."""
        ...

    async def list(self, prefix: str, *, delimiter: str | None = None) -> Listing: ...
    async def copy(self, src: str, dst: str) -> bool:
        """Create `dst` (under `raw/`) as a copy of `src`, with a new LastModified; False if
        `dst` exists. ObjectMissing if `src` is gone (expired)."""
        ...

    async def delete(self, key: str, *, if_match: str | None = None) -> None:
        """Delete an `ops/disabled/` object if present. With `if_match`, only if its etag is
        still that (else a `conflict` error): a disable written meanwhile is never lost."""
        ...


def validate_key(key: str) -> str:
    """A key both backends accept: under a known root, ASCII segments, none empty, `.` or `..`."""
    parts = key.split("/")
    if (
        not _KEY.fullmatch(key)
        or not key.startswith(ROOTS)
        or any(p in ("", ".", "..") for p in parts)
    ):
        raise StoreError("invalid", "key", key)
    return key


def _require_under(op: str, key: str, allowed: tuple[str, ...]) -> None:
    if not key.startswith(allowed):
        raise StoreError("invalid", op, key, f"only allowed under {', '.join(allowed)}")


@contextlib.contextmanager
def _os_errors(op: str, key: str) -> Iterator[None]:
    """File-system errors as store errors, so callers handle both backends the same way."""
    try:
        yield
    except FileNotFoundError:
        raise ObjectMissing(op, key) from None
    except PermissionError as err:
        raise StoreError("denied", op, key, str(err)) from err
    # FileExistsError here is `mkdir` meeting an object where a folder should be; the
    # create-only link handles its own.
    except (IsADirectoryError, NotADirectoryError, FileExistsError) as err:
        raise StoreError("invalid", op, key, "a key can't be both an object and a folder") from err
    except OSError as err:
        raise StoreError("server", op, key, errno.errorcode.get(err.errno or 0, str(err))) from err


class LocalFsRawStore:
    """The raw store on a local directory, for development and tests.

    LastModified comes from the injected store clock. Writes go to a temporary file (stamped
    with that time) first, then a hard link (create-only, atomic) or a replace (overwrite), so a
    reader never sees half an object or a wrong time. `list` walks the folder and `head` hashes
    the file for its etag: fine for development, not meant for large stores.
    """

    def __init__(self, root: Path, clock: Clock) -> None:
        self._root = root.resolve()
        self._clock = clock
        (self._root / _TMP_DIR).mkdir(parents=True, exist_ok=True)

    async def head(self, key: str) -> ObjectInfo | None:
        path = self._path(key)
        with _os_errors("head", key):
            return self._info(key, path) if path.is_file() else None

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        with _os_errors("get", key):
            if path.is_dir():
                raise ObjectMissing("get", key)
            return path.read_bytes()

    async def put_if_absent(self, key: str, data: bytes, *, content_type: str) -> bool:
        return self._link_new("put_if_absent", key, data)

    async def put(self, key: str, data: bytes, *, content_type: str) -> None:
        _require_under("put", key, _PUTTABLE)
        path = self._path(key)
        with _os_errors("put", key):
            path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(self._write_tmp(data), path)

    async def list(self, prefix: str, *, delimiter: str | None = None) -> Listing:
        if prefix and not prefix.startswith(ROOTS) or ".." in prefix.split("/"):
            raise StoreError("invalid", "list", prefix)
        objects: list[ObjectInfo] = []
        prefixes: set[str] = set()
        start = self._root / prefix.rsplit("/", 1)[0] if "/" in prefix else self._root
        for dirpath, dirnames, filenames in os.walk(start):
            if Path(dirpath) == self._root:  # the store's own temporary folder, not a key
                dirnames[:] = [d for d in dirnames if d != _TMP_DIR]
            for name in filenames:
                path = Path(dirpath) / name
                key = path.relative_to(self._root).as_posix()
                # A symlink (to the start folder or a file) must not list what's outside.
                if not key.startswith(prefix) or not path.resolve().is_relative_to(self._root):
                    continue
                rest = key[len(prefix) :]
                if delimiter and delimiter in rest:
                    prefixes.add(prefix + rest.split(delimiter, 1)[0] + delimiter)
                else:
                    objects.append(self._info(key, path))
        return Listing(tuple(sorted(objects, key=lambda o: o.key)), tuple(sorted(prefixes)))

    async def copy(self, src: str, dst: str) -> bool:
        _require_under("copy", dst, _COPY_TARGETS)
        data = await self.get(src)
        return self._link_new("copy", dst, data)

    async def delete(self, key: str, *, if_match: str | None = None) -> None:
        _require_under("delete", key, _DELETABLE)
        path = self._path(key)
        with _os_errors("delete", key):
            if not path.is_file():
                return
            if if_match is not None and self._etag(path) != if_match:
                raise StoreError("conflict", "delete", key, "changed since it was read")
            path.unlink(missing_ok=True)

    def _link_new(self, op: str, key: str, data: bytes) -> bool:
        path = self._path(key)
        with _os_errors(op, key):
            if path.is_dir():
                raise StoreError("invalid", op, key, "a key can't be both an object and a folder")
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._write_tmp(data)
            try:
                os.link(tmp, path)
            except FileExistsError:
                return False
            finally:
                tmp.unlink()
        return True

    def _path(self, key: str) -> Path:
        path = self._root / validate_key(key)
        # A symlink inside the store must not lead a write or read outside it.
        if not path.resolve().is_relative_to(self._root):
            raise StoreError("invalid", "key", key, "resolves outside the store")
        return path

    def _write_tmp(self, data: bytes) -> Path:
        tmp = self._root / _TMP_DIR / uuid.uuid4().hex
        tmp.write_bytes(data)
        # Integer microseconds, not float seconds: the time reads back exactly. Stamped before
        # the link or replace, so the object never shows the wall clock.
        ns = (self._clock.now() - _EPOCH) // _MICROSECOND * 1000
        os.utime(tmp, ns=(ns, ns))
        return tmp

    def _info(self, key: str, path: Path) -> ObjectInfo:
        stat = path.stat()
        modified = _EPOCH + timedelta(microseconds=stat.st_mtime_ns // 1000)
        return ObjectInfo(key, stat.st_size, modified, self._etag(path))

    @staticmethod
    def _etag(path: Path) -> str:
        return hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()
