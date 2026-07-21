#!/usr/bin/env python3
"""Seal one formatted Markdown artifact while proving source-text preservation."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

from protocol_version import PROTOCOL_VERSION
from shared.hashing import sha256_file
from shared.jsonio import inside, write_json
from shared.markdown_meta import markdown_body
from shared.text_preservation import missing_summary as preservation_missing_summary
from shared.text_preservation import preservation_report
from skill_run import formatter_paths

SKILL_NAME = "baoyu-format-markdown"
SKILL_IDENTIFIER = f"wechat-pipeline:{SKILL_NAME}"
SKILL_PATH = (PLUGIN_ROOT / "skills" / SKILL_NAME / "SKILL.md").resolve()

def body_lines(value: str) -> list[str]:
    return markdown_body(value).splitlines()


def inspect_markdown(value: str) -> dict:
    lines = body_lines(value)
    h1 = [line[2:].strip() for line in lines if re.match(r"^#\s+\S", line)]
    h2 = [line[3:].strip() for line in lines if re.match(r"^##\s+\S", line)]
    h3 = [line[4:].strip() for line in lines if re.match(r"^###\s+\S", line)]
    return {"h1": h1, "h2": h2, "h3": h3}


def missing_summary(missing: list[dict]) -> str:
    return preservation_missing_summary(missing, label="Formatter output")


def validate_candidate(run_dir: Path, source: Path) -> tuple[list[str], dict[str, Any]]:
    errors: list[str] = []
    pipeline = run_dir / ".pipeline"
    original = pipeline / "input.md"
    source_text = source.read_text(encoding="utf-8")
    headings = inspect_markdown(source_text)
    if len(headings["h1"]) != 1:
        errors.append("formatter output must contain exactly one level-1 heading")
    report = preservation_report(
        original.read_text(encoding="utf-8", errors="replace"),
        source_text,
    )
    missing = report["missing_source_segments"]
    if missing:
        errors.append(missing_summary(missing))
    report.update({
        "source_path": str(original),
        "candidate_path": str(source),
        "heading_counts": {
            "h1": len(headings["h1"]),
            "h2": len(headings["h2"]),
            "h3": len(headings["h3"]),
        },
        "errors": errors,
    })
    return errors, report


def validate_formatter_skill_run(run_dir: Path) -> tuple[list[str], dict | None]:
    errors: list[str] = []
    pipeline = run_dir / ".pipeline"
    receipt_path, input_path, _, _, output_path = formatter_paths(run_dir)
    try:
        run = json.loads((pipeline / "run.json").read_text(encoding="utf-8"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        return [f"unable to read Formatter Skill receipt: {err}"], None
    if receipt.get("protocol_version") != PROTOCOL_VERSION:
        errors.append(f"Formatter Skill protocol_version must be {PROTOCOL_VERSION}")
    if receipt.get("run_id") != run.get("run_id"):
        errors.append("Formatter Skill run_id must match run.json")
    if receipt.get("skill_name") != "baoyu-format-markdown":
        errors.append("Formatter Skill name must be baoyu-format-markdown")
    if receipt.get("skill_identifier") != SKILL_IDENTIFIER:
        errors.append(f"Formatter Skill identifier must be {SKILL_IDENTIFIER}")
    if Path(str(receipt.get("skill_path", ""))).expanduser().resolve() != SKILL_PATH:
        errors.append("Formatter Skill path must identify the bundled SKILL.md")
    elif receipt.get("skill_sha256") != sha256_file(SKILL_PATH):
        errors.append("Formatter Skill hash does not match the bundled SKILL.md")
    if receipt.get("invocation_method") != "native-skill":
        errors.append("Formatter invocation_method must be native-skill")
    if receipt.get("status") != "success":
        errors.append("Formatter Skill did not complete successfully")
    if receipt.get("input_path") != str(input_path) or receipt.get("input_sha256") != sha256_file(input_path):
        errors.append("Formatter Skill input must bind sealed .pipeline/input.md")
    if receipt.get("output_path") != str(output_path):
        errors.append("Formatter Skill output must bind baoyu-format-markdown/article-formatted.md")
    elif not output_path.is_file() or receipt.get("output_sha256") != sha256_file(output_path):
        errors.append("Formatter Skill output hash does not match article-formatted.md")
    try:
        started = datetime.fromisoformat(str(receipt.get("started_at", "")).replace("Z", "+00:00"))
        completed = datetime.fromisoformat(str(receipt.get("completed_at", "")).replace("Z", "+00:00"))
        if started.tzinfo is None or completed.tzinfo is None or completed < started:
            errors.append("Formatter Skill timestamps must be ordered and timezone-aware")
    except ValueError:
        errors.append("Formatter Skill timestamps must be valid ISO-8601 values")
    return errors, receipt


def validate_content_artifact(run_dir: Path) -> tuple[list[str], dict | None]:
    errors: list[str] = []
    pipeline = run_dir / ".pipeline"
    try:
        run = json.loads((pipeline / "run.json").read_text(encoding="utf-8"))
        receipt = json.loads((pipeline / "format-result.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        return [f"unable to read formatted content evidence: {err}"], None
    content = run_dir / "content.md"
    original = pipeline / "input.md"
    formatter_receipt_path, _, _, _, formatter_output = formatter_paths(run_dir)
    if not content.is_file():
        errors.append(f"formatted content not found: {content}")
        return errors, receipt
    if receipt.get("protocol_version") != PROTOCOL_VERSION:
        errors.append(f"format receipt protocol_version must be {PROTOCOL_VERSION}")
    if receipt.get("run_id") != run.get("run_id"):
        errors.append("format receipt run_id does not match run.json")
    if receipt.get("source_sha256") != sha256_file(original):
        errors.append("format receipt source hash does not match sealed input")
    skill_errors, skill_receipt = validate_formatter_skill_run(run_dir)
    errors.extend(skill_errors)
    if receipt.get("formatter_status") != "executed":
        errors.append("format receipt must prove that Formatter executed")
    if receipt.get("formatter_skill_identifier") != SKILL_IDENTIFIER:
        errors.append("format receipt must identify the native Formatter Skill")
    if receipt.get("formatter_skill_run_path") != str(formatter_receipt_path):
        errors.append("format receipt must bind formatter-skill-run.json")
    elif formatter_receipt_path.is_file() and receipt.get("formatter_skill_run_sha256") != sha256_file(formatter_receipt_path):
        errors.append("format receipt does not match formatter-skill-run.json")
    if skill_receipt and receipt.get("formatter_output_sha256") != skill_receipt.get("output_sha256"):
        errors.append("format receipt output hash does not match Formatter Skill receipt")
    if receipt.get("formatter_output_path") != str(formatter_output):
        errors.append("format receipt must bind baoyu-format-markdown/article-formatted.md")
    elif not formatter_output.is_file():
        errors.append(f"formatter output is missing: {formatter_output}")
    elif receipt.get("formatter_output_sha256") != sha256_file(formatter_output):
        errors.append("format receipt does not match article-formatted.md")
    if receipt.get("content_path") != str(content) or receipt.get("content_sha256") != sha256_file(content):
        errors.append("format receipt does not bind the canonical content.md")
    value = content.read_text(encoding="utf-8")
    headings = inspect_markdown(value)
    if len(headings["h1"]) != 1:
        errors.append("formatted content must contain exactly one level-1 heading")
    preservation = preservation_report(
        original.read_text(encoding="utf-8", errors="replace"),
        value,
    )
    missing = preservation["missing_source_segments"]
    if missing:
        errors.append("formatted content is invalid: " + missing_summary(missing))
    if receipt.get("missing_source_segments") != []:
        errors.append("format receipt must record zero missing source segments")
    return errors, receipt


def seal(run_dir: Path, source: Path, *, check_only: bool = False) -> int:
    pipeline = run_dir / ".pipeline"
    run_path = pipeline / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    if run.get("protocol_version") != PROTOCOL_VERSION:
        raise SystemExit(f"run protocol_version must be {PROTOCOL_VERSION}")
    if Path(str(run.get("canonical_output_dir", ""))).expanduser().resolve() != run_dir:
        raise SystemExit("run canonical_output_dir mismatch")
    if not source.is_file() or not inside(source, run_dir):
        raise SystemExit("formatter output must be an existing file inside canonical_output_dir")
    _, _, _, _, expected_source = formatter_paths(run_dir)
    if source != expected_source:
        raise SystemExit(
            "formatter output must be <run-dir>/baoyu-format-markdown/article-formatted.md; "
            "sealed input cannot bypass the native Formatter Skill"
        )

    target = run_dir / "content.md"
    receipt_path = pipeline / "format-result.json"
    if check_only:
        candidate_errors, candidate_report = validate_candidate(run_dir, source)
        print(json.dumps(candidate_report, ensure_ascii=False, indent=2))
        return 1 if candidate_errors else 0
    if target.exists() or receipt_path.exists():
        errors, receipt = validate_content_artifact(run_dir)
        if errors:
            raise SystemExit("existing formatted content is invalid: " + "; ".join(errors))
        print(json.dumps({"reused": True, **(receipt or {})}, ensure_ascii=False))
        return 0

    candidate_errors, candidate_report = validate_candidate(run_dir, source)
    source_text = source.read_text(encoding="utf-8")
    headings = inspect_markdown(source_text)
    original = pipeline / "input.md"
    if candidate_errors:
        raise SystemExit("; ".join(candidate_errors))

    formatter_errors, formatter_receipt = validate_formatter_skill_run(run_dir)
    if formatter_errors:
        raise SystemExit("; ".join(formatter_errors))

    source.chmod(0o400)
    formatter_receipt_path, _, _, _, _ = formatter_paths(run_dir)
    formatter_receipt_path.chmod(0o400)
    shutil.copyfile(source, target)
    target.chmod(0o400)
    receipt = {
        "schema_version": 1,
        "protocol_version": PROTOCOL_VERSION,
        "run_id": run.get("run_id"),
        "source_path": str(original),
        "source_sha256": sha256_file(original),
        "formatter_status": "executed",
        "formatter_skill_identifier": SKILL_IDENTIFIER,
        "formatter_skill_run_path": str(formatter_receipt_path),
        "formatter_skill_run_sha256": sha256_file(formatter_receipt_path),
        "formatter_output_path": str(source),
        "formatter_output_sha256": (formatter_receipt or {}).get("output_sha256"),
        "content_path": str(target),
        "content_sha256": sha256_file(target),
        "title": headings["h1"][0],
        "heading_counts": {"h1": 1, "h2": len(headings["h2"]), "h3": len(headings["h3"])},
        "source_segment_count": candidate_report["source_segment_count"],
        "missing_source_segments": [],
        "created_at": datetime.now().astimezone().isoformat(),
    }
    write_json(receipt_path, receipt, mode=0o400)
    print(json.dumps({"reused": False, **receipt}, ensure_ascii=False))
    return 0


def validate_command(run_dir: Path) -> int:
    errors, receipt = validate_content_artifact(run_dir)
    if errors:
        print("formatted content validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(json.dumps({"ok": True, "content_sha256": (receipt or {}).get("content_sha256")}, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    sealed = subparsers.add_parser("seal", help="validate and seal the Formatter output")
    sealed.add_argument("run_dir", type=Path)
    sealed.add_argument("--source", type=Path, required=True)
    sealed.add_argument("--check-only", action="store_true")
    validated = subparsers.add_parser("validate", help="validate canonical content and receipt")
    validated.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    run_dir = args.run_dir.expanduser().resolve()
    if args.command == "validate":
        return validate_command(run_dir)
    return seal(run_dir, args.source.expanduser().resolve(), check_only=args.check_only)


if __name__ == "__main__":
    raise SystemExit(main())
