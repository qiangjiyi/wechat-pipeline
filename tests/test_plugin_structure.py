from __future__ import annotations

import json
import hashlib
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
ROOT = REPO / "plugins" / "wechat-pipeline"


def frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        raise AssertionError(f"missing frontmatter: {path}")
    values: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t")):
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip()
    return values


class PluginStructureTests(unittest.TestCase):
    def test_dual_manifests_and_marketplaces(self) -> None:
        claude = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
        codex = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text())
        self.assertEqual(claude["name"], "wechat-pipeline")
        self.assertEqual(codex["name"], "wechat-pipeline")
        self.assertEqual(claude["version"], codex["version"])
        self.assertNotIn("dependencies", claude)
        self.assertEqual(codex["skills"], "./skills/")

        claude_market = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())
        codex_market = json.loads((REPO / ".agents" / "plugins" / "marketplace.json").read_text())
        claude_entry = claude_market["plugins"][0]
        codex_entry = codex_market["plugins"][0]
        self.assertEqual(claude_entry["source"], "./plugins/wechat-pipeline")
        self.assertEqual(codex_entry["source"]["path"], "./plugins/wechat-pipeline")
        self.assertEqual(codex_entry["policy"]["installation"], "AVAILABLE")
        self.assertEqual(codex_entry["policy"]["authentication"], "ON_USE")

    def test_agents_use_plugin_layout_and_tool_boundaries(self) -> None:
        expected = {"wechat-leader", "wechat-designer", "wechat-formatter", "wechat-publisher"}
        found = set()
        for path in (ROOT / "agents").glob("*.md"):
            data = frontmatter(path)
            found.add(data["name"])
            self.assertTrue(data.get("description"))
            self.assertEqual(data.get("background"), "false")
            if data["name"] == "wechat-leader":
                self.assertEqual(data.get("disallowedTools"), "Skill")
            else:
                self.assertEqual(data.get("disallowedTools"), "Agent")
        self.assertEqual(found, expected)
        self.assertFalse((ROOT / ".claude" / "agents").exists())

    def test_cross_host_front_door_and_bundled_skills(self) -> None:
        expected = {
            "wechat-pipeline",
            "wechat-pipeline-setup",
            "wechat-publisher",
            "baoyu-format-markdown",
            "baoyu-xhs-images",
            "baoyu-cover-image",
            "baoyu-article-illustrator",
            "baoyu-image-gen",
        }
        found = {
            frontmatter(path)["name"]
            for path in (ROOT / "skills").glob("*/SKILL.md")
        }
        self.assertEqual(found, expected)
        coordinator = (ROOT / "skills" / "wechat-pipeline" / "SKILL.md").read_text()
        self.assertIn("wechat-pipeline:wechat-leader", coordinator)
        self.assertIn("Use Codex subagent tools", coordinator)
        self.assertIn("wechat-pipeline:baoyu-xhs-images", coordinator)

    def test_protocol_version_is_consistent_across_runtime_prompts(self) -> None:
        version_source = (ROOT / "scripts" / "protocol_version.py").read_text(encoding="utf-8")
        match = re.search(r'PROTOCOL_VERSION = "([^"]+)"', version_source)
        self.assertIsNotNone(match)
        version = match.group(1)
        runtime_prompts = [
            ROOT / "docs" / "wechat-pipeline-protocol.md",
            ROOT / "skills" / "wechat-pipeline" / "SKILL.md",
            *(ROOT / "agents").glob("*.md"),
        ]
        for path in runtime_prompts:
            text = path.read_text(encoding="utf-8")
            self.assertIn(version, text, str(path))
            self.assertNotRegex(text, r"2026-07-11-00(?!2)\d", str(path))

        protocol = (ROOT / "docs" / "wechat-pipeline-protocol.md").read_text(encoding="utf-8")
        self.assertIn(
            "input_sealed -> planning -> rendering -> ready -> publishing -> published",
            protocol,
        )

    def test_baoyu_snapshot_matches_lock(self) -> None:
        lock = json.loads(
            (ROOT / "third_party" / "baoyu-skills.lock.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            lock["commit"], "6b7a2e417500561a5ecdd0b168332f4142584617"
        )
        for name, metadata in lock["skills"].items():
            skill_root = ROOT / "skills" / name
            digest = hashlib.sha256()
            files = sorted(
                path for path in skill_root.rglob("*")
                if path.is_file() and path.name != ".DS_Store"
            )
            for path in files:
                relative = path.relative_to(skill_root).as_posix().encode()
                contents = path.read_bytes()
                digest.update(len(relative).to_bytes(8, "big"))
                digest.update(relative)
                digest.update(len(contents).to_bytes(8, "big"))
                digest.update(contents)
            self.assertEqual(digest.hexdigest(), metadata["tree_sha256"], name)

    def test_repository_is_portable_and_contains_no_runtime_secrets(self) -> None:
        checked_suffixes = {".md", ".py", ".json", ".mjs", ".sh"}
        for path in REPO.rglob("*"):
            if ".git" in path.parts or not path.is_file() or path.suffix not in checked_suffixes:
                continue
            forbidden_home = "/Users/" + "jiyi"
            self.assertNotIn(forbidden_home, path.read_text(encoding="utf-8"), str(path))
        self.assertFalse((ROOT / "skills" / "wechat-publisher" / ".env").exists())
        self.assertFalse((ROOT / "skills" / "wechat-publisher" / "node_modules").exists())
        self.assertTrue((ROOT / "skills" / "wechat-publisher" / ".env.example").is_file())

    def test_plugin_init_does_not_overwrite_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / ".env"
            command = [
                sys.executable,
                str(ROOT / "scripts" / "plugin_doctor.py"),
                "--init",
                "--env-file",
                str(config),
            ]
            first = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            config.write_text("KEEP_ME=1\n", encoding="utf-8")
            second = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(config.read_text(encoding="utf-8"), "KEEP_ME=1\n")

    def test_installed_setup_uses_plugin_skill_not_system_path(self) -> None:
        setup = ROOT / "skills" / "wechat-pipeline-setup" / "SKILL.md"
        self.assertEqual(frontmatter(setup)["name"], "wechat-pipeline-setup")
        self.assertIn("${PIPELINE_ROOT}/scripts/plugin_doctor.py", setup.read_text())

        readme = (REPO / "README.md").read_text(encoding="utf-8")
        leader = (ROOT / "agents" / "wechat-leader.md").read_text(encoding="utf-8")
        self.assertIn("/wechat-pipeline:wechat-pipeline-setup", readme)
        self.assertIn("$wechat-pipeline:wechat-pipeline-setup", readme)
        self.assertNotIn("`wechat-pipeline-doctor --mode", leader)
        self.assertIn("${PIPELINE_ROOT}/scripts/plugin_doctor.py", leader)

    def test_embedded_publisher_newspic_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            image = root / "card.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\nportable-test")
            source = root / "source.md"
            source.write_text(
                "---\naccount: personal\nimages:\n  - card.png\n---\n\n# Test title\n\nTest content.\n",
                encoding="utf-8",
            )
            env_file = root / ".env"
            env_file.write_text(
                "WECHAT_ACCOUNTS=personal\nWECHAT_PERSONAL_ACCESS_TOKEN=test-token\n",
                encoding="utf-8",
            )
            command = [
                sys.executable,
                str(ROOT / "skills" / "wechat-publisher" / "scripts" / "publish.py"),
                "newspic",
                str(source),
                "--env-file",
                str(env_file),
                "--dry-run",
            ]
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["mode"], "newspic")
            self.assertEqual(payload["account"], "personal")
            self.assertEqual(payload["draft"]["images"], [str(image.resolve())])


if __name__ == "__main__":
    unittest.main()
