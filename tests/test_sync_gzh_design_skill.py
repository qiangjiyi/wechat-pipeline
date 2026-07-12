from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "sync_gzh_design_skill.py"


def run(*command: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)


class GzhDesignSyncTests(unittest.TestCase):
    def git(self, root: Path, *args: str) -> str:
        result = run("git", *args, cwd=root)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def make_source(self, root: Path) -> tuple[Path, str]:
        source = root / "source"
        source.mkdir()
        self.git(source, "init", "-q")
        self.git(source, "config", "user.name", "Test")
        self.git(source, "config", "user.email", "test@example.com")
        self.git(source, "remote", "add", "origin", "https://github.com/isjiamu/gzh-design-skill.git")
        (source / "SKILL.md").write_text("---\nname: gzh-design\n---\n", encoding="utf-8")
        (source / "LICENSE").write_text("license\n", encoding="utf-8")
        for directory, filename in (
            ("references", "theme-index.md"),
            ("scripts", "validate_gzh_html.py"),
            ("assets", "preview-template.html"),
        ):
            path = source / directory / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{directory}\n", encoding="utf-8")
        self.git(source, "add", ".")
        self.git(source, "commit", "-qm", "fixture")
        return source, self.git(source, "rev-parse", "HEAD")

    def make_project(self, root: Path) -> Path:
        project = root / "project"
        target = project / "plugins" / "wechat-pipeline" / "skills" / "gzh-design"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text("old\n", encoding="utf-8")
        third_party = project / "plugins" / "wechat-pipeline" / "third_party"
        third_party.mkdir(parents=True)
        (third_party / "gzh-design.lock.json").write_text("{}\n", encoding="utf-8")
        license_path = third_party / "gzh-design" / "LICENSE"
        license_path.parent.mkdir()
        license_path.write_text("old\n", encoding="utf-8")
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

    def test_sync_replaces_runtime_and_locks_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, commit = self.make_source(root)
            project = self.make_project(root)
            result = self.invoke(source, project)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            plugin = project / "plugins" / "wechat-pipeline"
            target = plugin / "skills" / "gzh-design"
            self.assertEqual(target.joinpath("SKILL.md").read_bytes(), source.joinpath("SKILL.md").read_bytes())
            self.assertTrue(target.joinpath("references", "theme-index.md").is_file())
            lock = json.loads((plugin / "third_party" / "gzh-design.lock.json").read_text())
            self.assertEqual(lock["commit"], commit)
            check = self.invoke(source, project, "--check")
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

    def test_dirty_upstream_runtime_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, _ = self.make_source(root)
            project = self.make_project(root)
            source.joinpath("SKILL.md").write_text("dirty\n", encoding="utf-8")
            result = self.invoke(source, project)
            self.assertEqual(result.returncode, 2)
            self.assertIn("uncommitted changes", result.stderr)


if __name__ == "__main__":
    unittest.main()
