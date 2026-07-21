"""Deterministic hashing helpers shared by the plugin runtime."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable, Iterable


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_file_set(paths: Iterable[Path], root: Path) -> str:
    """Hash relative names and contents for a deterministic file set."""
    digest = hashlib.sha256()
    for path in sorted(set(paths), key=lambda value: value.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        contents = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(contents).to_bytes(8, "big"))
        digest.update(contents)
    return digest.hexdigest()


def tree_sha256(
    root: Path,
    relevant: Callable[[Path], bool] | None = None,
) -> str:
    predicate = relevant or (lambda path: path.is_file())
    return hash_file_set((path for path in root.rglob("*") if predicate(path)), root)
