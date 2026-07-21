#!/usr/bin/env python3
"""Maintainer-only builder for the installed Plugin release integrity manifest."""

from __future__ import annotations

import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "plugins" / "wechat-pipeline"
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from integrity import LOCK_PATH, capture_release  # noqa: E402


def main() -> int:
    payload = capture_release()
    print(json.dumps({
        "ok": True,
        "path": str(LOCK_PATH),
        "file_count": payload["file_count"],
        "runtime_sha256": payload["runtime_sha256"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
