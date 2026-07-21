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
from shared.hashing import sha256_file
from shared.jsonio import load_json, now_iso, write_json

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
    "awaiting_input": {"input_sealed", "failed", "cancelled"},  # legacy recovery only
    "input_sealed": {"formatting", "failed", "cancelled"},
    "formatting": {"content_ready", "failed", "cancelled"},
    "content_ready": {"designing", "failed", "cancelled"},
    "designing": {"artwork_ready", "failed", "cancelled"},
    "publish_ready": {"publishing", "failed", "cancelled"},
    "publishing": {"published", "failed", "cancelled"},
    "failed": set(),
    "published": set(),
    "cancelled": set(),
}

MODE_STATUS_TRANSITIONS = {
    "newspic": {
        **COMMON_STATUS_TRANSITIONS,
        "artwork_ready": {"publish_ready", "failed", "cancelled"},
    },
    "news": {
        **COMMON_STATUS_TRANSITIONS,
        "artwork_ready": {"typesetting", "failed", "cancelled"},
        "typesetting": {"layout_ready", "failed", "cancelled"},
        "layout_ready": {"publish_ready", "failed", "cancelled"},
    },
}

EXPECTED_WORKER_STATUS = {
    "formatter": "formatting",
    "designer": "designing",
    "typesetter": "typesetting",
    "publisher": "publishing",
}
ALLOWED_ACTORS = {
    "wechat-leader",
    "wechat-formatter",
    "wechat-designer",
    "wechat-typesetter",
    "wechat-publisher",
}

HOST_RUNTIMES = ("claude-code", "codex")


def detect_host_runtime() -> str:
    """Best-effort host detection from environment markers."""
    if os.environ.get("CLAUDECODE"):
        return "claude-code"
    return "unknown"


def validate_host_runtime(declared: str) -> str:
    """Fail closed when the declared host runtime contradicts the environment.

    The pipeline's worker dispatch and native Skill invocation only work on
    hosts that can spawn pipeline subagents; a chat bridge that merely relays
    prompts must not silently degrade into a single-agent run.
    """
    if declared not in HOST_RUNTIMES:
        raise SystemExit(
            f"--host-runtime must be one of {list(HOST_RUNTIMES)}, got {declared!r}; "
            "declare the runtime that actually executes this run"
        )
    detected = detect_host_runtime()
    if declared == "claude-code" and detected != "claude-code":
        raise SystemExit(
            "--host-runtime claude-code requires the CLAUDECODE environment marker; "
            "this shell is not a Claude Code session"
        )
    if declared == "codex" and detected == "claude-code":
        raise SystemExit(
            "--host-runtime codex contradicts the CLAUDECODE environment marker; "
            "declare claude-code instead"
        )
    return declared


def check_run_host_runtime(data: dict) -> str | None:
    """Return an error when the current shell contradicts the run's host runtime."""
    declared = str(data.get("host_runtime", ""))
    if declared not in HOST_RUNTIMES:
        return f"run.json host_runtime must be one of {list(HOST_RUNTIMES)}"
    detected = detect_host_runtime()
    if declared == "claude-code" and detected != "claude-code":
        return "run was created for claude-code but CLAUDECODE is not set"
    if declared == "codex" and detected == "claude-code":
        return "run was created for codex but this shell is a Claude Code session"
    return None


