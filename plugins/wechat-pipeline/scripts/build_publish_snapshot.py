#!/usr/bin/env python3
"""Build and validate the immutable, complete input to the WeChat publisher."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

from integrity import validate_runtime
from protocol_version import PROTOCOL_VERSION
from prepare_content import validate_content_artifact
from prepare_layout import validate_layout_evidence
from shared.hashing import sha256_file
from shared.jsonio import write_json
from skill_run import formatter_paths
from validate_designer_manifest import validate as validate_manifest


VALIDATORS = (
    PLUGIN_ROOT / "scripts" / "integrity.py",
    PLUGIN_ROOT / "scripts" / "run_context.py",
    PLUGIN_ROOT / "scripts" / "skill_run.py",
    PLUGIN_ROOT / "scripts" / "prepare_content.py",
    PLUGIN_ROOT / "scripts" / "validate_designer_manifest.py",
    PLUGIN_ROOT / "scripts" / "prepare_layout.py",
    PLUGIN_ROOT / "scripts" / "validate_article_layout.py",
    PLUGIN_ROOT / "scripts" / "build_publish_snapshot.py",
    PLUGIN_ROOT / "scripts" / "validate_publish_result.py",
)


def fingerprint(value: dict) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def snapshot_path(run_dir: Path) -> Path:
    return run_dir / ".pipeline" / "publish-snapshot.json"


def file_binding(path: Path) -> dict:
    return {"path": str(path), "sha256": sha256_file(path)}


def build(run_dir: Path) -> dict:
    pipeline = run_dir / ".pipeline"
    run = json.loads((pipeline / "run.json").read_text(encoding="utf-8"))
    errors: list[str] = []
    runtime_errors, runtime = validate_runtime(run_dir, run)
    errors.extend(runtime_errors)
    format_errors, format_receipt = validate_content_artifact(run_dir)
    errors.extend(format_errors)
    manifest_path = pipeline / "manifest.json"
    errors.extend(validate_manifest(manifest_path))
    layout_result: dict | None = None
    if run.get("mode") == "news":
        layout_errors, layout_result = validate_layout_evidence(run_dir)
        errors.extend(layout_errors)
    if errors:
        raise SystemExit("publish snapshot preflight failed: " + "; ".join(errors))

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    images = [
        {
            "id": image.get("id"),
            "kind": image.get("kind"),
            **file_binding(Path(str(image.get("output_path"))).expanduser().resolve()),
        }
        for image in manifest.get("images", [])
    ]
    formatter_receipt, _, _, _, formatter_output = formatter_paths(run_dir)
    core = {
        "schema_version": 1,
        "protocol_version": PROTOCOL_VERSION,
        "run_id": run.get("run_id"),
        "mode": run.get("mode"),
        "account": run.get("account"),
        "canonical_output_dir": str(run_dir),
        "source": file_binding(pipeline / "input.md"),
        "formatter_skill_run": file_binding(formatter_receipt),
        "formatter_output": file_binding(formatter_output),
        "content": file_binding(run_dir / "content.md"),
        "format_result": file_binding(pipeline / "format-result.json"),
        "manifest": file_binding(manifest_path),
        "images": images,
        "runtime_sha256": (runtime or {}).get("runtime_sha256"),
        "validators": [file_binding(path) for path in VALIDATORS],
        "created_at": datetime.now().astimezone().isoformat(),
    }
    if run.get("mode") == "newspic":
        core["publication"] = {
            "title": (format_receipt or {}).get("title"),
            "author": "",
            "digest": "",
            "content_sha256": sha256_file(pipeline / "input.md"),
        }
    if run.get("mode") == "news":
        layout_path = pipeline / "layout.json"
        html_path = run_dir / "article-body.html"
        layout_manifest = json.loads(layout_path.read_text(encoding="utf-8"))
        metadata = layout_manifest.get("metadata") if isinstance(layout_manifest.get("metadata"), dict) else {}
        cover = next(image for image in images if image.get("kind") == "cover")
        core.update({
            "layout_skill_run": file_binding(pipeline / "layout-skill-run.json"),
            "layout_native_output": file_binding(
                Path(str(layout_manifest.get("output", {}).get("native_output_path", ""))).expanduser().resolve()
            ),
            "layout": file_binding(layout_path),
            "html": file_binding(html_path),
            "cover": cover,
            "body_images": [image for image in images if image.get("kind") != "cover"],
            "layout_validation": {
                "html_sha256": layout_result.get("html_sha256") if layout_result else None,
                "error_count": 0,
                "warning_count": 0,
            },
            "publication": {
                "title": metadata.get("title"),
                "author": metadata.get("author") or "",
                "summary": metadata.get("summary") or "",
            },
        })
    core["fingerprint"] = fingerprint(core)
    return core


def validate_binding(value: object, label: str, run_dir: Path, errors: list[str]) -> None:
    if not isinstance(value, dict):
        errors.append(f"snapshot {label} must be an object")
        return
    path = Path(str(value.get("path", ""))).expanduser().resolve()
    if path != run_dir and run_dir not in path.parents:
        errors.append(f"snapshot {label} path escapes canonical_output_dir")
    elif not path.is_file():
        errors.append(f"snapshot {label} file is missing: {path}")
    elif value.get("sha256") != sha256_file(path):
        errors.append(f"snapshot {label} hash mismatch: {path}")


def validate_snapshot_evidence(run_dir: Path) -> tuple[list[str], dict | None]:
    """Validate the sealed snapshot itself without rehashing all bound artifacts."""
    errors: list[str] = []
    try:
        snapshot = json.loads(snapshot_path(run_dir).read_text(encoding="utf-8"))
        run = json.loads((run_dir / ".pipeline" / "run.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        return [f"unable to read publish snapshot evidence: {err}"], None
    if snapshot.get("protocol_version") != PROTOCOL_VERSION:
        errors.append(f"snapshot protocol_version must be {PROTOCOL_VERSION}")
    for key in ("run_id", "mode", "account"):
        if snapshot.get(key) != run.get(key):
            errors.append(f"snapshot {key} does not match run.json")
    if Path(str(snapshot.get("canonical_output_dir", ""))).expanduser().resolve() != run_dir:
        errors.append("snapshot canonical_output_dir mismatch")
    claimed = snapshot.get("fingerprint")
    core = dict(snapshot)
    core.pop("fingerprint", None)
    if claimed != fingerprint(core):
        errors.append("snapshot fingerprint is invalid")
    return errors, snapshot


def validate_snapshot(run_dir: Path) -> tuple[list[str], dict | None]:
    errors, snapshot = validate_snapshot_evidence(run_dir)
    if snapshot is None:
        return errors, None
    run = json.loads((run_dir / ".pipeline" / "run.json").read_text(encoding="utf-8"))
    check_runtime = run.get("status") != "published"
    formatter_receipt, _, _, _, formatter_output = formatter_paths(run_dir)
    fixed_paths = {
        "source": run_dir / ".pipeline" / "input.md",
        "formatter_skill_run": formatter_receipt,
        "formatter_output": formatter_output,
        "content": run_dir / "content.md",
        "format_result": run_dir / ".pipeline" / "format-result.json",
        "manifest": run_dir / ".pipeline" / "manifest.json",
    }
    for label in (
        "source", "formatter_skill_run", "formatter_output", "content", "format_result", "manifest"
    ):
        validate_binding(snapshot.get(label), label, run_dir, errors)
        if isinstance(snapshot.get(label), dict):
            actual_path = Path(str(snapshot[label].get("path", ""))).expanduser().resolve()
            if actual_path != fixed_paths[label]:
                errors.append(f"snapshot {label} must bind {fixed_paths[label]}")
    try:
        manifest = json.loads(fixed_paths["manifest"].read_text(encoding="utf-8"))
        format_receipt = json.loads(fixed_paths["format_result"].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        errors.append(f"unable to rebuild snapshot bindings: {err}")
        manifest = {}
        format_receipt = {}
    images = snapshot.get("images")
    ordered_images = images if isinstance(images, list) else []
    if not isinstance(images, list) or not images:
        errors.append("snapshot images must be a non-empty ordered list")
    else:
        for index, image in enumerate(images, start=1):
            validate_binding(image, f"images[{index}]", run_dir, errors)
        expected_images = [
            {
                "id": image.get("id"),
                "kind": image.get("kind"),
                **file_binding(Path(str(image.get("output_path", ""))).expanduser().resolve()),
            }
            for image in manifest.get("images", [])
            if isinstance(image, dict)
        ]
        if images != expected_images:
            errors.append("snapshot ordered images do not exactly match designer manifest")
    publication = snapshot.get("publication")
    if not isinstance(publication, dict):
        errors.append("snapshot publication metadata must be an object")
    elif snapshot.get("mode") == "newspic":
        expected_publication = {
            "title": format_receipt.get("title"),
            "author": "",
            "digest": "",
            "content_sha256": sha256_file(fixed_paths["source"]),
        }
        if publication != expected_publication:
            errors.append("newspic publication metadata does not match formatted content and sealed source")
    if snapshot.get("mode") == "news":
        for label in ("layout_skill_run", "layout_native_output", "layout", "html", "cover"):
            validate_binding(snapshot.get(label), label, run_dir, errors)
        expected_news_paths = {
            "layout_skill_run": run_dir / ".pipeline" / "layout-skill-run.json",
            "layout": run_dir / ".pipeline" / "layout.json",
            "html": run_dir / "article-body.html",
        }
        for label, expected_path in expected_news_paths.items():
            if isinstance(snapshot.get(label), dict):
                actual_path = Path(str(snapshot[label].get("path", ""))).expanduser().resolve()
                if actual_path != expected_path:
                    errors.append(f"snapshot {label} must bind {expected_path}")
        body_images = snapshot.get("body_images")
        if not isinstance(body_images, list) or not body_images:
            errors.append("news snapshot must bind at least one body image")
        elif body_images != [image for image in ordered_images if image.get("kind") != "cover"]:
            errors.append("snapshot body_images do not match the ordered image bindings")
        try:
            layout_manifest = json.loads((run_dir / ".pipeline" / "layout.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            layout_manifest = {}
        metadata = layout_manifest.get("metadata") if isinstance(layout_manifest.get("metadata"), dict) else {}
        native_output = layout_manifest.get("output") if isinstance(layout_manifest.get("output"), dict) else {}
        expected_native_path = Path(str(native_output.get("native_output_path", ""))).expanduser().resolve()
        if isinstance(snapshot.get("layout_native_output"), dict):
            actual_native_path = Path(str(snapshot["layout_native_output"].get("path", ""))).expanduser().resolve()
            if actual_native_path != expected_native_path:
                errors.append("snapshot layout_native_output does not match layout manifest")
        expected_publication = {
            "title": metadata.get("title"),
            "author": metadata.get("author") or "",
            "summary": metadata.get("summary") or "",
        }
        if publication != expected_publication:
            errors.append("news publication metadata does not match layout manifest")
        expected_cover = next((image for image in ordered_images if image.get("kind") == "cover"), None)
        if snapshot.get("cover") != expected_cover:
            errors.append("snapshot cover does not match designer manifest")
    validators = snapshot.get("validators")
    if not isinstance(validators, list) or len(validators) != len(VALIDATORS):
        errors.append("snapshot validator bindings are incomplete")
    else:
        for index, value in enumerate(validators, start=1):
            if not isinstance(value, dict):
                errors.append(f"snapshot validators[{index}] must be an object")
                continue
            path = Path(str(value.get("path", ""))).expanduser().resolve()
            if check_runtime and path != VALIDATORS[index - 1]:
                errors.append(f"snapshot validators[{index}] path mismatch")
            elif check_runtime and (not path.is_file() or value.get("sha256") != sha256_file(path)):
                errors.append(f"snapshot validators[{index}] hash mismatch")
            elif not re.fullmatch(r"[0-9a-f]{64}", str(value.get("sha256", ""))):
                errors.append(f"snapshot validators[{index}] hash is invalid")
    runtime_errors, runtime = validate_runtime(run_dir, run)
    if check_runtime:
        errors.extend(runtime_errors)
    if snapshot.get("runtime_sha256") != (runtime or {}).get("runtime_sha256"):
        errors.append("snapshot runtime hash does not match the run integrity snapshot")
    return errors, snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--validate", action="store_true")
    args = parser.parse_args()
    run_dir = args.run_dir.expanduser().resolve()
    path = snapshot_path(run_dir)
    if args.validate:
        errors, snapshot = validate_snapshot(run_dir)
        if errors:
            print("publish snapshot validation failed:")
            for error in errors:
                print(f"- {error}")
            return 1
        print(json.dumps({"ok": True, "fingerprint": snapshot.get("fingerprint")}, ensure_ascii=False))
        return 0
    if path.exists():
        errors, snapshot = validate_snapshot(run_dir)
        if errors:
            raise SystemExit("existing publish snapshot is invalid: " + "; ".join(errors))
        print(json.dumps({"reused": True, **(snapshot or {})}, ensure_ascii=False))
        return 0
    snapshot = build(run_dir)
    write_json(path, snapshot, mode=0o400)
    print(json.dumps({"reused": False, **snapshot}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
