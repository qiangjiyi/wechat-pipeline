#!/usr/bin/env python3
"""Synchronize pinned Baoyu Skill snapshots from an updated local clone."""

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


SKILL_NAMES = (
    "baoyu-format-markdown",
    "baoyu-xhs-images",
    "baoyu-cover-image",
    "baoyu-article-illustrator",
    "baoyu-image-gen",
)
UPSTREAM_REPOSITORY = "https://github.com/JimLiu/baoyu-skills"
DEFAULT_SOURCE = Path("~/Workspace/downloads/skill-sources/baoyu-skills").expanduser()


class SyncError(RuntimeError):
    pass


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SyncError(result.stderr.strip() or result.stdout.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(path for path in root.rglob("*") if path.is_file() and path.name != ".DS_Store")
    for path in files:
        if path.is_symlink():
            raise SyncError(f"upstream snapshot contains a symlink: {path}")
        relative = path.relative_to(root).as_posix().encode()
        contents = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(contents).to_bytes(8, "big"))
        digest.update(contents)
    return digest.hexdigest()


def skill_version(skill_dir: Path) -> str:
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.is_file():
        raise SyncError(f"missing upstream SKILL.md: {skill_file}")
    match = re.search(r"^version:\s*['\"]?([^'\"\s]+)", skill_file.read_text(encoding="utf-8"), re.MULTILINE)
    if not match:
        raise SyncError(f"missing version in upstream SKILL.md: {skill_file}")
    return match.group(1)


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise SyncError(f"unable to read JSON {path}: {err}") from err
    if not isinstance(value, dict):
        raise SyncError(f"JSON root must be an object: {path}")
    return value


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def bump_plugin_version(project_root: Path) -> tuple[str, str]:
    manifests = [
        project_root / "plugins" / "wechat-pipeline" / ".claude-plugin" / "plugin.json",
        project_root / "plugins" / "wechat-pipeline" / ".codex-plugin" / "plugin.json",
    ]
    values = [read_json(path) for path in manifests]
    versions = {str(value.get("version", "")) for value in values}
    if len(versions) != 1:
        raise SyncError("Claude and Codex plugin versions must match before synchronization")
    current = versions.pop()
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", current)
    if not match:
        raise SyncError(f"plugin version must be semantic x.y.z, got: {current!r}")
    updated = f"{match.group(1)}.{match.group(2)}.{int(match.group(3)) + 1}"
    for path, value in zip(manifests, values):
        value["version"] = updated
        write_json(path, value)
    return current, updated


def replace_snapshots(source_skills: Path, target_skills: Path, names: list[str]) -> None:
    stage_root = Path(tempfile.mkdtemp(prefix=".baoyu-sync-stage-", dir=target_skills))
    backup_root = Path(tempfile.mkdtemp(prefix=".baoyu-sync-backup-", dir=target_skills))
    installed: list[str] = []
    backed_up: list[str] = []
    try:
        for name in names:
            source = source_skills / name
            staged = stage_root / name
            shutil.copytree(source, staged, copy_function=shutil.copy2)
            if tree_sha256(staged) != tree_sha256(source):
                raise SyncError(f"staging verification failed for {name}")

        for name in names:
            destination = target_skills / name
            if destination.exists():
                shutil.move(str(destination), str(backup_root / name))
                backed_up.append(name)
            shutil.move(str(stage_root / name), str(destination))
            installed.append(name)
    except Exception:
        for name in reversed(installed):
            destination = target_skills / name
            if destination.exists():
                shutil.rmtree(destination)
        for name in backed_up:
            backup = backup_root / name
            if backup.exists():
                shutil.move(str(backup), str(target_skills / name))
        raise
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)
        shutil.rmtree(backup_root, ignore_errors=True)


def update_notice(path: Path, commit: str) -> None:
    text = path.read_text(encoding="utf-8")
    updated, count = re.subn(
        r"Upstream commit: `[0-9a-f]{40}`\.",
        f"Upstream commit: `{commit}`.",
        text,
        count=1,
    )
    if count != 1:
        raise SyncError(f"unable to locate upstream commit in {path}")
    path.write_text(updated, encoding="utf-8")


def update_changelog(path: Path, commit: str) -> None:
    text = path.read_text(encoding="utf-8")
    entry = f"- 同步 Baoyu Skills 至上游 commit `{commit[:12]}`，并刷新固定快照校验。"
    if entry in text:
        return
    marker = "## Unreleased\n"
    if marker not in text:
        raise SyncError(f"missing Unreleased section in {path}")
    path.write_text(text.replace(marker, marker + "\n" + entry + "\n", 1), encoding="utf-8")


def relevant_status(root: Path, paths: list[str]) -> str:
    return git(root, "status", "--porcelain", "--untracked-files=all", "--", *paths)


