#!/usr/bin/env python3
"""Validate planning evidence or publish-ready image outputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from datetime import datetime
from pathlib import Path

from protocol_version import PROTOCOL_VERSION
from typing import Any


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
ALLOWED_VERDICTS = {
    "success",
    "network_error",
    "api_error",
    "quota_error",
    "empty_output",
    "invalid_output",
    "contract_error",
}
BACKEND_ALIASES = {
    "imagegen": "openai-native",
    "codex-image-gen": "codex-cli",
}
REQUIRED_TOP_LEVEL = {
    "schema_version",
    "protocol_version",
    "run_id",
    "mode",
    "canonical_output_dir",
    "source",
    "skill_contract",
    "images",
}
REQUIRED_IMAGE_FIELDS = {
    "id",
    "kind",
    "source_skill",
    "prompt_path",
    "prompt_sha256",
    "prompt_written_at",
    "output_path",
    "aspect",
    "attempts",
    "status",
}
REQUIRED_ATTEMPT_FIELDS = {
    "scope",
    "backend",
    "prompt_sha256",
    "started_at",
    "finished_at",
    "verdict",
    "error_summary",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def resolve_path(value: str, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base_dir / path).resolve()


def parse_time(value: Any, label: str, errors: list[str]) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"invalid ISO-8601 timestamp for {label}: {value!r}")
        return None


def is_png(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size <= len(PNG_SIGNATURE):
        return False
    with path.open("rb") as handle:
        return handle.read(len(PNG_SIGNATURE)) == PNG_SIGNATURE


def png_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        with path.open("rb") as handle:
            header = handle.read(24)
    except OSError:
        return None
    if len(header) < 24 or not header.startswith(PNG_SIGNATURE) or header[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", header[16:24])


def expected_ratio(value: str) -> float | None:
    try:
        width, height = value.split(":", 1)
        ratio = float(width) / float(height)
    except (ValueError, ZeroDivisionError):
        return None
    return ratio if ratio > 0 else None


def read_preferred_style(path: Path) -> str | None:
    """Read the small preferred_style subset without requiring a YAML dependency."""
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped.startswith("preferred_style:"):
            continue
        inline = stripped.split(":", 1)[1].strip().strip("\"'")
        if inline and inline not in {"null", "~"}:
            return inline
        base_indent = len(raw_line) - len(raw_line.lstrip())
        for child in lines[index + 1:]:
            child_stripped = child.strip()
            if not child_stripped or child_stripped.startswith("#"):
                continue
            child_indent = len(child) - len(child.lstrip())
            if child_indent <= base_indent:
                break
            if child_stripped.startswith("name:"):
                return child_stripped.split(":", 1)[1].strip().strip("\"'") or None
        return None
    return None


def validate_source(manifest: dict, base_dir: Path, phase: str, errors: list[str]) -> None:
    source = manifest.get("source")
    if not isinstance(source, dict):
        errors.append("manifest.source must be an object")
        return
    for key in ("original_path", "original_sha256"):
        if not source.get(key):
            errors.append(f"manifest.source missing field: {key}")
    if not source.get("original_path"):
        return
    original_path = resolve_path(str(source["original_path"]), base_dir)
    expected_input = (
        Path(manifest.get("canonical_output_dir", "")).expanduser().resolve()
        / ".pipeline"
        / "input.md"
    )
    if original_path != expected_input:
        errors.append(f"original_path must be the sealed run input: {expected_input}")
    if not original_path.is_file():
        errors.append(f"source original file not found: {original_path}")
        return
    actual_hash = sha256_file(original_path)
    if actual_hash != source.get("original_sha256"):
        errors.append(f"source original hash mismatch: expected {source.get('original_sha256')} got {actual_hash}")
    if phase == "publish-ready":
        adapter_hash = source.get("publisher_text_sha256")
        if adapter_hash != actual_hash:
            errors.append("publisher_text_sha256 must equal the sealed original input hash")


def validate_skill_contract(manifest: dict, base_dir: Path, errors: list[str]) -> None:
    contract = manifest.get("skill_contract")
    if not isinstance(contract, dict):
        errors.append("manifest.skill_contract must be an object")
        return
    for key in ("skill_name", "skill_path", "skill_sha256", "files_read", "preferences"):
        if key not in contract:
            errors.append(f"manifest.skill_contract missing field: {key}")
    skill_path_value = contract.get("skill_path")
    if skill_path_value:
        skill_path = resolve_path(str(skill_path_value), base_dir)
        if not skill_path.is_file():
            errors.append(f"skill file not found: {skill_path}")
        elif sha256_file(skill_path) != contract.get("skill_sha256"):
            errors.append("skill_sha256 does not match skill_path")
    files_read = contract.get("files_read")
    if not isinstance(files_read, list) or not files_read:
        errors.append("skill_contract.files_read must be a non-empty list")
    else:
        for value in files_read:
            path = resolve_path(str(value), base_dir)
            if not path.is_file():
                errors.append(f"declared skill/reference file not found: {path}")
    preferences = contract.get("preferences")
    if not isinstance(preferences, dict):
        errors.append("skill_contract.preferences must be an object")
        return
    if preferences.get("source") not in {"user", "extend", "auto"}:
        errors.append("preferences.source must be user, extend, or auto")
    extend_path = preferences.get("extend_path")
    extend_hash = preferences.get("extend_sha256")
    if extend_path:
        path = resolve_path(str(extend_path), base_dir)
        if not path.is_file():
            errors.append(f"EXTEND.md not found: {path}")
        elif sha256_file(path) != extend_hash:
            errors.append("extend_sha256 does not match extend_path")
        elif preferences.get("source") == "extend":
            preferred_style = read_preferred_style(path)
            if preferred_style and preferences.get("style") != preferred_style:
                errors.append(
                    "resolved style does not match EXTEND.md preferred_style: "
                    f"expected {preferred_style!r} got {preferences.get('style')!r}"
                )


def validate_image(
    image: Any,
    index: int,
    base_dir: Path,
    canonical_dir: Path,
    phase: str,
    configured_backends: set[str],
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

    prompt_path = resolve_path(str(image["prompt_path"]), base_dir)
    output_path = resolve_path(str(image["output_path"]), base_dir)
    if canonical_dir not in prompt_path.parents:
        errors.append(f"{label}.prompt_path must stay inside canonical_output_dir")
    if canonical_dir not in output_path.parents:
        errors.append(f"{label}.output_path must stay inside canonical_output_dir")
    if not prompt_path.is_file():
        errors.append(f"prompt not found: {prompt_path}")
        prompt_hash = None
    else:
        prompt_hash = sha256_file(prompt_path)
        if prompt_hash != image["prompt_sha256"]:
            errors.append(f"prompt hash mismatch: {prompt_path}")

    prompt_written_at = parse_time(image["prompt_written_at"], f"{label}.prompt_written_at", errors)
    attempts = image.get("attempts")
    if not isinstance(attempts, list):
        errors.append(f"{label}.attempts must be a list")
        attempts = []

    previous_finished: datetime | None = None
    for attempt_index, attempt in enumerate(attempts, start=1):
        attempt_label = f"{label}.attempts[{attempt_index}]"
        if not isinstance(attempt, dict):
            errors.append(f"{attempt_label} must be an object")
            continue
        missing_attempt = REQUIRED_ATTEMPT_FIELDS - set(attempt)
        if missing_attempt:
            errors.append(f"{attempt_label} missing fields: {sorted(missing_attempt)}")
            continue
        if attempt.get("scope") != "image":
            errors.append(f"{attempt_label}.scope must be image; preflight attempts belong in preflight.json")
        if attempt.get("verdict") not in ALLOWED_VERDICTS:
            errors.append(f"{attempt_label} invalid verdict: {attempt.get('verdict')!r}")
        backend = str(attempt.get("backend", ""))
        canonical_backend = BACKEND_ALIASES.get(backend, backend)
        if canonical_backend not in configured_backends:
            errors.append(
                f"{attempt_label}.backend was not configured by preflight: {backend!r}"
            )
        if attempt.get("prompt_sha256") != image.get("prompt_sha256"):
            errors.append(f"{attempt_label}.prompt_sha256 does not match the image prompt")
        started = parse_time(attempt.get("started_at"), f"{attempt_label}.started_at", errors)
        finished = parse_time(attempt.get("finished_at"), f"{attempt_label}.finished_at", errors)
        if started and prompt_written_at and started < prompt_written_at:
            errors.append(f"{attempt_label} started before its prompt was written")
        if started and finished and finished < started:
            errors.append(f"{attempt_label} finished before it started")
        if started and previous_finished and started < previous_finished:
            errors.append(f"{attempt_label} overlaps or precedes the previous attempt")
        if finished:
            previous_finished = finished

    if phase == "plan":
        return
    if image.get("status") != "success":
        errors.append(f"{label} is not publish-ready: status={image.get('status')!r}")
    if not attempts or attempts[-1].get("verdict") != "success":
        errors.append(f"{label} last attempt must have verdict success")
    if not is_png(output_path):
        errors.append(f"output is not a valid non-empty PNG: {output_path}")
    elif sha256_file(output_path) != image.get("output_sha256"):
        errors.append(f"output hash mismatch: {output_path}")
    else:
        dimensions = png_dimensions(output_path)
        ratio = expected_ratio(str(image.get("aspect", "")))
        if dimensions is None:
            errors.append(f"output PNG is missing a valid IHDR dimension header: {output_path}")
        elif ratio is None:
            errors.append(f"{label}.aspect must use a positive width:height ratio")
        else:
            actual = dimensions[0] / dimensions[1]
            if abs(actual - ratio) / ratio > 0.03:
                errors.append(
                    f"{label} dimensions {dimensions[0]}x{dimensions[1]} do not match aspect {image.get('aspect')}"
                )


def validate(manifest_path: Path, phase: str) -> list[str]:
    errors: list[str] = []
    manifest = load_json(manifest_path)
    if not isinstance(manifest, dict):
        return ["manifest root must be an object"]
    base_dir = manifest_path.parent
    missing = REQUIRED_TOP_LEVEL - set(manifest)
    if missing:
        errors.append(f"manifest missing top-level fields: {sorted(missing)}")
    if manifest.get("protocol_version") != PROTOCOL_VERSION:
        errors.append(f"protocol_version must be {PROTOCOL_VERSION}")

    canonical_dir = Path(str(manifest.get("canonical_output_dir", ""))).expanduser().resolve()
    configured_backends: set[str] = set()
    preflight_path = canonical_dir / ".pipeline" / "preflight.json"
    if not preflight_path.is_file():
        errors.append(f"image backend preflight not found: {preflight_path}")
    else:
        try:
            preflight = load_json(preflight_path)
            configured_backends = {
                str(value) for value in preflight.get("fallback_order", []) if value
            }
        except (OSError, ValueError) as err:
            errors.append(f"unable to read image backend preflight: {err}")
        if not configured_backends:
            errors.append("preflight fallback_order must contain at least one configured backend")
    expected_manifest = canonical_dir / ".pipeline" / "manifest.json"
    if manifest_path != expected_manifest:
        errors.append(f"manifest must live at {expected_manifest}")
    run_path = canonical_dir / ".pipeline" / "run.json"
    if not run_path.is_file():
        errors.append(f"run context not found: {run_path}")
    else:
        run = load_json(run_path)
        if run.get("run_id") != manifest.get("run_id"):
            errors.append("run_id does not match run.json")
        run_canonical = Path(str(run.get("canonical_output_dir", ""))).expanduser().resolve()
        if run_canonical != canonical_dir:
            errors.append("canonical_output_dir does not match run.json")

    validate_source(manifest, base_dir, phase, errors)
    validate_skill_contract(manifest, base_dir, errors)
    images = manifest.get("images")
    if not isinstance(images, list) or not images:
        errors.append("manifest.images must be a non-empty list")
    else:
        ids = [str(image.get("id", "")) for image in images if isinstance(image, dict)]
        prompt_paths = [str(image.get("prompt_path", "")) for image in images if isinstance(image, dict)]
        output_paths = [str(image.get("output_path", "")) for image in images if isinstance(image, dict)]
        if len(ids) != len(set(ids)):
            errors.append("manifest image IDs must be unique")
        if len(prompt_paths) != len(set(prompt_paths)):
            errors.append("manifest prompt paths must be unique")
        if len(output_paths) != len(set(output_paths)):
            errors.append("manifest output paths must be unique")
        for index, image in enumerate(images, start=1):
            validate_image(
                image,
                index,
                base_dir,
                canonical_dir,
                phase,
                configured_backends,
                errors,
            )
        if phase == "publish-ready":
            output_hashes = [
                str(image.get("output_sha256", ""))
                for image in images
                if isinstance(image, dict) and image.get("output_sha256")
            ]
            if len(output_hashes) != len(set(output_hashes)):
                errors.append("manifest contains duplicate rendered image files")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--phase", choices=("plan", "publish-ready"), default="publish-ready")
    args = parser.parse_args()
    manifest_path = args.manifest.expanduser().resolve()
    if not manifest_path.is_file():
        print(f"ERROR: manifest not found: {manifest_path}", file=sys.stderr)
        return 2
    errors = validate(manifest_path, args.phase)
    if errors:
        print(f"designer manifest validation failed ({args.phase}):")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"designer manifest validation passed ({args.phase}): {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