def state_checksum(data: dict) -> str:
    protected = {
        key: data.get(key)
        for key in (
            "protocol_version", "run_id", "mode", "account", "canonical_output_dir",
            "input_path", "source_sha256", "status", "failed_from", "revision",
            "host_runtime",
        )
    }
    encoded = json.dumps(protected, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    if not slug:
        raise SystemExit(
            f"slug must contain at least one ASCII letter or digit, got: {value!r}"
        )
    return slug


def normalize_run_slug(value: str) -> str:
    """Keep the semantic slug while run_id remains the sole uniqueness suffix."""
    slug = slugify(value)
    slug = re.sub(r"(?:^|-)\d{8}-\d{6}$", "", slug).strip("-")
    return slug or "article"


OBSIDIAN_IMAGE_EMBED = re.compile(
    r"!\[\[\s*([^\[\]|]*?\.(?:png|jpe?g|gif|webp|bmp|heic|svg))(?:\|[^\[\]]*)?\]\]",
    re.IGNORECASE,
)
LOCAL_IMAGE_REFERENCE = re.compile(
    r"!\[[^\]]*\]\(\s*(?!https?://|data:|#)([^)\s]+)[^)]*\)",
    re.IGNORECASE,
)


def find_local_image_references(text: str) -> list[str]:
    """List local image references the pipeline cannot carry into a WeChat draft."""
    found: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for match in OBSIDIAN_IMAGE_EMBED.finditer(line):
            found.append(f"第 {lineno} 行: ![[{match.group(1).strip()}]]")
        for match in LOCAL_IMAGE_REFERENCE.finditer(line):
            found.append(f"第 {lineno} 行: ![]({match.group(1)})")
    return found


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
    if actor not in ALLOWED_ACTORS:
        raise SystemExit(f"event actor must be one of {sorted(ALLOWED_ACTORS)}, got {actor!r}")
    pipeline_dir = run_dir / ".pipeline"
    record = {
        "schema_version": 1,
        "protocol_version": PROTOCOL_VERSION,
        "run_id": data["run_id"],
        "event_id": secrets.token_hex(8),
        "event": event,
        "actor": actor,
        "occurred_at": now_iso(),
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


@contextmanager
def run_lock(run_dir: Path):
    """Serialize state projection updates for one run."""
    if fcntl is None:
        raise SystemExit("run state locking requires a Unix-like host")
    lock_path = run_dir / ".pipeline" / "run.lock"
    with lock_path.open("a+") as handle:
        lock_path.chmod(0o600)
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
    if data.get("state_checksum") != state_checksum(data):
        raise SystemExit("run.json state checksum mismatch; direct state edits are forbidden")
    host_error = check_run_host_runtime(data)
    if host_error:
        raise SystemExit(host_error)
    input_path = Path(str(data.get("input_path", ""))).expanduser().resolve()
    expected_input = run_dir / ".pipeline" / "input.md"
    if input_path != expected_input:
        raise SystemExit("run.json input_path must point to the canonical .pipeline/input.md")
    return input_path


def validate_worker_stage(
    run_dir: Path,
    worker: str,
    *,
    check_integrity: bool = True,
) -> list[str]:
    """Fail closed unless a Worker is entering its exact authorized stage."""
    errors: list[str] = []
    try:
        run = load_json(run_dir / ".pipeline" / "run.json")
        validate_run_identity(run_dir, run)
    except (OSError, ValueError, json.JSONDecodeError, SystemExit) as err:
        return [f"unable to validate run identity: {err}"]
    expected = EXPECTED_WORKER_STATUS[worker]
    if run.get("status") != expected:
        errors.append(f"{worker} requires run status {expected}, got {run.get('status')!r}")
    if check_integrity:
        from integrity import validate_runtime

        runtime_errors, _ = validate_runtime(run_dir, run)
        errors.extend(runtime_errors)
    return errors


def guard_worker(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.expanduser().resolve()
    errors = validate_worker_stage(run_dir, args.worker)
    if errors:
        print("stage guard failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(json.dumps({"ok": True, "worker": args.worker, "status": EXPECTED_WORKER_STATUS[args.worker]}))
    return 0


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
    from integrity import validate_runtime

    candidates: list[tuple[str, Path]] = []
    for path, data in iter_runs(exports_root):
        if (
            data.get("protocol_version") == PROTOCOL_VERSION
            and data.get("mode") == mode
            and data.get("account") == account
            and data.get("source_sha256") == source_sha256
            and data.get("status") not in {"published", "cancelled"}
        ):
            run_dir = path.parent.parent
            errors, _ = validate_runtime(run_dir, data)
            if not errors:
                candidates.append((str(data.get("created_at", "")), run_dir))
    return max(candidates, default=("", None))[1]


def init_run(args: argparse.Namespace) -> int:
    exports_root = args.exports_root.expanduser().resolve()
    source = args.source.expanduser().resolve()
    host_runtime = validate_host_runtime(args.host_runtime)
    if not source.is_file():
        raise SystemExit(f"source file not found: {source}")
    if source.stat().st_size == 0:
        raise SystemExit("source file must not be empty")
    source_hash = sha256_file(source)

    with init_lock(exports_root):
        reusable = find_reusable_run(exports_root, args.mode, args.account, source_hash)
        if reusable and not args.force_new:
            print(json.dumps({"reused": True, "run_dir": str(reusable)}, ensure_ascii=False))
            return 0

        references = find_local_image_references(
            source.read_text(encoding="utf-8", errors="replace")
        )
        if references:
            listed = "\n".join(f"- {item}" for item in references[:10])
            raise SystemExit(
                "源文包含本地图片引用，发布流水线无法携带（正文图片由 Designer 统一生成并受 "
                "manifest 约束，本地图片只会变成占位文字或被静默丢弃）：\n"
                f"{listed}\n"
                "请先删除这些图片行或改写为文字说明，再重新发起。"
            )

        now = datetime.now().astimezone()
        run_id = f"{now:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
        parent = "image-cards" if args.mode == "newspic" else "wechat-pipeline"
        run_dir = exports_root / parent / f"{normalize_run_slug(args.slug)}-{run_id}"
        pipeline_dir = run_dir / ".pipeline"
        pipeline_dir.mkdir(parents=True, exist_ok=False)
        run_dir.chmod(0o700)
        pipeline_dir.chmod(0o700)

        input_path = pipeline_dir / "input.md"
        input_path.write_bytes(source.read_bytes())
        input_path.chmod(0o400)

        data = {
            "protocol_version": PROTOCOL_VERSION,
            "run_id": run_id,
            "mode": args.mode,
            "account": args.account,
            "host_runtime": host_runtime,
            "canonical_output_dir": str(run_dir),
            "input_path": str(input_path),
            "source_sha256": source_hash,
            "status": "input_sealed",
            "revision": 0,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        data["state_checksum"] = state_checksum(data)
        write_json(pipeline_dir / "run.json", data)
        from integrity import capture_runtime
        from preflight_image_backends import probe_image_backends

        capture_runtime(run_dir, data)
        write_json(
            pipeline_dir / "backends.json",
            {
                "schema_version": 1,
                "protocol_version": PROTOCOL_VERSION,
                "run_id": run_id,
                "probed_at": now_iso(),
                **probe_image_backends(),
            },
        )
        append_event(
            run_dir,
            data,
            "run.created",
            "wechat-leader",
            {
                "status": data["status"],
                "mode": args.mode,
                "account": args.account,
                "host_runtime": host_runtime,
            },
        )
    print(json.dumps({"reused": False, "run_dir": str(run_dir), **data}, ensure_ascii=False))
    return 0


def seal_run(args: argparse.Namespace) -> int:
    if args.actor != "wechat-leader":
        raise SystemExit("run input may only be sealed by actor wechat-leader")
    run_dir = args.run_dir.expanduser().resolve()
    run_path = run_dir / ".pipeline" / "run.json"
    with run_lock(run_dir):
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
        data["updated_at"] = now_iso()
        data["state_checksum"] = state_checksum(data)
        write_json(run_path, data)
        append_event(run_dir, data, "input.sealed", args.actor, {"source_sha256": source_hash})
    print(json.dumps(data, ensure_ascii=False))
    return 0


def set_status(args: argparse.Namespace) -> int:
    if args.actor != "wechat-leader":
        raise SystemExit("run status may only be changed by actor wechat-leader")
    run_dir = args.run_dir.expanduser().resolve()
    run_path = run_dir / ".pipeline" / "run.json"
    with run_lock(run_dir):
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
        if args.status != current:
            validate_transition_gate(run_dir, data, args.status)
        if args.status == "failed" and current != "failed":
            data["failed_from"] = current
        elif current == "failed" and args.status != "failed":
            data.pop("failed_from", None)
        previous = current
        data["status"] = args.status
        data["revision"] = int(data.get("revision", 0)) + (args.status != previous)
        data["updated_at"] = now_iso()
        data["state_checksum"] = state_checksum(data)
        write_json(run_path, data)
        if args.status != previous:
            append_event(
                run_dir,
                data,
                "status.changed",
                args.actor,
                {"from": previous, "to": args.status, "revision": data["revision"]},
            )
    print(json.dumps(data, ensure_ascii=False))
    return 0


def validate_transition_gate(run_dir: Path, data: dict, target: str) -> None:
    """Run the deterministic gate required to enter one target state."""
    if target not in {"failed", "cancelled"}:
        from integrity import validate_runtime

        errors, _ = validate_runtime(
            run_dir,
            data,
            force=target in {"publish_ready", "published"},
        )
        if errors:
            raise SystemExit("runtime integrity gate failed: " + "; ".join(errors))
    if target == "content_ready":
        from prepare_content import validate_content_artifact

        errors, _ = validate_content_artifact(run_dir)
    elif target == "artwork_ready":
        from validate_designer_manifest import validate

        errors = validate(run_dir / ".pipeline" / "manifest.json")
    elif target == "layout_ready":
        from prepare_layout import validate_layout_evidence

        errors, _ = validate_layout_evidence(run_dir)
    elif target == "publish_ready":
        from build_publish_snapshot import validate_snapshot_evidence

        errors, _ = validate_snapshot_evidence(run_dir)
    elif target == "publishing":
        from build_publish_snapshot import validate_snapshot

        errors, _ = validate_snapshot(run_dir)
    elif target == "published":
        from validate_publish_result import validate

        errors, _ = validate(run_dir)
    else:
        errors = []
    if errors:
        raise SystemExit(f"cannot enter {target}: " + "; ".join(errors))


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
    allowed_statuses = {
        "wechat-formatter": {"formatting"},
        "wechat-designer": {"designing"},
        "wechat-typesetter": {"typesetting"},
        "wechat-publisher": {"publishing"},
    }
    actor_statuses = allowed_statuses.get(args.actor)
    if actor_statuses is None or data.get("status") not in actor_statuses:
        raise SystemExit(
            f"progress actor {args.actor!r} cannot report while status is {data.get('status')!r}"
        )
    payload = {
        "schema_version": 1,
        "protocol_version": PROTOCOL_VERSION,
        "run_id": data["run_id"],
        "actor": args.actor,
        "stage": args.stage,
        "completed": args.completed,
        "total": args.total,
        "message": args.message or "",
        "updated_at": now_iso(),
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
    init_parser.add_argument("--source", type=Path, required=True)
    init_parser.add_argument("--host-runtime", choices=HOST_RUNTIMES, required=True)
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
        choices=("awaiting_input", "input_sealed", "formatting", "content_ready", "designing", "artwork_ready", "typesetting", "layout_ready", "publish_ready", "publishing", "published", "failed", "cancelled"),
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

    guard_parser = subparsers.add_parser("guard")
    guard_parser.add_argument("run_dir", type=Path)
    guard_parser.add_argument("worker", choices=tuple(EXPECTED_WORKER_STATUS))
    guard_parser.set_defaults(func=guard_worker)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
