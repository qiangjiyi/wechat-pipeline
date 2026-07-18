"""Deterministic checks for WeChat draft/get responses."""

from __future__ import annotations

import re
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlparse


class _DraftHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.images: list[str] = []

    def handle_data(self, data: str) -> None:
        self.text.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "img":
            # WeChat draft/get stores body images with data-src (lazy-load
            # attribute), while freshly submitted HTML uses src. Accept either
            # so the read-back image count reflects the actual draft content.
            attrs_dict = dict(attrs)
            source = attrs_dict.get("src") or attrs_dict.get("data-src")
            if source:
                self.images.append(source)


def _normalized_text(html: str) -> str:
    parser = _DraftHTMLParser()
    parser.feed(html)
    return re.sub(r"\s+", "", "".join(parser.text))


def _article(draft: dict) -> tuple[dict | None, list[str]]:
    errors: list[str] = []
    items = draft.get("news_item")
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        errors.append("draft/get response is missing news_item[0]")
        return None, errors
    return items[0], errors


def verify_article_draft(
    draft: dict,
    *,
    title: str,
    summary: str,
    source_html: str,
    expected_image_count: int,
) -> dict:
    item, errors = _article(draft)
    if item is None:
        return {"ok": False, "errors": errors}
    if item.get("title") != title:
        errors.append("draft title does not match the submitted title")
    if summary and item.get("digest", "") != summary[:120]:
        errors.append("draft digest does not match the submitted summary")
    content = item.get("content")
    if not isinstance(content, str) or not content:
        errors.append("draft content is empty")
        content = ""
    expected_text = _normalized_text(source_html)
    actual_text = _normalized_text(content)
    if expected_text != actual_text:
        errors.append("draft visible text does not exactly match the submitted article text")
    parser = _DraftHTMLParser()
    parser.feed(content)
    if len(parser.images) != expected_image_count:
        errors.append(
            f"draft image count mismatch: expected {expected_image_count}, got {len(parser.images)}"
        )
    for source in parser.images:
        host = (urlparse(source).hostname or "").lower()
        if host not in {"mmbiz.qpic.cn", "mmbiz.qlogo.cn"}:
            errors.append(f"draft contains a non-WeChat image URL: {source}")
    return {
        "ok": not errors,
        "status": "verified" if not errors else "failed",
        "method": "draft/get",
        "verified_at": datetime.now().astimezone().isoformat(),
        "errors": errors,
        "title": item.get("title", ""),
        "image_count": len(parser.images),
    }


def verify_newspic_draft(
    draft: dict,
    *,
    title: str,
    content: str,
    expected_image_count: int,
    expected_image_media_ids: list[str] | None = None,
) -> dict:
    item, errors = _article(draft)
    if item is None:
        return {"ok": False, "errors": errors}
    if item.get("title") != title:
        errors.append("draft title does not match the submitted title")
    if item.get("content") != content:
        errors.append("draft content does not match the submitted content")
    image_info = item.get("image_info")
    image_list = image_info.get("image_list") if isinstance(image_info, dict) else None
    actual_count = len(image_list) if isinstance(image_list, list) else 0
    if actual_count != expected_image_count:
        errors.append(
            f"draft image count mismatch: expected {expected_image_count}, got {actual_count}"
        )
    actual_media_ids = [
        str(item.get("image_media_id"))
        for item in image_list or []
        if isinstance(item, dict) and item.get("image_media_id")
    ]
    if expected_image_media_ids and actual_media_ids != expected_image_media_ids:
        errors.append("draft image media IDs or order do not match the uploaded images")
    return {
        "ok": not errors,
        "status": "verified" if not errors else "failed",
        "method": "draft/get",
        "verified_at": datetime.now().astimezone().isoformat(),
        "errors": errors,
        "title": item.get("title", ""),
        "image_count": actual_count,
        "image_media_ids": actual_media_ids,
    }
