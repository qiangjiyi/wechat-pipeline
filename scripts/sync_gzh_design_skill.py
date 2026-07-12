#!/usr/bin/env python3
"""Synchronize the pinned gzh-design runtime snapshot from a local clone."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


UPSTREAM_REPOSITORY = "https://github.com/isjiamu/gzh-design-skill"
DEFAULT_SOURCE = Path("~/Workspace/downloads/skill-sources/gzh-design-skill").expanduser()
RUNTIME_PATHS = ("SKILL.md", "references", "scripts", "assets")


class SyncError(RuntimeError):
    pass


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise SyncError(result.stderr.strip() or result.stdout.strip() or "git command failed")
    return result.stdout.strip()


def git_bytes(root: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)
    if result.returncode != 0:
        raise SyncError(result.stderr.decode(errors="replace").strip() or "git command failed")
    return result.stdout


def tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(
        path for path in root.rglob("*")
        if path.is_file()
        and path.name != ".DS_Store"
        and path.suffix != ".pyc"
        and "__pycache__" not in path.parts
    )
    for path in files:
        if path.is_symlink():
            raise SyncError(f"snapshot contains a symlink: {path}")
        relative = path.relative_to(root).as_posix().encode()
        contents = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(contents).to_bytes(8, "big"))
        digest.update(contents)
    return digest.hexdigest()


def archive_head(source: Path, destination: Path) -> None:
    result = subprocess.run(
        ["git", "-C", str(source), "archive", "--format=tar", "HEAD", *RUNTIME_PATHS],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise SyncError(result.stderr.decode(errors="replace").strip() or "git archive failed")
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
        members = archive.getmembers()
        for member in members:
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk():
                raise SyncError(f"unsafe path in upstream archive: {member.name}")
        archive.extractall(destination, members=members)


def copy_runtime(source: Path, destination: Path) -> None:
    stage = Path(tempfile.mkdtemp(prefix=".gzh-design-stage-", dir=destination.parent))
    backup = destination.with_name(destination.name + ".sync-backup")
    try:
        for relative in RUNTIME_PATHS:
            src = source / relative
            dst = stage / relative
            if src.is_dir():
                shutil.copytree(src, dst, copy_function=shutil.copy2)
            elif src.is_file():
                shutil.copy2(src, dst)
            else:
                raise SyncError(f"missing upstream runtime path: {src}")
        if backup.exists():
            shutil.rmtree(backup)
        if destination.exists():
            shutil.move(str(destination), str(backup))
        shutil.move(str(stage), str(destination))
        shutil.rmtree(backup, ignore_errors=True)
    except Exception:
        if destination.exists():
            shutil.rmtree(destination)
        if backup.exists():
            shutil.move(str(backup), str(destination))
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1], help=argparse.SUPPRESS)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    try:
        source = args.source.expanduser().resolve()
        project = args.project_root.expanduser().resolve()
        if Path(git(source, "rev-parse", "--show-toplevel")).resolve() != source:
            raise SyncError("--source must point to the gzh-design repository root")
        origin = git(source, "remote", "get-url", "origin")
        if not re.search(r"github\.com[:/]isjiamu/gzh-design-skill(?:\.git)?$", origin, re.I):
            raise SyncError(f"unexpected gzh-design origin: {origin}")
        dirty = git(source, "status", "--porcelain", "--", *RUNTIME_PATHS, "LICENSE")
        if dirty:
            raise SyncError(f"upstream runtime paths contain uncommitted changes:\n{dirty}")

        commit = git(source, "rev-parse", "HEAD")
        plugin = project / "plugins" / "wechat-pipeline"
        target = plugin / "skills" / "gzh-design"
        lock_path = plugin / "third_party" / "gzh-design.lock.json"
        target_license = plugin / "third_party" / "gzh-design" / "LICENSE"
        expected_license = git_bytes(source, "show", "HEAD:LICENSE")

        with tempfile.TemporaryDirectory(prefix="gzh-design-snapshot-") as temporary:
            snapshot = Path(temporary)
            archive_head(source, snapshot)
            expected_hash = tree_sha256(snapshot)
            current_hash = tree_sha256(target) if target.is_dir() else None

            lock = json.loads(lock_path.read_text(encoding="utf-8")) if lock_path.is_file() else {}
            outdated = (
                current_hash != expected_hash
                or lock.get("repository") != UPSTREAM_REPOSITORY
                or lock.get("commit") != commit
                or lock.get("tree_sha256") != expected_hash
                or not target_license.is_file()
                or target_license.read_bytes() != expected_license
            )
            print(f"source: {source}")
            print(f"commit: {commit}")
            print(f"tree_sha256: {expected_hash}")
            if args.check:
                print("result: update available" if outdated else "result: up to date")
                return 1 if outdated else 0
            if args.dry_run:
                print("result: dry run; no files changed")
                return 0
            if not outdated:
                print("result: already up to date")
                return 0

            status = git(project, "status", "--porcelain", "--", str(target.relative_to(project)))
            if status and not args.force:
                raise SyncError("vendored gzh-design Skill contains uncommitted changes; use --force to replace it")
            copy_runtime(snapshot, target)

        target_license.parent.mkdir(parents=True, exist_ok=True)
        target_license.write_bytes(expected_license)
        write_json(lock_path, {
            "repository": UPSTREAM_REPOSITORY,
            "commit": commit,
            "license": "AGPL-3.0-or-later with additional permission from the author",
            "runtime_paths": list(RUNTIME_PATHS),
            "tree_sha256": tree_sha256(target),
        })
        print("result: synchronized; review and commit the generated diff")
        return 0
    except (OSError, ValueError, json.JSONDecodeError, SyncError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
