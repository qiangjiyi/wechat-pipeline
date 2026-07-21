#!/usr/bin/env python3
"""Proof-of-execution evidence for images returned by visual Skill runs.

A visual receipt used to be pure self-report: the worker claimed an image was
generated and the pipeline only bound its hash.  Solid-colour placeholders
sailed through.  Every returned image must now be paired with one evidence
file (schema_version 1) written right after generation, and the pipeline
re-checks the cross-references at complete time and at the artwork gate.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from shared.hashing import sha256_file
from shared.jsonio import inside, load_json


EVIDENCE_SCHEMA_VERSION = 1
IMAGE_ROLES = {"card", "cover", "body"}
CLOCK_SKEW = timedelta(minutes=2)

REQUIRED_FIELDS = {
    "schema_version",
    "provider",
    "output_path",
    "output_bytes",
    "output_sha256",
    "generated_at",
    "elapsed_seconds",
    "cached",
    "attempts",
    "prompt_file",
}


def parse_timestamp(value: Any, label: str, errors: list[str]) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"{label} is not a valid ISO-8601 timestamp: {value!r}")
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        errors.append(f"{label} must include an explicit timezone offset")
        return None
    return parsed


def validate_image_evidence(
    evidence_path: Path,
    output_path: Path,
    workspace: Path,
    started_at: datetime,
    *,
    now: datetime | None = None,
) -> list[str]:
    """Cross-check one evidence file against the image it claims to prove."""
    errors: list[str] = []
    label = evidence_path.name
    evidence_path = evidence_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    workspace = workspace.expanduser().resolve()
    now = now or datetime.now().astimezone()

    if not inside(evidence_path, workspace):
        errors.append(f"{label}: evidence file must stay inside the Skill workspace")
        return errors
    if not evidence_path.is_file():
        errors.append(f"{label}: evidence file is missing: {evidence_path}")
        return errors
    try:
        evidence = load_json(evidence_path)
    except (OSError, ValueError) as err:
        errors.append(f"{label}: evidence file is not valid JSON: {err}")
        return errors

    missing = REQUIRED_FIELDS - set(evidence)
    if missing:
        errors.append(f"{label}: missing evidence fields: {sorted(missing)}")
        return errors
    if evidence.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
        errors.append(f"{label}: schema_version must be {EVIDENCE_SCHEMA_VERSION}")
    if not str(evidence.get("provider", "")).strip():
        errors.append(f"{label}: provider must name the image backend that ran")

    recorded_output = Path(str(evidence.get("output_path", ""))).expanduser().resolve()
    if recorded_output != output_path:
        errors.append(f"{label}: output_path does not match the returned image")
    if not output_path.is_file():
        errors.append(f"{label}: returned image is missing: {output_path}")
        return errors
    actual_size = output_path.stat().st_size
    if evidence.get("output_bytes") != actual_size or actual_size <= 0:
        errors.append(
            f"{label}: output_bytes {evidence.get('output_bytes')!r} does not match "
            f"the image on disk ({actual_size})"
        )
    if evidence.get("output_sha256") != sha256_file(output_path):
        errors.append(f"{label}: output_sha256 does not match the image on disk")

    generated_at = parse_timestamp(evidence.get("generated_at"), f"{label}: generated_at", errors)
    if generated_at is not None:
        if generated_at < started_at - CLOCK_SKEW:
            errors.append(f"{label}: generated_at predates the Skill start")
        if generated_at > now + CLOCK_SKEW:
            errors.append(f"{label}: generated_at is in the future")

    try:
        elapsed = float(evidence.get("elapsed_seconds"))
    except (TypeError, ValueError):
        elapsed = -1
        errors.append(f"{label}: elapsed_seconds must be a number")
    cached = bool(evidence.get("cached"))
    if elapsed >= 0 and not cached and elapsed <= 0:
        errors.append(
            f"{label}: elapsed_seconds must be > 0 for a real backend render "
            "(only a cache hit may report 0)"
        )
    try:
        attempts = int(evidence.get("attempts"))
    except (TypeError, ValueError):
        attempts = 0
    if attempts < 1:
        errors.append(f"{label}: attempts must be >= 1")

    prompt_file = Path(str(evidence.get("prompt_file", ""))).expanduser().resolve()
    prompts_dir = workspace / "prompts"
    if not inside(prompt_file, prompts_dir):
        errors.append(f"{label}: prompt_file must live under {prompts_dir}")
    elif not prompt_file.is_file() or prompt_file.stat().st_size == 0:
        errors.append(f"{label}: prompt_file is missing or empty: {prompt_file}")
    elif prompt_file.stat().st_mtime > output_path.stat().st_mtime + CLOCK_SKEW.total_seconds():
        errors.append(f"{label}: prompt_file was written after the image it claims to produce")

    if evidence_path.stat().st_mtime < output_path.stat().st_mtime - CLOCK_SKEW.total_seconds():
        errors.append(f"{label}: evidence was written before the image it proves")
    return errors
