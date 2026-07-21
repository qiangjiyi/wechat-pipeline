#!/usr/bin/env python3
"""Validate completed native artwork Skill runs and their returned images."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

from image_contracts import skill_options, validate_output_contract
from image_evidence import IMAGE_ROLES, validate_image_evidence
from protocol_version import PROTOCOL_VERSION
from shared.hashing import sha256_file
from shared.jsonio import inside, load_json
from shared.markdown_meta import split_frontmatter
from skill_run import ALLOWED_ROLES, EXPECTED_SKILLS


SKILLS_ROOT = PLUGIN_ROOT / "skills"
REQUIRED_TOP_LEVEL = {
    "schema_version",
    "protocol_version",
    "run_id",
    "mode",
    "canonical_output_dir",
    "source",
    "skill_runs",
    "images",
}
REQUIRED_IMAGE_FIELDS = {
    "id",
    "kind",
    "source_skill_run_id",
    "output_path",
    "output_sha256",
    "evidence_path",
    "evidence_sha256",
}


def parse_time(value: Any, label: str, errors: list[str]) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"invalid ISO-8601 timestamp for {label}: {value!r}")
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        errors.append(f"{label} must include an explicit timezone offset")
        return None
    return parsed


def validate_binding(value: Any, label: str, canonical_dir: Path, errors: list[str]) -> Path | None:
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return None
    path = Path(str(value.get("path", ""))).expanduser().resolve()
    if not inside(path, canonical_dir):
        errors.append(f"{label}.path must stay inside canonical_output_dir")
    elif not path.is_file():
        errors.append(f"{label}.path is missing: {path}")
    elif path.stat().st_size == 0:
        errors.append(f"{label}.path is empty: {path}")
    elif value.get("sha256") != sha256_file(path):
        errors.append(f"{label}.sha256 does not match its file")
    return path


def validate_source(manifest: dict, canonical_dir: Path, errors: list[str]) -> str | None:
    source = manifest.get("source")
    if not isinstance(source, dict):
        errors.append("manifest.source must be an object")
        return None
    expected = {
        "original": canonical_dir / ".pipeline" / "input.md",
        "content": canonical_dir / "content.md",
    }
    hashes: dict[str, str] = {}
    for name, expected_path in expected.items():
        actual = Path(str(source.get(f"{name}_path", ""))).expanduser().resolve()
        if actual != expected_path:
            errors.append(f"source.{name}_path must be {expected_path}")
            continue
        if not actual.is_file():
            errors.append(f"source file is missing: {actual}")
            continue
        hashes[name] = sha256_file(actual)
        if source.get(f"{name}_sha256") != hashes[name]:
            errors.append(f"source.{name}_sha256 does not match {actual}")
    if hashes.get("original") and source.get("publisher_text_sha256") != hashes["original"]:
        errors.append("publisher_text_sha256 must equal the sealed original input hash")
    return hashes.get("content")


def skill_frontmatter_name(path: Path) -> str | None:
    text = path.read_text(encoding="utf-8", errors="replace")
    metadata, _ = split_frontmatter(text)
    match = re.search(r"(?m)^name:\s*['\"]?([^'\"\s]+)", metadata)
    return match.group(1) if match else None


def validate_skill_runs(
    manifest: dict,
    canonical_dir: Path,
    content_hash: str | None,
    errors: list[str],
) -> dict[str, dict]:
    runs = manifest.get("skill_runs")
    if not isinstance(runs, list):
        errors.append("manifest.skill_runs must be a list")
        return {}
    by_id: dict[str, dict] = {}
    names: set[str] = set()
    for index, run in enumerate(runs, start=1):
        label = f"skill_runs[{index}]"
        if not isinstance(run, dict):
            errors.append(f"{label} must be an object")
            continue
        invocation_id = str(run.get("invocation_id", ""))
        name = str(run.get("skill_name", ""))
        if not invocation_id or invocation_id in by_id:
            errors.append(f"{label}.invocation_id must be non-empty and unique")
        else:
            by_id[invocation_id] = run
        names.add(name)
        if run.get("schema_version") != 1:
            errors.append(f"{label}.schema_version must be 1")
        if run.get("protocol_version") != PROTOCOL_VERSION:
            errors.append(f"{label}.protocol_version must be {PROTOCOL_VERSION}")
        if run.get("run_id") != manifest.get("run_id"):
            errors.append(f"{label}.run_id must match the manifest run")
        expected_path = (SKILLS_ROOT / name / "SKILL.md").resolve()
        actual_path = Path(str(run.get("skill_path", ""))).expanduser().resolve()
        if actual_path != expected_path:
            errors.append(f"{label}.skill_path must be the bundled {expected_path}")
        elif not actual_path.is_file():
            errors.append(f"{label}.skill_path is missing")
        else:
            if run.get("skill_sha256") != sha256_file(actual_path):
                errors.append(f"{label}.skill_sha256 does not match the bundled Skill")
            if skill_frontmatter_name(actual_path) != name:
                errors.append(f"{label}.skill_name does not match SKILL.md frontmatter")
        if run.get("skill_identifier") != f"wechat-pipeline:{name}":
            errors.append(f"{label}.skill_identifier must name the plugin Skill")
        if run.get("invocation_method") != "native-skill":
            errors.append(f"{label}.invocation_method must be native-skill")
        expected_options = skill_options(str(manifest.get("mode")), name)
        if run.get("skill_options") != expected_options:
            errors.append(f"{label}.skill_options must be {expected_options}")
        if run.get("status") != "success":
            errors.append(f"{label} did not complete successfully")
        started = parse_time(run.get("started_at"), f"{label}.started_at", errors)
        completed = parse_time(run.get("completed_at"), f"{label}.completed_at", errors)
        if started and completed and completed < started:
            errors.append(f"{label}.completed_at predates started_at")
        if Path(str(run.get("input_path", ""))).expanduser().resolve() != canonical_dir / "content.md":
            errors.append(f"{label}.input_path must bind canonical content.md")
        if content_hash and run.get("input_sha256") != content_hash:
            errors.append(f"{label}.input_sha256 must bind canonical content.md")
        workspace = Path(str(run.get("workspace", ""))).expanduser().resolve()
        expected_workspace = canonical_dir / invocation_id
        if workspace != expected_workspace:
            errors.append(f"{label}.workspace must be the direct Skill output directory {expected_workspace}")
        elif not workspace.is_dir():
            errors.append(f"{label}.workspace is missing")
        outputs = run.get("returned_outputs")
        if not isinstance(outputs, list) or not outputs:
            errors.append(f"{label}.returned_outputs must contain the Skill's final results")
            continue
        seen: set[Path] = set()
        for output_index, output in enumerate(outputs, start=1):
            output_label = f"{label}.returned_outputs[{output_index}]"
            if not isinstance(output, dict):
                errors.append(f"{output_label} must be an object")
                continue
            role = str(output.get("role", ""))
            if role not in ALLOWED_ROLES.get(name, set()):
                errors.append(f"{output_label}.role is not a final result of {name}")
            path = validate_binding(output, output_label, canonical_dir, errors)
            if role in IMAGE_ROLES:
                evidence_path = Path(str(output.get("evidence_path", ""))).expanduser().resolve()
                if not output.get("evidence_path"):
                    errors.append(f"{output_label} must carry proof-of-execution evidence")
                elif not evidence_path.is_file():
                    errors.append(f"{output_label}.evidence_path is missing: {evidence_path}")
                elif output.get("evidence_sha256") != sha256_file(evidence_path):
                    errors.append(f"{output_label}.evidence_sha256 does not match its file")
                elif path is not None and started is not None:
                    for issue in validate_image_evidence(evidence_path, path, workspace, started):
                        errors.append(f"{output_label}: {issue}")
            if path is not None:
                if not inside(path, workspace):
                    errors.append(f"{output_label}.path must stay inside its Skill workspace")
                if path in seen:
                    errors.append(f"{output_label}.path is duplicated")
                seen.add(path)
    expected = EXPECTED_SKILLS.get(str(manifest.get("mode")))
    expected_names = set(expected) if expected is not None else None
    if expected_names is None:
        errors.append(f"manifest.mode must be newspic or news, got {manifest.get('mode')!r}")
    elif names != expected_names or len(runs) != len(expected_names):
        errors.append(
            f"native Skill runs must be exactly once each: {sorted(expected_names)}, "
            f"got {len(runs)} run(s) named {sorted(names)}"
        )
    return by_id


def validate_image(
    image: Any,
    index: int,
    mode: str,
    canonical_dir: Path,
    skill_runs: dict[str, dict],
    errors: list[str],
) -> None:
    label = f"images[{index}]"
    if not isinstance(image, dict):
        errors.append(f"{label} must be an object")
        return
    missing = REQUIRED_IMAGE_FIELDS - set(image)
    if missing:
        errors.append(f"{label} missing fields: {sorted(missing)}")
        return
    output_path = Path(str(image["output_path"])).expanduser().resolve()
    if not inside(output_path, canonical_dir):
        errors.append(f"{label}.output_path must stay inside canonical_output_dir")
        return
    if not output_path.is_file():
        errors.append(f"{label}.output_path is missing: {output_path}")
        return
    if sha256_file(output_path) != image.get("output_sha256"):
        errors.append(f"{label}.output_sha256 mismatch")
    evidence_path = Path(str(image.get("evidence_path", ""))).expanduser().resolve()
    if not evidence_path.is_file():
        errors.append(f"{label}.evidence_path is missing: {evidence_path}")
    elif image.get("evidence_sha256") != sha256_file(evidence_path):
        errors.append(f"{label}.evidence_sha256 mismatch")
    invocation_id = str(image.get("source_skill_run_id", ""))
    skill_run = skill_runs.get(invocation_id)
    if skill_run is None:
        errors.append(f"{label}.source_skill_run_id does not reference a completed Skill run")
    else:
        returned = {
            (
                str(value.get("role", "")),
                str(Path(str(value.get("path", ""))).expanduser().resolve()),
                value.get("sha256"),
            )
            for value in skill_run.get("returned_outputs", [])
            if isinstance(value, dict)
        }
        expected = (str(image.get("kind", "")), str(output_path), image.get("output_sha256"))
        if expected not in returned:
            errors.append(f"{label} is not a final output returned by its native Skill run")
        else:
            matched = next(
                value
                for value in skill_run.get("returned_outputs", [])
                if isinstance(value, dict)
                and str(Path(str(value.get("path", ""))).expanduser().resolve()) == str(output_path)
            )
            receipt_evidence = str(Path(str(matched.get("evidence_path", ""))).expanduser().resolve())
            if not matched.get("evidence_path") or receipt_evidence != str(evidence_path):
                errors.append(f"{label}.evidence_path does not match its Skill run receipt")
        for contract_error in validate_output_contract(
            mode,
            str(skill_run.get("skill_name", "")),
            str(image.get("kind", "")),
            output_path,
        ):
            errors.append(f"{label}: {contract_error}")


def validate_mode_results(manifest: dict, canonical_dir: Path, errors: list[str]) -> None:
    images = [value for value in manifest.get("images", []) if isinstance(value, dict)]
    kinds = [str(value.get("kind", "")) for value in images]
    if manifest.get("mode") == "newspic":
        if not images or any(kind != "card" for kind in kinds):
            errors.append("newspic requires final card results returned by baoyu-xhs-images")
        if "layout_input" in manifest:
            errors.append("newspic manifest must not declare layout_input")
    elif manifest.get("mode") == "news":
        if kinds.count("cover") != 1:
            errors.append("news requires one selected cover returned by baoyu-cover-image")
        if kinds.count("body") < 1 or any(kind not in {"cover", "body"} for kind in kinds):
            errors.append("news requires at least one body image returned by baoyu-article-illustrator")
        layout_path = validate_binding(manifest.get("layout_input"), "layout_input", canonical_dir, errors)
        illustrator = next(
            (run for run in manifest.get("skill_runs", []) if isinstance(run, dict) and run.get("skill_name") == "baoyu-article-illustrator"),
            None,
        )
        if layout_path is not None and illustrator is not None:
            returned_articles = {
                (str(Path(str(value.get("path", ""))).expanduser().resolve()), value.get("sha256"))
                for value in illustrator.get("returned_outputs", [])
                if isinstance(value, dict) and value.get("role") == "article"
            }
            if (str(layout_path), manifest.get("layout_input", {}).get("sha256")) not in returned_articles:
                errors.append("layout_input must be the final illustrated article returned by baoyu-article-illustrator")


def validate(manifest_path: Path) -> list[str]:
    errors: list[str] = []
    manifest = load_json(manifest_path)
    missing = REQUIRED_TOP_LEVEL - set(manifest)
    if missing:
        errors.append(f"manifest missing top-level fields: {sorted(missing)}")
    if manifest.get("schema_version") != 5:
        errors.append("manifest.schema_version must be 5")
    if manifest.get("protocol_version") != PROTOCOL_VERSION:
        errors.append(f"protocol_version must be {PROTOCOL_VERSION}")
    canonical_dir = Path(str(manifest.get("canonical_output_dir", ""))).expanduser().resolve()
    if manifest_path != canonical_dir / ".pipeline" / "manifest.json":
        errors.append("manifest must live at <canonical_output_dir>/.pipeline/manifest.json")
    run_path = canonical_dir / ".pipeline" / "run.json"
    if not run_path.is_file():
        errors.append(f"run context not found: {run_path}")
    else:
        run = load_json(run_path)
        if run.get("run_id") != manifest.get("run_id"):
            errors.append("run_id does not match run.json")
        if run.get("mode") != manifest.get("mode"):
            errors.append("mode does not match run.json")
        if Path(str(run.get("canonical_output_dir", ""))).expanduser().resolve() != canonical_dir:
            errors.append("canonical_output_dir does not match run.json")
    content_hash = validate_source(manifest, canonical_dir, errors)
    skill_runs = validate_skill_runs(manifest, canonical_dir, content_hash, errors)
    images = manifest.get("images")
    if not isinstance(images, list) or not images:
        errors.append("manifest.images must be a non-empty list")
    else:
        ids = [str(value.get("id", "")) for value in images if isinstance(value, dict)]
        paths = [str(value.get("output_path", "")) for value in images if isinstance(value, dict)]
        if len(ids) != len(set(ids)):
            errors.append("manifest image IDs must be unique")
        if len(paths) != len(set(paths)):
            errors.append("manifest image paths must be unique")
        for index, image in enumerate(images, start=1):
            validate_image(
                image, index, str(manifest.get("mode", "")), canonical_dir, skill_runs, errors
            )
    validate_mode_results(manifest, canonical_dir, errors)
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    manifest_path = args.manifest.expanduser().resolve()
    if not manifest_path.is_file():
        print(f"ERROR: manifest not found: {manifest_path}", file=sys.stderr)
        return 2
    try:
        errors = validate(manifest_path)
    except (OSError, ValueError, json.JSONDecodeError) as err:
        errors = [f"unable to validate manifest: {err}"]
    if errors:
        print("designer manifest validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"designer manifest validation passed: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
