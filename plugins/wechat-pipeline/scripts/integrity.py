#!/usr/bin/env python3
"""Capture or validate release/runtime integrity with per-run metadata caching."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

from protocol_version import PROTOCOL_VERSION  # noqa: E402
from shared.file_utils import is_relevant_file  # noqa: E402
from shared.hashing import sha256_file  # noqa: E402
from shared.jsonio import load_json, write_json  # noqa: E402


LOCK_PATH = PLUGIN_ROOT / "release-integrity.json"
RUNTIME_ROOTS = (
    PLUGIN_ROOT / "agents",
    PLUGIN_ROOT / "scripts",
    PLUGIN_ROOT / "shared",
    PLUGIN_ROOT / "skills" / "wechat-pipeline",
    PLUGIN_ROOT / "skills" / "wechat-publisher",
    PLUGIN_ROOT / "skills" / "gzh-design",
) + tuple(sorted(PLUGIN_ROOT.glob("skills/baoyu-*")))
RUNTIME_FILES = (
    PLUGIN_ROOT / "docs" / "wechat-pipeline-protocol.md",
    PLUGIN_ROOT / "release-integrity.json",
    PLUGIN_ROOT / "third_party" / "baoyu-skills.lock.json",
    PLUGIN_ROOT / "third_party" / "gzh-design.lock.json",
)


def relevant(path: Path) -> bool:
    return path.is_file() and is_relevant_file(path) and ".in_use" not in path.parts


def release_files() -> list[Path]:
    return sorted(
        (path for path in PLUGIN_ROOT.rglob("*") if relevant(path) and path != LOCK_PATH),
        key=lambda path: path.relative_to(PLUGIN_ROOT).as_posix(),
    )


def runtime_files() -> list[Path]:
    files = [path for root in RUNTIME_ROOTS for path in root.rglob("*") if relevant(path)]
    files.extend(path for path in RUNTIME_FILES if path.is_file())
    return sorted(set(files), key=lambda path: path.relative_to(PLUGIN_ROOT).as_posix())


def aggregate(files: dict[str, str]) -> str:
    encoded = json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def payload_checksum(payload: dict[str, Any]) -> str:
    """Bind an integrity snapshot's claims so editing one field is detectable."""
    core = {key: value for key, value in payload.items() if key != "snapshot_checksum"}
    encoded = json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_hashes(paths: list[Path]) -> dict[str, str]:
    return {path.relative_to(PLUGIN_ROOT).as_posix(): sha256_file(path) for path in paths}


def file_state(paths: list[Path]) -> dict[str, int]:
    stats = [path.stat() for path in paths]
    return {
        "file_count": len(paths),
        "latest_mtime_ns": max((value.st_mtime_ns for value in stats), default=0),
        "total_size": sum(value.st_size for value in stats),
    }


def current_release_payload() -> dict[str, Any]:
    files = file_hashes(release_files())
    return {
        "schema_version": 1,
        "protocol_version": PROTOCOL_VERSION,
        "file_count": len(files),
        "runtime_sha256": aggregate(files),
        "files": files,
    }


def capture_release() -> dict[str, Any]:
    payload = current_release_payload()
    write_json(LOCK_PATH, payload, mode=0o644)
    return payload


def validate_release() -> tuple[list[str], dict | None]:
    try:
        expected = load_json(LOCK_PATH)
    except (OSError, ValueError, json.JSONDecodeError) as err:
        return [f"unable to read release integrity manifest: {err}"], None
    actual = current_release_payload()
    errors: list[str] = []
    if expected.get("schema_version") != 1:
        errors.append("release integrity schema_version must be 1")
    if expected.get("protocol_version") != PROTOCOL_VERSION:
        errors.append(f"release integrity protocol_version must be {PROTOCOL_VERSION}")
    expected_files = expected.get("files")
    if not isinstance(expected_files, dict):
        errors.append("release integrity files must be an object")
        expected_files = {}
    missing = sorted(set(expected_files) - set(actual["files"]))
    unexpected = sorted(set(actual["files"]) - set(expected_files))
    changed = sorted(
        name
        for name in set(expected_files) & set(actual["files"])
        if expected_files[name] != actual["files"][name]
    )
    if missing:
        errors.append("release files are missing: " + ", ".join(missing[:10]))
    if unexpected:
        errors.append("release contains unregistered runtime files: " + ", ".join(unexpected[:10]))
    if changed:
        errors.append("release runtime files changed: " + ", ".join(changed[:10]))
    if expected.get("file_count") != actual["file_count"]:
        errors.append("release integrity file_count mismatch")
    if expected.get("runtime_sha256") != actual["runtime_sha256"]:
        errors.append("release integrity aggregate hash mismatch")
    return errors, expected


def runtime_snapshot_path(run_dir: Path) -> Path:
    return run_dir / ".pipeline" / "runtime-integrity.json"


def runtime_cache_path(run_dir: Path) -> Path:
    return run_dir / ".pipeline" / "integrity-cache.json"


