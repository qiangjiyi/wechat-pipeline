"""Durable publish receipts used to make draft creation resumable and duplicate-safe."""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import PublishError

try:
    import fcntl
except ImportError:  # pragma: no cover - supported publisher hosts are Unix-like
    fcntl = None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def resolve_result_path(value: str | None) -> Path | None:
    return Path(value).expanduser().resolve() if value else None


def run_identity(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    run_path = path.parent / "run.json"
    if not run_path.is_file():
        return {}
    try:
        run = json.loads(run_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise PublishError(f"unable to read run identity for publish receipt: {err}") from err
    return {
        key: run.get(key)
        for key in ("protocol_version", "run_id", "mode", "account", "canonical_output_dir")
    }


@contextmanager
def publish_lock(path: Path | None):
    """Serialize one durable receipt so concurrent resumes cannot create duplicates."""
    if path is None:
        yield
        return
    if fcntl is None:
        raise PublishError("duplicate-safe publishing requires a Unix-like file lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    with lock_path.open("a+") as handle:
        lock_path.chmod(0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_matching_receipt(path: Path | None, expected_fingerprint: str) -> dict | None:
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise PublishError(f"unable to read publish receipt {path}: {err}") from err
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise PublishError(f"publish receipt has an unsupported schema: {path}")
    if data.get("publish_fingerprint") != expected_fingerprint:
        raise PublishError(
            "publish receipt belongs to different content; refuse to create another draft "
            f"until it is reviewed: {path}"
        )
    return data


def write_receipt(path: Path | None, value: dict) -> dict:
    identity = run_identity(path)
    now = datetime.now().astimezone().isoformat()
    payload = {
        "schema_version": 1,
        "protocol_version": identity.get("protocol_version"),
        "run_id": identity.get("run_id"),
        **value,
    }
    payload.setdefault("recorded_at", now)
    payload["updated_at"] = now
    if path is None:
        return payload
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temp.chmod(0o600)
    os.replace(temp, path)
    return payload
