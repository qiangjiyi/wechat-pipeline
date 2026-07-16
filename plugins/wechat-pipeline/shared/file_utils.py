"""Common file utilities for wechat-pipeline plugin scripts."""

from __future__ import annotations

from pathlib import Path


def is_relevant_file(path: Path) -> bool:
    """Return True if a file should be included in scanning/validation.

    Filters out macOS system files (`.DS_Store`), Python bytecode, and
    cache directories that have no bearing on pipeline outputs or
    correctness checks.
    """
    if path.name == ".DS_Store":
        return False
    if path.suffix == ".pyc":
        return False
    if "__pycache__" in path.parts:
        return False
    return True
