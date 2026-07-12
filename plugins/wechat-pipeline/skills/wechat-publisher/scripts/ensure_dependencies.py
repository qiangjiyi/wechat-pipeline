#!/usr/bin/env python3
"""Install article-renderer Node dependencies outside the plugin cache."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parent.parent


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def data_root(override: Path | None) -> Path:
    if override:
        return override.expanduser().resolve()
    plugin_data = os.environ.get("CLAUDE_PLUGIN_DATA")
    if plugin_data:
        return Path(plugin_data).expanduser().resolve() / "wechat-publisher"
    return Path("~/.cache/wechat-pipeline/wechat-publisher").expanduser().resolve()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    npm = shutil.which("npm")
    if not npm:
        raise SystemExit("npm not found; install Node.js before publishing article mode")

    destination = data_root(args.data_dir)
    destination.mkdir(parents=True, exist_ok=True)
    package_json = SKILL_DIR / "package.json"
    package_lock = SKILL_DIR / "package-lock.json"
    marker = destination / ".package-lock.sha256"
    expected = sha256(package_lock)
    installed = destination / "node_modules" / "baoyu-md"
    if installed.is_dir() and marker.is_file() and marker.read_text().strip() == expected:
        print(destination)
        return 0

    shutil.copy2(package_json, destination / "package.json")
    shutil.copy2(package_lock, destination / "package-lock.json")
    command = [npm, "ci", "--omit=dev", "--no-audit", "--no-fund"]
    result = subprocess.run(command, cwd=destination, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise SystemExit(f"npm dependency installation failed: {detail}")
    marker.write_text(expected + "\n", encoding="utf-8")
    if not args.quiet:
        print(f"installed wechat-publisher dependencies in {destination}")
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
