"""Single parser for the Markdown metadata used across pipeline boundaries."""

from __future__ import annotations

import re
from typing import Any


def parse_scalar(value: str) -> Any:
    value = value.strip()
    if value in {"", "null", "Null", "NULL", "~"}:
        return None
    if value in {"true", "True", "TRUE"}:
        return True
    if value in {"false", "False", "FALSE"}:
        return False
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def split_frontmatter(text: str) -> tuple[str, str]:
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                return "\n".join(lines[1:index]), "\n".join(lines[index + 1 :])
    return "", text


def parse_simple_yaml(text: str) -> dict[str, Any]:
    """Parse the deliberately flat YAML contract supported by the publisher."""
    data: dict[str, Any] = {}
    current_list: str | None = None
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if raw_line.startswith((" ", "\t")):
            if current_list and raw_line.strip().startswith("- "):
                data.setdefault(current_list, [])
                if not isinstance(data[current_list], list):
                    raise ValueError(f"unsupported YAML line: {raw_line}")
                data[current_list].append(parse_scalar(raw_line.strip()[2:]))
                continue
            raise ValueError(f"unsupported YAML line: {raw_line}")
        match = re.match(r"^([A-Za-z][A-Za-z0-9_-]*):\s*(.*)$", raw_line)
        if not match:
            raise ValueError(f"unsupported YAML line: {raw_line}")
        key, raw_value = match.groups()
        if raw_value == "":
            data[key] = []
            current_list = key
        else:
            data[key] = parse_scalar(raw_value)
            current_list = None
    return data


def frontmatter(text: str) -> dict[str, Any]:
    metadata, _ = split_frontmatter(text)
    return parse_simple_yaml(metadata) if metadata.strip() else {}


def markdown_body(text: str) -> str:
    return split_frontmatter(text)[1]


def first_h1(text: str) -> str | None:
    for line in markdown_body(text).splitlines():
        match = re.match(r"^\s*#\s+(.+?)\s*$", line)
        if match:
            return match.group(1)
    return None


def title(text: str) -> str | None:
    metadata = frontmatter(text)
    configured = str(metadata.get("title") or "").strip()
    if configured:
        return configured
    heading = first_h1(text)
    if heading:
        return heading
    for line in markdown_body(text).splitlines():
        if line.strip():
            return line.strip()
    return None
