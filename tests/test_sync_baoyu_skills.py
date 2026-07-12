from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "sync_baoyu_skills.py"
SKILLS = (
    "baoyu-format-markdown",
    "baoyu-xhs-images",
    "baoyu-cover-image",
    "baoyu-article-illustrator",
    "baoyu-image-gen",
)


def run(*command: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)


def digest(root: Path) -> str:
    value = hashlib.sha256()
    for path in sorted(path for path in root.rglob("*") if path.is_file() and path.name != ".DS_Store"):
        relative = path.relative_to(root).as_posix().encode()
        contents = path.read_bytes()
        value.update(len(relative).to_bytes(8, "big"))
        value.update(relative)
        value.update(len(contents).to_bytes(8, "big"))
        value.update(contents)
    return value.hexdigest()


class BaoyuSyncTests(unittest.TestCase):
    def git(self, root: Path, *args: str) -> str:
        result = run("git", *args, cwd=root)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def make_source(self, root: Path) -> str:
        source = root / "source"
        source.mkdir()
        self.git(source, "init", "-q")
        self.git(source, "config", "user.name", "Test")
        self.git(source, "config", "user.email", "test@example.com")
        self.git(source, "remote", "add", "origin", "https://github.com/JimLiu/baoyu-skills.git")
        (source / "LICENSE").write_text("upstream license\n", encoding="utf-8")
        for name in SKILLS:
            skill = source / "skills" / name
            skill.mkdir(parents=True)
            skill.joinpath("SKILL.md").write_text(
                f"---\nname: {name}\nversion: 2.0.0\n---\n\n# {name}\n",
                encoding="utf-8",
            )
            skill.joinpath("new-reference.md").write_text("new\n", encoding="utf-8")
        self.git(source, "add", ".")
        self.git(source, "commit", "-qm", "fixture")
        return self.git(source, "rev-parse", "HEAD")

    def make_project(self, root: Path) -> Path:
        project = root / "project"
        plugin = project / "plugins" / "wechat-pipeline"
        for name in SKILLS:
            skill = plugin / "skills" / name
            skill.mkdir(parents=True)
            skill.joinpath("SKILL.md").write_text(
                f"---\nname: {name}\nversion: 1.0.0\n---\n",
                encoding="utf-8",
            )
            skill.joinpath("removed-upstream.md").write_text("old\n", encoding="utf-8")
        for host in (".claude-plugin", ".codex-plugin"):
            manifest = plugin / host / "plugin.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text('{"name":"wechat-pipeline","version":"0.1.0"}\n', encoding="utf-8")
        lock = plugin / "third_party" / "baoyu-skills.lock.json"
        lock.parent.mkdir(parents=True)
        lock.write_text(
            json.dumps({
                "repository": "old",
                "commit": "0" * 40,
                "license": "MIT",
                "skills": {},
            }),
            encoding="utf-8",
        )
        license_path = plugin / "third_party" / "baoyu-skills" / "LICENSE"
        license_path.parent.mkdir(parents=True)
        license_path.write_text("old license\n", encoding="utf-8")
        (plugin / "THIRD_PARTY_NOTICES.md").write_text(
            f"Upstream commit: `{'0' * 40}`.\n", encoding="utf-8"
        )
        (project / "CHANGELOG.md").write_text("# Changelog\n\n## Unreleased\n", encoding="utf-8")
        self.git(project, "init", "-q")
        self.git(project, "config", "user.name", "Test")
        self.git(project, "config", "user.email", "test@example.com")
        self.git(project, "add", ".")
        self.git(project, "commit", "-qm", "fixture")
        return project

    def invoke(self, source: Path, project: Path, *extra: str) -> subprocess.CompletedProcess[str]:
        return run(
            sys.executable,
            str(SCRIPT),
            "--source", str(source),
            "--project-root", str(project),
            *extra,
            cwd=project,
        )

    def test_sync_replaces_complete_directories_and_updates_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            commit = self.make_source(root)
            project = self.make_project(root)
            source = root / "source"
            (source / ".git" / "info" / "exclude").write_text("local-only.md\n", encoding="utf-8")
            (source / "skills" / SKILLS[0] / "local-only.md").write_text(
                "must not be vendored\n", encoding="utf-8"
            )

            result = self.invoke(source, project)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("plugin version: 0.1.0 -> 0.1.1", result.stdout)

            plugin = project / "plugins" / "wechat-pipeline"
            lock = json.loads(
                (plugin / "third_party" / "baoyu-skills.lock.json").read_text(encoding="utf-8")
            )
            self.assertEqual(lock["commit"], commit)
            for name in SKILLS:
                target = plugin / "skills" / name
                self.assertFalse((target / "removed-upstream.md").exists())
                self.assertTrue((target / "new-reference.md").is_file())
                self.assertFalse((target / "local-only.md").exists())
                self.assertEqual(lock["skills"][name]["tree_sha256"], digest(target))
            for host in (".claude-plugin", ".codex-plugin"):
                manifest = json.loads((plugin / host / "plugin.json").read_text(encoding="utf-8"))
                self.assertEqual(manifest["version"], "0.1.1")
            self.assertIn(commit, (plugin / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8"))

            check = self.invoke(source, project, "--check")
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
            self.assertIn("result: up to date", check.stdout)

    def test_dry_run_does_not_modify_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_source(root)
            project = self.make_project(root)
            before = self.git(project, "status", "--porcelain")
            result = self.invoke(root / "source", project, "--dry-run")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("dry run", result.stdout)
            self.assertEqual(self.git(project, "status", "--porcelain"), before)

    def test_dirty_source_skill_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_source(root)
            project = self.make_project(root)
            source = root / "source"
            (source / "skills" / SKILLS[0] / "SKILL.md").write_text("dirty\n", encoding="utf-8")
            result = self.invoke(source, project)
            self.assertEqual(result.returncode, 2)
            self.assertIn("uncommitted changes", result.stderr)


if __name__ == "__main__":
    unittest.main()
