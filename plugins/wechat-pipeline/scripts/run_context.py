#!/usr/bin/env python3
"""Create and maintain one canonical directory for a WeChat pipeline run."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

from protocol_version import PROTOCOL_VERSION
from shared.dotenv import load_dotenv

try:
    import fcntl
except ImportError:  # pragma: no cover - supported hosts are Unix-like
    fcntl = None

def default_exports_root() -> Path:
    configured = os.environ.get("WECHAT_PIPELINE_EXPORTS_DIR", "")
    if not configured:
        config = Path(
            os.environ.get("WECHAT_PUBLISHER_ENV_FILE", "~/.config/wechat-pipeline/.env")
        ).expanduser()
        local = config.with_name(".env.local")
        values = load_dotenv(local if local.is_file() else config)
        configured = values.get("WECHAT_PIPELINE_EXPORTS_DIR", "")
    return Path(configured or "~/Workspace/exports").expanduser()

COMMON_STATUS_TRANSITIONS = {
    "awaiting_input": {"input_sealed", "failed", "cancelled"},
    "input_sealed": {"planning", "failed", "cancelled"},
    "planning": {"rendering", "failed", "cancelled"},
    "rendering": {"ready", "failed", "cancelled"},
    "publishing": {"published", "failed", "cancelled"},
    "failed": set(),
    "published": set(),
    "cancelled": set(),
}

MODE_STATUS_TRANSITIONS = {
    "newspic": {
        **COMMON_STATUS_TRANSITIONS,
        "ready": {"publishing", "failed", "cancelled"},
    },
    "news": {
        **COMMON_STATUS_TRANSITIONS,
        "ready": {"typesetting", "failed", "cancelled"},
        "typesetting": {"layout_ready", "failed", "cancelled"},
        "layout_ready": {"publishing", "failed", "cancelled"},
    },
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    if not slug:
        raise SystemExit(
            f"slug must contain at least one ASCII letter or digit, got: {value!r}"
        )
    return slug


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temp.chmod(0o600)
    os.replace(temp, path)


def append_event(
    run_dir: Path,
    data: dict,
    event: str,
    actor: str,
    details: dict | None = None,
) -> dict:
    """Append one durable, lock-protected audit event for a canonical run."""
    if fcntl is None:
        raise SystemExit("run event logging requires a Unix-like host")
    if not re.fullmatch(r"[a-z][a-z0-9_.-]*", event):
        raise SystemExit(f"invalid event name: {event!r}")
    if not actor.strip():
        raise SystemExit("event actor must not be empty")
    pipeline_dir = run_dir / ".pipeline"
    record = {
        "schema_version": 1,
        "protocol_version": PROTOCOL_VERSION,
        "run_id": data["run_id"],
        "event_id": secrets.token_hex(8),
        "event": event,
        "actor": actor,
        "occurred_at": datetime.now().astimezone().isoformat(),
        "details": details or {},
    }
    events_path = pipeline_dir / "events.jsonl"
    with events_path.open("a", encoding="utf-8") as handle:
        events_path.chmod(0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return record


@contextmanager
def init_lock(exports_root: Path):
    """Serialize reuse lookup and run creation for one exports root."""
    if fcntl is None:
        raise SystemExit("run initialization locking requires a Unix-like host")
    exports_root.mkdir(parents=True, exist_ok=True)
    lock_path = exports_root / ".wechat-pipeline-init.lock"
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def validate_run_identity(run_dir: Path, data: dict) -> Path:
    expected_dir = Path(str(data.get("canonical_output_dir", ""))).expanduser().resolve()
    if expected_dir != run_dir:
        raise SystemExit("run.json canonical_output_dir does not match the requested run directory")
    run_id = str(data.get("run_id", ""))
    if not run_id or not run_dir.name.endswith(f"-{run_id}"):
        raise SystemExit("run.json run_id does not match the requested run directory")
    if data.get("protocol_version") != PROTOCOL_VERSION:
        raise SystemExit(f"run protocol_version must be {PROTOCOL_VERSION}")
    input_path = Path(str(data.get("input_path", ""))).expanduser().resolve()
    expected_input = run_dir / ".pipeline" / "input.md"
    if input_path != expected_input:
        raise SystemExit("run.json input_path must point to the canonical .pipeline/input.md")
    return input_path


def iter_runs(exports_root: Path):
    for path in exports_root.glob("*/**/.pipeline/run.json"):
        try:
            yield path, load_json(path)
        except (OSError, ValueError):
            continue


def find_reusable_run(
    exports_root: Path,
    mode: str,
    account: str,
    source_sha256: str | None,
) -> Path | None:
    if not source_sha256:
        return None
    candidates: list[tuple[str, Path]] = []
    for path, data in iter_runs(exports_root):
        if (
            data.get("mode") == mode
            and data.get("account") == account
            and data.get("source_sha256") == source_sha256
            and data.get("status") not in {"published", "cancelled"}
        ):
            candidates.append((str(data.get("created_at", "")), path.parent.parent))
    return max(candidates, default=("", None))[1]


def init_run(args: argparse.Namespace) -> int:
    exports_root = args.exports_root.expanduser().resolve()
    source = args.source.expanduser().resolve() if args.source else None
    if source and not source.is_file():
        raise SystemExit(f"source file not found: {source}")
    source_hash = sha256_file(source) if source else None

    with init_lock(exports_root):
        reusable = find_reusable_run(exports_root, args.mode, args.account, source_hash)
        if reusable and not args.force_new:
            print(json.dumps({"reused": True, "run_dir": str(reusable)}, ensure_ascii=False))
            return 0

        now = datetime.now().astimezone()
        run_id = f"{now:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
        parent = "image-cards" if args.mode == "newspic" else "wechat-articles"
        run_dir = exports_root / parent / f"{slugify(args.slug)}-{run_id}"
        pipeline_dir = run_dir / ".pipeline"
        pipeline_dir.mkdir(parents=True, exist_ok=False)
        run_dir.chmod(0o700)
        pipeline_dir.chmod(0o700)

        input_path = pipeline_dir / "input.md"
        if source:
            input_path.write_bytes(source.read_bytes())
            input_path.chmod(0o400)

        data = {
            "protocol_version": PROTOCOL_VERSION,
            "run_id": run_id,
            "mode": args.mode,
            "account": args.account,
            "canonical_output_dir": str(run_dir),
            "input_path": str(input_path),
            "source_sha256": source_hash,
            "status": "input_sealed" if source else "awaiting_input",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        write_json(pipeline_dir / "run.json", data)
        append_event(
            run_dir,
            data,
            "run.created",
            "wechat-leader",
            {"status": data["status"], "mode": args.mode, "account": args.account},
        )
    print(json.dumps({"reused": False, "run_dir": str(run_dir), **data}, ensure_ascii=False))
    return 0


def seal_run(args: argparse.Namespace) -> int:
    if args.actor != "wechat-leader":
        raise SystemExit("run input may only be sealed by actor wechat-leader")
    run_dir = args.run_dir.expanduser().resolve()
    run_path = run_dir / ".pipeline" / "run.json"
    data = load_json(run_path)
    input_path = validate_run_identity(run_dir, data)
    current = str(data.get("status", ""))
    if current not in {"awaiting_input", "input_sealed"}:
        raise SystemExit(f"cannot seal run while status is {current!r}")
    if not input_path.is_file() or input_path.stat().st_size == 0:
        raise SystemExit(f"run input is missing or empty: {input_path}")
    source_hash = sha256_file(input_path)
    existing = data.get("source_sha256")
    if existing and existing != source_hash:
        raise SystemExit("sealed run input changed; create a new run instead")
    input_path.chmod(0o400)
    data["source_sha256"] = source_hash
    data["status"] = "input_sealed"
    data["updated_at"] = datetime.now().astimezone().isoformat()
    write_json(run_path, data)
    append_event(run_dir, data, "input.sealed", args.actor, {"source_sha256": source_hash})
    print(json.dumps(data, ensure_ascii=False))
    return 0


def set_status(args: argparse.Namespace) -> int:
    if args.actor != "wechat-leader":
        raise SystemExit("run status may only be changed by actor wechat-leader")
    run_dir = args.run_dir.expanduser().resolve()
    run_path = run_dir / ".pipeline" / "run.json"
    data = load_json(run_path)
    validate_run_identity(run_dir, data)
    current = str(data.get("status", ""))
    transitions = MODE_STATUS_TRANSITIONS.get(str(data.get("mode", "")))
    if transitions is None:
        raise SystemExit(f"unknown run mode: {data.get('mode')!r}")
    if current not in transitions:
        raise SystemExit(f"unknown current run status: {current}")
    allowed = transitions[current]
    if current == "failed":
        failed_from = str(data.get("failed_from", ""))
        allowed = {failed_from, "cancelled"} if failed_from else {"cancelled"}
    if args.status != current and args.status not in allowed:
        raise SystemExit(f"invalid run status transition: {current} -> {args.status}")
    if args.status == "published" and current != "published":
        from validate_publish_result import validate as validate_publish_receipt

        receipt_errors, _ = validate_publish_receipt(run_dir)
        if receipt_errors:
            raise SystemExit(
                "cannot mark published: " + "; ".join(receipt_errors)
            )
    if args.status == "failed" and current != "failed":
        data["failed_from"] = current
    elif current == "failed" and args.status != "failed":
        data.pop("failed_from", None)
    previous = current
    data["status"] = args.status
    data["updated_at"] = datetime.now().astimezone().isoformat()
    write_json(run_path, data)
    if args.status != previous:
        append_event(
            run_dir,
            data,
            "status.changed",
            args.actor,
            {"from": previous, "to": args.status},
        )
    print(json.dumps(data, ensure_ascii=False))
    return 0


def record_event(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.expanduser().resolve()
    run_path = run_dir / ".pipeline" / "run.json"
    data = load_json(run_path)
    validate_run_identity(run_dir, data)
    try:
        details = json.loads(args.details_json) if args.details_json else {}
    except json.JSONDecodeError as err:
        raise SystemExit(f"event details must be valid JSON: {err}") from err
    if not isinstance(details, dict):
        raise SystemExit("event details must be a JSON object")
    record = append_event(run_dir, data, args.event, args.actor, details)
    print(json.dumps(record, ensure_ascii=False))
    return 0


def update_progress(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.expanduser().resolve()
    run_path = run_dir / ".pipeline" / "run.json"
    data = load_json(run_path)
    validate_run_identity(run_dir, data)
    if not args.actor.strip():
        raise SystemExit("progress actor must not be empty")
    if args.completed < 0 or args.total < 0 or args.completed > args.total:
        raise SystemExit("progress requires 0 <= completed <= total")
    payload = {
        "schema_version": 1,
        "protocol_version": PROTOCOL_VERSION,
        "run_id": data["run_id"],
        "actor": args.actor,
        "stage": args.stage,
        "completed": args.completed,
        "total": args.total,
        "message": args.message or "",
        "updated_at": datetime.now().astimezone().isoformat(),
    }
    write_json(run_dir / ".pipeline" / "progress.json", payload)
    append_event(
        run_dir,
        data,
        "progress.updated",
        args.actor,
        {
            "stage": args.stage,
            "completed": args.completed,
            "total": args.total,
        },
    )
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--mode", choices=("newspic", "news"), required=True)
    init_parser.add_argument("--account", required=True)
    init_parser.add_argument("--slug", required=True)
    init_parser.add_argument("--source", type=Path)
    init_parser.add_argument("--exports-root", type=Path, default=default_exports_root())
    init_parser.add_argument("--force-new", action="store_true")
    init_parser.set_defaults(func=init_run)

    seal_parser = subparsers.add_parser("seal")
    seal_parser.add_argument("run_dir", type=Path)
    seal_parser.add_argument("--actor", required=True)
    seal_parser.set_defaults(func=seal_run)

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("run_dir", type=Path)
    status_parser.add_argument(
        "status",
        choices=("awaiting_input", "input_sealed", "planning", "rendering", "ready", "typesetting", "layout_ready", "publishing", "published", "failed", "cancelled"),
    )
    status_parser.add_argument("--actor", required=True)
    status_parser.set_defaults(func=set_status)

    event_parser = subparsers.add_parser("event")
    event_parser.add_argument("run_dir", type=Path)
    event_parser.add_argument("event")
    event_parser.add_argument("--actor", required=True)
    event_parser.add_argument("--details-json")
    event_parser.set_defaults(func=record_event)

    progress_parser = subparsers.add_parser("progress")
    progress_parser.add_argument("run_dir", type=Path)
    progress_parser.add_argument("--actor", required=True)
    progress_parser.add_argument("--stage", required=True)
    progress_parser.add_argument("--completed", required=True, type=int)
    progress_parser.add_argument("--total", required=True, type=int)
    progress_parser.add_argument("--message")
    progress_parser.set_defaults(func=update_progress)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
