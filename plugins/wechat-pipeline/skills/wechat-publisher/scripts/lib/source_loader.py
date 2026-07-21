"""Source file loaders for newspic mode: .md / .yaml / .json."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PLUGIN_ROOT))

from .errors import CONTENT_MAX_CHARS, MAX_IMAGES, PublishError, TITLE_MAX_CHARS
from shared.markdown_meta import (
    first_h1,
    frontmatter,
    markdown_body,
    parse_simple_yaml as _parse_simple_yaml,
    title as markdown_title,
)


def parse_simple_yaml(text: str) -> dict:
    """Expose the shared flat parser through the publisher's public error type."""
    try:
        return _parse_simple_yaml(text)
    except ValueError as err:
        raise PublishError(str(err)) from err


def parse_markdown(text: str) -> dict:
    """Parse source.md format: optional YAML frontmatter, then first H1 = title, rest = content."""
    data = frontmatter(text)
    body = markdown_body(text)
    body_lines = body.splitlines()
    parsed_title = first_h1(text)
    content_start = 0
    for idx, line in enumerate(body_lines):
        if not line.strip():
            continue
        if line.lstrip().startswith("# "):
            content_start = idx + 1
        else:
            content_start = idx
        break
    content = "\n".join(body_lines[content_start:]).strip()
    if parsed_title and not data.get("title"):
        data["title"] = parsed_title
    if content and not data.get("content"):
        data["content"] = content
    return data


def load_source(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    try:
        if suffix == ".json":
            data = json.loads(text)
        elif suffix in (".yaml", ".yml"):
            data = parse_simple_yaml(text)
        elif suffix in (".md", ".markdown"):
            data = parse_markdown(text)
        else:
            raise PublishError("source must be .md, .yaml, .yml, or .json")
    except ValueError as err:
        raise PublishError(str(err)) from err
    if not isinstance(data, dict):
        raise PublishError("source must contain an object at the top level")
    return data


def validate_newspic_source(source: dict, base_dir: Path) -> tuple[str, str, str, str, list[Path]]:
    """Validate the newspic source fields. Returns (title, content, author, digest, images)."""
    title = str(source.get("title") or "").strip()
    content = str(source.get("content") or "").strip()
    author = str(source.get("author") or "").strip()
    digest = str(source.get("digest") or "").strip()
    images_raw = source.get("images")
    if not title:
        raise PublishError("title is required")
    if len(title) > TITLE_MAX_CHARS:
        raise PublishError(f"title must be at most {TITLE_MAX_CHARS} characters, got {len(title)}")
    if not content:
        raise PublishError("content is required")
    if len(content) > CONTENT_MAX_CHARS:
        raise PublishError(
            f"content must be at most {CONTENT_MAX_CHARS} characters, got {len(content)}; "
            "refuse to truncate user text"
        )
    if not isinstance(images_raw, list):
        raise PublishError("images must be a list")
    if not 1 <= len(images_raw) <= MAX_IMAGES:
        raise PublishError(f"images must contain 1-{MAX_IMAGES} paths")
    images: list[Path] = []
    for item in images_raw:
        p = Path(str(item)).expanduser()
        if not p.is_absolute():
            p = base_dir / p
        if not p.exists() or not p.is_file():
            raise PublishError(f"image not found: {p}")
        images.append(p)
    return title, content, author, digest, images
