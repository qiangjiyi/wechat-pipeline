#!/usr/bin/env python3
"""Validate the canonical formatted Markdown and its durable receipt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from prepare_content import validate_content_artifact


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    run_dir = args.run_dir.expanduser().resolve()
    errors, receipt = validate_content_artifact(run_dir)
    if errors:
        print("formatted content validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(json.dumps({"ok": True, "content_sha256": receipt.get("content_sha256")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

