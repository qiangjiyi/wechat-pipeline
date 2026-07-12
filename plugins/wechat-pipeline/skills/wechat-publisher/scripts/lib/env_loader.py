"""Load publisher settings without storing secrets in the plugin cache."""

from __future__ import annotations

import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PLUGIN_ROOT))

from shared.dotenv import load_dotenv


def merged_env(base_dir: Path, env_file: str | None, skill_dir: Path) -> tuple[dict[str, str], Path | None]:
    """Look for config in explicit, environment, source, and user config paths.

    Returns the merged env dict and the path that was loaded (or None).
    """
    candidates: list[Path] = []
    if env_file:
        candidates.append(Path(env_file).expanduser())
    else:
        configured = os.environ.get("WECHAT_PUBLISHER_ENV_FILE")
        if configured:
            candidates.append(Path(configured).expanduser())
        candidates.extend([
            base_dir / ".env.local",
            base_dir / ".env",
            Path("~/.config/wechat-pipeline/.env.local").expanduser(),
            Path("~/.config/wechat-pipeline/.env").expanduser(),
        ])
    file_env: dict[str, str] = {}
    used: Path | None = None
    for candidate in candidates:
        if candidate.exists():
            file_env = load_dotenv(candidate)
            used = candidate
            break
    env = dict(file_env)
    env.update(os.environ)
    return env, used
