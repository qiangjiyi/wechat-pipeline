#!/usr/bin/env python3
"""Capture and verify the immutable plugin runtime used by one pipeline run."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

from protocol_version import PROTOCOL_VERSION


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
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
    PLUGIN_ROOT / "third_party" / "baoyu-skills.lock.json",
    PLUGIN_ROOT / "third_party" / "gzh-design.lock.json",
)


def relevant(path: Path) -> bool:
    return (
        path.is_file()
        and path.name != ".DS_Store"
        and path.suffix != ".pyc"
        and "__pycache__" not in path.parts
    )


def runtime_files() -> list[Path]:
    files = [path for root in RUNTIME_ROOTS for path in root.rglob("*") if relevant(path)]
    files.extend(path for path in RUNTIME_FILES if path.is_file())
    return sorted(set(files), key=lambda path: path.relative_to(PLUGIN_ROOT).as_posix())


def runtime_sha256() -> str:
    digest = hashlib.sha256()
    for path in runtime_files():
        relative = path.relative_to(PLUGIN_ROOT).as_posix().encode("utf-8")
        contents = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(contents).to_bytes(8, "big"))
        digest.update(contents)
    return digest.hexdigest()


def snapshot_path(run_dir: Path) -> Path:
    return run_dir / ".pipeline" / "runtime-integrity.json"


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)
    path.chmod(0o400)


def capture(run_dir: Path, run: dict) -> dict:
    path = snapshot_path(run_dir)
    if path.exists():
        errors, existing = validate(run_dir, run)
        if errors:
            raise SystemExit("runtime integrity snapshot mismatch: " + "; ".join(errors))
        return existing or {}
    payload = {
        "schema_version": 1,
        "protocol_version": PROTOCOL_VERSION,
        "run_id": run.get("run_id"),
        "plugin_root": str(PLUGIN_ROOT),
        "runtime_sha256": runtime_sha256(),
        "file_count": len(runtime_files()),
        "captured_at": datetime.now().astimezone().isoformat(),
    }
    write_json(path, payload)
    return payload


def validate(run_dir: Path, run: dict | None = None) -> tuple[list[str], dict | None]:
    errors: list[str] = []
    path = snapshot_path(run_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        return [f"unable to read runtime integrity snapshot: {err}"], None
    if run is None:
        try:
            run = json.loads((run_dir / ".pipeline" / "run.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            return [f"unable to read run context: {err}"], payload
    if payload.get("protocol_version") != PROTOCOL_VERSION:
        errors.append(f"runtime protocol_version must be {PROTOCOL_VERSION}")
    if payload.get("run_id") != run.get("run_id"):
        errors.append("runtime integrity run_id does not match run.json")
    if Path(str(payload.get("plugin_root", ""))).expanduser().resolve() != PLUGIN_ROOT:
        errors.append("runtime integrity plugin_root does not match the executing plugin")
    actual = runtime_sha256()
    if payload.get("runtime_sha256") != actual:
        errors.append("plugin runtime changed after this run was initialized")
    return errors, payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("capture", "validate"))
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    run_dir = args.run_dir.expanduser().resolve()
    run = json.loads((run_dir / ".pipeline" / "run.json").read_text(encoding="utf-8"))
    if args.command == "capture":
        print(json.dumps(capture(run_dir, run), ensure_ascii=False))
        return 0
    errors, payload = validate(run_dir, run)
    if errors:
        print("runtime integrity validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(json.dumps({"ok": True, **(payload or {})}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
