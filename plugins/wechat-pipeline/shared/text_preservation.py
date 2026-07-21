"""Visible-text preservation checks shared by Formatter, Designer and Typesetter."""

from __future__ import annotations

import re
import unicodedata
from html.parser import HTMLParser
from typing import Any

from .markdown_meta import markdown_body


class _VisibleHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def visible_characters(value: str) -> str:
    line = value.strip()
    line = re.sub(r"^(?:#{1,6}|>|[-+*]|\d+[.)])\s*", "", line)
    line = re.sub(r"!\[([^]]*)\]\([^)]+\)", r"\1", line)
    line = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", line)
    line = re.sub(r"</?[^>]+>", "", line)
    line = re.sub(r"(?:\*\*|__|~~|==|\+\+|`)", "", line)
    normalized = unicodedata.normalize("NFKC", line).casefold()
    return "".join(
        character
        for character in normalized
        if unicodedata.category(character)[0] in {"L", "N"}
        or unicodedata.category(character) == "So"
    )


def segment_records(
    markdown: str,
    *,
    skip_h1: bool = False,
    split_phrases: bool = False,
) -> list[dict[str, Any]]:
    body = markdown_body(markdown)
    offset = len(markdown.splitlines()) - len(body.splitlines())
    records: list[dict[str, Any]] = []
    in_fence = False
    for index, raw in enumerate(body.splitlines(), start=offset + 1):
        line = raw.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line or re.fullmatch(r"[-*_]{3,}", line):
            continue
        if skip_h1 and re.match(r"^#(?!#)\s+", line):
            continue
        if re.fullmatch(r"\|?[\s:|-]+\|?", line):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")] if "|" in line else [line]
        for cell in cells:
            phrases = re.split(r"[。！？；.!?;,，、]+", cell) if split_phrases else [cell]
            for phrase in phrases:
                normalized = visible_characters(phrase)
                if len(normalized) >= 2:
                    records.append({"line": index, "text": phrase.strip(), "normalized": normalized})
    return records


def normalized_document(candidate: str, *, html: bool = False) -> str:
    if html:
        parser = _VisibleHTML()
        parser.feed(candidate)
        candidate = "\n".join(parser.parts)
    return "".join(visible_characters(line) for line in candidate.splitlines())


def preservation_report(
    source: str,
    candidate: str,
    *,
    candidate_is_html: bool = False,
    skip_h1: bool = False,
    split_phrases: bool = False,
) -> dict[str, Any]:
    segments = segment_records(source, skip_h1=skip_h1, split_phrases=split_phrases)
    normalized_candidate = normalized_document(candidate, html=candidate_is_html)
    missing = [
        {
            "line": segment["line"],
            "preview": str(segment["text"])[:160],
            "normalized": segment["normalized"],
        }
        for segment in segments
        if str(segment["normalized"]) not in normalized_candidate
    ]
    return {
        "ok": not missing,
        "source_segment_count": len(segments),
        "missing_source_segments": missing,
    }


def missing_summary(missing: list[dict[str, Any]], *, label: str) -> str:
    details = "; ".join(
        f"line {item.get('line')}: {item.get('preview')!r}" for item in missing[:3]
    )
    suffix = f"; {len(missing) - 3} more" if len(missing) > 3 else ""
    return f"{label} removed or rewrote {len(missing)} source text segment(s): {details}{suffix}"
