#!/usr/bin/env python3
"""Seal one successful natural gzh-design result into canonical layout artifacts."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

from protocol_version import PROTOCOL_VERSION
from run_context import validate_worker_stage
from shared.hashing import sha256_file
from shared.jsonio import load_json, write_json
from shared.markdown_meta import frontmatter
from validate_article_layout import GZH_ROOT, LOCK_PATH, validate as validate_layout


SKILL_PATH = (GZH_ROOT / "SKILL.md").resolve()
SKILL_IDENTIFIER = "wechat-pipeline:gzh-design"


def copy_atomic(source: Path, destination: Path) -> None:
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_bytes(source.read_bytes())
    temporary.chmod(0o600)
    os.replace(temporary, destination)


def validate_receipt(run_dir: Path, run: dict, designer: dict) -> tuple[dict, Path]:
    path = run_dir / ".pipeline" / "layout-skill-run.json"
    receipt = load_json(path)
    errors: list[str] = []
    if receipt.get("protocol_version") != PROTOCOL_VERSION or receipt.get("run_id") != run.get("run_id"):
        errors.append("layout Skill receipt identity mismatch")
    if receipt.get("status") != "success":
        errors.append("gzh-design did not complete successfully")
    if receipt.get("skill_identifier") != SKILL_IDENTIFIER:
        errors.append(f"layout Skill must be {SKILL_IDENTIFIER}")
    if Path(str(receipt.get("skill_path", ""))).expanduser().resolve() != SKILL_PATH:
        errors.append(f"layout Skill path must be the bundled Skill: {SKILL_PATH}")
    elif receipt.get("skill_sha256") != sha256_file(SKILL_PATH):
        errors.append("layout Skill receipt hash mismatch")
    layout_input = designer.get("layout_input") if isinstance(designer.get("layout_input"), dict) else {}
    input_path = Path(str(receipt.get("input_path", ""))).expanduser().resolve()
    expected_input = Path(str(layout_input.get("path", ""))).expanduser().resolve()
    if input_path != expected_input or receipt.get("input_sha256") != layout_input.get("sha256"):
        errors.append("layout Skill receipt does not bind the native illustrator layout_input")
    elif not input_path.is_file() or sha256_file(input_path) != receipt.get("input_sha256"):
        errors.append("layout Skill input changed after invocation")
    returned = receipt.get("returned_output") if isinstance(receipt.get("returned_output"), dict) else {}
    output_path = Path(str(returned.get("path", ""))).expanduser().resolve()
    workspace = Path(str(receipt.get("workspace", ""))).expanduser().resolve()
    if workspace not in output_path.parents:
        errors.append("layout Skill final output escapes its isolated workspace")
    elif not output_path.is_file() or returned.get("sha256") != sha256_file(output_path):
        errors.append("layout Skill final output is missing or changed")
    try:
        started = datetime.fromisoformat(str(receipt.get("started_at", "")).replace("Z", "+00:00"))
        completed = datetime.fromisoformat(str(receipt.get("completed_at", "")).replace("Z", "+00:00"))
        if started.tzinfo is None or completed.tzinfo is None or completed < started:
            raise ValueError
    except ValueError:
        errors.append("layout Skill receipt timestamps are invalid")
    if errors:
        raise SystemExit("layout Skill receipt validation failed: " + "; ".join(errors))
    return receipt, output_path


def build(run_dir: Path) -> dict:
    stage_errors = validate_worker_stage(run_dir, "typesetter", check_integrity=False)
    if stage_errors:
        raise SystemExit("Typesetter stage guard failed: " + "; ".join(stage_errors))
    pipeline = run_dir / ".pipeline"
    run = load_json(pipeline / "run.json")
    if run.get("mode") != "news":
        raise SystemExit("layout sealing is only valid for a news run")
    designer = load_json(pipeline / "manifest.json")
    receipt, native_output = validate_receipt(run_dir, run, designer)

    html_path = run_dir / "article-body.html"
    copy_atomic(native_output, html_path)
    original_path = pipeline / "input.md"
    content_path = run_dir / "content.md"
    format_result = load_json(pipeline / "format-result.json")
    metadata_source = frontmatter(content_path.read_text(encoding="utf-8"))
    images = [value for value in designer.get("images", []) if isinstance(value, dict)]
    covers = [Path(str(value.get("output_path", ""))).expanduser().resolve() for value in images if value.get("kind") == "cover"]
    if len(covers) != 1 or not covers[0].is_file():
        raise SystemExit("designer manifest must provide exactly one existing cover for layout sealing")
    layout_input = designer["layout_input"]
    lock = load_json(LOCK_PATH)
    layout = {
        "schema_version": 1,
        "protocol_version": PROTOCOL_VERSION,
        "run_id": run["run_id"],
        "mode": "news",
        "canonical_output_dir": str(run_dir),
        "source": {
            "markdown_path": layout_input["path"],
            "markdown_sha256": layout_input["sha256"],
            "original_path": str(original_path),
            "original_sha256": sha256_file(original_path),
        },
        "skill_run": {
            "path": str(pipeline / "layout-skill-run.json"),
            "sha256": sha256_file(pipeline / "layout-skill-run.json"),
        },
        "skill_contract": {
            "skill_name": "gzh-design",
            "skill_identifier": SKILL_IDENTIFIER,
            "skill_path": str(SKILL_PATH),
            "skill_sha256": sha256_file(SKILL_PATH),
            "tree_sha256": lock["tree_sha256"],
            "upstream_commit": lock["commit"],
            "invocation_method": "native-skill",
        },
        "decision": {
            "content_policy": "preserve-visible-text",
            "engagement_footer_policy": "no-generated-engagement-footer",
        },
        "metadata": {
            "title": format_result.get("title") or metadata_source.get("title") or "",
            "author": metadata_source.get("author", ""),
            "summary": metadata_source.get("summary") or metadata_source.get("description") or "",
            "cover_path": str(covers[0]),
        },
        "output": {
            "html_path": str(html_path),
            "html_sha256": sha256_file(html_path),
            "generated_at": receipt["completed_at"],
            "native_output_path": str(native_output),
            "native_output_sha256": sha256_file(native_output),
        },
    }
    layout_path = pipeline / "layout.json"
    write_json(layout_path, layout)
    result = validate_layout(html_path, layout_path)
    result["layout_sha256"] = sha256_file(layout_path)
    write_json(pipeline / "layout-validation.json", result)
    if not result.get("ok"):
        issues = list(result.get("errors", [])) + list(result.get("warnings", []))
        raise SystemExit("layout final acceptance failed: " + "; ".join(issues))
    print(json.dumps({"ok": True, "layout": layout, "validation": result}, ensure_ascii=False))
    return layout


def validate_layout_evidence(run_dir: Path) -> tuple[list[str], dict | None]:
    """Cheaply revalidate sealed layout evidence without rerunning the full validator."""
    errors: list[str] = []
    pipeline = run_dir / ".pipeline"
    try:
        result = load_json(pipeline / "layout-validation.json")
        layout = load_json(pipeline / "layout.json")
    except (OSError, ValueError, json.JSONDecodeError) as err:
        return [f"unable to read layout evidence: {err}"], None
    html = run_dir / "article-body.html"
    if result.get("ok") is not True or result.get("errors") or result.get("warnings"):
        errors.append("layout-validation.json does not record a clean acceptance")
    layout_path = pipeline / "layout.json"
    if not html.is_file() or result.get("html_sha256") != sha256_file(html):
        errors.append("canonical HTML changed after layout validation")
    if not layout_path.is_file() or result.get("layout_sha256") != sha256_file(layout_path):
        errors.append("layout.json changed after layout validation")
    output = layout.get("output") if isinstance(layout.get("output"), dict) else {}
    if Path(str(output.get("html_path", ""))).expanduser().resolve() != html:
        errors.append("layout output no longer points to canonical HTML")
    elif output.get("html_sha256") != result.get("html_sha256"):
        errors.append("layout output hash no longer matches validation evidence")
    return errors, result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    build(args.run_dir.expanduser().resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
