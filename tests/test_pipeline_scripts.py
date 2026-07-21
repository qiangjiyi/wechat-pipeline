from __future__ import annotations

import hashlib
import json
import os
import subprocess
import struct
import sys
import tempfile
import unittest
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "plugins" / "wechat-pipeline"
PYTHON = sys.executable

sys.path.insert(0, str(ROOT / "scripts"))

import plugin_doctor  # noqa: E402


def fake_png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload)) + kind + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            rows.extend(((x + y) & 0xFF, (2 * x + y) & 0xFF, (x + 3 * y) & 0xFF))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(rows), level=1))
        + chunk(b"IEND", b"")
    )


def solid_png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload)) + kind + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    rows = b"".join(b"\x00" + b"\xff\xff\xff" * width for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


PNG = fake_png(900, 1200)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PipelineScriptTests(unittest.TestCase):
    def run_script(self, script: str, *args: str) -> subprocess.CompletedProcess[str]:
        translated = list(args)
        if script == "native_skill_run.py":
            script = "skill_run.py"
            translated = ["--boundary", "visual", *translated]
        elif script == "formatter_skill_run.py":
            script = "skill_run.py"
            command, run_dir, *rest = translated
            translated = ["--boundary", "formatter", command, run_dir, *rest]
            if command == "complete":
                translated.extend([
                    "--invocation-id", "baoyu-format-markdown",
                    "--output", f"formatted={Path(run_dir) / 'baoyu-format-markdown' / 'article-formatted.md'}",
                ])
            elif command == "fail" and "--invocation-id" not in translated:
                translated.extend(["--invocation-id", "baoyu-format-markdown"])
        elif script == "layout_skill_run.py":
            script = "skill_run.py"
            command, run_dir, *rest = translated
            if command == "resume":
                return subprocess.CompletedProcess([], 2, "", "resume/attempt-2 is not supported")
            if command == "complete" and "--output" in rest:
                index = rest.index("--output") + 1
                rest[index] = f"html={rest[index]}"
            translated = ["--boundary", "layout", command, run_dir, "--invocation-id", "gzh-design", *rest]
            if command == "start":
                translated = ["--boundary", "layout", command, run_dir]
        elif script == "stage_guard.py":
            script = "run_context.py"
            translated = ["guard", *translated]
        elif script == "prepare_content.py" and translated and translated[0] not in {"seal", "validate"}:
            translated = ["seal", *translated]
        elif script == "validate_designer_manifest.py" and "--phase" in translated:
            index = translated.index("--phase")
            del translated[index : index + 2]
        if (
            script == "run_context.py"
            and translated
            and translated[0] == "init"
            and "--host-runtime" not in translated
        ):
            translated.extend(["--host-runtime", "codex"])
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        return subprocess.run(
            [PYTHON, str(ROOT / "scripts" / script), *translated],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    def write_image_evidence(
        self,
        workspace: Path,
        image_path: Path,
        *,
        name: str | None = None,
        prompt: str = "A real test render prompt.",
    ) -> Path:
        prompts = workspace / "prompts"
        prompts.mkdir(parents=True, exist_ok=True)
        prompt_path = prompts / f"{name or image_path.stem}.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        evidence_path = workspace / f"{name or image_path.stem}.evidence.json"
        evidence_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "provider": "test-backend",
                    "output_path": str(image_path.resolve()),
                    "output_bytes": image_path.stat().st_size,
                    "output_sha256": sha256(image_path),
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "elapsed_seconds": 1.0,
                    "cached": False,
                    "attempts": 1,
                    "prompt_file": str(prompt_path.resolve()),
                }
            ),
            encoding="utf-8",
        )
        return evidence_path

    def refresh_image_evidence(self, manifest: dict, image_index: int = 0) -> None:
        image = manifest["images"][image_index]
        output_path = Path(image["output_path"])
        digest = sha256(output_path)
        evidence_path = Path(image["evidence_path"])
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        evidence["output_bytes"] = output_path.stat().st_size
        evidence["output_sha256"] = digest
        evidence["generated_at"] = datetime.now(timezone.utc).isoformat()
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
        evidence_digest = sha256(evidence_path)
        image.update(
            {
                "output_sha256": digest,
                "evidence_sha256": evidence_digest,
            }
        )
        receipt = next(
            item
            for item in manifest["skill_runs"]
            if item["invocation_id"] == image["source_skill_run_id"]
        )
        returned = next(
            item for item in receipt["returned_outputs"] if item["path"] == str(output_path)
        )
        returned.update({"sha256": digest, "evidence_sha256": evidence_digest})

    def write_formatter_output(self, run_dir: Path, value: str | None = None) -> Path:
        started = self.run_script("formatter_skill_run.py", "start", str(run_dir))
        self.assertEqual(started.returncode, 0, started.stderr)
        start_record = json.loads(started.stdout)
        self.assertEqual(
            start_record["skill_identifier"], "wechat-pipeline:baoyu-format-markdown"
        )
        output = Path(start_record["output_path"])
        working_input = Path(start_record["working_input_path"])
        self.assertEqual(output, run_dir / "baoyu-format-markdown" / "article-formatted.md")
        self.assertTrue(working_input.is_file())
        if value is None:
            value = (run_dir / ".pipeline" / "input.md").read_text(encoding="utf-8")
        output.write_text(value, encoding="utf-8")
        completed = self.run_script("formatter_skill_run.py", "complete", str(run_dir))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(working_input.exists())
        return output

    def make_v2_newspic_run(self, root: Path) -> tuple[Path, Path]:
        source = root / "source.md"
        source.write_text("# Article\n\nBody.\n", encoding="utf-8")
        created = self.run_script(
            "run_context.py", "init", "--mode", "newspic", "--account", "xiyue",
            "--slug", "v2-newspic", "--source", str(source),
            "--exports-root", str(root / "exports"),
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        run_dir = Path(json.loads(created.stdout)["run_dir"])
        for status in ("formatting",):
            moved = self.run_script(
                "run_context.py", "status", str(run_dir), status, "--actor", "wechat-leader"
            )
            self.assertEqual(moved.returncode, 0, moved.stderr)
        formatter_output = self.write_formatter_output(run_dir)
        prepared = self.run_script(
            "prepare_content.py", str(run_dir), "--source", str(formatter_output)
        )
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        for status in ("content_ready", "designing"):
            moved = self.run_script(
                "run_context.py", "status", str(run_dir), status, "--actor", "wechat-leader"
            )
            self.assertEqual(moved.returncode, 0, moved.stderr)

        pipeline = run_dir / ".pipeline"
        started = self.run_script(
            "native_skill_run.py", "start", str(run_dir), "--skill", "baoyu-xhs-images"
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        native_run = json.loads(started.stdout)
        self.assertIn("image_backend_capabilities", native_run)
        self.assertIn("fallback_order", native_run["image_backend_capabilities"])
        image = Path(native_run["workspace"]) / "01-card.png"
        image.write_bytes(PNG)
        evidence = self.write_image_evidence(Path(native_run["workspace"]), image)
        completed = self.run_script(
            "native_skill_run.py", "complete", str(run_dir),
            "--invocation-id", "baoyu-xhs-images", "--output", f"card={image}",
            "--evidence", str(evidence),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        built_manifest = self.run_script("native_skill_run.py", "build-manifest", str(run_dir))
        self.assertEqual(built_manifest.returncode, 0, built_manifest.stderr)
        manifest_path = pipeline / "manifest.json"
        moved = self.run_script(
            "run_context.py", "status", str(run_dir), "artwork_ready", "--actor", "wechat-leader"
        )
        self.assertEqual(moved.returncode, 0, moved.stderr)
        built = self.run_script("build_publish_snapshot.py", str(run_dir))
        self.assertEqual(built.returncode, 0, built.stderr)
        ready = self.run_script(
            "run_context.py", "status", str(run_dir), "publish_ready", "--actor", "wechat-leader"
        )
        self.assertEqual(ready.returncode, 0, ready.stderr)
        return run_dir, manifest_path

    def make_designing_run(self, root: Path, mode: str) -> Path:
        source = root / "source.md"
        source.write_text("# Article\n\nBody.\n", encoding="utf-8")
        created = self.run_script(
            "run_context.py", "init", "--mode", mode, "--account", "xiyue",
            "--slug", f"contract-{mode}", "--source", str(source),
            "--exports-root", str(root / "exports"),
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        run_dir = Path(json.loads(created.stdout)["run_dir"])
        formatting = self.run_script(
            "run_context.py", "status", str(run_dir), "formatting", "--actor", "wechat-leader"
        )
        self.assertEqual(formatting.returncode, 0, formatting.stderr)
        formatter_output = self.write_formatter_output(run_dir)
        prepared = self.run_script(
            "prepare_content.py", str(run_dir), "--source", str(formatter_output)
        )
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        for status in ("content_ready", "designing"):
            moved = self.run_script(
                "run_context.py", "status", str(run_dir), status, "--actor", "wechat-leader"
            )
            self.assertEqual(moved.returncode, 0, moved.stderr)
        return run_dir

    def test_host_runtime_is_required_and_cross_host_resume_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.md"
            source.write_text("# Article\n", encoding="utf-8")
            base = [
                PYTHON,
                str(ROOT / "scripts" / "run_context.py"),
                "init",
                "--mode",
                "newspic",
                "--account",
                "xiyue",
                "--slug",
                "host-gate",
                "--source",
                str(source),
                "--exports-root",
                str(root / "exports"),
            ]
            env = os.environ.copy()
            env.pop("CLAUDECODE", None)
            missing = subprocess.run(base, capture_output=True, text=True, check=False, env=env)
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("--host-runtime", missing.stderr)

            claude_without_marker = subprocess.run(
                [*base, "--host-runtime", "claude-code"],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
            self.assertNotEqual(claude_without_marker.returncode, 0)
            self.assertIn("requires the CLAUDECODE environment marker", claude_without_marker.stderr)

            created = self.run_script(
                "run_context.py",
                *base[2:],
                "--host-runtime",
                "codex",
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            payload = json.loads(created.stdout)
            self.assertEqual(payload["host_runtime"], "codex")
            run_dir = Path(payload["run_dir"])

            claude_env = env | {"CLAUDECODE": "1"}
            cross_host_guard = subprocess.run(
                [
                    PYTHON,
                    str(ROOT / "scripts" / "run_context.py"),
                    "guard",
                    str(run_dir),
                    "formatter",
                ],
                capture_output=True,
                text=True,
                check=False,
                env=claude_env,
            )
            self.assertNotEqual(cross_host_guard.returncode, 0)
            self.assertIn(
                "created for codex", cross_host_guard.stdout + cross_host_guard.stderr
            )

    def test_visual_complete_requires_valid_execution_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = self.make_designing_run(Path(temp), "news")
            started = self.run_script(
                "skill_run.py",
                "--boundary",
                "visual",
                "start",
                str(run_dir),
                "--skill",
                "baoyu-cover-image",
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            workspace = Path(json.loads(started.stdout)["workspace"])
            image_dir = workspace / "imgs"
            image_dir.mkdir()
            cover = image_dir / "cover.png"
            cover.write_bytes(fake_png(470, 200))

            missing = self.run_script(
                "skill_run.py",
                "--boundary",
                "visual",
                "complete",
                str(run_dir),
                "--invocation-id",
                "baoyu-cover-image",
                "--output",
                f"cover={cover}",
            )
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("requires one --evidence", missing.stderr)

            evidence = self.write_image_evidence(workspace, cover, prompt="")
            empty_prompt = self.run_script(
                "skill_run.py",
                "--boundary",
                "visual",
                "complete",
                str(run_dir),
                "--invocation-id",
                "baoyu-cover-image",
                "--output",
                f"cover={cover}",
                "--evidence",
                str(evidence),
            )
            self.assertNotEqual(empty_prompt.returncode, 0)
            self.assertIn("prompt_file is missing or empty", empty_prompt.stderr)

            evidence = self.write_image_evidence(workspace, cover)
            completed = self.run_script(
                "skill_run.py",
                "--boundary",
                "visual",
                "complete",
                str(run_dir),
                "--invocation-id",
                "baoyu-cover-image",
                "--output",
                f"cover={cover}",
                "--evidence",
                str(evidence),
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            returned = json.loads(completed.stdout)["returned_outputs"][0]
            self.assertEqual(returned["evidence_path"], str(evidence.resolve()))

    def test_failed_visual_receipt_requires_leader_reset_and_rebuilds_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = self.make_designing_run(Path(temp), "newspic")
            started = self.run_script(
                "skill_run.py", "--boundary", "visual", "start", str(run_dir),
                "--skill", "baoyu-xhs-images",
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            record = json.loads(started.stdout)
            workspace = Path(record["workspace"])
            (workspace / "partial.tmp").write_text("partial", encoding="utf-8")
            failed = self.run_script(
                "skill_run.py", "--boundary", "visual", "fail", str(run_dir),
                "--invocation-id", "baoyu-xhs-images", "--error", "backend failed",
            )
            self.assertEqual(failed.returncode, 0, failed.stderr)
            denied = self.run_script(
                "skill_run.py", "--boundary", "visual", "reset", str(run_dir),
                "--invocation-id", "baoyu-xhs-images", "--actor", "wechat-designer",
            )
            self.assertNotEqual(denied.returncode, 0)
            reset = self.run_script(
                "skill_run.py", "--boundary", "visual", "reset", str(run_dir),
                "--invocation-id", "baoyu-xhs-images", "--actor", "wechat-leader",
            )
            self.assertEqual(reset.returncode, 0, reset.stderr)
            repaired = json.loads(reset.stdout)
            self.assertEqual(repaired["status"], "started")
            self.assertEqual(repaired["reset_count"], 1)
            self.assertFalse((workspace / "partial.tmp").exists())
            self.assertTrue(Path(repaired["working_input_path"]).is_file())
            events = (run_dir / ".pipeline" / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn('"event":"skill.reset"', events)

    def test_visual_complete_rejects_wrong_role_before_manifest_build(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = self.make_designing_run(Path(temp), "news")
            started = self.run_script(
                "skill_run.py", "--boundary", "visual", "start", str(run_dir),
                "--skill", "baoyu-article-illustrator",
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            workspace = Path(json.loads(started.stdout)["workspace"])
            article = workspace / "article.md"
            article.write_bytes((run_dir / "content.md").read_bytes())
            image = workspace / "body.png"
            image.write_bytes(PNG)
            rejected = self.run_script(
                "skill_run.py", "--boundary", "visual", "complete", str(run_dir),
                "--invocation-id", "baoyu-article-illustrator",
                "--output", f"body={article}", "--output", f"body={image}",
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("exactly one article", rejected.stderr)
            self.assertIn("Copyable example", rejected.stderr)
            receipt = json.loads(
                (run_dir / ".pipeline" / "skill-runs" / "baoyu-article-illustrator.json").read_text()
            )
            self.assertEqual(receipt["status"], "started")

    def test_illustrator_complete_rejects_rewritten_source_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = self.make_designing_run(Path(temp), "news")
            started = self.run_script(
                "skill_run.py", "--boundary", "visual", "start", str(run_dir),
                "--skill", "baoyu-article-illustrator",
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            workspace = Path(json.loads(started.stdout)["workspace"])
            article = workspace / "article.md"
            article.write_text("# Article\n\nCompletely different copy.\n", encoding="utf-8")
            image = workspace / "body.png"
            image.write_bytes(PNG)
            rejected = self.run_script(
                "skill_run.py", "--boundary", "visual", "complete", str(run_dir),
                "--invocation-id", "baoyu-article-illustrator",
                "--output", f"article={article}", "--output", f"body={image}",
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("Illustrator article removed or rewrote", rejected.stderr)

    def test_leader_can_auditably_amend_one_historical_visual_role(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = self.make_designing_run(Path(temp), "news")
            started = self.run_script(
                "skill_run.py", "--boundary", "visual", "start", str(run_dir),
                "--skill", "baoyu-article-illustrator",
            )
            workspace = Path(json.loads(started.stdout)["workspace"])
            article = workspace / "article.md"
            article.write_bytes((run_dir / "content.md").read_bytes())
            image = workspace / "body.png"
            image.write_bytes(PNG)
            evidence = self.write_image_evidence(workspace, image)
            completed = self.run_script(
                "skill_run.py", "--boundary", "visual", "complete", str(run_dir),
                "--invocation-id", "baoyu-article-illustrator",
                "--output", f"article={article}", "--output", f"body={image}",
                "--evidence", str(evidence),
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            receipt_path = run_dir / ".pipeline" / "skill-runs" / "baoyu-article-illustrator.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["returned_outputs"][0]["role"] = "body"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            amended = self.run_script(
                "skill_run.py", "--boundary", "visual", "amend-role", str(run_dir),
                "--invocation-id", "baoyu-article-illustrator", "--from", "body",
                "--to", "article", "--path", str(article), "--actor", "wechat-leader",
            )
            self.assertEqual(amended.returncode, 0, amended.stderr)
            amended_record = json.loads(amended.stdout)
            self.assertEqual(amended_record["returned_outputs"][0]["role"], "article")
            self.assertEqual(amended_record["amendments"][-1]["actor"], "wechat-leader")
            self.assertIn(
                '"event":"skill.role_amended"',
                (run_dir / ".pipeline" / "events.jsonl").read_text(encoding="utf-8"),
            )

    def test_news_cover_dimension_contract_accepts_jpeg_and_webp(self) -> None:
        sys.path.insert(0, str(ROOT / "scripts"))
        from image_contracts import image_dimensions, validate_output_contract

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            width, height = 2350, 1000
            jpeg = root / "cover.jpg"
            jpeg.write_bytes(
                b"\xff\xd8\xff\xc0\x00\x0b\x08"
                + height.to_bytes(2, "big") + width.to_bytes(2, "big")
                + b"\x01\x01\x11\x00\xff\xd9"
            )
            webp = root / "cover.webp"
            webp.write_bytes(
                b"RIFF" + (22).to_bytes(4, "little") + b"WEBPVP8X"
                + (10).to_bytes(4, "little") + b"\x00" * 4
                + (width - 1).to_bytes(3, "little")
                + (height - 1).to_bytes(3, "little")
            )
            for path in (jpeg, webp):
                self.assertEqual(image_dimensions(path), (width, height))
                self.assertEqual(
                    validate_output_contract("news", "baoyu-cover-image", "cover", path), []
                )

    def test_run_context_reuses_active_matching_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.md"
            source.write_text("same article\n", encoding="utf-8")
            args = (
                "init",
                "--mode", "newspic",
                "--account", "xiyue",
                "--slug", "same-article",
                "--source", str(source),
                "--exports-root", str(root / "exports"),
            )
            first = self.run_script("run_context.py", *args)
            second = self.run_script("run_context.py", *args)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            first_data = json.loads(first.stdout)
            second_data = json.loads(second.stdout)
            self.assertFalse(first_data["reused"])
            self.assertTrue(second_data["reused"])
            self.assertEqual(first_data["run_dir"], second_data["run_dir"])

    def test_run_context_does_not_reuse_a_runtime_mismatched_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.md"
            source.write_text("same article\n", encoding="utf-8")
            args = (
                "init",
                "--mode", "newspic",
                "--account", "xiyue",
                "--slug", "runtime-changed",
                "--source", str(source),
                "--exports-root", str(root / "exports"),
            )
            first = self.run_script("run_context.py", *args)
            self.assertEqual(first.returncode, 0, first.stderr)
            first_data = json.loads(first.stdout)
            runtime_path = Path(first_data["run_dir"]) / ".pipeline" / "runtime-integrity.json"
            runtime_path.chmod(0o600)
            runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
            runtime["runtime_sha256"] = "0" * 64
            runtime_path.write_text(json.dumps(runtime), encoding="utf-8")
            runtime_path.chmod(0o400)

            second = self.run_script("run_context.py", *args)
            self.assertEqual(second.returncode, 0, second.stderr)
            second_data = json.loads(second.stdout)
            self.assertFalse(second_data["reused"])
            self.assertNotEqual(first_data["run_dir"], second_data["run_dir"])

    def test_run_context_reads_exports_root_from_pipeline_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"
            config = home / ".config" / "wechat-pipeline" / ".env"
            config.parent.mkdir(parents=True)
            exports = root / "configured-exports"
            config.write_text(f"WECHAT_PIPELINE_EXPORTS_DIR={exports}\n", encoding="utf-8")
            source = root / "source.md"
            source.write_text("configured root\n", encoding="utf-8")
            env = dict(os.environ)
            env.pop("WECHAT_PIPELINE_EXPORTS_DIR", None)
            env.pop("CLAUDECODE", None)
            env["WECHAT_PUBLISHER_ENV_FILE"] = str(config)
            result = subprocess.run(
                [
                    PYTHON, str(ROOT / "scripts" / "run_context.py"), "init",
                    "--mode", "newspic", "--account", "xiyue", "--slug", "configured",
                    "--source", str(source),
                    "--host-runtime", "codex",
                ],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(
                Path(json.loads(result.stdout)["run_dir"]).is_relative_to(exports.resolve())
            )

    def test_concurrent_init_creates_only_one_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.md"
            source.write_text("same concurrent article\n", encoding="utf-8")
            args = (
                "init",
                "--mode", "newspic",
                "--account", "xiyue",
                "--slug", "concurrent",
                "--source", str(source),
                "--exports-root", str(root / "exports"),
            )
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: self.run_script("run_context.py", *args), range(2)))
            self.assertTrue(all(result.returncode == 0 for result in results))
            payloads = [json.loads(result.stdout) for result in results]
            self.assertEqual({payload["run_dir"] for payload in payloads}, {payloads[0]["run_dir"]})
            self.assertEqual(sorted(payload["reused"] for payload in payloads), [False, True])

    def test_run_files_are_private_and_progress_is_structured(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.md"
            source.write_text("private\n", encoding="utf-8")
            created = self.run_script(
                "run_context.py", "init", "--mode", "newspic", "--account", "xiyue",
                "--slug", "private", "--source", str(source),
                "--exports-root", str(root / "exports"),
            )
            run_dir = Path(json.loads(created.stdout)["run_dir"])
            self.assertEqual(run_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual((run_dir / ".pipeline").stat().st_mode & 0o777, 0o700)
            self.assertEqual((run_dir / ".pipeline" / "input.md").stat().st_mode & 0o777, 0o400)
            moved = self.run_script(
                "run_context.py", "status", str(run_dir), "formatting",
                "--actor", "wechat-leader",
            )
            self.assertEqual(moved.returncode, 0, moved.stderr)
            progress = self.run_script(
                "run_context.py", "progress", str(run_dir),
                "--actor", "wechat-formatter", "--stage", "formatting",
                "--completed", "1", "--total", "3", "--message", "first image",
            )
            self.assertEqual(progress.returncode, 0, progress.stderr)
            payload = json.loads((run_dir / ".pipeline" / "progress.json").read_text())
            self.assertEqual((payload["completed"], payload["total"]), (1, 3))

    def test_run_status_rejects_invalid_transition(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.md"
            source.write_text("article\n", encoding="utf-8")
            created = self.run_script(
                "run_context.py", "init",
                "--mode", "newspic",
                "--account", "xiyue",
                "--slug", "status-test",
                "--source", str(source),
                "--exports-root", str(root / "exports"),
            )
            run_dir = json.loads(created.stdout)["run_dir"]
            worker_write = self.run_script(
                "run_context.py", "status", run_dir, "formatting", "--actor", "wechat-designer"
            )
            self.assertNotEqual(worker_write.returncode, 0)
            self.assertIn("only be changed by actor wechat-leader", worker_write.stderr)
            invalid = self.run_script("run_context.py", "status", run_dir, "published", "--actor", "wechat-leader")
            self.assertNotEqual(invalid.returncode, 0)
            self.assertIn("invalid run status transition", invalid.stderr)
            valid = self.run_script("run_context.py", "status", run_dir, "formatting", "--actor", "wechat-leader")
            self.assertEqual(valid.returncode, 0, valid.stderr)
            failed = self.run_script("run_context.py", "status", run_dir, "failed", "--actor", "wechat-leader")
            self.assertEqual(failed.returncode, 0, failed.stderr)
            wrong_resume = self.run_script("run_context.py", "status", run_dir, "content_ready", "--actor", "wechat-leader")
            self.assertNotEqual(wrong_resume.returncode, 0)
            resumed = self.run_script("run_context.py", "status", run_dir, "formatting", "--actor", "wechat-leader")
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            events = [
                json.loads(line)
                for line in (Path(run_dir) / ".pipeline" / "events.jsonl").read_text().splitlines()
            ]
            self.assertEqual(events[0]["event"], "run.created")
            self.assertEqual(events[-1]["details"]["from"], "failed")
            self.assertEqual(events[-1]["details"]["to"], "formatting")
            self.assertTrue(all(event["actor"] == "wechat-leader" for event in events))

    def test_content_ready_gate_requires_formatted_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.md"
            source.write_text("# Article\n", encoding="utf-8")
            created = self.run_script(
                "run_context.py", "init",
                "--mode", "news",
                "--account", "xiyue",
                "--slug", "layout-status",
                "--source", str(source),
                "--exports-root", str(root / "exports"),
            )
            run_dir = Path(json.loads(created.stdout)["run_dir"])
            formatting = self.run_script(
                "run_context.py", "status", str(run_dir), "formatting", "--actor", "wechat-leader"
            )
            self.assertEqual(formatting.returncode, 0, formatting.stderr)
            rejected = self.run_script(
                "run_context.py", "status", str(run_dir), "content_ready", "--actor", "wechat-leader"
            )
            self.assertNotEqual(rejected.returncode, 0)
            bypass_check = self.run_script(
                "prepare_content.py", str(run_dir),
                "--source", str(run_dir / ".pipeline" / "input.md"), "--check-only",
            )
            self.assertNotEqual(bypass_check.returncode, 0)
            self.assertIn("sealed input cannot bypass", bypass_check.stderr)
            bypass_seal = self.run_script(
                "prepare_content.py", str(run_dir),
                "--source", str(run_dir / ".pipeline" / "input.md"),
            )
            self.assertNotEqual(bypass_seal.returncode, 0)
            self.assertIn("sealed input cannot bypass", bypass_seal.stderr)
            unproven_workspace = run_dir / "baoyu-format-markdown"
            unproven_workspace.mkdir()
            unproven_output = unproven_workspace / "article-formatted.md"
            unproven_output.write_bytes((run_dir / ".pipeline" / "input.md").read_bytes())
            unproven = self.run_script(
                "prepare_content.py", str(run_dir), "--source", str(unproven_output)
            )
            self.assertNotEqual(unproven.returncode, 0)
            self.assertIn("Formatter Skill receipt", unproven.stderr)
            unproven_output.unlink()
            unproven_workspace.rmdir()
            formatter_output = self.write_formatter_output(run_dir)
            prepared = self.run_script(
                "prepare_content.py", str(run_dir), "--source", str(formatter_output)
            )
            self.assertEqual(prepared.returncode, 0, prepared.stderr)
            receipt = json.loads((run_dir / ".pipeline" / "format-result.json").read_text())
            self.assertEqual(receipt["formatter_status"], "executed")
            self.assertEqual(
                receipt["formatter_skill_identifier"],
                "wechat-pipeline:baoyu-format-markdown",
            )
            accepted = self.run_script(
                "run_context.py", "status", str(run_dir), "content_ready", "--actor", "wechat-leader"
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)

    def test_published_requires_a_verified_durable_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir, _ = self.make_v2_newspic_run(root)
            publishing = self.run_script(
                "run_context.py", "status", str(run_dir), "publishing", "--actor", "wechat-leader"
            )
            self.assertEqual(publishing.returncode, 0, publishing.stderr)
            rejected = self.run_script(
                "run_context.py", "status", str(run_dir), "published",
                "--actor", "wechat-leader",
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("publish-result.json", rejected.stderr)

    def test_direct_run_state_edit_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.md"
            source.write_text("# Article\n", encoding="utf-8")
            created = self.run_script(
                "run_context.py", "init", "--mode", "newspic", "--account", "xiyue",
                "--slug", "tamper", "--source", str(source),
                "--exports-root", str(root / "exports"),
            )
            run_dir = Path(json.loads(created.stdout)["run_dir"])
            run_path = run_dir / ".pipeline" / "run.json"
            run = json.loads(run_path.read_text(encoding="utf-8"))
            run["status"] = "designing"
            run_path.write_text(json.dumps(run), encoding="utf-8")
            result = self.run_script(
                "run_context.py", "status", str(run_dir), "artwork_ready", "--actor", "wechat-leader"
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("state checksum mismatch", result.stderr)

    def test_publish_result_validator_accepts_verified_matching_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir, manifest_path = self.make_v2_newspic_run(root)
            publishing = self.run_script(
                "run_context.py", "status", str(run_dir), "publishing", "--actor", "wechat-leader"
            )
            self.assertEqual(publishing.returncode, 0, publishing.stderr)
            run = json.loads((run_dir / ".pipeline" / "run.json").read_text())
            manifest = json.loads(manifest_path.read_text())
            image_path = Path(manifest["images"][0]["output_path"]).resolve()
            source_path = Path(manifest["source"]["original_path"]).resolve()
            snapshot_path = run_dir / ".pipeline" / "publish-snapshot.json"
            snapshot = json.loads(snapshot_path.read_text())
            (run_dir / ".pipeline" / "publish-result.json").write_text(
                json.dumps({
                    "schema_version": 1,
                    "ok": True,
                    "protocol_version": "2026-07-20-001",
                    "run_id": run["run_id"],
                    "mode": "newspic",
                    "account": "xiyue",
                    "publish_fingerprint": "f" * 64,
                    "draft_media_id": "draft-1",
                    "creation_status": "created",
                    "snapshot_sha256": sha256(snapshot_path),
                    "snapshot_fingerprint": snapshot["fingerprint"],
                    "manifest_sha256": sha256(manifest_path),
                    "source_sha256": sha256(source_path),
                    "images": [{"path": str(image_path), "sha256": sha256(image_path)}],
                    "uploaded_image_media_ids": ["image-1"],
                    "verification": {
                        "ok": True,
                        "status": "verified",
                        "method": "draft/get",
                        "verified_at": datetime.now(timezone.utc).isoformat(),
                    },
                }),
                encoding="utf-8",
            )
            validated = self.run_script("validate_publish_result.py", str(run_dir))
            self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)
            self.assertEqual(json.loads(validated.stdout)["draft_media_id"], "draft-1")
            published = self.run_script(
                "run_context.py", "status", str(run_dir), "published", "--actor", "wechat-leader"
            )
            self.assertEqual(published.returncode, 0, published.stderr)
            integrity_cache = json.loads(
                (run_dir / ".pipeline" / "integrity-cache.json").read_text(encoding="utf-8")
            )
            self.assertEqual(integrity_cache["full_hash_count"], 3)

    def test_mode_specific_state_machine_rejects_skipped_stages(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.md"
            source.write_text("# Article\n", encoding="utf-8")
            created = self.run_script(
                "run_context.py", "init", "--mode", "news", "--account", "xiyue",
                "--slug", "strict-state", "--source", str(source),
                "--exports-root", str(root / "exports"),
            )
            run_dir = json.loads(created.stdout)["run_dir"]
            formatting = self.run_script(
                "run_context.py", "status", run_dir, "formatting", "--actor", "wechat-leader"
            )
            self.assertEqual(formatting.returncode, 0, formatting.stderr)
            skipped_render = self.run_script(
                "run_context.py", "status", run_dir, "designing", "--actor", "wechat-leader"
            )
            self.assertNotEqual(skipped_render.returncode, 0)

    def test_publish_result_rejects_skipped_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest_path = self.make_run(Path(temp), status="success")
            run_dir = manifest_path.parent.parent
            run_path = run_dir / ".pipeline" / "run.json"
            run = json.loads(run_path.read_text())
            run["status"] = "publishing"
            run_path.write_text(json.dumps(run), encoding="utf-8")
            receipt = {
                "schema_version": 1,
                "protocol_version": "2026-07-20-001",
                "run_id": run["run_id"],
                "mode": "newspic",
                "account": "xiyue",
                "publish_fingerprint": "f" * 64,
                "draft_media_id": "draft-1",
                "creation_status": "created",
                "verification": {"ok": True, "status": "skipped"},
            }
            (run_dir / ".pipeline" / "publish-result.json").write_text(
                json.dumps(receipt), encoding="utf-8"
            )
            result = self.run_script("validate_publish_result.py", str(run_dir))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("draft/get", result.stdout)

    def test_newspic_dry_run_is_bound_to_manifest_text_and_images(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir, manifest_path = self.make_v2_newspic_run(Path(temp))
            result = subprocess.run(
                [
                    PYTHON,
                    str(ROOT / "skills" / "wechat-publisher" / "scripts" / "publish.py"),
                    "newspic",
                    "--manifest", str(manifest_path),
                    "--snapshot", str(run_dir / ".pipeline" / "publish-snapshot.json"),
                    "--account", "xiyue",
                    "--result-output", str(run_dir / ".pipeline" / "publish-result.json"),
                    "--verify-draft",
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual(payload["draft"]["title"], "Article")
            self.assertEqual(
                payload["draft"]["images"],
                [str(Path(manifest["images"][0]["output_path"]).resolve())],
            )

    def test_seal_rejects_mismatched_run_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.md"
            source.write_text("# Article\n", encoding="utf-8")
            created = self.run_script(
                "run_context.py", "init",
                "--mode", "newspic",
                "--account", "xiyue",
                "--slug", "seal-test",
                "--source", str(source),
                "--exports-root", str(root / "exports"),
            )
            run_dir = Path(json.loads(created.stdout)["run_dir"])
            run_path = run_dir / ".pipeline" / "run.json"
            run = json.loads(run_path.read_text(encoding="utf-8"))
            run["canonical_output_dir"] = str(root / "wrong-run")
            run_path.write_text(json.dumps(run), encoding="utf-8")
            result = self.run_script(
                "run_context.py", "seal", str(run_dir), "--actor", "wechat-leader"
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("canonical_output_dir", result.stderr)

    def test_seal_rejects_active_or_terminal_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.md"
            source.write_text("article\n", encoding="utf-8")
            created = self.run_script(
                "run_context.py", "init",
                "--mode", "newspic",
                "--account", "xiyue",
                "--slug", "seal-status",
                "--source", str(source),
                "--exports-root", str(root / "exports"),
            )
            run_dir = json.loads(created.stdout)["run_dir"]
            planning = self.run_script(
                "run_context.py", "status", run_dir, "formatting",
                "--actor", "wechat-leader",
            )
            self.assertEqual(planning.returncode, 0, planning.stderr)
            result = self.run_script(
                "run_context.py", "seal", run_dir, "--actor", "wechat-leader"
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("cannot seal run while status is 'formatting'", result.stderr)

    def test_slug_error_reports_the_original_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.md"
            source.write_text("# Article\n", encoding="utf-8")
            result = self.run_script(
                "run_context.py", "init",
                "--mode", "newspic",
                "--account", "xiyue",
                "--slug", "周报",
                "--source", str(source),
                "--exports-root", str(Path(temp) / "exports"),
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("'周报'", result.stderr)

    def test_slug_timestamp_suffix_is_not_duplicated_with_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.md"
            source.write_text("# Article\n", encoding="utf-8")
            result = self.run_script(
                "run_context.py", "init",
                "--mode", "news",
                "--account", "xiyue",
                "--slug", "article-20260719-171758",
                "--source", str(source),
                "--exports-root", str(root / "exports"),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            run_dir = Path(payload["run_dir"])
            self.assertRegex(run_dir.name, r"^article-\d{8}-\d{6}-[0-9a-f]{6}$")
            self.assertNotIn("article-20260719-171758-20260719-171758", run_dir.name)

    def test_preflight_reads_entire_env_without_printing_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            env_file = Path(temp) / ".env"
            lines = [f"# filler {index}" for index in range(60)]
            lines.append("OPENAI_API_KEY=super-secret-value")
            lines.append("OPENAI_BASE_URL=https://example.test/v1")
            env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
            result = self.run_script("preflight_image_backends.py", "--env-file", str(env_file))
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertIn("openai-native", data["fallback_order"])
            self.assertNotIn("super-secret-value", result.stdout)

    def make_run(self, root: Path, *, status: str, attempt_before_prompt: bool = False) -> Path:
        run_dir = root / "image-cards" / "sample-run"
        pipeline = run_dir / ".pipeline"
        pipeline.mkdir(parents=True)
        input_path = pipeline / "input.md"
        input_path.write_text("article\n", encoding="utf-8")
        content_path = run_dir / "content.md"
        content_path.write_text("# Article\n\nBody.\n", encoding="utf-8")
        workspace = run_dir / "baoyu-xhs-images"
        workspace.mkdir(parents=True)
        image_path = workspace / "01-card.png"
        image_path.write_bytes(PNG)
        evidence_path = self.write_image_evidence(workspace, image_path)
        skill_path = ROOT / "skills" / "baoyu-xhs-images" / "SKILL.md"

        started = datetime.now(timezone.utc)
        completed = started - timedelta(seconds=1) if attempt_before_prompt else started + timedelta(seconds=1)
        run = {
            "protocol_version": "2026-07-20-001",
            "run_id": "sample-run",
            "mode": "newspic",
            "account": "xiyue",
            "canonical_output_dir": str(run_dir),
            "input_path": str(input_path),
            "source_sha256": sha256(input_path),
            "status": "artwork_ready",
        }
        (pipeline / "run.json").write_text(json.dumps(run), encoding="utf-8")
        skill_run = {
            "schema_version": 1,
            "protocol_version": "2026-07-20-001",
            "run_id": "sample-run",
            "invocation_id": "baoyu-xhs-images",
            "skill_name": "baoyu-xhs-images",
            "skill_identifier": "wechat-pipeline:baoyu-xhs-images",
            "skill_path": str(skill_path),
            "skill_sha256": sha256(skill_path),
            "invocation_method": "native-skill",
            "skill_options": {},
            "input_path": str(content_path),
            "input_sha256": sha256(content_path),
            "workspace": str(workspace),
            "working_input_path": str(workspace / "article.md"),
            "working_input_initial_sha256": sha256(content_path),
            "started_at": started.isoformat(),
            "completed_at": completed.isoformat(),
            "status": status,
            "returned_outputs": [{
                "role": "card", "path": str(image_path), "sha256": sha256(image_path),
                "evidence_path": str(evidence_path),
                "evidence_sha256": sha256(evidence_path),
            }],
            "error_summary": "" if status == "success" else "native Skill failed",
        }
        manifest = {
            "schema_version": 5,
            "protocol_version": "2026-07-20-001",
            "run_id": "sample-run",
            "mode": "newspic",
            "canonical_output_dir": str(run_dir),
            "source": {
                "original_path": str(input_path),
                "original_sha256": sha256(input_path),
                "publisher_text_sha256": sha256(input_path),
                "content_path": str(content_path),
                "content_sha256": sha256(content_path),
            },
            "skill_runs": [skill_run],
            "images": [{
                "id": "01",
                "kind": "card",
                "source_skill_run_id": "baoyu-xhs-images",
                "output_path": str(image_path),
                "output_sha256": sha256(image_path),
                "evidence_path": str(evidence_path),
                "evidence_sha256": sha256(evidence_path),
            }],
        }
        manifest_path = pipeline / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return manifest_path

    def test_publish_ready_rejects_failed_images(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest = self.make_run(Path(temp), status="failed")
            result = self.run_script(
                "validate_designer_manifest.py", str(manifest), "--phase", "publish-ready"
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("did not complete successfully", result.stdout)

    def test_missing_native_skill_run_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest_path = self.make_run(Path(temp), status="success")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["skill_runs"] = []
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            result = self.run_script(
                "validate_designer_manifest.py", str(manifest_path), "--phase", "publish-ready"
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("native Skill runs must be exactly", result.stdout)

    def test_skill_completion_cannot_predate_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest = self.make_run(Path(temp), status="success", attempt_before_prompt=True)
            result = self.run_script(
                "validate_designer_manifest.py", str(manifest), "--phase", "complete"
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("completed_at predates started_at", result.stdout)

    def test_extend_file_cannot_masquerade_as_native_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest_path = self.make_run(Path(temp), status="success")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            workspace = Path(manifest["skill_runs"][0]["workspace"])
            fake_skill = workspace / "EXTEND.md"
            fake_skill.write_text("preferred_style: fresh\n", encoding="utf-8")
            manifest["skill_runs"][0]["skill_path"] = str(fake_skill)
            manifest["skill_runs"][0]["skill_sha256"] = sha256(fake_skill)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            result = self.run_script(
                "validate_designer_manifest.py", str(manifest_path), "--phase", "publish-ready"
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("skill_path must be the bundled", result.stdout)

    def test_native_skill_internal_files_are_not_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest_path = self.make_run(Path(temp), status="success")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            workspace = Path(manifest["skill_runs"][0]["workspace"])
            (workspace / "illustration-outline.md").write_text("natural outline\n", encoding="utf-8")
            prompts = workspace / "prompts"
            prompts.mkdir(exist_ok=True)
            (prompts / "01.yaml").write_text("prompt: natural prompt\n", encoding="utf-8")
            result = self.run_script(
                "validate_designer_manifest.py", str(manifest_path), "--phase", "publish-ready"
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_returned_output_must_stay_inside_skill_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest_path = self.make_run(Path(temp), status="success")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            outside = manifest_path.parent.parent / "outside.png"
            outside.write_bytes(PNG)
            digest = sha256(outside)
            manifest["skill_runs"][0]["returned_outputs"][0].update(
                {"path": str(outside), "sha256": digest}
            )
            manifest["images"][0].update(
                {"output_path": str(outside), "output_sha256": digest}
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            result = self.run_script(
                "validate_designer_manifest.py", str(manifest_path), "--phase", "publish-ready"
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must stay inside its Skill workspace", result.stdout)

    def test_incomplete_native_skill_run_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest_path = self.make_run(Path(temp), status="success")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["skill_runs"][0]["status"] = "started"
            manifest["skill_runs"][0]["completed_at"] = None
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            result = self.run_script(
                "validate_designer_manifest.py", str(manifest_path), "--phase", "complete"
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("did not complete successfully", result.stdout)

    def test_skill_identifier_must_be_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest_path = self.make_run(Path(temp), status="success")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["skill_runs"][0]["skill_identifier"] = "manual-prompt-generator"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            result = self.run_script(
                "validate_designer_manifest.py", str(manifest_path), "--phase", "publish-ready"
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("skill_identifier must name the plugin Skill", result.stdout)

    def test_publish_ready_accepts_valid_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest = self.make_run(Path(temp), status="success")
            result = self.run_script(
                "validate_designer_manifest.py", str(manifest), "--phase", "publish-ready"
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_stage_guard_rejects_worker_outside_exact_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.md"
            source.write_text("# Article\n\nBody.\n", encoding="utf-8")
            created = self.run_script(
                "run_context.py", "init", "--mode", "news", "--account", "xiyue",
                "--slug", "guard", "--source", str(source),
                "--exports-root", str(root / "exports"),
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            run_dir = json.loads(created.stdout)["run_dir"]
            rejected = self.run_script("stage_guard.py", run_dir, "typesetter")
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("requires run status typesetting", rejected.stdout)

    def test_pipeline_does_not_force_h2_count_on_native_formatter(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paragraphs = [f"第{index}段" + "内容" * 55 + "。" for index in range(1, 8)]
            source = root / "source.txt"
            source.write_text("\n\n".join(paragraphs), encoding="utf-8")
            created = self.run_script(
                "run_context.py", "init", "--mode", "news", "--account", "xiyue",
                "--slug", "long-form", "--source", str(source),
                "--exports-root", str(root / "exports"),
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            run_dir = Path(json.loads(created.stdout)["run_dir"])
            formatting = self.run_script(
                "run_context.py", "status", str(run_dir), "formatting", "--actor", "wechat-leader"
            )
            self.assertEqual(formatting.returncode, 0, formatting.stderr)
            formatted = self.write_formatter_output(
                run_dir, "# 标题\n\n" + "\n\n".join(paragraphs)
            )
            result = self.run_script(
                "prepare_content.py", str(run_dir), "--source", str(formatted)
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_formatter_check_only_accepts_natural_markdown_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.md"
            source.write_text(
                '# 原标题\n\n真正压垮我的，是那句"她能挣，你不能挣"。2022年底，我开始自我PUA。\n',
                encoding="utf-8",
            )
            created = self.run_script(
                "run_context.py", "init", "--mode", "news", "--account", "xiyue",
                "--slug", "natural-format", "--source", str(source),
                "--exports-root", str(root / "exports"),
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            run_dir = Path(json.loads(created.stdout)["run_dir"])
            formatting = self.run_script(
                "run_context.py", "status", str(run_dir), "formatting", "--actor", "wechat-leader"
            )
            self.assertEqual(formatting.returncode, 0, formatting.stderr)
            formatter_workspace = run_dir / "baoyu-format-markdown"
            formatter_workspace.mkdir()
            formatted = formatter_workspace / "article-formatted.md"
            formatted.write_text(
                "---\ntitle: 原标题\n---\n\n# 原标题\n\n## 至暗时光\n\n"
                "真正压垮我的，是那句：\n\n> “她能挣，你不能挣”\n\n"
                "- 2022 年底\n- 我开始**自我 PUA**\n",
                encoding="utf-8",
            )
            checked = self.run_script(
                "prepare_content.py", str(run_dir), "--source", str(formatted), "--check-only"
            )
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
            report = json.loads(checked.stdout)
            self.assertTrue(report["ok"])
            self.assertEqual(report["missing_source_segments"], [])
            self.assertFalse((run_dir / "content.md").exists())
            self.assertFalse((run_dir / ".pipeline" / "format-result.json").exists())

    def test_formatter_check_only_reports_missing_source_line_and_preview(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.md"
            source.write_text("# 标题\n\n第一段完整文字。\n\n第二段不能删除。\n", encoding="utf-8")
            created = self.run_script(
                "run_context.py", "init", "--mode", "news", "--account", "xiyue",
                "--slug", "format-diagnostic", "--source", str(source),
                "--exports-root", str(root / "exports"),
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            run_dir = Path(json.loads(created.stdout)["run_dir"])
            formatting = self.run_script(
                "run_context.py", "status", str(run_dir), "formatting", "--actor", "wechat-leader"
            )
            self.assertEqual(formatting.returncode, 0, formatting.stderr)
            formatter_workspace = run_dir / "baoyu-format-markdown"
            formatter_workspace.mkdir()
            formatted = formatter_workspace / "article-formatted.md"
            formatted.write_text("# 标题\n\n第一段完整文字。\n", encoding="utf-8")
            checked = self.run_script(
                "prepare_content.py", str(run_dir), "--source", str(formatted), "--check-only"
            )
            self.assertEqual(checked.returncode, 1)
            report = json.loads(checked.stdout)
            self.assertFalse(report["ok"])
            self.assertEqual(report["missing_source_segments"][0]["line"], 5)
            self.assertIn("第二段不能删除", report["missing_source_segments"][0]["preview"])
            self.assertIn("line 5", report["errors"][0])

    def test_pipeline_does_not_validate_native_backend_or_preferences(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest_path = self.make_run(Path(temp), status="success")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["skill_runs"][0]["native_details"] = {
                "backend": "whatever-the-skill-selected",
                "preferences": {"style": "whatever-the-skill-selected"},
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            result = self.run_script(
                "validate_designer_manifest.py", str(manifest_path), "--phase", "publish-ready"
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def run_load_extend(self, skill: str, base_dir: Path, env: dict) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [PYTHON, str(ROOT / "scripts" / "load_extend.py"), skill,
             "--base-dir", str(base_dir), "--json"],
            capture_output=True, text=True, check=False, env=env,
        )

    def test_load_extend_prefers_project_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"
            home.mkdir()
            project = root / "proj"
            project_extend = project / ".baoyu-skills" / "baoyu-xhs-images" / "EXTEND.md"
            project_extend.parent.mkdir(parents=True)
            project_extend.write_text("preferred_style: sketch-notes\n", encoding="utf-8")
            home_extend = home / ".baoyu-skills" / "baoyu-xhs-images" / "EXTEND.md"
            home_extend.parent.mkdir(parents=True)
            home_extend.write_text("preferred_style: fresh\n", encoding="utf-8")
            env = {**os.environ, "HOME": str(home)}
            result = self.run_load_extend("baoyu-xhs-images", project, env)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertTrue(data["found"])
            self.assertEqual(data["source"], "project")
            self.assertEqual(Path(data["path"]), project_extend.resolve())
            self.assertEqual(data["sha256"], sha256(project_extend))

    def test_load_extend_falls_through_to_user_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"
            home_extend = home / ".baoyu-skills" / "baoyu-image-gen" / "EXTEND.md"
            home_extend.parent.mkdir(parents=True)
            home_extend.write_text("default_provider: codex-cli\n", encoding="utf-8")
            env = {**os.environ, "HOME": str(home)}
            result = self.run_load_extend("baoyu-image-gen", root / "empty-proj", env)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertTrue(data["found"])
            self.assertEqual(data["source"], "home")
            self.assertEqual(Path(data["path"]), home_extend.resolve())

    def test_load_extend_reports_not_found_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"
            home.mkdir()
            env = {**os.environ, "HOME": str(home)}
            result = self.run_load_extend("baoyu-xhs-images", root / "proj", env)
            # Not found is exit code 1 (a clean "absent" signal), not a crash.
            self.assertEqual(result.returncode, 1)
            data = json.loads(result.stdout)
            self.assertFalse(data["found"])
            self.assertIsNone(data["path"])
            self.assertEqual(len(data["searched"]), 3)

    def test_load_extend_respects_xdg_config_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"
            home.mkdir()
            xdg = root / "xdg"
            xdg_extend = xdg / "baoyu-skills" / "baoyu-cover-image" / "EXTEND.md"
            xdg_extend.parent.mkdir(parents=True)
            xdg_extend.write_text("quick_mode: true\n", encoding="utf-8")
            env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(xdg)}
            result = self.run_load_extend("baoyu-cover-image", root / "proj", env)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data["source"], "xdg")
            self.assertEqual(Path(data["path"]), xdg_extend.resolve())

    def test_load_extend_detects_legacy_baoyu_imagine(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"
            legacy = home / ".baoyu-skills" / "baoyu-imagine" / "EXTEND.md"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("default_provider: codex-cli\n", encoding="utf-8")
            env = {**os.environ, "HOME": str(home)}
            result = self.run_load_extend("baoyu-image-gen", root / "proj", env)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertTrue(data["found"])
            self.assertEqual(data["source"], "legacy-home")
            self.assertEqual(data["legacy_skill"], "baoyu-imagine")

    def test_publish_ready_rejects_missing_returned_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest_path = self.make_run(Path(temp), status="success")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["skill_runs"][0]["returned_outputs"] = []
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            result = self.run_script(
                "validate_designer_manifest.py", str(manifest_path), "--phase", "publish-ready"
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("returned_outputs must contain the Skill's final results", result.stdout)

    def test_publish_ready_rejects_output_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest_path = self.make_run(Path(temp), status="success")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["images"][0]["output_sha256"] = "0" * 64
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            result = self.run_script(
                "validate_designer_manifest.py", str(manifest_path), "--phase", "publish-ready"
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("output_sha256 mismatch", result.stdout)

    def test_pipeline_does_not_revalidate_native_image_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest_path = self.make_run(Path(temp), status="success")
            manifest = json.loads(manifest_path.read_text())
            image_path = Path(manifest["images"][0]["output_path"])
            wrong = fake_png(400, 300)
            image_path.write_bytes(wrong)
            self.refresh_image_evidence(manifest)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            result = self.run_script(
                "validate_designer_manifest.py", str(manifest_path), "--phase", "publish-ready"
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_pipeline_does_not_reimplement_native_image_decoding(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest_path = self.make_run(Path(temp), status="success")
            manifest = json.loads(manifest_path.read_text())
            image_path = Path(manifest["images"][0]["output_path"])
            image_path.write_bytes(
                b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
                + struct.pack(">II", 900, 1200) + b"\x08\x02\x00\x00\x00" + b"x" * 5000
            )
            self.refresh_image_evidence(manifest)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            result = self.run_script(
                "validate_designer_manifest.py", str(manifest_path), "--phase", "publish-ready"
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_pipeline_does_not_rejudge_native_image_pixels(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest_path = self.make_run(Path(temp), status="success")
            manifest = json.loads(manifest_path.read_text())
            image_path = Path(manifest["images"][0]["output_path"])
            image_path.write_bytes(solid_png(900, 1200))
            self.refresh_image_evidence(manifest)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            result = self.run_script(
                "validate_designer_manifest.py", str(manifest_path), "--phase", "publish-ready"
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_publish_ready_rejects_publisher_text_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest_path = self.make_run(Path(temp), status="success")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["source"]["publisher_text_sha256"] = "0" * 64
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            result = self.run_script(
                "validate_designer_manifest.py", str(manifest_path), "--phase", "publish-ready"
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("publisher_text_sha256", result.stdout)

    def test_returned_output_outside_canonical_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest_path = self.make_run(root, status="success")
            outside = root / "outside.png"
            outside.write_bytes(PNG)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            digest = sha256(outside)
            manifest["skill_runs"][0]["returned_outputs"][0].update(
                {"path": str(outside), "sha256": digest}
            )
            manifest["images"][0].update(
                {"output_path": str(outside), "output_sha256": digest}
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            result = self.run_script(
                "validate_designer_manifest.py", str(manifest_path), "--phase", "complete"
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("path must stay inside canonical_output_dir", result.stdout)

    def test_news_cover_contract_overrides_global_default_and_rejects_wrong_ratio(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = self.make_designing_run(Path(temp), "news")
            started = self.run_script(
                "native_skill_run.py", "start", str(run_dir), "--skill", "baoyu-cover-image"
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            start_record = json.loads(started.stdout)
            self.assertEqual(start_record["skill_options"], {"aspect": "2.35:1"})
            self.assertEqual(Path(start_record["workspace"]), run_dir / "baoyu-cover-image")
            cover_dir = Path(start_record["workspace"]) / "imgs"
            cover_dir.mkdir()
            cover = cover_dir / "cover.png"

            cover.write_bytes(fake_png(108, 144))
            rejected = self.run_script(
                "native_skill_run.py", "complete", str(run_dir),
                "--invocation-id", "baoyu-cover-image", "--output", f"cover={cover}",
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("do not match required aspect 2.35:1", rejected.stderr)

            cover.write_bytes(fake_png(470, 200))
            cover_evidence = self.write_image_evidence(Path(start_record["workspace"]), cover)
            completed = self.run_script(
                "native_skill_run.py", "complete", str(run_dir),
                "--invocation-id", "baoyu-cover-image", "--output", f"cover={cover}",
                "--evidence", str(cover_evidence),
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(Path(start_record["working_input_path"]).exists())

            illustrator_started = self.run_script(
                "native_skill_run.py", "start", str(run_dir),
                "--skill", "baoyu-article-illustrator",
            )
            self.assertEqual(illustrator_started.returncode, 0, illustrator_started.stderr)
            illustrator_record = json.loads(illustrator_started.stdout)
            self.assertEqual(illustrator_record["skill_options"], {})
            self.assertEqual(
                illustrator_record["confirmation_authorization"],
                "直接生成，不用确认，跳过确认，按默认出图。",
            )
            workspace = Path(illustrator_record["workspace"])
            self.assertEqual(workspace, run_dir / "baoyu-article-illustrator")
            image_dir = workspace / "imgs"
            image_dir.mkdir()
            body = image_dir / "body.png"
            body.write_bytes(fake_png(160, 90))
            body_evidence = self.write_image_evidence(workspace, body)
            article = workspace / "illustrated.md"
            article.write_text(
                (run_dir / "content.md").read_text(encoding="utf-8")
                + f"\n\n![Body]({body})\n",
                encoding="utf-8",
            )
            illustrator_completed = self.run_script(
                "native_skill_run.py", "complete", str(run_dir),
                "--invocation-id", "baoyu-article-illustrator",
                "--output", f"body={body}", "--output", f"article={article}",
                "--evidence", str(body_evidence),
            )
            self.assertEqual(illustrator_completed.returncode, 0, illustrator_completed.stderr)
            self.assertFalse(Path(illustrator_record["working_input_path"]).exists())
            built = self.run_script("native_skill_run.py", "build-manifest", str(run_dir))
            self.assertEqual(built.returncode, 0, built.stderr)

            manifest_path = run_dir / ".pipeline" / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            cover.write_bytes(fake_png(108, 144))
            wrong_hash = sha256(cover)
            cover_image = next(image for image in manifest["images"] if image["kind"] == "cover")
            cover_image["output_sha256"] = wrong_hash
            cover_run = next(
                skill_run for skill_run in manifest["skill_runs"]
                if skill_run["skill_name"] == "baoyu-cover-image"
            )
            cover_run["returned_outputs"][0]["sha256"] = wrong_hash
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            validated = self.run_script(
                "validate_designer_manifest.py", str(manifest_path), "--phase", "complete"
            )
            self.assertEqual(validated.returncode, 1)
            self.assertIn("do not match required aspect 2.35:1", validated.stdout)

    def test_news_v2_offline_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.md"
            source.write_text("# Title\n\n## Section\n\nBody.\n", encoding="utf-8")
            created = self.run_script(
                "run_context.py", "init", "--mode", "news", "--account", "xiyue",
                "--slug", "v2-news", "--source", str(source),
                "--exports-root", str(root / "exports"),
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            run_dir = Path(json.loads(created.stdout)["run_dir"])
            pipeline = run_dir / ".pipeline"
            for status in ("formatting",):
                result = self.run_script(
                    "run_context.py", "status", str(run_dir), status, "--actor", "wechat-leader"
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            formatter_output = self.write_formatter_output(run_dir)
            prepared = self.run_script(
                "prepare_content.py", str(run_dir), "--source", str(formatter_output)
            )
            self.assertEqual(prepared.returncode, 0, prepared.stderr)
            for status in ("content_ready", "designing"):
                result = self.run_script(
                    "run_context.py", "status", str(run_dir), status, "--actor", "wechat-leader"
                )
                self.assertEqual(result.returncode, 0, result.stderr)

            cover_started = self.run_script(
                "native_skill_run.py", "start", str(run_dir), "--skill", "baoyu-cover-image"
            )
            self.assertEqual(cover_started.returncode, 0, cover_started.stderr)
            cover_record = json.loads(cover_started.stdout)
            self.assertEqual(cover_record["skill_options"], {"aspect": "2.35:1"})
            cover_workspace = Path(cover_record["workspace"])
            self.assertEqual(cover_workspace, run_dir / "baoyu-cover-image")
            cover_dir = cover_workspace / "imgs"
            cover_dir.mkdir()
            cover = cover_dir / "cover.png"
            cover.write_bytes(fake_png(1880, 800))
            cover_evidence = self.write_image_evidence(cover_workspace, cover)
            cover_done = self.run_script(
                "native_skill_run.py", "complete", str(run_dir),
                "--invocation-id", "baoyu-cover-image", "--output", f"cover={cover}",
                "--evidence", str(cover_evidence),
            )
            self.assertEqual(cover_done.returncode, 0, cover_done.stderr)
            self.assertFalse(Path(cover_record["working_input_path"]).exists())

            illustrator_started = self.run_script(
                "native_skill_run.py", "start", str(run_dir),
                "--skill", "baoyu-article-illustrator",
            )
            self.assertEqual(illustrator_started.returncode, 0, illustrator_started.stderr)
            illustrator_record = json.loads(illustrator_started.stdout)
            illustrator_workspace = Path(illustrator_record["workspace"])
            self.assertEqual(
                illustrator_record["confirmation_authorization"],
                "直接生成，不用确认，跳过确认，按默认出图。",
            )
            self.assertEqual(illustrator_workspace, run_dir / "baoyu-article-illustrator")
            image_dir = illustrator_workspace / "imgs"
            image_dir.mkdir()
            body = image_dir / "body.png"
            body.write_bytes(fake_png(1600, 900))
            body_evidence = self.write_image_evidence(illustrator_workspace, body)
            illustrated = illustrator_workspace / "illustrated.md"
            illustrated.write_text(
                (run_dir / "content.md").read_text(encoding="utf-8")
                + f"\n\n![Body illustration]({body})\n",
                encoding="utf-8",
            )
            illustrator_done = self.run_script(
                "native_skill_run.py", "complete", str(run_dir),
                "--invocation-id", "baoyu-article-illustrator",
                "--output", f"body={body}", "--output", f"article={illustrated}",
                "--evidence", str(body_evidence),
            )
            self.assertEqual(illustrator_done.returncode, 0, illustrator_done.stderr)
            self.assertFalse(Path(illustrator_record["working_input_path"]).exists())
            built_manifest = self.run_script("native_skill_run.py", "build-manifest", str(run_dir))
            self.assertEqual(built_manifest.returncode, 0, built_manifest.stderr)
            manifest_path = pipeline / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            run = json.loads((pipeline / "run.json").read_text(encoding="utf-8"))
            original = pipeline / "input.md"
            for status in ("artwork_ready", "typesetting"):
                result = self.run_script(
                    "run_context.py", "status", str(run_dir), status, "--actor", "wechat-leader"
                )
                self.assertEqual(result.returncode, 0, result.stderr)

            layout_started = self.run_script("layout_skill_run.py", "start", str(run_dir))
            self.assertEqual(layout_started.returncode, 0, layout_started.stderr)
            layout_record = json.loads(layout_started.stdout)
            self.assertEqual(layout_record["skill_identifier"], "wechat-pipeline:gzh-design")
            self.assertEqual(Path(layout_record["workspace"]), run_dir / "gzh-design")
            self.assertIn("不要新增作者签名", layout_record["invocation_args"])
            duplicate_typesetter = self.run_script("layout_skill_run.py", "start", str(run_dir))
            self.assertEqual(duplicate_typesetter.returncode, 0, duplicate_typesetter.stderr)
            self.assertTrue(json.loads(duplicate_typesetter.stdout)["reused"])
            native_html = Path(layout_record["workspace"]) / "natural-layout.html"
            native_html.write_text(
                '<section><h2><span leaf="">Section</span></h2>'
                '<p><span leaf="">Changed copy.</span></p>'
                '<img src="body.png"></section>',
                encoding="utf-8",
            )
            correction_needed = self.run_script(
                "layout_skill_run.py", "complete", str(run_dir), "--output", str(native_html)
            )
            self.assertNotEqual(correction_needed.returncode, 0)
            self.assertIn("gzh-design output needs native self-correction", correction_needed.stderr)
            self.assertIn("HTML is missing", correction_needed.stderr)
            self.assertIn("image path must be absolute", correction_needed.stderr)
            pending_receipt = json.loads((pipeline / "layout-skill-run.json").read_text(encoding="utf-8"))
            self.assertEqual(pending_receipt["attempt"], 1)
            self.assertEqual(pending_receipt["status"], "started")
            self.assertEqual(pending_receipt["returned_outputs"], [])
            native_html.write_text(
                '<section><h2><span leaf="">Section</span></h2>'
                '<p><span leaf="">Body.</span></p>'
                f'<img src="{body}"></section>',
                encoding="utf-8",
            )
            layout_completed = self.run_script(
                "layout_skill_run.py", "complete", str(run_dir), "--output", str(native_html)
            )
            self.assertEqual(layout_completed.returncode, 0, layout_completed.stderr)
            self.assertFalse(Path(layout_record["working_input_path"]).exists())
            self.assertFalse((run_dir / "skill-output").exists())
            prepared_layout = self.run_script("prepare_layout.py", str(run_dir))
            self.assertEqual(prepared_layout.returncode, 0, prepared_layout.stderr)
            html = run_dir / "article-body.html"
            layout_path = pipeline / "layout.json"
            layout = json.loads(layout_path.read_text(encoding="utf-8"))
            self.assertEqual(layout["skill_run"]["path"], str(pipeline / "layout-skill-run.json"))
            self.assertEqual(layout["output"]["native_output_path"], str(native_html))
            layout_ready = self.run_script(
                "run_context.py", "status", str(run_dir), "layout_ready", "--actor", "wechat-leader"
            )
            self.assertEqual(layout_ready.returncode, 0, layout_ready.stderr)
            built = self.run_script("build_publish_snapshot.py", str(run_dir))
            self.assertEqual(built.returncode, 0, built.stderr)
            for status in ("publish_ready", "publishing"):
                result = self.run_script(
                    "run_context.py", "status", str(run_dir), status, "--actor", "wechat-leader"
                )
                self.assertEqual(result.returncode, 0, result.stderr)

            snapshot_path = pipeline / "publish-snapshot.json"
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            receipt = {
                "schema_version": 1, "protocol_version": "2026-07-20-001",
                "run_id": run["run_id"], "ok": True, "mode": "article-html", "account": "xiyue",
                "publish_fingerprint": "f" * 64, "draft_media_id": "draft-news",
                "creation_status": "created", "snapshot_sha256": sha256(snapshot_path),
                "snapshot_fingerprint": snapshot["fingerprint"],
                "layout_sha256": sha256(layout_path), "html_sha256": sha256(html),
                "body_image_count": 1, "uploaded_body_image_count": 1,
                "uploaded_body_images": {str(body): "https://mmbiz.qpic.cn/body"},
                "cover_path": str(cover),
                "verification": {
                    "ok": True, "status": "verified", "method": "draft/get",
                    "verified_at": datetime.now(timezone.utc).isoformat(),
                },
            }
            (pipeline / "publish-result.json").write_text(json.dumps(receipt), encoding="utf-8")
            validated = self.run_script("validate_publish_result.py", str(run_dir))
            self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)
            published = self.run_script(
                "run_context.py", "status", str(run_dir), "published", "--actor", "wechat-leader"
            )
            self.assertEqual(published.returncode, 0, published.stderr)
            self.assertEqual(
                json.loads((pipeline / "run.json").read_text(encoding="utf-8"))["status"],
                "published",
            )

    def make_layout(self, root: Path, *, placeholder: bool = False) -> tuple[Path, Path]:
        source = root / "source.md"
        source.write_text("# 标题\n\n正文。\n", encoding="utf-8")
        created = self.run_script(
            "run_context.py", "init",
            "--mode", "news",
            "--account", "xiyue",
            "--slug", "layout",
            "--source", str(source),
            "--exports-root", str(root / "exports"),
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        run = json.loads(created.stdout)
        run_dir = Path(run["run_dir"])
        pipeline = run_dir / ".pipeline"
        run_path = pipeline / "run.json"
        run_context = json.loads(run_path.read_text(encoding="utf-8"))
        run_context["status"] = "typesetting"
        run_path.write_text(json.dumps(run_context), encoding="utf-8")
        typesetting_at = datetime.now(timezone.utc)
        (pipeline / "events.jsonl").write_text(
            json.dumps({
                "event": "status.changed",
                "occurred_at": typesetting_at.isoformat(),
                "details": {"to": "typesetting"},
            }) + "\n",
            encoding="utf-8",
        )
        cover_workspace = run_dir / "baoyu-cover-image"
        illustrator_workspace = run_dir / "baoyu-article-illustrator"
        cover_images = cover_workspace / "imgs"
        illustrator_images = illustrator_workspace / "imgs"
        cover_images.mkdir(parents=True)
        illustrator_images.mkdir(parents=True)
        cover = cover_images / "cover.png"
        cover.write_bytes(PNG)
        body = illustrator_images / "body.png"
        body.write_bytes(PNG)
        html_path = run_dir / "article-body.html"
        visible = "{{作者名}}" if placeholder else "正文。"
        html_path.write_text(
            f'<section><p><span leaf="">{visible}</span></p>'
            f'<img src="{body}"></section>', encoding="utf-8"
        )
        gzh = ROOT / "skills" / "gzh-design"
        lock = json.loads(
            (ROOT / "third_party" / "gzh-design.lock.json").read_text(encoding="utf-8")
        )
        original = pipeline / "input.md"
        markdown = illustrator_workspace / "illustrated.md"
        markdown.write_text(
            original.read_text(encoding="utf-8") + f"\n\n![正文配图]({body})\n",
            encoding="utf-8",
        )
        (pipeline / "manifest.json").write_text(
            json.dumps({
                "layout_input": {"path": str(markdown), "sha256": sha256(markdown)},
                "images": [
                    {"id": "00", "kind": "cover", "output_path": str(cover)},
                    {"id": "01", "kind": "body", "output_path": str(body)},
                ]
            }),
            encoding="utf-8",
        )
        native_workspace = run_dir / "gzh-design"
        native_workspace.mkdir(parents=True)
        native_html = native_workspace / "natural.html"
        native_html.write_bytes(html_path.read_bytes())
        skill_receipt = {
            "schema_version": 1,
            "protocol_version": "2026-07-20-001",
            "run_id": run["run_id"],
            "attempt": 1,
            "skill_name": "gzh-design",
            "skill_identifier": "wechat-pipeline:gzh-design",
            "skill_path": str(gzh / "SKILL.md"),
            "skill_sha256": sha256(gzh / "SKILL.md"),
            "invocation_method": "native-skill",
            "input_path": str(markdown),
            "input_sha256": sha256(markdown),
            "workspace": str(native_workspace),
            "started_at": (typesetting_at + timedelta(seconds=1)).isoformat(),
            "completed_at": (typesetting_at + timedelta(seconds=2)).isoformat(),
            "status": "success",
            "returned_output": {
                "role": "html", "path": str(native_html), "sha256": sha256(native_html),
            },
        }
        skill_receipt_path = pipeline / "layout-skill-run.json"
        skill_receipt_path.write_text(json.dumps(skill_receipt), encoding="utf-8")
        layout = {
            "schema_version": 1,
            "protocol_version": "2026-07-20-001",
            "run_id": run["run_id"],
            "mode": "news",
            "canonical_output_dir": str(run_dir),
            "source": {
                "markdown_path": str(markdown),
                "markdown_sha256": sha256(markdown),
                "original_path": str(original),
                "original_sha256": sha256(original),
            },
            "skill_run": {
                "path": str(skill_receipt_path),
                "sha256": sha256(skill_receipt_path),
            },
            "skill_contract": {
                "skill_name": "gzh-design",
                "skill_identifier": "wechat-pipeline:gzh-design",
                "skill_path": str(gzh / "SKILL.md"),
                "skill_sha256": sha256(gzh / "SKILL.md"),
                "tree_sha256": lock["tree_sha256"],
                "upstream_commit": lock["commit"],
                "invocation_method": "native-skill",
            },
            "decision": {
                "content_policy": "preserve-visible-text",
                "engagement_footer_policy": "no-generated-engagement-footer",
            },
            "metadata": {
                "title": "标题",
                "author": "",
                "summary": "正文摘要",
                "cover_path": str(cover),
            },
            "output": {
                "html_path": str(html_path),
                "html_sha256": sha256(html_path),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "native_output_path": str(native_html),
                "native_output_sha256": sha256(native_html),
            },
        }
        manifest = pipeline / "layout.json"
        manifest.write_text(json.dumps(layout), encoding="utf-8")
        return html_path, manifest

    def test_layout_validator_accepts_valid_gzh_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            html_path, manifest = self.make_layout(Path(temp))
            result = self.run_script(
                "validate_article_layout.py", str(html_path), "--manifest", str(manifest)
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["warnings"], [])

    def test_layout_validator_rejects_unresolved_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            html_path, manifest = self.make_layout(Path(temp), placeholder=True)
            result = self.run_script(
                "validate_article_layout.py", str(html_path), "--manifest", str(manifest)
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("unresolved placeholder", result.stdout)

    def test_layout_validator_rejects_missing_source_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            html_path, manifest = self.make_layout(Path(temp))
            html_path.write_text(
                '<section><p><span leaf="">另一段内容。</span></p></section>', encoding="utf-8"
            )
            layout = json.loads(manifest.read_text(encoding="utf-8"))
            layout["output"]["html_sha256"] = sha256(html_path)
            manifest.write_text(json.dumps(layout), encoding="utf-8")
            result = self.run_script(
                "validate_article_layout.py", str(html_path), "--manifest", str(manifest)
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("source text segments", result.stdout)

    def test_layout_validator_rejects_generated_engagement_footer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            html_path, manifest = self.make_layout(Path(temp))
            layout = json.loads(manifest.read_text(encoding="utf-8"))
            native_html = Path(layout["output"]["native_output_path"])
            value = (
                '<section><p><span leaf="">正文。</span></p>'
                '<p><span leaf="">欢迎点赞、在看、转发，我们下篇见。</span></p>'
                f'<img src="{json.loads((html_path.parent / ".pipeline" / "manifest.json").read_text())["images"][1]["output_path"]}"></section>'
            )
            html_path.write_text(value, encoding="utf-8")
            native_html.write_text(value, encoding="utf-8")
            receipt_path = html_path.parent / ".pipeline" / "layout-skill-run.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["returned_output"]["sha256"] = sha256(native_html)
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            layout["skill_run"]["sha256"] = sha256(receipt_path)
            layout["output"]["html_sha256"] = sha256(html_path)
            layout["output"]["native_output_sha256"] = sha256(native_html)
            manifest.write_text(json.dumps(layout), encoding="utf-8")
            result = self.run_script(
                "validate_article_layout.py", str(html_path), "--manifest", str(manifest)
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("prohibited author/engagement footer", result.stdout)


    def test_init_rejects_obsidian_image_embeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.md"
            source.write_text(
                "# Article\n\nBody.\n\n![[Pasted image 20260720092709.png|678]]\n",
                encoding="utf-8",
            )
            result = self.run_script(
                "run_context.py", "init",
                "--mode", "news",
                "--account", "xiyue",
                "--slug", "obsidian-embed",
                "--source", str(source),
                "--exports-root", str(root / "exports"),
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("本地图片", result.stderr)
            self.assertIn("Pasted image 20260720092709.png", result.stderr)
            self.assertFalse(list(root.glob("exports/**/.pipeline/run.json")))

    def test_init_rejects_local_markdown_images_but_allows_remote(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            local_source = root / "local.md"
            local_source.write_text("# Article\n\n![截图](images/a.png)\n", encoding="utf-8")
            rejected = self.run_script(
                "run_context.py", "init",
                "--mode", "news",
                "--account", "xiyue",
                "--slug", "local-image",
                "--source", str(local_source),
                "--exports-root", str(root / "exports"),
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("本地图片", rejected.stderr)

            remote_source = root / "remote.md"
            remote_source.write_text(
                "# Article\n\n![示意图](https://example.com/a.png)\n", encoding="utf-8"
            )
            accepted = self.run_script(
                "run_context.py", "init",
                "--mode", "news",
                "--account", "xiyue",
                "--slug", "remote-image",
                "--source", str(remote_source),
                "--exports-root", str(root / "exports"),
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)


class DoctorDialectTests(unittest.TestCase):
    def write_env(self, root: Path, content: str) -> Path:
        env_path = root / ".env"
        env_path.write_text(content, encoding="utf-8")
        return env_path

    def write_extend(self, root: Path, dialect_line: str | None) -> Path:
        extend_path = root / "EXTEND.md"
        lines = ["---", "version: 1"]
        if dialect_line is not None:
            lines.append(dialect_line)
        lines.append("---\n")
        extend_path.write_text("\n".join(lines), encoding="utf-8")
        return extend_path

    def test_env_dialect_with_inline_comment_is_reported_with_location(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env_path = self.write_env(
                root,
                "OPENAI_API_KEY=secret\n"
                "OPENAI_IMAGE_API_DIALECT=ratio-metadata   # 兼容网关用 ratio-metadata\n",
            )
            errors = plugin_doctor.image_gen_dialect_errors(
                env_path, root / "missing-EXTEND.md"
            )
            self.assertEqual(len(errors), 1)
            self.assertIn(f"{env_path}:2", errors[0])
            self.assertIn("行内注释", errors[0])
            self.assertNotIn("secret", errors[0])

    def test_valid_env_dialect_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env_path = self.write_env(root, "OPENAI_IMAGE_API_DIALECT=ratio-metadata\n")
            self.assertEqual(
                plugin_doctor.image_gen_dialect_errors(env_path, root / "missing.md"), []
            )

    def test_extend_dialect_short_circuits_env_like_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env_path = self.write_env(
                root, "OPENAI_IMAGE_API_DIALECT=ratio-metadata # trailing comment\n"
            )
            extend_path = self.write_extend(
                root, "default_image_api_dialect: openai-native"
            )
            self.assertEqual(
                plugin_doctor.image_gen_dialect_errors(env_path, extend_path), []
            )

    def test_invalid_extend_dialect_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env_path = self.write_env(root, "")
            extend_path = self.write_extend(
                root, "default_image_api_dialect: ratio-metadata # comment"
            )
            errors = plugin_doctor.image_gen_dialect_errors(env_path, extend_path)
            self.assertEqual(len(errors), 1)
            self.assertIn("default_image_api_dialect", errors[0])


if __name__ == "__main__":
    unittest.main()
