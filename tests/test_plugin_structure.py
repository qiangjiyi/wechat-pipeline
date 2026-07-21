from __future__ import annotations

import json
import hashlib
import re
import shutil
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
    def test_python_runtime_resolver_selects_supported_interpreter(self) -> None:
        resolver = ROOT / "scripts" / "run_python.sh"
        self.assertTrue(resolver.is_file())
        self.assertTrue(resolver.stat().st_mode & 0o111)
        result = subprocess.run(
            [str(resolver), "-c", "import sys; print(sys.version_info[:2])"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"\(3, 1[0-9]\)")

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
        expected = {"wechat-leader", "wechat-designer", "wechat-formatter", "wechat-typesetter", "wechat-publisher"}
        found = set()
        for path in (ROOT / "agents").glob("*.md"):
            data = frontmatter(path)
            found.add(data["name"])
            self.assertTrue(data.get("description"))
            if data["name"] == "wechat-leader":
                self.assertEqual(data.get("tools"), "Agent, Bash, Read")
            elif data["name"] == "wechat-publisher":
                self.assertEqual(data.get("tools"), "Bash, Read")
            else:
                self.assertEqual(data.get("tools"), "Bash, Read, Write, Edit, Skill")
            self.assertNotIn("background", data)
            self.assertNotIn("disallowedTools", data)
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
            "gzh-design",
        }
        found = {
            frontmatter(path)["name"]
            for path in (ROOT / "skills").glob("*/SKILL.md")
        }
        self.assertEqual(found, expected)
        coordinator = (ROOT / "skills" / "wechat-pipeline" / "SKILL.md").read_text()
        self.assertIn("wechat-pipeline:wechat-leader", coordinator)
        self.assertIn("使用宿主 subagent 工具派发所需 Worker", coordinator)
        self.assertIn("HOST_RUNTIME=claude-code", coordinator)
        self.assertIn("HOST_RUNTIME=codex", coordinator)
        self.assertIn("任一能力不可用时直接返回 `blocked`", coordinator)
        self.assertIn("wechat-pipeline:baoyu-xhs-images", coordinator)
        self.assertIn("wechat-pipeline:gzh-design", coordinator)

    def test_protocol_version_is_consistent_across_runtime_prompts(self) -> None:
        version_source = (ROOT / "scripts" / "protocol_version.py").read_text(encoding="utf-8")
        match = re.search(r'PROTOCOL_VERSION = "([^"]+)"', version_source)
        self.assertIsNotNone(match)
        version = match.group(1)
        runtime_prompts = [
            ROOT / "docs" / "wechat-pipeline-protocol.md",
            ROOT / "skills" / "wechat-pipeline" / "SKILL.md",
            ROOT / "skills" / "wechat-publisher" / "SKILL.md",
            *(ROOT / "agents").glob("*.md"),
        ]
        for path in runtime_prompts:
            text = path.read_text(encoding="utf-8")
            self.assertIn(version, text, str(path))
            self.assertNotRegex(text, r"2026-07-11-00(?!2)\d", str(path))
        occurrences = sum(
            path.read_text(encoding="utf-8").count(version) for path in runtime_prompts
        )
        self.assertEqual(occurrences, 9)

        protocol = (ROOT / "docs" / "wechat-pipeline-protocol.md").read_text(encoding="utf-8")
        self.assertIn("input_sealed", protocol)
        self.assertIn("formatting", protocol)
        self.assertIn("content_ready", protocol)
        self.assertIn("designing", protocol)
        self.assertIn("artwork_ready", protocol)
        self.assertIn("publish_ready", protocol)
        self.assertNotRegex(protocol, r"content_ready\s*→\s*planning")

    def test_publish_snapshot_enforces_runtime_integrity(self) -> None:
        snapshot_builder = (
            ROOT / "scripts" / "build_publish_snapshot.py"
        ).read_text(encoding="utf-8")
        self.assertGreaterEqual(snapshot_builder.count("errors.extend(runtime_errors)"), 2)
        self.assertNotIn("Skip runtime integrity", snapshot_builder)
        run_context = (ROOT / "scripts" / "run_context.py").read_text(encoding="utf-8")
        self.assertIn('raise SystemExit("runtime integrity gate failed:', run_context)
        self.assertIn('force=target in {"publish_ready", "published"}', run_context)

    def test_visual_orchestration_delegates_complete_native_skills(self) -> None:
        designer = (ROOT / "agents" / "wechat-designer.md").read_text(encoding="utf-8")
        leader = (ROOT / "agents" / "wechat-leader.md").read_text(encoding="utf-8")
        coordinator = (ROOT / "skills" / "wechat-pipeline" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        protocol = (ROOT / "docs" / "wechat-pipeline-protocol.md").read_text(
            encoding="utf-8"
        )
        for skill in ("baoyu-xhs-images", "baoyu-cover-image", "baoyu-article-illustrator"):
            self.assertIn(f"--invocation-id {skill}", designer)
        self.assertIn("一次 Worker 只接受一份", designer)
        self.assertIn("不得自创 outline、prompt", designer)
        self.assertIn("不运行 start/build-manifest", designer)
        self.assertIn("--boundary visual start", leader)
        self.assertIn("--boundary visual build-manifest", leader)
        self.assertIn('--host-runtime "$HOST_RUNTIME"', leader)
        self.assertIn("不得由 Leader 手写 Markdown、prompt、图片、HTML、manifest 或回执", leader)
        self.assertIn('fork_turns: "none"', leader)
        self.assertIn("同时派两个", leader)
        self.assertIn("没有先后依赖", leader)
        self.assertIn("news 最终封面是否满足 `2.35:1`", protocol)
        self.assertIn("缓存的 backend 能力事实", designer)
        self.assertIn("每 5 秒一次", designer)
        self.assertIn("illustrator 改写原文会当场拒收", protocol)
        self.assertEqual(designer.count("--evidence"), 3)
        self.assertIn('"schema_version": 1', designer)
        self.assertIn("非空提示词", designer)
        self.assertIn("执行证据", protocol)
        self.assertIn("每个原生视觉 Skill", coordinator)
        self.assertIn("具体分析、图片数量、风格、配色、构图", coordinator)
        self.assertNotIn("wechat-pipeline:baoyu-image-gen`", coordinator)

    def test_formatter_self_checks_and_leader_never_repairs_markdown(self) -> None:
        formatter = (ROOT / "agents" / "wechat-formatter.md").read_text(encoding="utf-8")
        leader = (ROOT / "agents" / "wechat-leader.md").read_text(encoding="utf-8")
        protocol = (ROOT / "docs" / "wechat-pipeline-protocol.md").read_text(encoding="utf-8")
        self.assertIn("--check-only", formatter)
        self.assertIn("--invocation-id baoyu-format-markdown", formatter)
        self.assertIn("--output \"formatted=", formatter)
        self.assertIn("无条件派 Formatter", leader)
        self.assertNotIn("结构已合格时跳过 Formatter", leader)
        self.assertIn("每次完整 Pipeline 都必须原生执行一次", protocol)
        self.assertIn("禁止把 `.pipeline/input.md` 作为候选", protocol)
        self.assertTrue((ROOT / "scripts" / "skill_run.py").is_file())
        self.assertFalse((ROOT / "scripts" / "formatter_skill_run.py").exists())
        self.assertIn("--boundary formatter start", formatter)
        self.assertIn("Pipeline 不规定 H2/H3 数量", protocol)
        self.assertIn("baoyu-format-markdown/article-formatted.md", formatter)
        self.assertIn("顶层独立 `baoyu-format-markdown/` workspace", protocol)

    def test_typesetter_runs_gzh_naturally_and_leader_only_seals(self) -> None:
        typesetter = (ROOT / "agents" / "wechat-typesetter.md").read_text(encoding="utf-8")
        leader = (ROOT / "agents" / "wechat-leader.md").read_text(encoding="utf-8")
        protocol = (ROOT / "docs" / "wechat-pipeline-protocol.md").read_text(encoding="utf-8")
        skill_run = (ROOT / "scripts" / "skill_run.py").read_text(encoding="utf-8")
        self.assertIn("--boundary layout start", typesetter)
        self.assertIn("invocation_args", typesetter)
        self.assertIn("主题、组件、结构和 HTML 均由 Skill 决定", typesetter)
        self.assertNotIn("不新增作者介绍", typesetter)
        self.assertIn("不要新增作者签名", skill_run)
        self.assertIn("不写 canonical HTML", typesetter)
        self.assertIn("started / attempt-1", typesetter)
        self.assertIn("同一 gzh-design 上下文", typesetter)
        self.assertIn("禁止", typesetter)
        self.assertIn("prepare_layout.py", leader)
        self.assertIn("只用宿主 wait/终态通知", leader)
        self.assertIn("不提供 resume/attempt-2 活接口", protocol)
        self.assertFalse((ROOT / "scripts" / "layout_skill_run.py").exists())
        self.assertTrue((ROOT / "scripts" / "prepare_layout.py").is_file())

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

    def test_gzh_design_snapshot_matches_lock(self) -> None:
        lock = json.loads(
            (ROOT / "third_party" / "gzh-design.lock.json").read_text(encoding="utf-8")
        )
        self.assertEqual(lock["commit"], "ba1f4175519b481cb3566616c9e5178705067904")
        skill_root = ROOT / "skills" / "gzh-design"
        digest = hashlib.sha256()
        for path in sorted(
            path for path in skill_root.rglob("*")
            if path.is_file()
            and path.name != ".DS_Store"
            and path.suffix != ".pyc"
            and "__pycache__" not in path.parts
        ):
            relative = path.relative_to(skill_root).as_posix().encode()
            contents = path.read_bytes()
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            digest.update(len(contents).to_bytes(8, "big"))
            digest.update(contents)
        self.assertEqual(digest.hexdigest(), lock["tree_sha256"])

    def test_release_integrity_detects_modified_installed_runtime(self) -> None:
        current = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "integrity.py"), "validate", "--scope", "release"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(current.returncode, 0, current.stdout + current.stderr)
        with tempfile.TemporaryDirectory() as temp:
            copied = Path(temp) / "wechat-pipeline"
            shutil.copytree(ROOT, copied, ignore=shutil.ignore_patterns(".in_use", "__pycache__", "*.pyc"))
            leader = copied / "agents" / "wechat-leader.md"
            leader.write_text(leader.read_text(encoding="utf-8") + "\nmodified\n", encoding="utf-8")
            rejected = subprocess.run(
                [sys.executable, str(copied / "scripts" / "integrity.py"), "validate", "--scope", "release"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("agents/wechat-leader.md", rejected.stdout)

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
        self.assertIn("$PIPELINE_ROOT/scripts/plugin_doctor.py", leader)

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
