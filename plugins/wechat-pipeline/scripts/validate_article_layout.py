#!/usr/bin/env python3
"""Validate a gzh-design HTML artifact and its pipeline layout manifest."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
from urllib.parse import unquote, urlparse
from html.parser import HTMLParser
from pathlib import Path

from protocol_version import PROTOCOL_VERSION


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

from shared.file_utils import is_relevant_file

GZH_ROOT = PLUGIN_ROOT / "skills" / "gzh-design"
UPSTREAM_VALIDATOR = GZH_ROOT / "scripts" / "validate_gzh_html.py"
LOCK_PATH = PLUGIN_ROOT / "third_party" / "gzh-design.lock.json"
MAX_HTML_BYTES = 1_000_000
FORBIDDEN_DOCUMENT = re.compile(r"<!doctype\b|</?(?:html|head|body)(?:\s|>)", re.I)
PLACEHOLDERS = (
    re.compile(r"\{\{[^{}]+\}\}"),
    re.compile(r"(?:图片|动图|封面|名片)(?:URL|地址)"),
    re.compile(r"【(?:插入|待补)[^】]*】"),
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(
        path for path in root.rglob("*")
        if path.is_file() and is_relevant_file(path)
    ):
        relative = path.relative_to(root).as_posix().encode()
        contents = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(contents).to_bytes(8, "big"))
        digest.update(contents)
    return digest.hexdigest()


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


def normalize_visible_text(value: str) -> str:
    table = str.maketrans({
        ",": "，", ";": "；", "!": "！", "?": "？", ":": "：",
        '"': "Ｑ", "“": "Ｑ", "”": "Ｑ", "'": "Ｓ", "‘": "Ｓ", "’": "Ｓ",
    })
    return re.sub(r"\s+", "", value.translate(table))


def markdown_segments(markdown: str) -> list[str]:
    lines = markdown.splitlines()
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                lines = lines[index + 1:]
                break
    segments: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("```") or re.fullmatch(r"[-*_]{3,}", line):
            continue
        if re.match(r"^#(?!#)\s+", line):
            continue  # The article title is carried by draft metadata, not the body fragment.
        if re.fullmatch(r"\|?[\s:|-]+\|?", line):
            continue
        line = re.sub(r"^(?:#{1,6}|>|[-+*]|\d+[.)])\s*", "", line)
        line = re.sub(r"!\[([^]]*)\]\([^)]+\)", r"\1", line)
        line = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", line)
        line = re.sub(r"</?u>", "", line, flags=re.I)
        line = re.sub(r"(?:\*\*|__|~~|==|\+\+|`)", "", line)
        cells = [cell.strip() for cell in line.strip("|").split("|")] if "|" in line else [line]
        for cell in cells:
            # Formatting may split one prose line into multiple paragraphs or list
            # items. Sentence/phrase-sized segments still catch omissions without
            # requiring the typesetter to preserve the original tag boundaries.
            for phrase in re.split(r"[。！？；.!?;,，、]+", cell):
                normalized = normalize_visible_text(phrase)
                if len(normalized) >= 2:
                    segments.append(normalized)
    return segments


def missing_source_segments(markdown_path: Path, html: str) -> tuple[int, list[str]]:
    parser = VisibleText()
    parser.feed(html)
    visible = normalize_visible_text("".join(parser.parts))
    segments = markdown_segments(markdown_path.read_text(encoding="utf-8", errors="replace"))
    return len(segments), [segment for segment in segments if segment not in visible]


def load_upstream_validator():
    spec = importlib.util.spec_from_file_location("gzh_design_validate", UPSTREAM_VALIDATOR)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load upstream validator: {UPSTREAM_VALIDATOR}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def validate_manifest(manifest_path: Path, html_path: Path, errors: list[str]) -> dict:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        errors.append(f"unable to read layout manifest: {err}")
        return {}
    required = {"schema_version", "protocol_version", "run_id", "mode", "canonical_output_dir", "source", "skill_contract", "decision", "metadata", "output"}
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

    source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    markdown_path = Path(str(source.get("markdown_path", ""))).expanduser().resolve()
    expected_markdown = canonical / "content.md"
    if markdown_path != expected_markdown:
        errors.append("layout source.markdown_path must be <canonical_output_dir>/content.md")
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
    files_read = contract.get("files_read")
    if not isinstance(files_read, list) or len(files_read) < 4:
        errors.append("layout files_read must include SKILL, theme index, selected theme, and common components")
    else:
        for value in files_read:
            path = Path(str(value)).expanduser().resolve()
            if not inside(path, GZH_ROOT) or not path.is_file():
                errors.append(f"layout files_read contains an invalid path: {path}")

    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    if contract.get("upstream_commit") != lock.get("commit"):
        errors.append("layout upstream_commit does not match the bundled lock")
    actual_tree = tree_sha256(GZH_ROOT)
    if actual_tree != lock.get("tree_sha256"):
        errors.append("bundled gzh-design tree hash does not match its lock")
    if contract.get("tree_sha256") != actual_tree:
        errors.append("layout skill_contract.tree_sha256 does not match the bundled Skill")

    decision = manifest.get("decision") if isinstance(manifest.get("decision"), dict) else {}
    if decision.get("theme_source") not in {"user", "auto"}:
        errors.append("layout decision.theme_source must be user or auto")
    if not decision.get("theme") or not decision.get("article_type"):
        errors.append("layout decision must include theme and article_type")
    if decision.get("content_policy") != "preserve-visible-text":
        errors.append("layout content_policy must be preserve-visible-text")

    metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
    if not str(metadata.get("title", "")).strip():
        errors.append("layout metadata.title must be non-empty")
    cover_value = metadata.get("cover_path")
    if cover_value:
        cover_path = Path(str(cover_value)).expanduser().resolve()
        if not inside(cover_path, canonical) or not cover_path.is_file():
            errors.append("layout metadata.cover_path must be an existing file inside canonical_output_dir")

    designer_path = canonical / ".pipeline" / "manifest.json"
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
        parser = VisibleText()
        parser.feed(html_path.read_text(encoding="utf-8", errors="replace"))
        actual_images: list[Path] = []
        for value in parser.images:
            parsed = urlparse(value)
            if parsed.scheme or parsed.netloc:
                errors.append(f"pre-publish HTML image must be a local absolute path: {value}")
                continue
            path = Path(unquote(parsed.path)).expanduser()
            if not path.is_absolute():
                errors.append(f"pre-publish HTML image path must be absolute: {value}")
                continue
            actual_images.append(path.resolve())
        if actual_images != bodies:
            errors.append("HTML body image paths and order must exactly match designer manifest outputs")

    output = manifest.get("output") if isinstance(manifest.get("output"), dict) else {}
    output_path = Path(str(output.get("html_path", ""))).expanduser().resolve()
    if output_path != html_path:
        errors.append("layout output.html_path does not match the validated HTML")
    elif html_path.is_file() and sha256_file(html_path) != output.get("html_sha256"):
        errors.append("layout output HTML hash mismatch")
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
    for pattern in PLACEHOLDERS:
        match = pattern.search(html)
        if match:
            errors.append(f"unresolved placeholder: {match.group(0)}")
    upstream = load_upstream_validator()
    upstream_errors, upstream_warnings, leaf_count = upstream.validate(html, str(html_path))
    errors.extend(upstream_errors)
    warnings.extend(upstream_warnings)
    source_segment_count = 0
    missing_segments: list[str] = []
    if manifest_path:
        manifest = validate_manifest(manifest_path, html_path, errors)
        source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
        original_path = Path(str(source.get("original_path", ""))).expanduser().resolve()
        if original_path.is_file():
            source_segment_count, missing_segments = missing_source_segments(original_path, html)
            if missing_segments:
                sample = ", ".join(repr(value[:80]) for value in missing_segments[:5])
                errors.append(f"HTML is missing {len(missing_segments)} source text segments: {sample}")
    return {
        "ok": not errors and not warnings,
        "protocol_version": PROTOCOL_VERSION,
        "html_path": str(html_path),
        "html_sha256": sha256_file(html_path),
        "html_bytes": size,
        "span_leaf_count": leaf_count,
        "source_segment_count": source_segment_count,
        "missing_source_segments": missing_segments,
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
