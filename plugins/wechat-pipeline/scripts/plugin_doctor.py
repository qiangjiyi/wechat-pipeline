#!/usr/bin/env python3
"""Initialize and verify the portable wechat-pipeline plugin environment."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

from shared.dotenv import load_dotenv
from integrity import validate_release
from run_context import detect_host_runtime


DEFAULT_CONFIG = Path("~/.config/wechat-pipeline/.env").expanduser()
ENV_TEMPLATE = PLUGIN_ROOT / "skills" / "wechat-publisher" / ".env.example"

BAOYU_ENV = Path("~/.baoyu-skills/.env").expanduser()
BAOYU_EXTEND = Path("~/.baoyu-skills/baoyu-image-gen/EXTEND.md").expanduser()
IMAGE_GEN_DIALECTS = {"openai-native", "ratio-metadata"}


def upstream_dialect(raw: str) -> str | None:
    """Mirror baoyu-image-gen parseOpenAIImageApiDialect (strips quotes, not comments)."""
    value = raw.replace("'", "").replace('"', "").strip()
    if not value or value == "null":
        return None
    if value in IMAGE_GEN_DIALECTS:
        return value
    raise ValueError(raw.strip())


def extend_dialect(extend_path: Path) -> tuple[str | None, str | None]:
    """Return (dialect, error) from the EXTEND.md frontmatter dialect key."""
    if not extend_path.is_file():
        return None, None
    text = extend_path.read_text(encoding="utf-8", errors="replace")
    match = re.match(r"^---\s*\n([\s\S]*?)\n---\s*$", text)
    if not match:
        return None, None
    for lineno, line in enumerate(match.group(1).splitlines(), 2):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        if key.strip() != "default_image_api_dialect":
            continue
        try:
            return upstream_dialect(value), None
        except ValueError:
            return None, (
                f"{extend_path}:{lineno} default_image_api_dialect 的值 {value.strip()!r} "
                "不合法（合法值：openai-native | ratio-metadata；上游不会剥离行内注释，"
                "注释请写成独立行）"
            )
    return None, None


def image_gen_dialect_errors(
    env_path: Path = BAOYU_ENV,
    extend_path: Path = BAOYU_EXTEND,
) -> list[str]:
    """Fail fast on baoyu-image-gen dialect config its own loader would reject.

    The vendored image skill keeps inline comments in values and then aborts
    deep inside a Designer worker; resolve the same CLI > EXTEND.md > env
    chain here so bad config surfaces before a run is created.
    """
    dialect, error = extend_dialect(extend_path)
    if error:
        return [error]
    if dialect or not env_path.is_file():
        return []
    errors: list[str] = []
    for lineno, raw_line in enumerate(
        env_path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() != "OPENAI_IMAGE_API_DIALECT":
            continue
        try:
            upstream_dialect(value)
        except ValueError:
            errors.append(
                f"{env_path}:{lineno} OPENAI_IMAGE_API_DIALECT 的值 {value.strip()!r} "
                "不合法（合法值：openai-native | ratio-metadata；上游不会剥离行内注释，"
                "注释请写成独立行）"
            )
    return errors


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


def doctor(args: argparse.Namespace) -> int:
    config_path = resolve_env_file(args.env_file)
    file_values = load_dotenv(config_path)
    values = {**file_values, **{key: value for key, value in os.environ.items() if value}}
    errors: list[str] = []
    warnings: list[str] = []
    info: list[str] = []

    release_errors, _ = validate_release()
    errors.extend(release_errors)
    errors.extend(image_gen_dialect_errors())
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

    exports_root = Path(
        values.get("WECHAT_PIPELINE_EXPORTS_DIR", "~/Workspace/exports")
    ).expanduser().resolve()
    info.append(f"pipeline exports directory: {exports_root}")

    host_runtime = detect_host_runtime()
    if host_runtime == "unknown":
        warnings.append(
            "host runtime is not recognized (no CLAUDECODE marker); runs require an "
            "explicit --host-runtime declaration and a host that can dispatch workers"
        )

    result = {
        "ok": not errors,
        "plugin_root": str(PLUGIN_ROOT),
        "python_executable": sys.executable,
        "python_version": ".".join(str(part) for part in sys.version_info[:3]),
        "host_runtime_detected": host_runtime,
        "config_file": str(config_path),
        "config_file_exists": config_path.is_file(),
        "exports_root": str(
            Path(values.get("WECHAT_PIPELINE_EXPORTS_DIR", "~/Workspace/exports"))
            .expanduser()
            .resolve()
        ),
        "requested_account": args.account,
        "configured_accounts": accounts,
        "image_backends": "resolved-by-native-skill",
        "mode": args.mode,
        "errors": errors,
        "warnings": warnings,
        "info": info,
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
