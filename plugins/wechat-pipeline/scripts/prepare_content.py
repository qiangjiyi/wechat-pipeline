#!/usr/bin/env python3
"""Seal one formatted Markdown artifact while proving source-text preservation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from protocol_version import PROTOCOL_VERSION


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def body_lines(value: str) -> list[str]:
    lines = value.splitlines()
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                return lines[index + 1:]
    return lines


def normalized_segments(value: str) -> list[str]:
    segments: list[str] = []
    for raw in body_lines(value):
        line = raw.strip()
        if not line or line.startswith("```") or re.fullmatch(r"[-*_]{3,}", line):
            continue
        line = re.sub(r"^(?:#{1,6}|>|[-+*]|\d+[.)])\s*", "", line)
        line = re.sub(r"!\[([^]]*)\]\([^)]+\)", r"\1", line)
        line = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", line)
        line = re.sub(r"(?:\*\*|__|~~|==|\+\+|`)", "", line)
        normalized = re.sub(r"\s+", "", line)
        if len(normalized) >= 2:
            segments.append(normalized)
    return segments


def normalized_document(value: str) -> str:
    return "".join(normalized_segments(value))


def inspect_markdown(value: str) -> dict:
    lines = body_lines(value)
    h1 = [line[2:].strip() for line in lines if re.match(r"^#\s+\S", line)]
    h2 = [line[3:].strip() for line in lines if re.match(r"^##\s+\S", line)]
    h3 = [line[4:].strip() for line in lines if re.match(r"^###\s+\S", line)]
    return {"h1": h1, "h2": h2, "h3": h3}


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
    if not content.is_file():
        errors.append(f"formatted content not found: {content}")
        return errors, receipt
    if receipt.get("protocol_version") != PROTOCOL_VERSION:
        errors.append(f"format receipt protocol_version must be {PROTOCOL_VERSION}")
    if receipt.get("run_id") != run.get("run_id"):
        errors.append("format receipt run_id does not match run.json")
    if receipt.get("source_sha256") != sha256_file(original):
        errors.append("format receipt source hash does not match sealed input")
    if receipt.get("content_path") != str(content) or receipt.get("content_sha256") != sha256_file(content):
        errors.append("format receipt does not bind the canonical content.md")
    value = content.read_text(encoding="utf-8")
    headings = inspect_markdown(value)
    if len(headings["h1"]) != 1:
        errors.append("formatted content must contain exactly one level-1 heading")
    source_segments = normalized_segments(original.read_text(encoding="utf-8", errors="replace"))
    normalized_content = normalized_document(value)
    missing = [segment for segment in source_segments if segment not in normalized_content]
    if missing:
        errors.append(f"formatted content is missing {len(missing)} source text segment(s)")
    if receipt.get("missing_source_segments") != []:
        errors.append("format receipt must record zero missing source segments")
    return errors, receipt


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)
    path.chmod(0o400)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    run_dir = args.run_dir.expanduser().resolve()
    source = args.source.expanduser().resolve()
    pipeline = run_dir / ".pipeline"
    run_path = pipeline / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    if run.get("protocol_version") != PROTOCOL_VERSION:
        raise SystemExit(f"run protocol_version must be {PROTOCOL_VERSION}")
    if Path(str(run.get("canonical_output_dir", ""))).expanduser().resolve() != run_dir:
        raise SystemExit("run canonical_output_dir mismatch")
    if not source.is_file() or not inside(source, run_dir):
        raise SystemExit("formatter output must be an existing file inside canonical_output_dir")

    target = run_dir / "content.md"
    receipt_path = pipeline / "format-result.json"
    if target.exists() or receipt_path.exists():
        errors, receipt = validate_content_artifact(run_dir)
        if errors:
            raise SystemExit("existing formatted content is invalid: " + "; ".join(errors))
        print(json.dumps({"reused": True, **(receipt or {})}, ensure_ascii=False))
        return 0

    source_text = source.read_text(encoding="utf-8")
    headings = inspect_markdown(source_text)
    if len(headings["h1"]) != 1:
        raise SystemExit("formatter output must contain exactly one level-1 heading")
    original = pipeline / "input.md"
    source_segments = normalized_segments(original.read_text(encoding="utf-8", errors="replace"))
    normalized_output = normalized_document(source_text)
    missing = [segment for segment in source_segments if segment not in normalized_output]
    if missing:
        raise SystemExit(f"formatter output removed {len(missing)} source text segment(s)")

    shutil.copyfile(source, target)
    target.chmod(0o400)
    receipt = {
        "schema_version": 1,
        "protocol_version": PROTOCOL_VERSION,
        "run_id": run.get("run_id"),
        "source_path": str(original),
        "source_sha256": sha256_file(original),
        "formatter_output_path": str(source),
        "formatter_output_sha256": sha256_file(source),
        "content_path": str(target),
        "content_sha256": sha256_file(target),
        "title": headings["h1"][0],
        "heading_counts": {"h1": 1, "h2": len(headings["h2"]), "h3": len(headings["h3"])},
        "source_segment_count": len(source_segments),
        "missing_source_segments": [],
        "created_at": datetime.now().astimezone().isoformat(),
    }
    write_json(receipt_path, receipt)
    print(json.dumps({"reused": False, **receipt}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
