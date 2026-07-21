"""HTML patterns shared by layout and publisher validation."""

from __future__ import annotations

import re


PLACEHOLDER_PATTERNS = (
    re.compile(r"\{\{[^{}]+\}\}"),
    re.compile(r"(?:图片|动图|封面|名片)(?:URL|地址)"),
    re.compile(r"【(?:插入|待补)[^】]*】"),
    re.compile(r"<!--\s*(?:TODO|PLACEHOLDER|IMAGE)[^>]*-->", re.IGNORECASE),
    re.compile(r"\[(?:TODO|PLACEHOLDER|IMAGE)[^]]*\]", re.IGNORECASE),
)
