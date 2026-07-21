"""Inspect gzh-design HTML fragments and replace body image sources safely."""

from __future__ import annotations

import hashlib
import html as html_module
import mimetypes
import re
import sys
import tempfile
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

PLUGIN_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PLUGIN_ROOT))

from lib.errors import PublishError, USER_AGENT
from shared.html_contracts import PLACEHOLDER_PATTERNS


MAX_HTML_BYTES = 1_000_000
MAX_REMOTE_IMAGE_BYTES = 20_000_000
IMAGE_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".webp"}
DOCUMENT_TAG = re.compile(r"<!doctype\b|</?(?:html|head|body)(?:\s|>)", re.I)
IMG_TAG = re.compile(r"<img\b[^>]*>", re.I | re.S)
SRC_ATTR = re.compile(
    r"(?P<prefix>(?<![\w:-])src\s*=\s*)(?:(?P<quote>['\"])(?P<quoted>.*?)(?P=quote)|(?P<bare>[^\s>]+))",
    re.I | re.S,
)


class ImageCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "img":
            return
        source = dict(attrs).get("src")
        if not source:
            raise PublishError("every <img> in article HTML must have a non-empty src")
        self.sources.append(source)

    handle_startendtag = handle_starttag


def inspect_html(html: str) -> list[str]:
    if not html.strip():
        raise PublishError("article HTML is empty")
    size = len(html.encode("utf-8"))
    if size > MAX_HTML_BYTES:
        raise PublishError(f"article HTML exceeds {MAX_HTML_BYTES} UTF-8 bytes: {size}")
    if DOCUMENT_TAG.search(html):
        raise PublishError("article HTML must be a body fragment without doctype/html/head/body")
    for pattern in PLACEHOLDER_PATTERNS:
        match = pattern.search(html)
        if match:
            raise PublishError(f"article HTML contains an unresolved placeholder: {match.group(0)}")
    collector = ImageCollector()
    try:
        collector.feed(html)
        collector.close()
    except PublishError:
        raise
    except Exception as err:
        raise PublishError(f"unable to parse article HTML: {err}") from err
    return collector.sources


def _is_wechat_image(source: str) -> bool:
    parsed = urllib.parse.urlparse(source)
    host = (parsed.hostname or "").lower()
    return parsed.scheme in {"http", "https"} and host.startswith("mmbiz") and host.endswith(
        (".qpic.cn", ".qlogo.cn")
    )


def _validate_image_bytes(data: bytes, source: str) -> None:
    valid = (
        data.startswith(b"\x89PNG\r\n\x1a\n")
        or data.startswith((b"GIF87a", b"GIF89a"))
        or data.startswith(b"\xff\xd8\xff")
        or len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    )
    if not valid:
        raise PublishError(f"body image is not a valid PNG/JPEG/GIF/WebP file: {source}")


def _local_image(source: str, base_dir: Path) -> Path:
    parsed = urllib.parse.urlsplit(source)
    if parsed.scheme and parsed.scheme != "file":
        raise PublishError(f"unsupported image source scheme: {source}")
    raw_path = urllib.parse.unquote(parsed.path if parsed.scheme == "file" else source.split("?", 1)[0].split("#", 1)[0])
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    path = path.resolve()
    if not path.is_file():
        raise PublishError(f"body image not found: {source} (resolved to {path})")
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        raise PublishError(f"unsupported body image format: {path.suffix or '(none)'} for {path}")
    _validate_image_bytes(path.read_bytes(), str(path))
    return path


def _download_image(source: str, temp_dir: Path) -> Path:
    parsed = urllib.parse.urlparse(source)
    suffix = Path(parsed.path).suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        suffix = ".img"
    target = temp_dir / f"remote-{hashlib.sha256(source.encode()).hexdigest()[:16]}{suffix}"
    request = urllib.request.Request(source, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read(MAX_REMOTE_IMAGE_BYTES + 1)
            content_type = response.headers.get_content_type()
    except Exception as err:
        raise PublishError(f"unable to download body image {source}: {err}") from err
    if not data:
        raise PublishError(f"downloaded body image is empty: {source}")
    if len(data) > MAX_REMOTE_IMAGE_BYTES:
        raise PublishError(f"remote body image exceeds {MAX_REMOTE_IMAGE_BYTES} bytes: {source}")
    if target.suffix == ".img":
        guessed = mimetypes.guess_extension(content_type) or ""
        if guessed == ".jpe":
            guessed = ".jpg"
        if guessed not in IMAGE_SUFFIXES:
            raise PublishError(f"remote body image has unsupported content type {content_type}: {source}")
        target = target.with_suffix(guessed)
    _validate_image_bytes(data, source)
    target.write_bytes(data)
    return target


def rewrite_image_sources(html: str, replacements: dict[str, str]) -> str:
    """Rewrite src only inside parsed-and-counted img start tags."""
    expected_sources = inspect_html(html)
    rewritten_sources: list[str] = []

    def replace_tag(tag_match: re.Match[str]) -> str:
        tag = tag_match.group(0)
        source_match = SRC_ATTR.search(tag)
        if not source_match:
            raise PublishError("unable to locate src in parsed <img> tag")
        source = html_module.unescape(source_match.group("quoted") or source_match.group("bare") or "")
        rewritten_sources.append(source)
        replacement = replacements.get(source)
        if replacement is None:
            return tag
        quote = source_match.group("quote") or '"'
        escaped = html_module.escape(replacement, quote=True)
        new_attr = f"{source_match.group('prefix')}{quote}{escaped}{quote}"
        return tag[:source_match.start()] + new_attr + tag[source_match.end():]

    output = IMG_TAG.sub(replace_tag, html)
    if rewritten_sources != expected_sources:
        raise PublishError("article HTML image scan was inconsistent; refusing to rewrite")
    return output


def upload_html_images(
    html: str,
    base_dir: Path,
    uploader: Callable[[Path], str],
    *,
    existing: dict[str, str] | None = None,
    on_uploaded: Callable[[str, str], None] | None = None,
) -> tuple[str, int]:
    sources = inspect_html(html)
    replacements: dict[str, str] = {}
    uploaded: dict[str, str] = dict(existing or {})
    with tempfile.TemporaryDirectory(prefix="wechat-publisher-html-") as temporary:
        temp_dir = Path(temporary)
        for index, source in enumerate(sources, start=1):
            if _is_wechat_image(source):
                continue
            if source in uploaded:
                replacements[source] = uploaded[source]
                continue
            parsed = urllib.parse.urlparse(source)
            image = _download_image(source, temp_dir) if parsed.scheme in {"http", "https"} else _local_image(source, base_dir)
            print(f"[{index}/{len(sources)}] upload body image {image.name}", flush=True)
            mmbiz_url = uploader(image)
            uploaded[source] = mmbiz_url
            replacements[source] = mmbiz_url
            if on_uploaded:
                on_uploaded(source, mmbiz_url)
    return rewrite_image_sources(html, replacements), len(uploaded)
