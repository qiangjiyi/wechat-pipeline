#!/usr/bin/env python3
"""Resolve image backends without exposing credential values."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

from shared.dotenv import load_dotenv


PROVIDER_KEYS = {
    "openai-native": ("OPENAI_API_KEY",),
    "google": ("GOOGLE_API_KEY",),
    "azure": ("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_BASE_URL"),
    "openrouter": ("OPENROUTER_API_KEY",),
    "dashscope": ("DASHSCOPE_API_KEY",),
    "zai": ("ZAI_API_KEY",),
    "minimax": ("MINIMAX_API_KEY",),
    "replicate": ("REPLICATE_API_TOKEN",),
    "jimeng": ("JIMENG_ACCESS_KEY_ID", "JIMENG_SECRET_ACCESS_KEY"),
    "seedream": ("ARK_API_KEY",),
    "agnes": ("AGNES_API_KEY",),
}


def command_output(command: list[str]) -> str | None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = (result.stdout or result.stderr).strip()
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return None
    return next(
        (line for line in lines if re.search(r"\b\d+\.\d+(?:\.\d+)?\b", line)),
        lines[-1],
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=Path("~/.baoyu-skills/.env"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    env_file = args.env_file.expanduser().resolve()
    values = load_dotenv(env_file)
    merged = {**values, **{key: value for key, value in os.environ.items() if value}}

    codex_path = shutil.which("codex")
    providers = [{
        "name": "codex-cli",
        "configured": bool(codex_path),
        "executable": codex_path,
        "version": command_output([codex_path, "--version"]) if codex_path else None,
        "note": "availability only; model compatibility is decided by the real render attempt",
    }]
    for name, keys in PROVIDER_KEYS.items():
        providers.append({
            "name": name,
            "configured": all(bool(merged.get(key)) for key in keys),
            "required_keys": list(keys),
        })

    result = {
        "env_file": str(env_file),
        "env_file_exists": env_file.is_file(),
        "providers": providers,
        "fallback_order": [item["name"] for item in providers if item["configured"]],
    }
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result["fallback_order"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
