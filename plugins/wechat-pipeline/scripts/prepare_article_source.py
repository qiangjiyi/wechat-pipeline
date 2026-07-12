#!/usr/bin/env python3
"""Create one reusable writable Markdown source for news design and typesetting."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from datetime import datetime
from pathlib import Path

from protocol_version import PROTOCOL_VERSION


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()

    run_dir = args.run_dir.expanduser().resolve()
    source = args.source.expanduser().resolve()
    run_path = run_dir / ".pipeline" / "run.json"
    if not run_path.is_file():
        raise SystemExit(f"run context not found: {run_path}")
    run = json.loads(run_path.read_text(encoding="utf-8"))
    if run.get("protocol_version") != PROTOCOL_VERSION:
        raise SystemExit(f"run protocol_version must be {PROTOCOL_VERSION}")
    if run.get("mode") != "news":
        raise SystemExit("article source is only valid for news mode")
    if Path(str(run.get("canonical_output_dir", ""))).expanduser().resolve() != run_dir:
        raise SystemExit("run canonical_output_dir mismatch")
    if not inside(source, run_dir) or not source.is_file():
        raise SystemExit("article source input must be an existing file inside canonical_output_dir")

    target = run_dir / "article-source.md"
    audit_path = run_dir / ".pipeline" / "article-source.json"
    source_hash = sha256_file(source)
    if target.is_file() or audit_path.is_file():
        if not target.is_file() or not audit_path.is_file():
            raise SystemExit("article source artifact is incomplete; do not overwrite it implicitly")
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        if audit.get("origin_path") != str(source) or audit.get("origin_sha256") != source_hash:
            raise SystemExit("article source already exists for a different formatter output")
        print(json.dumps({
            "reused": True,
            "article_source_path": str(target),
            "article_source_sha256": sha256_file(target),
            "audit_path": str(audit_path),
        }, ensure_ascii=False))
        return 0

    shutil.copyfile(source, target)
    target.chmod(0o644)
    created_hash = sha256_file(target)
    audit = {
        "protocol_version": PROTOCOL_VERSION,
        "run_id": run.get("run_id"),
        "origin_path": str(source),
        "origin_sha256": source_hash,
        "article_source_path": str(target),
        "created_sha256": created_hash,
        "created_at": datetime.now().astimezone().isoformat(),
    }
    write_json(audit_path, audit)
    print(json.dumps({
        "reused": False,
        "article_source_path": str(target),
        "article_source_sha256": created_hash,
        "audit_path": str(audit_path),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
