"""Blob storage abstraction (M3A).

Raw uploaded source bytes are stored separately from parsed rows. Only ``LocalBlobStore``
(a configured filesystem root) is implemented. S3-compatible object storage is a documented
future adapter swap — the rest of the app depends on the :class:`BlobStore` interface, never
on filesystem paths directly. Object keys are server-generated (never derived from the
untrusted client filename).
"""
from __future__ import annotations

import abc
import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class BlobInfo:
    key: str
    size_bytes: int
    sha256: str


class BlobStore(abc.ABC):
    @abc.abstractmethod
    def put(self, data: bytes, *, suffix: str = "") -> BlobInfo:
        """Store bytes under a server-generated key; return key + size + sha256."""

    @abc.abstractmethod
    def get(self, key: str) -> bytes: ...

    @abc.abstractmethod
    def exists(self, key: str) -> bool: ...

    @abc.abstractmethod
    def delete(self, key: str) -> None: ...


class LocalBlobStore(BlobStore):
    """Filesystem-backed store. Keys are opaque (uuid + safe suffix); files live under root,
    sharded by a 2-char prefix so a directory doesn't accumulate unbounded entries."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # key = "<hex>[.ext]"; shard by first 2 chars. Reject traversal.
        safe = Path(key).name
        if safe != key or "/" in key or ".." in key:
            raise ValueError("invalid blob key")
        return self.root / safe[:2] / safe

    def put(self, data: bytes, *, suffix: str = "") -> BlobInfo:
        ext = ""
        if suffix:
            s = "".join(ch for ch in suffix if ch.isalnum() or ch == ".")
            ext = s if s.startswith(".") else f".{s}" if s else ""
        key = f"{uuid.uuid4().hex}{ext}"
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)  # atomic-ish publish
        return BlobInfo(key=key, size_bytes=len(data), sha256=sha256_hex(data))

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> None:
        p = self._path(key)
        if p.exists():
            p.unlink()
