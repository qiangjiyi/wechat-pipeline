#!/usr/bin/env python3
"""Initialize and verify the portable wechat-pipeline plugin environment."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

from shared.dotenv import load_dotenv


DEFAULT_CONFIG = Path("~/.config/wechat-pipeline/.env").expanduser()
ENV_TEMPLATE = PLUGIN_ROOT / "skills" / "wechat-publisher" / ".env.example"


def normalize_account(value: str) -> str:
    return "".join(character if character.isalnum() else "_" for character in value.upper())


def resolve_env_file(explicit: Path | None) -> Path:
    if explicit:
        return explicit.expanduser().resolve()
    configured = os.environ.get("WECHAT_PUBLISHER_ENV_FILE")
    if configured:
        return Path(configured).expanduser().resolve()
    local = DEFAULT_CONFIG.with_name(".env.local")
    return local if local.is_file() else DEFAULT_CONFIG


def init_config(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        print(json.dumps({"created": False, "config": str(path)}, ensure_ascii=False))
        return 0
    shutil.copy2(ENV_TEMPLATE, path)
    path.chmod(0o600)
    print(json.dumps({"created": True, "config": str(path)}, ensure_ascii=False))
    return 0


def account_ready(values: dict[str, str], account: str) -> bool:
    key = normalize_account(account)
    token = values.get(f"WECHAT_{key}_ACCESS_TOKEN", "")
    app_id = values.get(f"WECHAT_{key}_APP_ID", "")
    secret = values.get(f"WECHAT_{key}_APP_SECRET", "")
    return bool(token or (app_id and secret))


def image_backends() -> tuple[list[str], str | None]:
    command = [sys.executable, str(PLUGIN_ROOT / "scripts" / "preflight_image_backends.py")]
    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=20)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return [], result.stderr.strip() or "image backend preflight returned invalid output"
    return data.get("fallback_order", []), None


def tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(
        path for path in root.rglob("*")
        if path.is_file()
        and path.name != ".DS_Store"
        and path.suffix != ".pyc"
        and "__pycache__" not in path.parts
    ):
        relative = path.relative_to(root).as_posix().encode()
        contents = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(contents).to_bytes(8, "big"))
        digest.update(contents)
    return digest.hexdigest()


def validate_gzh_snapshot() -> str | None:
    skill_root = PLUGIN_ROOT / "skills" / "gzh-design"
    lock_path = PLUGIN_ROOT / "third_party" / "gzh-design.lock.json"
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        return f"unable to read bundled gzh-design lock: {err}"
    required = [
        skill_root / "SKILL.md",
        skill_root / "references" / "theme-index.md",
        skill_root / "references" / "common-components.md",
        skill_root / "scripts" / "validate_gzh_html.py",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        return "bundled gzh-design runtime is incomplete: " + ", ".join(missing)
    actual = tree_sha256(skill_root)
    if actual != lock.get("tree_sha256"):
        return "bundled gzh-design runtime hash does not match its lock"
    return None


def doctor(args: argparse.Namespace) -> int:
    config_path = resolve_env_file(args.env_file)
    file_values = load_dotenv(config_path)
    values = {**file_values, **{key: value for key, value in os.environ.items() if value}}
    errors: list[str] = []
    warnings: list[str] = []

    if sys.version_info < (3, 10):
        errors.append("Python 3.10 or newer is required")
    if not config_path.is_file():
        errors.append(
            f"WeChat config not found: {config_path}; invoke wechat-pipeline:wechat-pipeline-setup"
        )

    accounts = [item.strip() for item in values.get("WECHAT_ACCOUNTS", "").split(",") if item.strip()]
    if args.account:
        if not account_ready(values, args.account):
            errors.append(f"WeChat account is not configured: {args.account}")
    elif not accounts and not (
        values.get("WECHAT_ACCESS_TOKEN")
        or (values.get("WECHAT_APP_ID") and values.get("WECHAT_APP_SECRET"))
    ):
        errors.append("no WeChat account is configured")

    backends, backend_error = image_backends()
    if backend_error:
        errors.append(backend_error)
    elif not backends:
        errors.append("no image backend is configured")

    if args.mode == "news":
        gzh_error = validate_gzh_snapshot()
        if gzh_error:
            errors.append(gzh_error)

    result = {
        "ok": not errors,
        "plugin_root": str(PLUGIN_ROOT),
        "config_file": str(config_path),
        "config_file_exists": config_path.is_file(),
        "requested_account": args.account,
        "configured_accounts": accounts,
        "image_backends": backends,
        "mode": args.mode,
        "errors": errors,
        "warnings": warnings,
    }
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result["ok"] else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--account")
    parser.add_argument("--mode", choices=("newspic", "news"), default="newspic")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    config_path = resolve_env_file(args.env_file)
    if args.init:
        return init_config(config_path)
    return doctor(args)


if __name__ == "__main__":
    raise SystemExit(main())
