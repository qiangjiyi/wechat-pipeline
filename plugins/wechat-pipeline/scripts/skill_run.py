#!/usr/bin/env python3
"""Manage Formatter, visual and layout native-Skill execution boundaries.

Every boundary uses the same lifecycle: start -> complete|fail, with an
explicit Leader-only reset for failed receipts.  Complete always receives an
invocation id and one or more role=/absolute/path outputs.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

from image_contracts import skill_options, validate_output_contract  # noqa: E402
from image_evidence import IMAGE_ROLES, validate_image_evidence  # noqa: E402
from preflight_image_backends import probe_image_backends  # noqa: E402
from protocol_version import PROTOCOL_VERSION  # noqa: E402
from run_context import append_event, validate_worker_stage  # noqa: E402
from shared.hashing import sha256_file  # noqa: E402
from shared.jsonio import inside, load_json, now_iso, write_json  # noqa: E402
from shared.text_preservation import missing_summary, preservation_report  # noqa: E402
from validate_article_layout import validate_native_output  # noqa: E402


SKILLS_ROOT = PLUGIN_ROOT / "skills"
EXPECTED_SKILLS = {
    "newspic": ("baoyu-xhs-images",),
    "news": ("baoyu-cover-image", "baoyu-article-illustrator"),
}
ALLOWED_ROLES = {
    "baoyu-format-markdown": {"formatted"},
    "baoyu-xhs-images": {"card"},
    "baoyu-cover-image": {"cover"},
    "baoyu-article-illustrator": {"article", "body"},
    "gzh-design": {"html"},
}
BOUNDARY_SKILLS = {
    "formatter": "baoyu-format-markdown",
    "layout": "gzh-design",
}
BOUNDARY_WORKERS = {
    "formatter": "formatter",
    "visual": "designer",
    "layout": "typesetter",
}
ARTICLE_ILLUSTRATOR_CONFIRMATION_AUTHORIZATION = "直接生成，不用确认，跳过确认，按默认出图。"


def skill_path(skill_name: str) -> Path:
    path = (SKILLS_ROOT / skill_name / "SKILL.md").resolve()
    if not path.is_file():
        raise SystemExit(f"bundled Skill not found: {path}")
    return path


def formatter_paths(run_dir: Path) -> tuple[Path, Path, Path, Path, Path]:
    workspace = run_dir / "baoyu-format-markdown"
    return (
        run_dir / ".pipeline" / "formatter-skill-run.json",
        run_dir / ".pipeline" / "input.md",
        workspace,
        workspace / "article.md",
        workspace / "article-formatted.md",
    )


def layout_paths(run_dir: Path) -> tuple[Path, Path]:
    return run_dir / ".pipeline" / "layout-skill-run.json", run_dir / "gzh-design"


def visual_receipt_path(run_dir: Path, invocation_id: str) -> Path:
    return run_dir / ".pipeline" / "skill-runs" / f"{invocation_id}.json"


def invocation_id_for(boundary: str, skill_name: str) -> str:
    return skill_name


def validate_invocation_id(value: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,63}", value):
        raise SystemExit("invocation id must be 2-64 lowercase ASCII letters, digits, or hyphens")
    return value


def receipt_path(run_dir: Path, boundary: str, invocation_id: str) -> Path:
    if boundary == "formatter":
        return formatter_paths(run_dir)[0]
    if boundary == "layout":
        return layout_paths(run_dir)[0]
    return visual_receipt_path(run_dir, invocation_id)


def command_prefix() -> str:
    return f'bash "{PLUGIN_ROOT / "scripts" / "run_python.sh"}" "{Path(__file__).resolve()}"'


def complete_example(boundary: str, run_dir: Path, invocation_id: str, record: dict | None = None) -> str:
    workspace = Path(str((record or {}).get("workspace", run_dir / "WORKSPACE")))
    if boundary == "formatter":
        outputs = f'--output "formatted={workspace / "article-formatted.md"}"'
    elif boundary == "layout":
        outputs = f'--output "html={workspace / "FINAL.html"}"'
    else:
        skill_name = str((record or {}).get("skill_name", invocation_id))
        if skill_name == "baoyu-xhs-images":
            outputs = (
                f'--output "card={workspace / "card-01.png"}" '
                f'--evidence "{workspace / "card-01.evidence.json"}"'
            )
        elif skill_name == "baoyu-cover-image":
            outputs = (
                f'--output "cover={workspace / "cover.png"}" '
                f'--evidence "{workspace / "cover.evidence.json"}"'
            )
        else:
            outputs = (
                f'--output "article={workspace / "article.md"}" '
                f'--output "body={workspace / "image-01.png"}" '
                f'--evidence "{workspace / "image-01.evidence.json"}"'
            )
    return (
        f"{command_prefix()} --boundary {boundary} complete \"{run_dir}\" "
        f"--invocation-id {invocation_id} {outputs}"
    )


def contract_error(
    message: str,
    boundary: str,
    run_dir: Path,
    invocation_id: str,
    record: dict | None = None,
) -> None:
    skill_name = str((record or {}).get("skill_name", invocation_id))
    roles = ", ".join(sorted(ALLOWED_ROLES.get(skill_name, set()))) or "none"
    raise SystemExit(
        f"{message}\nAllowed roles for {skill_name}: {roles}. "
        "Every output must use role=/absolute/path and stay inside the recorded workspace.\n"
        f"Copyable example:\n{complete_example(boundary, run_dir, invocation_id, record)}"
    )


def load_run_for_boundary(run_dir: Path, boundary: str) -> dict:
    errors = validate_worker_stage(run_dir, BOUNDARY_WORKERS[boundary], check_integrity=False)
    if errors:
        raise SystemExit(f"{boundary} stage guard failed: " + "; ".join(errors))
    run = load_json(run_dir / ".pipeline" / "run.json")
    if boundary == "layout" and run.get("mode") != "news":
        raise SystemExit("gzh-design is only valid for a news run")
    return run


def visual_skill_for_start(run: dict, requested: str | None) -> str:
    if not requested:
        raise SystemExit("visual start requires --skill")
    expected = EXPECTED_SKILLS.get(str(run.get("mode")), ())
    if requested not in expected:
        raise SystemExit(f"Skill {requested!r} is not part of the {run.get('mode')!r} artwork flow")
    return requested


def integration_input(run_dir: Path) -> tuple[Path, str]:
    manifest = load_json(run_dir / ".pipeline" / "manifest.json")
    value = manifest.get("layout_input")
    if not isinstance(value, dict):
        raise SystemExit("designer manifest does not provide layout_input")
    path = Path(str(value.get("path", ""))).expanduser().resolve()
    expected_hash = str(value.get("sha256", ""))
    if not path.is_file() or sha256_file(path) != expected_hash:
        raise SystemExit("native illustrator layout input is missing or changed")
    return path, expected_hash


def cached_backends(run_dir: Path, run: dict) -> dict:
    path = run_dir / ".pipeline" / "backends.json"
    if path.is_file():
        payload = load_json(path)
        if payload.get("run_id") == run.get("run_id"):
            return payload
    payload = {
        "schema_version": 1,
        "protocol_version": PROTOCOL_VERSION,
        "run_id": run.get("run_id"),
        "probed_at": now_iso(),
        **probe_image_backends(),
    }
    write_json(path, payload)
    return payload


def invocation_args(working_input: Path, workspace: Path) -> str:
    return (
        f"对 {working_input} 执行全自动公众号排版，所有自然产物写入 {workspace}。"
        "完整保留输入文章已有的可见正文，让文章在原文结束处自然结束；"
        "不要新增作者签名、作者介绍、关注、点赞、在看、转发、分享、三连、下篇见等互动引导。"
        "主题、组件、结构解析和排版细节均由 gzh-design 自主决定。"
    )


def expected_identity(boundary: str, run_dir: Path, run: dict, skill_name: str) -> tuple[Path, str, Path, Path]:
    if boundary == "formatter":
        _, source, workspace, working_input, _ = formatter_paths(run_dir)
        return source, sha256_file(source), workspace, working_input
    if boundary == "layout":
        source, source_hash = integration_input(run_dir)
        workspace = layout_paths(run_dir)[1]
        return source, source_hash, workspace, workspace / "article.md"
    source = run_dir / "content.md"
    workspace = run_dir / skill_name
    return source, sha256_file(source), workspace, workspace / "article.md"


def validate_reusable_record(record: dict, run_dir: Path, boundary: str, run: dict) -> None:
    skill_name = str(record.get("skill_name", ""))
    source, source_hash, workspace, _ = expected_identity(boundary, run_dir, run, skill_name)
    errors: list[str] = []
    if record.get("run_id") != run.get("run_id") or record.get("protocol_version") != PROTOCOL_VERSION:
        errors.append("receipt identity does not match the current run")
    if Path(str(record.get("input_path", ""))).expanduser().resolve() != source:
        errors.append("receipt input path changed")
    if not source.is_file() or record.get("input_sha256") != source_hash:
        errors.append("receipt input hash no longer matches the workspace boundary")
    if Path(str(record.get("workspace", ""))).expanduser().resolve() != workspace or not workspace.is_dir():
        errors.append("receipt workspace is missing or moved")
    installed = skill_path(skill_name)
    if record.get("skill_sha256") != sha256_file(installed):
        errors.append("bundled Skill changed after the receipt was created")
    if record.get("status") == "success":
        for output in record_outputs(record):
            output_path = Path(str(output.get("path", ""))).expanduser().resolve()
            if not output_path.is_file() or output.get("sha256") != sha256_file(output_path):
                errors.append(f"completed output changed: {output_path}")
            evidence_raw = output.get("evidence_path")
            if evidence_raw:
                evidence_path = Path(str(evidence_raw)).expanduser().resolve()
                if not evidence_path.is_file() or output.get("evidence_sha256") != sha256_file(evidence_path):
                    errors.append(f"completed output evidence changed: {evidence_path}")
    if errors:
        raise SystemExit("reusable Skill receipt is stale: " + "; ".join(errors))


def start(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.expanduser().resolve()
    run = load_run_for_boundary(run_dir, args.boundary)
    skill_name = (
        visual_skill_for_start(run, args.skill)
        if args.boundary == "visual"
        else BOUNDARY_SKILLS[args.boundary]
    )
    invocation_id = invocation_id_for(args.boundary, skill_name)
    path = receipt_path(run_dir, args.boundary, invocation_id)
    if path.exists():
        existing = load_json(path)
        if existing.get("status") in {"started", "success"}:
            validate_reusable_record(existing, run_dir, args.boundary, run)
            print(json.dumps({"reused": True, **existing}, ensure_ascii=False))
            return 0
        raise SystemExit(
            f"Skill receipt is failed; Leader must use reset before start: {path}\n"
            f"{command_prefix()} --boundary {args.boundary} reset \"{run_dir}\" "
            f"--invocation-id {invocation_id} --actor wechat-leader"
        )

    source, source_hash, workspace, working_input = expected_identity(
        args.boundary, run_dir, run, skill_name
    )
    if not source.is_file():
        raise SystemExit(f"Skill input is missing: {source}")
    if workspace.exists():
        raise SystemExit(f"Skill workspace already exists without a reusable receipt: {workspace}")
    workspace.mkdir(parents=True)
    shutil.copy2(source, working_input)
    working_input.chmod(0o600)
    installed = skill_path(skill_name)
    record: dict[str, Any] = {
        "schema_version": 1,
        "protocol_version": PROTOCOL_VERSION,
        "run_id": run["run_id"],
        "boundary": args.boundary,
        "invocation_id": invocation_id,
        "attempt": 1,
        "reset_count": 0,
        "skill_name": skill_name,
        "skill_identifier": f"wechat-pipeline:{skill_name}",
        "skill_path": str(installed),
        "skill_sha256": sha256_file(installed),
        "invocation_method": "native-skill",
        "input_path": str(source),
        "input_sha256": source_hash,
        "workspace": str(workspace),
        "working_input_path": str(working_input),
        "working_input_initial_sha256": sha256_file(working_input),
        "started_at": now_iso(),
        "completed_at": None,
        "status": "started",
        "returned_outputs": [],
        "error_summary": "",
    }
    if args.boundary == "formatter":
        record["expected_output_path"] = str(formatter_paths(run_dir)[4])
        record["output_path"] = str(formatter_paths(run_dir)[4])
        record["output_sha256"] = None
    elif args.boundary == "layout":
        record["invocation_args"] = invocation_args(working_input, workspace)
    else:
        record["skill_options"] = skill_options(str(run.get("mode")), skill_name)
        record["confirmation_authorization"] = (
            ARTICLE_ILLUSTRATOR_CONFIRMATION_AUTHORIZATION
            if skill_name == "baoyu-article-illustrator"
            else ""
        )
        record["image_backend_capabilities"] = cached_backends(run_dir, run)
    write_json(path, record)
    print(json.dumps({"reused": False, **record}, ensure_ascii=False))
    return 0


def parse_outputs(values: list[str], boundary: str, run_dir: Path, invocation_id: str, record: dict) -> list[tuple[str, Path]]:
    parsed: list[tuple[str, Path]] = []
    for value in values:
        role, separator, raw_path = value.partition("=")
        if not separator or not role or not raw_path or not Path(raw_path).expanduser().is_absolute():
            contract_error(f"invalid --output value: {value!r}", boundary, run_dir, invocation_id, record)
        parsed.append((role, Path(raw_path).expanduser().resolve()))
    if not parsed:
        contract_error("complete requires at least one --output", boundary, run_dir, invocation_id, record)
    return parsed


def validate_visual_evidence(
    values: list[str],
    outputs: list[dict[str, str]],
    workspace: Path,
    started: datetime,
) -> None:
    """Bind every returned image to one proof-of-execution evidence file."""
    image_outputs = [value for value in outputs if value["role"] in IMAGE_ROLES]
    if not image_outputs and not values:
        return
    if not values:
        raise ValueError(
            "visual complete requires one --evidence /abs/evidence.json per returned image; "
            "a receipt without backend proof-of-execution is not accepted"
        )
    by_output: dict[Path, Path] = {}
    for raw in values:
        if not raw or not Path(raw).expanduser().is_absolute():
            raise ValueError(f"invalid --evidence value (absolute path required): {raw!r}")
        evidence_path = Path(raw).expanduser().resolve()
        try:
            payload = load_json(evidence_path)
        except (OSError, ValueError) as err:
            raise ValueError(f"unable to read evidence file {evidence_path}: {err}") from err
        claimed = Path(str(payload.get("output_path", ""))).expanduser().resolve()
        if claimed in by_output:
            raise ValueError(f"duplicate evidence for image: {claimed}")
        by_output[claimed] = evidence_path
    returned = {Path(value["path"]) for value in image_outputs}
    unmatched = sorted(set(by_output) - returned)
    if unmatched:
        raise ValueError(f"evidence does not match any returned image: {unmatched[0]}")
    for value in image_outputs:
        image_path = Path(value["path"])
        evidence_path = by_output.get(image_path)
        if evidence_path is None:
            raise ValueError(
                f"missing --evidence for generated image {image_path}; "
                "generate it with a real backend and record the proof"
            )
        errors = validate_image_evidence(evidence_path, image_path, workspace, started)
        if errors:
            raise ValueError("; ".join(errors))
        value["evidence_path"] = str(evidence_path)
        value["evidence_sha256"] = sha256_file(evidence_path)


def validate_role_cardinality(skill_name: str, parsed: list[tuple[str, Path]]) -> None:
    roles = [role for role, _ in parsed]
    if any(role not in ALLOWED_ROLES.get(skill_name, set()) for role in roles):
        raise ValueError(f"unsupported role(s): {sorted(set(roles) - ALLOWED_ROLES.get(skill_name, set()))}")
    if skill_name == "baoyu-format-markdown" and roles != ["formatted"]:
        raise ValueError("Formatter must return exactly one formatted output")
    if skill_name == "gzh-design" and roles != ["html"]:
        raise ValueError("gzh-design must return exactly one html output")
    if skill_name == "baoyu-cover-image" and roles != ["cover"]:
        raise ValueError("baoyu-cover-image must return exactly one cover output")
    if skill_name == "baoyu-xhs-images" and (not roles or any(role != "card" for role in roles)):
        raise ValueError("baoyu-xhs-images must return one or more card outputs")
    if skill_name == "baoyu-article-illustrator":
        if roles.count("article") != 1 or roles.count("body") < 1:
            raise ValueError(
                "baoyu-article-illustrator must return exactly one article and at least one body output"
            )


def record_outputs(record: dict) -> list[dict]:
    values = record.get("returned_outputs")
    if isinstance(values, list):
        return [value for value in values if isinstance(value, dict)]
    legacy = record.get("returned_output")
    return [legacy] if isinstance(legacy, dict) else []


def validate_preservation(source: Path, candidate: Path, *, html: bool, label: str) -> None:
    try:
        report = preservation_report(
            source.read_text(encoding="utf-8", errors="replace"),
            candidate.read_text(encoding="utf-8", errors="replace"),
            candidate_is_html=html,
        )
    except OSError as err:
        raise ValueError(f"unable to compare source text: {err}") from err
    missing = report["missing_source_segments"]
    if missing:
        raise ValueError(missing_summary(missing, label=label))


def validate_layout_output(run_dir: Path, record: dict, output: Path) -> None:
    designer = load_json(run_dir / ".pipeline" / "manifest.json")
    images = [value for value in designer.get("images", []) if isinstance(value, dict)]
    expected_body_images = [
        Path(str(value.get("output_path", "")))
        for value in images
        if value.get("kind") != "cover"
    ]
    result = validate_native_output(
        output,
        run_dir / ".pipeline" / "input.md",
        Path(str(record.get("input_path", ""))).expanduser().resolve(),
        expected_body_images,
    )
    issues = list(result.get("errors", [])) + list(result.get("warnings", []))
    if issues:
        raise ValueError("gzh-design output needs native self-correction: " + "; ".join(issues))


def complete(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.expanduser().resolve()
    run = load_run_for_boundary(run_dir, args.boundary)
    invocation_id = validate_invocation_id(args.invocation_id)
    path = receipt_path(run_dir, args.boundary, invocation_id)
    try:
        record = load_json(path)
    except (OSError, ValueError) as err:
        contract_error(f"unable to read Skill receipt: {err}", args.boundary, run_dir, invocation_id)
        raise AssertionError("unreachable")
    if record.get("invocation_id") != invocation_id or record.get("boundary") != args.boundary:
        contract_error("receipt invocation/boundary mismatch", args.boundary, run_dir, invocation_id, record)
    if record.get("status") == "success":
        validate_reusable_record(record, run_dir, args.boundary, run)
        print(json.dumps({"reused": True, **record}, ensure_ascii=False))
        return 0
    if record.get("status") != "started":
        contract_error(
            f"Skill run is not completable from {record.get('status')!r}",
            args.boundary,
            run_dir,
            invocation_id,
            record,
        )
    parsed = parse_outputs(args.output, args.boundary, run_dir, invocation_id, record)
    skill_name = str(record.get("skill_name", ""))
    try:
        validate_role_cardinality(skill_name, parsed)
        workspace = Path(str(record.get("workspace", ""))).expanduser().resolve()
        started = datetime.fromisoformat(str(record.get("started_at", "")).replace("Z", "+00:00"))
        if started.tzinfo is None:
            raise ValueError("receipt started_at must include a timezone")
        outputs: list[dict[str, str]] = []
        seen_paths: set[Path] = set()
        for role, output in parsed:
            if not inside(output, workspace):
                raise ValueError(f"output must stay inside recorded workspace: {output}")
            if output in seen_paths:
                raise ValueError(f"duplicate output path: {output}")
            if not output.is_file() or output.stat().st_size == 0:
                raise ValueError(f"output is missing or empty: {output}")
            if output.stat().st_mtime < started.timestamp():
                raise ValueError(f"output predates Skill start: {output}")
            contract_errors = validate_output_contract(str(run.get("mode")), skill_name, role, output)
            if contract_errors:
                raise ValueError("; ".join(contract_errors))
            if skill_name == "baoyu-format-markdown":
                if output != formatter_paths(run_dir)[4]:
                    raise ValueError(f"Formatter output must be {formatter_paths(run_dir)[4]}")
                validate_preservation(run_dir / ".pipeline" / "input.md", output, html=False, label="Formatter output")
                h1_count = sum(1 for line in output.read_text(encoding="utf-8").splitlines() if re.match(r"^#\s+\S", line))
                if h1_count != 1:
                    raise ValueError("Formatter output must contain exactly one level-1 heading")
            elif skill_name == "baoyu-article-illustrator" and role == "article":
                validate_preservation(run_dir / "content.md", output, html=False, label="Illustrator article")
            elif skill_name == "gzh-design":
                validate_layout_output(run_dir, record, output)
            seen_paths.add(output)
            outputs.append({"role": role, "path": str(output), "sha256": sha256_file(output)})
        if args.boundary == "visual":
            validate_visual_evidence(args.evidence, outputs, workspace, started)
    except (OSError, ValueError) as err:
        contract_error(str(err), args.boundary, run_dir, invocation_id, record)
        raise AssertionError("unreachable")

    working_input = Path(str(record.get("working_input_path", ""))).expanduser().resolve()
    returned = {Path(value["path"]).resolve() for value in outputs}
    if working_input.is_file() and working_input not in returned:
        if sha256_file(working_input) != record.get("working_input_initial_sha256"):
            contract_error(
                "Skill changed its working input without returning it as a final result",
                args.boundary,
                run_dir,
                invocation_id,
                record,
            )
        working_input.unlink()
    record["returned_outputs"] = outputs
    if args.boundary == "formatter":
        record["output_path"] = outputs[0]["path"]
        record["output_sha256"] = outputs[0]["sha256"]
    elif args.boundary == "layout":
        record["returned_output"] = outputs[0]
    record["completed_at"] = now_iso()
    record["status"] = "success"
    write_json(path, record)
    print(json.dumps({"reused": False, **record}, ensure_ascii=False))
    return 0


def fail(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.expanduser().resolve()
    load_run_for_boundary(run_dir, args.boundary)
    invocation_id = validate_invocation_id(args.invocation_id)
    path = receipt_path(run_dir, args.boundary, invocation_id)
    record = load_json(path)
    if record.get("status") != "started":
        raise SystemExit(f"Skill run is not fail-able from {record.get('status')!r}")
    if not args.error.strip():
        raise SystemExit("--error must explain why the native Skill did not complete")
    record["completed_at"] = now_iso()
    record["status"] = "failed"
    record["error_summary"] = args.error.strip()
    write_json(path, record)
    print(json.dumps(record, ensure_ascii=False))
    return 0


def reset(args: argparse.Namespace) -> int:
    if args.actor != "wechat-leader":
        raise SystemExit("Skill reset may only be performed by actor wechat-leader")
    run_dir = args.run_dir.expanduser().resolve()
    run = load_run_for_boundary(run_dir, args.boundary)
    invocation_id = validate_invocation_id(args.invocation_id)
    path = receipt_path(run_dir, args.boundary, invocation_id)
    previous = load_json(path)
    if previous.get("status") != "failed":
        raise SystemExit(f"Skill reset requires a failed receipt, got {previous.get('status')!r}")
    skill_name = str(previous.get("skill_name", ""))
    source, source_hash, workspace, working_input = expected_identity(
        args.boundary, run_dir, run, skill_name
    )
    recorded_workspace = Path(str(previous.get("workspace", ""))).expanduser().resolve()
    if workspace != recorded_workspace or not inside(workspace, run_dir) or workspace == run_dir:
        raise SystemExit("refusing to reset an unexpected Skill workspace")
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    shutil.copy2(source, working_input)
    working_input.chmod(0o600)
    record = {
        **previous,
        "input_path": str(source),
        "input_sha256": source_hash,
        "workspace": str(workspace),
        "working_input_path": str(working_input),
        "working_input_initial_sha256": sha256_file(working_input),
        "reset_count": int(previous.get("reset_count", 0)) + 1,
        "started_at": now_iso(),
        "completed_at": None,
        "status": "started",
        "returned_outputs": [],
        "returned_output": None if args.boundary == "layout" else previous.get("returned_output"),
        "output_sha256": None if args.boundary == "formatter" else previous.get("output_sha256"),
        "error_summary": "",
    }
    if args.boundary == "visual":
        record["image_backend_capabilities"] = cached_backends(run_dir, run)
    write_json(path, record)
    append_event(
        run_dir,
        run,
        "skill.reset",
        args.actor,
        {
            "boundary": args.boundary,
            "invocation_id": invocation_id,
            "reset_count": record["reset_count"],
            "previous_error": previous.get("error_summary", ""),
        },
    )
    print(json.dumps(record, ensure_ascii=False))
    return 0


def amend_role(args: argparse.Namespace) -> int:
    if args.boundary != "visual":
        raise SystemExit("amend-role is only available for the visual boundary")
    if args.actor != "wechat-leader":
        raise SystemExit("role amendments may only be performed by actor wechat-leader")
    run_dir = args.run_dir.expanduser().resolve()
    run = load_run_for_boundary(run_dir, args.boundary)
    invocation_id = validate_invocation_id(args.invocation_id)
    path = receipt_path(run_dir, args.boundary, invocation_id)
    record = load_json(path)
    if record.get("status") != "success":
        raise SystemExit("amend-role requires a successful visual receipt")
    outputs = record_outputs(record)
    candidates = [value for value in outputs if value.get("role") == args.from_role]
    if args.path:
        selected = args.path.expanduser().resolve()
        candidates = [
            value for value in candidates
            if Path(str(value.get("path", ""))).expanduser().resolve() == selected
        ]
    elif args.to_role == "article":
        markdown_candidates = [
            value for value in candidates
            if Path(str(value.get("path", ""))).suffix.lower() in {".md", ".markdown"}
        ]
        if len(markdown_candidates) == 1:
            candidates = markdown_candidates
    if len(candidates) != 1:
        raise SystemExit("amend-role must identify exactly one returned output; add --path when ambiguous")
    if args.to_role not in ALLOWED_ROLES.get(str(record.get("skill_name")), set()):
        raise SystemExit(f"role {args.to_role!r} is not allowed for {record.get('skill_name')}")
    candidate = candidates[0]
    candidate_path = Path(str(candidate.get("path", ""))).expanduser().resolve()
    previous_role = str(candidate.get("role", ""))
    candidate["role"] = args.to_role
    candidate["sha256"] = sha256_file(candidate_path)
    try:
        validate_role_cardinality(
            str(record.get("skill_name", "")),
            [(str(value.get("role", "")), Path(str(value.get("path", "")))) for value in outputs],
        )
        if args.to_role == "article":
            validate_preservation(run_dir / "content.md", candidate_path, html=False, label="Illustrator article")
    except ValueError as err:
        candidate["role"] = previous_role
        raise SystemExit(f"role amendment rejected: {err}") from err
    record["returned_outputs"] = outputs
    record.setdefault("amendments", []).append({
        "actor": args.actor,
        "amended_at": now_iso(),
        "path": str(candidate_path),
        "from": previous_role,
        "to": args.to_role,
        "sha256": candidate["sha256"],
    })
    write_json(path, record)
    append_event(
        run_dir,
        run,
        "skill.role_amended",
        args.actor,
        {"invocation_id": invocation_id, "path": str(candidate_path), "from": previous_role, "to": args.to_role},
    )
    print(json.dumps(record, ensure_ascii=False))
    return 0


def build_manifest(args: argparse.Namespace) -> int:
    if args.boundary != "visual":
        raise SystemExit("build-manifest is only available for the visual boundary")
    run_dir = args.run_dir.expanduser().resolve()
    run = load_run_for_boundary(run_dir, "visual")
    mode = str(run.get("mode"))
    receipts: list[dict] = []
    for name in EXPECTED_SKILLS[mode]:
        path = visual_receipt_path(run_dir, name)
        if not path.is_file():
            raise SystemExit(f"native Skill was not started: {name}")
        receipt = load_json(path)
        validate_reusable_record(receipt, run_dir, "visual", run)
        if receipt.get("status") != "success":
            raise SystemExit(f"native Skill did not complete successfully: {name}")
        validate_role_cardinality(
            name,
            [(str(value.get("role", "")), Path(str(value.get("path", "")))) for value in record_outputs(receipt)],
        )
        receipts.append(receipt)

    images: list[dict[str, Any]] = []
    layout_input: dict[str, str] | None = None
    counters = {"card": 0, "cover": 0, "body": 0}
    for receipt in receipts:
        for output in record_outputs(receipt):
            role = str(output.get("role", ""))
            if role == "article":
                layout_input = {"path": output["path"], "sha256": output["sha256"]}
                continue
            counters[role] += 1
            entry: dict[str, Any] = {
                "id": f"{role}-{counters[role]}",
                "kind": role,
                "source_skill_run_id": receipt["invocation_id"],
                "output_path": output["path"],
                "output_sha256": output["sha256"],
            }
            if output.get("evidence_path"):
                entry["evidence_path"] = output["evidence_path"]
                entry["evidence_sha256"] = output["evidence_sha256"]
            images.append(entry)
    input_path = run_dir / ".pipeline" / "input.md"
    content_path = run_dir / "content.md"
    manifest: dict[str, Any] = {
        "schema_version": 5,
        "protocol_version": PROTOCOL_VERSION,
        "run_id": run["run_id"],
        "mode": mode,
        "canonical_output_dir": str(run_dir),
        "source": {
            "original_path": str(input_path),
            "original_sha256": sha256_file(input_path),
            "publisher_text_sha256": sha256_file(input_path),
            "content_path": str(content_path),
            "content_sha256": sha256_file(content_path),
        },
        "skill_runs": receipts,
        "images": images,
    }
    if layout_input:
        manifest["layout_input"] = layout_input
    write_json(run_dir / ".pipeline" / "manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Complete contract:\n"
            "  formatter roles: formatted\n"
            "  visual roles: card | cover | article + body\n"
            "  layout roles: html\n"
            "All outputs use role=/absolute/path and must stay inside the recorded workspace.\n"
            "Every returned image (card/cover/body) also requires one --evidence JSON proving\n"
            "the real backend render; receipts without proof-of-execution are rejected.\n"
            f"Use: {command_prefix()} --boundary <formatter|visual|layout> <command> ..."
        ),
    )
    parser.add_argument("--boundary", choices=tuple(BOUNDARY_WORKERS), required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)

    started = subparsers.add_parser("start", help="create or reuse one started receipt")
    started.add_argument("run_dir", type=Path)
    started.add_argument("--skill", choices=tuple(EXPECTED_SKILLS["newspic"] + EXPECTED_SKILLS["news"]))
    started.set_defaults(func=start)

    completed = subparsers.add_parser("complete", help="register final role=/absolute/path outputs")
    completed.add_argument("run_dir", type=Path)
    completed.add_argument("--invocation-id", required=True)
    completed.add_argument("--output", action="append", default=[], metavar="ROLE=/ABSOLUTE/PATH")
    completed.add_argument("--evidence", action="append", default=[], metavar="/ABSOLUTE/EVIDENCE.json")
    completed.set_defaults(func=complete)

    failed = subparsers.add_parser("fail", help="record a genuine native-Skill failure")
    failed.add_argument("run_dir", type=Path)
    failed.add_argument("--invocation-id", required=True)
    failed.add_argument("--error", required=True)
    failed.set_defaults(func=fail)

    reset_parser = subparsers.add_parser("reset", help="Leader-only reset of a failed receipt")
    reset_parser.add_argument("run_dir", type=Path)
    reset_parser.add_argument("--invocation-id", required=True)
    reset_parser.add_argument("--actor", required=True)
    reset_parser.set_defaults(func=reset)

    amend = subparsers.add_parser("amend-role", help="Leader-only audited correction of a returned role")
    amend.add_argument("run_dir", type=Path)
    amend.add_argument("--invocation-id", required=True)
    amend.add_argument("--from", dest="from_role", required=True)
    amend.add_argument("--to", dest="to_role", required=True)
    amend.add_argument("--path", type=Path)
    amend.add_argument("--actor", required=True)
    amend.set_defaults(func=amend_role)

    manifest = subparsers.add_parser("build-manifest", help="materialize the deterministic artwork manifest")
    manifest.add_argument("run_dir", type=Path)
    manifest.set_defaults(func=build_manifest)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
