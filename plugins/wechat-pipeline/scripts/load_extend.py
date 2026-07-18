#!/usr/bin/env python3
"""Deterministically resolve a Baoyu skill's EXTEND.md user-preference file.

The image skills ship a 3-tier lookup table for EXTEND.md (project, XDG, user
home) whose XDG tier uses a shell-style default: ``${XDG_CONFIG_HOME:-$HOME/.config}``.
When that lookup is left to an LLM worker it is flaky: shell parameter
expansion is not performed and the worker often fails to fall through to the
user-home tier, so an EXTEND.md that really exists gets reported as "not found".

This script is the single source of truth. It performs the exact same lookup
deterministically and emits machine-readable JSON, so the designer worker reads
the resolved absolute path directly instead of re-interpreting the table.

Usage:
    run_python.sh load_extend.py <skill-name> [--base-dir <dir>] [--json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path


# baoyu-image-gen was previously named baoyu-imagine. Its SKILL.md says the
# runtime renames a legacy EXTEND.md when the new path is absent. This script
# is read-only: it reports a legacy hit so the caller can migrate, but never
# writes or renames on its own.
LEGACY_ALIASES = {
    "baoyu-image-gen": "baoyu-imagine",
}


def candidate_paths(skill: str, base_dir: Path) -> list[tuple[str, Path]]:
    """Return (source_label, path) pairs in priority order; first hit wins."""
    home = Path(os.path.expanduser("~"))
    xdg_raw = os.environ.get("XDG_CONFIG_HOME") or str(home / ".config")
    xdg = Path(os.path.expandvars(xdg_raw)).expanduser()
    base = base_dir.expanduser().resolve()
    candidates: list[tuple[str, Path]] = [
        ("project", base / ".baoyu-skills" / skill / "EXTEND.md"),
        ("xdg", xdg / "baoyu-skills" / skill / "EXTEND.md"),
        ("home", home / ".baoyu-skills" / skill / "EXTEND.md"),
    ]
    legacy = LEGACY_ALIASES.get(skill)
    if legacy:
        candidates.extend(
            [
                ("legacy-project", base / ".baoyu-skills" / legacy / "EXTEND.md"),
                ("legacy-xdg", xdg / "baoyu-skills" / legacy / "EXTEND.md"),
                ("legacy-home", home / ".baoyu-skills" / legacy / "EXTEND.md"),
            ]
        )
    return candidates


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve(skill: str, base_dir: Path) -> dict:
    legacy_name = LEGACY_ALIASES.get(skill)
    searched: list[str] = []
    for source, path in candidate_paths(skill, base_dir):
        searched.append(str(path))
        if path.is_file():
            return {
                "found": True,
                "source": source,
                "skill": skill,
                "path": str(path.resolve()),
                "sha256": sha256_file(path),
                "searched": searched,
                "legacy_skill": legacy_name if source.startswith("legacy") else None,
            }
    return {
        "found": False,
        "source": "none",
        "skill": skill,
        "path": None,
        "sha256": None,
        "searched": searched,
        "legacy_skill": legacy_name,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve a Baoyu skill's EXTEND.md.")
    parser.add_argument("skill", help="Skill directory name, e.g. baoyu-image-gen")
    parser.add_argument(
        "--base-dir",
        default=".",
        help="Project root for the project-scope lookup (default: current directory)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON on stdout",
    )
    args = parser.parse_args()

    result = resolve(args.skill, Path(args.base_dir))

    if args.json:
        print(json.dumps(result))
    else:
        if result["found"]:
            print(f"EXTEND.md found ({result['source']}): {result['path']}")
            print(f"sha256: {result['sha256']}")
        else:
            print(f"EXTEND.md not found for {args.skill}")
            for path in result["searched"]:
                print(f"  searched: {path}")

    return 0 if result["found"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