def runtime_hash_from_validated_release(release: dict[str, Any]) -> str:
    """Derive the runtime subset from an already validated release file map."""
    release_hashes = release.get("files") if isinstance(release.get("files"), dict) else {}
    hashes: dict[str, str] = {}
    for path in runtime_files():
        relative = path.relative_to(PLUGIN_ROOT).as_posix()
        # The release manifest deliberately excludes itself from its signed file map.
        hashes[relative] = (
            sha256_file(path)
            if path == LOCK_PATH
            else str(release_hashes.get(relative) or sha256_file(path))
        )
    return aggregate(hashes)


def capture_runtime(run_dir: Path, run: dict) -> dict[str, Any]:
    release_errors, release = validate_release()
    if release_errors:
        raise SystemExit("plugin release integrity check failed: " + "; ".join(release_errors))
    snapshot = runtime_snapshot_path(run_dir)
    if snapshot.exists():
        errors, existing = validate_runtime(run_dir, run)
        if errors:
            raise SystemExit("runtime integrity snapshot mismatch: " + "; ".join(errors))
        return existing or {}
    paths = runtime_files()
    state = file_state(paths)
    runtime_hash = runtime_hash_from_validated_release(release or {})
    payload = {
        "schema_version": 2,
        "protocol_version": PROTOCOL_VERSION,
        "run_id": run.get("run_id"),
        "plugin_root": str(PLUGIN_ROOT),
        "runtime_sha256": runtime_hash,
        **state,
        "captured_at": datetime.now().astimezone().isoformat(),
    }
    payload["snapshot_checksum"] = payload_checksum(payload)
    write_json(snapshot, payload, mode=0o400)
    write_json(runtime_cache_path(run_dir), {
        **state,
        "runtime_sha256": runtime_hash,
        "full_hash_count": 1,
        "last_full_hash_at": payload["captured_at"],
    })
    return payload


def validate_runtime(
    run_dir: Path,
    run: dict | None = None,
    *,
    force: bool = False,
) -> tuple[list[str], dict | None]:
    errors: list[str] = []
    try:
        payload = load_json(runtime_snapshot_path(run_dir))
    except (OSError, ValueError, json.JSONDecodeError) as err:
        return [f"unable to read runtime integrity snapshot: {err}"], None
    if run is None:
        try:
            run = load_json(run_dir / ".pipeline" / "run.json")
        except (OSError, ValueError, json.JSONDecodeError) as err:
            return [f"unable to read run context: {err}"], payload
    if payload.get("schema_version") != 2:
        errors.append("runtime integrity schema_version must be 2")
    if payload.get("snapshot_checksum") != payload_checksum(payload):
        errors.append("runtime integrity snapshot checksum mismatch")
    if payload.get("protocol_version") != PROTOCOL_VERSION:
        errors.append(f"runtime protocol_version must be {PROTOCOL_VERSION}")
    if payload.get("run_id") != run.get("run_id"):
        errors.append("runtime integrity run_id does not match run.json")
    if Path(str(payload.get("plugin_root", ""))).expanduser().resolve() != PLUGIN_ROOT:
        errors.append("runtime integrity plugin_root does not match the executing plugin")

    paths = runtime_files()
    state = file_state(paths)
    baseline_state = {
        key: payload.get(key) for key in ("file_count", "latest_mtime_ns", "total_size")
    }
    actual_hash: str
    if not force and state == baseline_state:
        actual_hash = str(payload.get("runtime_sha256", ""))
    else:
        # A changed metadata fingerprint is never trusted from the writable cache.
        # The cache is evidence for diagnostics; only the sealed baseline can skip hashing.
        actual_hash = aggregate(file_hashes(paths))
        try:
            previous_cache = load_json(runtime_cache_path(run_dir))
        except (OSError, ValueError, json.JSONDecodeError):
            previous_cache = {}
        write_json(runtime_cache_path(run_dir), {
            **state,
            "runtime_sha256": actual_hash,
            "full_hash_count": int(previous_cache.get("full_hash_count", 1)) + 1,
            "last_full_hash_at": datetime.now().astimezone().isoformat(),
        })
    if payload.get("runtime_sha256") != actual_hash:
        errors.append("plugin runtime changed after this run was initialized")
    return errors, payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("capture", "validate"))
    parser.add_argument("--scope", choices=("release", "runtime"), required=True)
    parser.add_argument("run_dir", type=Path, nargs="?")
    parser.add_argument("--force", action="store_true", help="rehash runtime contents even when metadata is unchanged")
    args = parser.parse_args()
    if args.scope == "release":
        if args.run_dir:
            raise SystemExit("release integrity does not accept run_dir")
        if args.command == "capture":
            payload = capture_release()
            print(json.dumps({"ok": True, "path": str(LOCK_PATH), **payload}, ensure_ascii=False))
            return 0
        errors, payload = validate_release()
    else:
        if not args.run_dir:
            raise SystemExit("runtime integrity requires run_dir")
        run_dir = args.run_dir.expanduser().resolve()
        run = load_json(run_dir / ".pipeline" / "run.json")
        if args.command == "capture":
            print(json.dumps(capture_runtime(run_dir, run), ensure_ascii=False))
            return 0
        errors, payload = validate_runtime(run_dir, run, force=args.force)
    if errors:
        print(f"{args.scope} integrity validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(json.dumps({"ok": True, **(payload or {})}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
