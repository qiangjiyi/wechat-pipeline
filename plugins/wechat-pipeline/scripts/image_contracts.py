#!/usr/bin/env python3
"""Publication-level image contracts shared by native Skill boundaries."""

from __future__ import annotations

import struct
from pathlib import Path


NEWS_COVER_ASPECT = "2.35:1"
NEWS_COVER_RATIO = 2.35
ASPECT_TOLERANCE = 0.03


def skill_options(mode: str, skill_name: str) -> dict[str, str]:
    """Return destination constraints that the pipeline must pass to a native Skill."""
    if mode == "news" and skill_name == "baoyu-cover-image":
        return {"aspect": NEWS_COVER_ASPECT}
    return {}


def _jpeg_dimensions(data: bytes) -> tuple[int, int]:
    if not data.startswith(b"\xff\xd8"):
        raise ValueError("invalid JPEG SOI header")
    offset = 2
    sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while offset + 4 <= len(data):
        while offset < len(data) and data[offset] != 0xFF:
            offset += 1
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in {0x01, *range(0xD0, 0xDA)}:
            continue
        if offset + 2 > len(data):
            break
        length = struct.unpack(">H", data[offset : offset + 2])[0]
        if length < 2 or offset + length > len(data):
            break
        if marker in sof_markers and length >= 7:
            height, width = struct.unpack(">HH", data[offset + 3 : offset + 7])
            return width, height
        offset += length
    raise ValueError("JPEG does not contain a supported SOF dimension header")


def _webp_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ValueError("invalid WebP RIFF header")
    chunk = data[12:16]
    if chunk == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return width, height
    if chunk == b"VP8L" and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    raise ValueError("WebP does not contain a supported VP8/VP8L/VP8X dimension header")


def image_dimensions(path: Path) -> tuple[int, int]:
    """Read PNG, JPEG or WebP dimensions without decoding the image pixels."""
    data = path.read_bytes()
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
    elif data.startswith(b"\xff\xd8"):
        width, height = _jpeg_dimensions(data)
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        width, height = _webp_dimensions(data)
    else:
        raise ValueError("expected a PNG, JPEG, or WebP dimension header")
    if width <= 0 or height <= 0:
        raise ValueError("image dimensions must be positive")
    return width, height


def validate_output_contract(
    mode: str,
    skill_name: str,
    role: str,
    output_path: Path,
) -> list[str]:
    """Validate only constraints required by the downstream publication mode."""
    if not (mode == "news" and skill_name == "baoyu-cover-image" and role == "cover"):
        return []
    try:
        width, height = image_dimensions(output_path)
    except (OSError, ValueError) as err:
        return [f"news cover cannot verify required aspect {NEWS_COVER_ASPECT}: {err}"]
    actual = width / height
    if abs(actual - NEWS_COVER_RATIO) / NEWS_COVER_RATIO > ASPECT_TOLERANCE:
        return [
            f"news cover dimensions {width}x{height} do not match required aspect "
            f"{NEWS_COVER_ASPECT}"
        ]
    return []
