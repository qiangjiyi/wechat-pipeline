#!/usr/bin/env python3
"""Validate the durable Publisher receipt before a run becomes published."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from protocol_version import PROTOCOL_VERSION


def load(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate(run_dir: Path) -> tuple[list[str], dict | None]:
    errors: list[str] = []
    pipeline = run_dir / ".pipeline"
    run_path = pipeline / "run.json"
    receipt_path = pipeline / "publish-result.json"
    snapshot_path = pipeline / "publish-snapshot.json"
    try:
        run = load(run_path)
    except (OSError, ValueError, json.JSONDecodeError) as err:
        return [f"unable to read run context: {err}"], None
    try:
        receipt = load(receipt_path)
    except (OSError, ValueError, json.JSONDecodeError) as err:
        return [f"unable to read publish receipt: {err}"], None
    if run.get("protocol_version") != PROTOCOL_VERSION:
        errors.append(f"run protocol_version must be {PROTOCOL_VERSION}")
    if Path(str(run.get("canonical_output_dir", ""))).expanduser().resolve() != run_dir:
        errors.append("run canonical_output_dir does not match the requested directory")
    if run.get("status") not in {"publishing", "published"}:
        errors.append("run must be publishing or published for publish receipt validation")
    if receipt.get("schema_version") != 1:
        errors.append("publish receipt schema_version must be 1")
    if receipt.get("ok") is not True:
        errors.append("publish receipt top-level ok must be true")
    if receipt.get("protocol_version") != run.get("protocol_version"):
        errors.append("publish receipt protocol_version does not match run.json")
    if receipt.get("run_id") != run.get("run_id"):
        errors.append("publish receipt run_id does not match run.json")
    if receipt.get("account") != run.get("account"):
        errors.append("publish receipt account does not match run.json")
    try:
        snapshot = load(snapshot_path)
    except (OSError, ValueError, json.JSONDecodeError) as err:
        errors.append(f"unable to read publish snapshot: {err}")
        snapshot = {}
    else:
        from build_publish_snapshot import validate_snapshot

        snapshot_errors, _ = validate_snapshot(run_dir)
        errors.extend(snapshot_errors)
        if receipt.get("snapshot_sha256") != sha256_file(snapshot_path):
            errors.append("publish receipt snapshot_sha256 does not match publish-snapshot.json")
        if receipt.get("snapshot_fingerprint") != snapshot.get("fingerprint"):
            errors.append("publish receipt snapshot_fingerprint does not match publish snapshot")
    expected_modes = {"newspic"} if run.get("mode") == "newspic" else {"article-html"}
    if receipt.get("mode") not in expected_modes:
        errors.append("publish receipt mode does not match run.json")
    if not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("publish_fingerprint", ""))):
        errors.append("publish receipt publish_fingerprint must be a SHA-256 hex digest")
    if not receipt.get("draft_media_id"):
        errors.append("publish receipt is missing draft_media_id")
    if receipt.get("creation_status") not in {"created", "recovered"}:
        errors.append("publish receipt does not prove that draft creation completed")
    verification = receipt.get("verification")
    if not isinstance(verification, dict) or verification.get("ok") is not True:
        errors.append("publish receipt read-back verification is not successful")
    elif (
        verification.get("status") != "verified"
        or verification.get("method") != "draft/get"
        or not verification.get("verified_at")
    ):
        errors.append("publish receipt does not prove a completed draft/get verification")
    if run.get("mode") == "newspic":
        manifest_path = pipeline / "manifest.json"
        try:
            manifest = load(manifest_path)
        except (OSError, ValueError, json.JSONDecodeError) as err:
            errors.append(f"unable to read newspic manifest: {err}")
        else:
            if receipt.get("manifest_sha256") != sha256_file(manifest_path):
                errors.append("publish receipt manifest_sha256 does not match manifest.json")
            source_path = Path(str(manifest.get("source", {}).get("original_path", ""))).expanduser().resolve()
            if not source_path.is_file() or receipt.get("source_sha256") != sha256_file(source_path):
                errors.append("publish receipt source_sha256 does not match sealed input")
            expected_images = []
            for item in manifest.get("images", []):
                path = Path(str(item.get("output_path", ""))).expanduser().resolve()
                if not path.is_file():
                    errors.append(f"manifest image is missing during publish receipt validation: {path}")
                    continue
                expected_images.append({"path": str(path), "sha256": sha256_file(path)})
            if receipt.get("images") != expected_images:
                errors.append("publish receipt ordered images do not match manifest outputs")
            uploaded = receipt.get("uploaded_image_media_ids")
            if not isinstance(uploaded, list) or len(uploaded) != len(expected_images):
                errors.append("publish receipt uploaded image media IDs are incomplete")
    else:
        layout_path = pipeline / "layout.json"
        html_path = run_dir / "article-body.html"
        if not layout_path.is_file() or receipt.get("layout_sha256") != sha256_file(layout_path):
            errors.append("publish receipt layout_sha256 does not match layout.json")
        if not html_path.is_file() or receipt.get("html_sha256") != sha256_file(html_path):
            errors.append("publish receipt html_sha256 does not match article-body.html")
        body_images = snapshot.get("body_images") if isinstance(snapshot.get("body_images"), list) else []
        if receipt.get("body_image_count") != len(body_images):
            errors.append("publish receipt body_image_count does not match publish snapshot")
        if receipt.get("uploaded_body_image_count") != len(body_images):
            errors.append("publish receipt uploaded body image count is incomplete")
        cover = snapshot.get("cover") if isinstance(snapshot.get("cover"), dict) else {}
        if Path(str(receipt.get("cover_path", ""))).expanduser().resolve() != Path(str(cover.get("path", ""))).expanduser().resolve():
            errors.append("publish receipt cover_path does not match publish snapshot")
    return errors, receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    run_dir = args.run_dir.expanduser().resolve()
    errors, receipt = validate(run_dir)
    if errors:
        print("publish receipt validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(json.dumps({
        "ok": True,
        "protocol_version": PROTOCOL_VERSION,
        "draft_media_id": receipt["draft_media_id"],
        "verification": receipt["verification"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
