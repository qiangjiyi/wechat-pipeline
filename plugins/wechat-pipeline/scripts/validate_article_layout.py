#!/usr/bin/env python3
"""Validate a gzh-design HTML artifact and its pipeline layout manifest."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timedelta
from urllib.parse import unquote, urlparse
from html.parser import HTMLParser
from pathlib import Path

from protocol_version import PROTOCOL_VERSION


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

from shared.file_utils import is_relevant_file
from shared.hashing import sha256_file, tree_sha256
from shared.html_contracts import PLACEHOLDER_PATTERNS
from shared.jsonio import inside
from shared.text_preservation import preservation_report

GZH_ROOT = PLUGIN_ROOT / "skills" / "gzh-design"
UPSTREAM_VALIDATOR = GZH_ROOT / "scripts" / "validate_gzh_html.py"
LOCK_PATH = PLUGIN_ROOT / "third_party" / "gzh-design.lock.json"
MAX_HTML_BYTES = 1_000_000
FORBIDDEN_DOCUMENT = re.compile(r"<!doctype\b|</?(?:html|head|body)(?:\s|>)", re.I)
ENGAGEMENT_PATTERNS = (
    re.compile(r"点赞.*在看.*(?:转发|分享)"),
    re.compile(r"(?:欢迎|记得|别忘了)(?:关注|点赞|点在看|转发|分享)"),
    re.compile(r"点个在看"),
    re.compile(r"关注(?:我|我们|本号|公众号)"),
    re.compile(r"(?:转发|分享)给(?:朋友|身边|更多)"),
    re.compile(r"我们下篇见"),
)
LAYOUT_ALLOWED_STATUSES = {"typesetting", "layout_ready", "publish_ready", "publishing", "published"}


class VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.images: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "img":
            source = dict(attrs).get("src")
            if source:
                self.images.append(source)


def _extract_local_images(html: str, errors: list[str]) -> list[Path]:
    parser = VisibleText()
    parser.feed(html)
    images: list[Path] = []
    for value in parser.images:
        parsed = urlparse(value)
        if parsed.scheme or parsed.netloc:
            errors.append(f"pre-publish HTML image must be a local absolute path: {value}")
            continue
        image = Path(unquote(parsed.path)).expanduser()
        if not image.is_absolute():
            errors.append(f"pre-publish HTML image path must be absolute: {value}")
            continue
        images.append(image.resolve())
    return images


def normalize_visible_text(value: str) -> str:
    table = str.maketrans({
        ",": "，", ";": "；", "!": "！", "?": "？", ":": "：",
        '"': "Ｑ", "“": "Ｑ", "”": "Ｑ", "「": "Ｑ", "」": "Ｑ",
        "'": "Ｓ", "‘": "Ｓ", "’": "Ｓ",
    })
    return re.sub(r"\s+", "", value.translate(table))


def missing_source_segments(markdown_path: Path, html: str) -> tuple[int, list[str]]:
    report = preservation_report(
        markdown_path.read_text(encoding="utf-8", errors="replace"),
        html,
        candidate_is_html=True,
        skip_h1=True,
        split_phrases=True,
    )
    return report["source_segment_count"], [
        str(item.get("normalized") or item.get("preview") or "")
        for item in report["missing_source_segments"]
    ]


def generated_engagement_footers(markdown_path: Path, html: str) -> list[str]:
    parser = VisibleText()
    parser.feed(html)
    visible = normalize_visible_text("".join(parser.parts))
    source = normalize_visible_text(markdown_path.read_text(encoding="utf-8", errors="replace"))
    generated: list[str] = []
    for pattern in ENGAGEMENT_PATTERNS:
        visible_matches = list(pattern.finditer(visible))
        source_count = sum(1 for _ in pattern.finditer(source))
        if len(visible_matches) > source_count:
            generated.append(visible_matches[source_count].group(0))
    tail = visible[-400:]
    if "我是" in tail and visible.count("我是") > source.count("我是"):
        generated.append("generated author introduction")
    return generated


def validate_native_output(
    html_path: Path,
    original_path: Path,
    markdown_path: Path,
    expected_body_images: list[Path],
) -> dict:
    """Validate publish-integration facts before accepting a native Skill result."""
    result = validate(html_path, None)
    errors = list(result.get("errors", []))
    warnings = list(result.get("warnings", []))
    html = html_path.read_text(encoding="utf-8", errors="replace") if html_path.is_file() else ""

    source_segment_count = 0
    missing_segments: list[str] = []
    if original_path.is_file():
        source_segment_count, missing_segments = missing_source_segments(original_path, html)
        if missing_segments:
            sample = ", ".join(repr(value[:80]) for value in missing_segments[:5])
            errors.append(f"HTML is missing {len(missing_segments)} source text segments: {sample}")

    generated_footers: list[str] = []
    if markdown_path.is_file():
        generated_footers = generated_engagement_footers(markdown_path, html)
        if generated_footers:
            errors.append(
                "HTML adds prohibited author/engagement footer text: "
                + ", ".join(repr(value[:80]) for value in generated_footers[:5])
            )

    actual_images = _extract_local_images(html, errors)
    expected_images = [value.expanduser().resolve() for value in expected_body_images]
    if actual_images != expected_images:
        errors.append("HTML body image paths and order must exactly match designer manifest outputs")

    result.update({
        "ok": not errors and not warnings,
        "source_segment_count": source_segment_count,
        "missing_source_segments": missing_segments,
        "generated_engagement_footers": generated_footers,
        "errors": errors,
        "warnings": warnings,
    })
    return result


def load_upstream_validator():
    spec = importlib.util.spec_from_file_location("gzh_design_validate", UPSTREAM_VALIDATOR)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load upstream validator: {UPSTREAM_VALIDATOR}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def state_entered_at(canonical: Path, target: str) -> datetime | None:
    events = canonical / ".pipeline" / "events.jsonl"
    try:
        records = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines() if line]
    except (OSError, json.JSONDecodeError):
        return None
    for record in records:
        details = record.get("details") if isinstance(record.get("details"), dict) else {}
        if record.get("event") == "status.changed" and details.get("to") == target:
            try:
                entered = datetime.fromisoformat(str(record.get("occurred_at", "")).replace("Z", "+00:00"))
            except ValueError:
                return None
            return entered if entered.tzinfo is not None else None
    return None


def validate_manifest(manifest_path: Path, html_path: Path, errors: list[str]) -> dict:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        errors.append(f"unable to read layout manifest: {err}")
        return {}
    required = {"schema_version", "protocol_version", "run_id", "mode", "canonical_output_dir", "source", "skill_run", "skill_contract", "decision", "metadata", "output"}
    missing = required - set(manifest)
    if missing:
        errors.append(f"layout manifest missing fields: {sorted(missing)}")
    if manifest.get("schema_version") != 1:
        errors.append("layout manifest schema_version must be 1")
    if manifest.get("protocol_version") != PROTOCOL_VERSION:
        errors.append(f"layout manifest protocol_version must be {PROTOCOL_VERSION}")
    if manifest.get("mode") != "news":
        errors.append("layout manifest mode must be news")

    canonical = Path(str(manifest.get("canonical_output_dir", ""))).expanduser().resolve()
    if manifest_path != canonical / ".pipeline" / "layout.json":
        errors.append("layout manifest must be <canonical_output_dir>/.pipeline/layout.json")
    if html_path != canonical / "article-body.html":
        errors.append("layout HTML must be <canonical_output_dir>/article-body.html")
    run_path = canonical / ".pipeline" / "run.json"
    if not run_path.is_file():
        errors.append(f"run context not found: {run_path}")
    else:
        run = json.loads(run_path.read_text(encoding="utf-8"))
        if run.get("run_id") != manifest.get("run_id"):
            errors.append("layout run_id does not match run.json")
        if run.get("protocol_version") != PROTOCOL_VERSION:
            errors.append("run.json protocol_version does not match current protocol")
        if run.get("status") not in LAYOUT_ALLOWED_STATUSES:
            errors.append("layout validation requires the run to have entered typesetting")
    typesetting_at = state_entered_at(canonical, "typesetting")
    if typesetting_at is None:
        errors.append("run event log does not prove entry into typesetting")
    else:
        for label, artifact in (("layout manifest", manifest_path), ("layout HTML", html_path)):
            if artifact.is_file():
                written_at = datetime.fromtimestamp(artifact.stat().st_mtime, tz=typesetting_at.tzinfo)
                if written_at < typesetting_at - timedelta(seconds=1):
                    errors.append(f"{label} was written before the run entered typesetting")

    source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    markdown_path = Path(str(source.get("markdown_path", ""))).expanduser().resolve()
    designer_path = canonical / ".pipeline" / "manifest.json"
    try:
        designer_source = json.loads(designer_path.read_text(encoding="utf-8"))
        layout_input = designer_source.get("layout_input") if isinstance(designer_source.get("layout_input"), dict) else {}
        expected_markdown = Path(str(layout_input.get("path", ""))).expanduser().resolve()
    except (OSError, json.JSONDecodeError):
        expected_markdown = canonical / ".pipeline" / "missing-layout-input.md"
    if markdown_path != expected_markdown:
        errors.append("layout source.markdown_path must equal the native illustrator layout_input")
    elif not markdown_path.is_file():
        errors.append(f"layout source markdown not found: {markdown_path}")
    elif sha256_file(markdown_path) != source.get("markdown_sha256"):
        errors.append("layout source markdown hash mismatch")
    original_path = Path(str(source.get("original_path", ""))).expanduser().resolve()
    expected_original = canonical / ".pipeline" / "input.md"
    if original_path != expected_original or not original_path.is_file():
        errors.append("layout source.original_path must be the sealed .pipeline/input.md")
    elif sha256_file(original_path) != source.get("original_sha256"):
        errors.append("layout source original hash mismatch")

    contract = manifest.get("skill_contract") if isinstance(manifest.get("skill_contract"), dict) else {}
    if contract.get("skill_name") != "gzh-design":
        errors.append("layout skill_contract.skill_name must be gzh-design")
    skill_path = Path(str(contract.get("skill_path", ""))).expanduser().resolve()
    if skill_path != GZH_ROOT / "SKILL.md":
        errors.append(f"layout skill_path must be the bundled Skill: {GZH_ROOT / 'SKILL.md'}")
    elif sha256_file(skill_path) != contract.get("skill_sha256"):
        errors.append("layout skill_sha256 mismatch")
    try:
        lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        errors.append(f"unable to read bundled gzh-design lock: {err}")
        lock = {}
    if contract.get("upstream_commit") != lock.get("commit"):
        errors.append("layout upstream_commit does not match the bundled lock")
    actual_tree = tree_sha256(
        GZH_ROOT,
        lambda path: path.is_file() and is_relevant_file(path),
    )
    if actual_tree != lock.get("tree_sha256"):
        errors.append("bundled gzh-design tree hash does not match its lock")
    if contract.get("tree_sha256") != actual_tree:
        errors.append("layout skill_contract.tree_sha256 does not match the bundled Skill")

    layout_output = manifest.get("output") if isinstance(manifest.get("output"), dict) else {}
    skill_run_binding = manifest.get("skill_run") if isinstance(manifest.get("skill_run"), dict) else {}
    skill_run_path = Path(str(skill_run_binding.get("path", ""))).expanduser().resolve()
    expected_skill_run = canonical / ".pipeline" / "layout-skill-run.json"
    if skill_run_path != expected_skill_run:
        errors.append(f"layout skill_run.path must be {expected_skill_run}")
    elif not skill_run_path.is_file():
        errors.append(f"layout Skill receipt is missing: {skill_run_path}")
    elif skill_run_binding.get("sha256") != sha256_file(skill_run_path):
        errors.append("layout Skill receipt binding hash mismatch")
    else:
        try:
            skill_run = json.loads(skill_run_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            errors.append("layout Skill receipt is not valid JSON")
        else:
            if skill_run.get("status") != "success":
                errors.append("layout Skill receipt does not prove successful completion")
            if skill_run.get("protocol_version") != PROTOCOL_VERSION:
                errors.append("layout Skill receipt protocol_version mismatch")
            if skill_run.get("run_id") != manifest.get("run_id"):
                errors.append("layout Skill receipt run_id mismatch")
            if skill_run.get("skill_identifier") != "wechat-pipeline:gzh-design":
                errors.append("layout Skill receipt does not prove native gzh-design invocation")
            if Path(str(skill_run.get("skill_path", ""))).expanduser().resolve() != GZH_ROOT / "SKILL.md":
                errors.append("layout Skill receipt path does not match bundled gzh-design")
            elif skill_run.get("skill_sha256") != sha256_file(GZH_ROOT / "SKILL.md"):
                errors.append("layout Skill receipt hash mismatch")
            if skill_run.get("invocation_method") != "native-skill":
                errors.append("layout Skill receipt invocation_method must be native-skill")
            if Path(str(skill_run.get("input_path", ""))).expanduser().resolve() != markdown_path:
                errors.append("layout Skill receipt input does not match layout source markdown")
            if skill_run.get("input_sha256") != source.get("markdown_sha256"):
                errors.append("layout Skill receipt input hash mismatch")
            returned = skill_run.get("returned_output") if isinstance(skill_run.get("returned_output"), dict) else {}
            native_path = Path(str(returned.get("path", ""))).expanduser().resolve()
            if not inside(native_path, canonical):
                errors.append("layout Skill native HTML escapes canonical_output_dir")
            elif not native_path.is_file() or returned.get("sha256") != sha256_file(native_path):
                errors.append("layout Skill native HTML is missing or changed")
            elif layout_output.get("native_output_path") and Path(str(layout_output.get("native_output_path"))).expanduser().resolve() != native_path:
                errors.append("layout output does not bind the native gzh-design HTML")
            elif layout_output.get("native_output_sha256") != returned.get("sha256"):
                errors.append("layout native output hash mismatch")

    decision = manifest.get("decision") if isinstance(manifest.get("decision"), dict) else {}
    if decision.get("content_policy") != "preserve-visible-text":
        errors.append("layout decision.content_policy must be preserve-visible-text")
    if decision.get("engagement_footer_policy") != "no-generated-engagement-footer":
        errors.append("layout decision.engagement_footer_policy must prohibit generated engagement footers")

    metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
    if not str(metadata.get("title", "")).strip():
        errors.append("layout metadata.title must be non-empty")
    cover_value = metadata.get("cover_path")
    if cover_value:
        cover_path = Path(str(cover_value)).expanduser().resolve()
        if not inside(cover_path, canonical) or not cover_path.is_file():
            errors.append("layout metadata.cover_path must be an existing file inside canonical_output_dir")

    try:
        designer = json.loads(designer_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        errors.append(f"unable to read designer manifest for layout binding: {err}")
    else:
        images = [image for image in designer.get("images", []) if isinstance(image, dict)]
        covers = [Path(str(image.get("output_path", ""))).expanduser().resolve() for image in images if image.get("kind") == "cover"]
        bodies = [Path(str(image.get("output_path", ""))).expanduser().resolve() for image in images if image.get("kind") != "cover"]
        if len(covers) != 1 or not cover_value or Path(str(cover_value)).expanduser().resolve() != covers[0]:
            errors.append("layout cover_path must exactly match the designer manifest cover output")
        actual_images = _extract_local_images(
            html_path.read_text(encoding="utf-8", errors="replace"),
            errors,
        )
        if actual_images != bodies:
            errors.append("HTML body image paths and order must exactly match designer manifest outputs")

    output = layout_output
    output_path = Path(str(output.get("html_path", ""))).expanduser().resolve()
    if output_path != html_path:
        errors.append("layout output.html_path does not match the validated HTML")
    elif html_path.is_file() and sha256_file(html_path) != output.get("html_sha256"):
        errors.append("layout output HTML hash mismatch")
    generated_at_value = output.get("generated_at")
    try:
        generated_at = datetime.fromisoformat(str(generated_at_value).replace("Z", "+00:00"))
    except ValueError:
        generated_at = None
        errors.append("layout output.generated_at must be ISO-8601")
    if generated_at is not None and (generated_at.tzinfo is None or generated_at.utcoffset() is None):
        errors.append("layout output.generated_at must include an explicit timezone offset")
    elif generated_at is not None and typesetting_at is not None and generated_at < typesetting_at:
        errors.append("layout output.generated_at predates the typesetting state")
    return manifest


def validate(html_path: Path, manifest_path: Path | None) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    if not html_path.is_file():
        return {"ok": False, "errors": [f"HTML not found: {html_path}"], "warnings": []}
    html = html_path.read_text(encoding="utf-8", errors="replace")
    size = len(html.encode("utf-8"))
    if size > MAX_HTML_BYTES:
        errors.append(f"HTML exceeds {MAX_HTML_BYTES} UTF-8 bytes: {size}")
    if FORBIDDEN_DOCUMENT.search(html):
        errors.append("HTML must be a body fragment without doctype/html/head/body")
    if not html.lstrip().startswith("<section"):
        errors.append("HTML body fragment must start with the gzh-design root <section>")
    for pattern in PLACEHOLDER_PATTERNS:
        match = pattern.search(html)
        if match:
            errors.append(f"unresolved placeholder: {match.group(0)}")
    upstream = load_upstream_validator()
    upstream_errors, upstream_warnings, leaf_count = upstream.validate(html, str(html_path))
    errors.extend(upstream_errors)
    warnings.extend(upstream_warnings)
    source_segment_count = 0
    missing_segments: list[str] = []
    generated_footers: list[str] = []
    if manifest_path:
        manifest = validate_manifest(manifest_path, html_path, errors)
        source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
        original_path = Path(str(source.get("original_path", ""))).expanduser().resolve()
        if original_path.is_file():
            source_segment_count, missing_segments = missing_source_segments(original_path, html)
            if missing_segments:
                sample = ", ".join(repr(value[:80]) for value in missing_segments[:5])
                errors.append(f"HTML is missing {len(missing_segments)} source text segments: {sample}")
        markdown_path = Path(str(source.get("markdown_path", ""))).expanduser().resolve()
        if markdown_path.is_file():
            generated_footers = generated_engagement_footers(markdown_path, html)
            if generated_footers:
                errors.append(
                    "HTML adds prohibited author/engagement footer text: "
                    + ", ".join(repr(value[:80]) for value in generated_footers[:5])
                )
    return {
        "ok": not errors and not warnings,
        "protocol_version": PROTOCOL_VERSION,
        "html_path": str(html_path),
        "html_sha256": sha256_file(html_path),
        "html_bytes": size,
        "span_leaf_count": leaf_count,
        "source_segment_count": source_segment_count,
        "missing_source_segments": missing_segments,
        "generated_engagement_footers": generated_footers,
        "errors": errors,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("html", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    html_path = args.html.expanduser().resolve()
    manifest_path = args.manifest.expanduser().resolve() if args.manifest else None
    result = validate(html_path, manifest_path)
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, output)
    print(payload, end="")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
