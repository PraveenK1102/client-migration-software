"""M3A: LocalBlobStore write/read/checksum + integrity (corruption is detectable).

Raw uploaded bytes are stored separately from parsed rows, under server-generated keys (never
the untrusted client filename). SHA-256 is computed at store time so a later size/checksum
mismatch (a corrupt blob) can block parsing instead of ingesting garbage.
"""
from __future__ import annotations

import pytest

from app.blobstore import BlobInfo, LocalBlobStore, sha256_hex


def test_put_get_roundtrip_and_checksum(tmp_path):
    store = LocalBlobStore(tmp_path / "blobs")
    data = b"employee_id,full_name\n001,Alice\n"
    info = store.put(data, suffix=".csv")
    assert isinstance(info, BlobInfo)
    assert info.size_bytes == len(data)
    assert info.sha256 == sha256_hex(data)
    assert store.exists(info.key)
    assert store.get(info.key) == data
    # Key is server-generated and opaque — not the client filename.
    assert "Alice" not in info.key and info.key.endswith(".csv")


def test_keys_are_unique_per_put(tmp_path):
    store = LocalBlobStore(tmp_path / "blobs")
    a = store.put(b"x", suffix=".csv")
    b = store.put(b"x", suffix=".csv")
    assert a.key != b.key                      # distinct keys even for identical bytes
    assert a.sha256 == b.sha256                # same content hash


def test_delete_removes_blob(tmp_path):
    store = LocalBlobStore(tmp_path / "blobs")
    info = store.put(b"bytes", suffix=".csv")
    assert store.exists(info.key)
    store.delete(info.key)
    assert not store.exists(info.key)


def test_path_traversal_key_rejected(tmp_path):
    store = LocalBlobStore(tmp_path / "blobs")
    for bad in ("../escape", "a/b", "..", "/etc/passwd"):
        with pytest.raises(ValueError):
            store.get(bad)


def test_checksum_detects_corruption(tmp_path):
    """After a blob is corrupted on disk, size/sha no longer match what was recorded at store
    time — the integrity check the worker runs before parsing would catch this."""
    store = LocalBlobStore(tmp_path / "blobs")
    data = b"good,bytes\n1,2\n"
    info = store.put(data, suffix=".csv")
    # Corrupt the underlying file directly.
    store._path(info.key).write_bytes(b"tampered")
    fetched = store.get(info.key)
    assert fetched != data
    assert sha256_hex(fetched) != info.sha256   # mismatch is detectable
    assert len(fetched) != info.size_bytes