def archive_head(source_root: Path, destination: Path, paths: list[str]) -> None:
    result = subprocess.run(
        ["git", "-C", str(source_root), "archive", "--format=tar", "HEAD", *paths],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise SyncError(result.stderr.decode(errors="replace").strip() or "git archive failed")
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
        members = archive.getmembers()
        for member in members:
            member_path = Path(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise SyncError(f"unsafe path in upstream archive: {member.name}")
            if member.issym() or member.islnk():
                raise SyncError(f"upstream archive contains a link: {member.name}")
        archive.extractall(destination, members=members)


def sync_snapshot(
    args: argparse.Namespace,
    project_root: Path,
    source_root: Path,
    snapshot_root: Path,
    commit: str,
) -> int:
    plugin_root = project_root / "plugins" / "wechat-pipeline"
    source_skills = snapshot_root / "skills"
    target_skills = plugin_root / "skills"
    lock_path = plugin_root / "third_party" / "baoyu-skills.lock.json"
    source_metadata: dict[str, dict[str, str]] = {}
    changed: list[str] = []
    for name in SKILL_NAMES:
        source_dir = source_skills / name
        metadata = {
            "version": skill_version(source_dir),
            "tree_sha256": tree_sha256(source_dir),
        }
        source_metadata[name] = metadata
        target_dir = target_skills / name
        if not target_dir.is_dir() or tree_sha256(target_dir) != metadata["tree_sha256"]:
            changed.append(name)

    source_license = snapshot_root / "LICENSE"
    target_license = plugin_root / "third_party" / "baoyu-skills" / "LICENSE"
    if not source_license.is_file():
        raise SyncError(f"missing upstream LICENSE: {source_license}")
    license_changed = not target_license.is_file() or source_license.read_bytes() != target_license.read_bytes()

    lock = read_json(lock_path)
    metadata_changed = (
        lock.get("repository") != UPSTREAM_REPOSITORY
        or lock.get("commit") != commit
        or lock.get("skills") != source_metadata
    )
    outdated = bool(changed or license_changed or metadata_changed)

    print(f"source: {source_root}")
    print(f"commit: {commit}")
    for name in SKILL_NAMES:
        state = "update" if name in changed else "unchanged"
        print(f"{name}: {state} ({source_metadata[name]['version']})")
    if license_changed:
        print("LICENSE: update")

    if args.check:
        print("result: update available" if outdated else "result: up to date")
        return 1 if outdated else 0
    if args.dry_run:
        print("result: dry run; no files changed")
        return 0
    if not outdated:
        print("result: already up to date")
        return 0

    target_paths = [f"plugins/wechat-pipeline/skills/{name}" for name in SKILL_NAMES]
    dirty_target = relevant_status(project_root, target_paths)
    if dirty_target and not args.force:
        raise SyncError(
            "vendored Skill directories contain uncommitted changes; commit, restore, or pass --force:\n"
            + dirty_target
        )

    if changed:
        replace_snapshots(source_skills, target_skills, changed)
    if license_changed:
        target_license.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_license, target_license)

    lock["repository"] = UPSTREAM_REPOSITORY
    lock["commit"] = commit
    lock["license"] = "MIT"
    lock["skills"] = source_metadata
    write_json(lock_path, lock)
    update_notice(plugin_root / "THIRD_PARTY_NOTICES.md", commit)

    if changed or license_changed:
        previous, updated = bump_plugin_version(project_root)
        update_changelog(project_root / "CHANGELOG.md", commit)
        print(f"plugin version: {previous} -> {updated}")

    for name in SKILL_NAMES:
        actual = tree_sha256(target_skills / name)
        if actual != source_metadata[name]["tree_sha256"]:
            raise SyncError(f"post-sync hash mismatch for {name}")
    print("result: synchronized; review and commit the generated diff")
    return 0


def sync(args: argparse.Namespace) -> int:
    project_root = args.project_root.expanduser().resolve()
    source_root = args.source.expanduser().resolve()
    if not source_root.is_dir():
        raise SyncError(f"Baoyu source repository not found: {source_root}")
    repository_root = Path(git(source_root, "rev-parse", "--show-toplevel")).resolve()
    if repository_root != source_root:
        raise SyncError(f"--source must point to the Baoyu repository root: {repository_root}")
    origin = git(source_root, "remote", "get-url", "origin")
    if not re.search(r"(?:github\.com[:/])JimLiu/baoyu-skills(?:\.git)?$", origin, re.IGNORECASE):
        raise SyncError(f"unexpected Baoyu origin: {origin}")

    source_paths = [f"skills/{name}" for name in SKILL_NAMES]
    dirty_source = relevant_status(source_root, source_paths)
    if dirty_source:
        raise SyncError(f"source Skill directories contain uncommitted changes:\n{dirty_source}")

    commit = git(source_root, "rev-parse", "HEAD")
    with tempfile.TemporaryDirectory(prefix="baoyu-source-snapshot-") as temporary:
        snapshot_root = Path(temporary)
        archive_head(source_root, snapshot_root, [*source_paths, "LICENSE"])
        return sync_snapshot(args, project_root, source_root, snapshot_root, commit)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=Path(os.environ.get("BAOYU_SKILLS_SOURCE", str(DEFAULT_SOURCE))),
        help="Updated local JimLiu/baoyu-skills repository",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--check", action="store_true", help="Exit 1 when synchronization is needed")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without modifying files")
    parser.add_argument("--force", action="store_true", help="Replace locally modified vendored Skill directories")
    args = parser.parse_args()
    try:
        return sync(args)
    except (OSError, SyncError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
