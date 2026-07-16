from __future__ import annotations

import hashlib
import json
import os
import subprocess
import struct
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "plugins" / "wechat-pipeline"
PYTHON = sys.executable
PNG = (
    b"\x89PNG\r\n\x1a\n"
    + b"\x00\x00\x00\rIHDR"
    + struct.pack(">II", 300, 400)
    + b"\x08\x06\x00\x00\x00"
    + b"test-png-payload"
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PipelineScriptTests(unittest.TestCase):
    def run_script(self, script: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [PYTHON, str(ROOT / "scripts" / script), *args],
            capture_output=True,
            text=True,
            check=False,
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
            env["WECHAT_PUBLISHER_ENV_FILE"] = str(config)
            result = subprocess.run(
                [
                    PYTHON, str(ROOT / "scripts" / "run_context.py"), "init",
                    "--mode", "newspic", "--account", "xiyue", "--slug", "configured",
                    "--source", str(source),
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
            progress = self.run_script(
                "run_context.py", "progress", str(run_dir),
                "--actor", "wechat-designer", "--stage", "rendering",
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
                "run_context.py", "status", run_dir, "planning", "--actor", "wechat-designer"
            )
            self.assertNotEqual(worker_write.returncode, 0)
            self.assertIn("only be changed by actor wechat-leader", worker_write.stderr)
            invalid = self.run_script("run_context.py", "status", run_dir, "published", "--actor", "wechat-leader")
            self.assertNotEqual(invalid.returncode, 0)
            self.assertIn("invalid run status transition", invalid.stderr)
            valid = self.run_script("run_context.py", "status", run_dir, "planning", "--actor", "wechat-leader")
            self.assertEqual(valid.returncode, 0, valid.stderr)
            failed = self.run_script("run_context.py", "status", run_dir, "failed", "--actor", "wechat-leader")
            self.assertEqual(failed.returncode, 0, failed.stderr)
            wrong_resume = self.run_script("run_context.py", "status", run_dir, "ready", "--actor", "wechat-leader")
            self.assertNotEqual(wrong_resume.returncode, 0)
            resumed = self.run_script("run_context.py", "status", run_dir, "planning", "--actor", "wechat-leader")
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            events = [
                json.loads(line)
                for line in (Path(run_dir) / ".pipeline" / "events.jsonl").read_text().splitlines()
            ]
            self.assertEqual(events[0]["event"], "run.created")
            self.assertEqual(events[-1]["details"], {"from": "failed", "to": "planning"})
            self.assertTrue(all(event["actor"] == "wechat-leader" for event in events))

    def test_news_layout_status_sequence_is_supported(self) -> None:
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
            run_dir = json.loads(created.stdout)["run_dir"]
            for status in ("planning", "rendering", "ready", "typesetting", "layout_ready", "publishing"):
                result = self.run_script(
                    "run_context.py", "status", run_dir, status,
                    "--actor", "wechat-leader",
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_published_requires_a_verified_durable_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.md"
            source.write_text("article\n", encoding="utf-8")
            created = self.run_script(
                "run_context.py", "init", "--mode", "newspic", "--account", "xiyue",
                "--slug", "receipt", "--source", str(source),
                "--exports-root", str(root / "exports"),
            )
            run_dir = Path(json.loads(created.stdout)["run_dir"])
            for status in ("planning", "rendering", "ready", "publishing"):
                result = self.run_script(
                    "run_context.py", "status", str(run_dir), status,
                    "--actor", "wechat-leader",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            rejected = self.run_script(
                "run_context.py", "status", str(run_dir), "published",
                "--actor", "wechat-leader",
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("publish-result.json", rejected.stderr)

    def test_publish_result_validator_accepts_verified_matching_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest_path = self.make_run(root, status="success")
            run_dir = manifest_path.parent.parent
            run_path = run_dir / ".pipeline" / "run.json"
            run = json.loads(run_path.read_text())
            run["status"] = "publishing"
            run_path.write_text(json.dumps(run), encoding="utf-8")
            manifest = json.loads(manifest_path.read_text())
            image_path = Path(manifest["images"][0]["output_path"]).resolve()
            source_path = Path(manifest["source"]["original_path"]).resolve()
            (run_dir / ".pipeline" / "publish-result.json").write_text(
                json.dumps({
                    "schema_version": 1,
                    "ok": True,
                    "protocol_version": "2026-07-13-001",
                    "run_id": run["run_id"],
                    "mode": "newspic",
                    "account": "xiyue",
                    "publish_fingerprint": "f" * 64,
                    "draft_media_id": "draft-1",
                    "creation_status": "created",
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
            planning = self.run_script(
                "run_context.py", "status", run_dir, "planning", "--actor", "wechat-leader"
            )
            self.assertEqual(planning.returncode, 0, planning.stderr)
            skipped_render = self.run_script(
                "run_context.py", "status", run_dir, "ready", "--actor", "wechat-leader"
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
                "protocol_version": "2026-07-13-001",
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
            manifest_path = self.make_run(Path(temp), status="success")
            run_dir = manifest_path.parent.parent
            result = subprocess.run(
                [
                    PYTHON,
                    str(ROOT / "skills" / "wechat-publisher" / "scripts" / "publish.py"),
                    "newspic",
                    "--manifest", str(manifest_path),
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
            self.assertEqual(payload["draft"]["title"], "article")
            self.assertEqual(
                payload["draft"]["images"],
                [str(Path(manifest["images"][0]["output_path"]).resolve())],
            )

    def test_article_source_is_created_once_and_reused_after_designer_edits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.md"
            source.write_text("# Article\n\nBody.\n", encoding="utf-8")
            created = self.run_script(
                "run_context.py", "init",
                "--mode", "news",
                "--account", "xiyue",
                "--slug", "article-source",
                "--source", str(source),
                "--exports-root", str(root / "exports"),
            )
            run_dir = Path(json.loads(created.stdout)["run_dir"])
            sealed = run_dir / ".pipeline" / "input.md"
            prepared = self.run_script(
                "prepare_article_source.py", str(run_dir), "--source", str(sealed)
            )
            self.assertEqual(prepared.returncode, 0, prepared.stderr)
            payload = json.loads(prepared.stdout)
            article_source = Path(payload["article_source_path"])
            self.assertFalse(payload["reused"])
            self.assertEqual(article_source.read_bytes(), sealed.read_bytes())
            self.assertTrue(article_source.stat().st_mode & 0o200)

            article_source.write_text(article_source.read_text() + "\n![](imgs/01.png)\n", encoding="utf-8")
            resumed = self.run_script(
                "prepare_article_source.py", str(run_dir), "--source", str(sealed)
            )
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertTrue(json.loads(resumed.stdout)["reused"])
            self.assertIn("imgs/01.png", article_source.read_text(encoding="utf-8"))

    def test_article_source_rejects_input_outside_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.md"
            source.write_text("article\n", encoding="utf-8")
            created = self.run_script(
                "run_context.py", "init",
                "--mode", "news",
                "--account", "xiyue",
                "--slug", "article-source-boundary",
                "--source", str(source),
                "--exports-root", str(root / "exports"),
            )
            run_dir = json.loads(created.stdout)["run_dir"]
            result = self.run_script(
                "prepare_article_source.py", run_dir, "--source", str(source)
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("inside canonical_output_dir", result.stderr)

    def test_seal_rejects_mismatched_run_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            created = self.run_script(
                "run_context.py", "init",
                "--mode", "newspic",
                "--account", "xiyue",
                "--slug", "seal-test",
                "--exports-root", str(root / "exports"),
            )
            run_dir = Path(json.loads(created.stdout)["run_dir"])
            input_path = run_dir / ".pipeline" / "input.md"
            input_path.write_text("article\n", encoding="utf-8")
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
                "run_context.py", "status", run_dir, "planning",
                "--actor", "wechat-leader",
            )
            self.assertEqual(planning.returncode, 0, planning.stderr)
            result = self.run_script(
                "run_context.py", "seal", run_dir, "--actor", "wechat-leader"
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("cannot seal run while status is 'planning'", result.stderr)

    def test_slug_error_reports_the_original_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = self.run_script(
                "run_context.py", "init",
                "--mode", "newspic",
                "--account", "xiyue",
                "--slug", "周报",
                "--exports-root", str(Path(temp) / "exports"),
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("'周报'", result.stderr)

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
        prompts = run_dir / "prompts"
        pipeline.mkdir(parents=True)
        prompts.mkdir()
        input_path = pipeline / "input.md"
        input_path.write_text("article\n", encoding="utf-8")
        prompt_path = prompts / "01-cover-sample.md"
        prompt_path.write_text("full native prompt\n", encoding="utf-8")
        image_path = run_dir / "01-cover-sample.png"
        image_path.write_bytes(PNG)
        skill_path = root / "SKILL.md"
        skill_path.write_text("# Skill\n", encoding="utf-8")
        extend_path = root / "EXTEND.md"
        extend_path.write_text("preferred_style: sketch-notes\n", encoding="utf-8")

        written = datetime.now(timezone.utc)
        started = written - timedelta(seconds=5) if attempt_before_prompt else written + timedelta(seconds=1)
        finished = started + timedelta(seconds=1)
        run = {
            "protocol_version": "2026-07-13-001",
            "run_id": "sample-run",
            "mode": "newspic",
            "account": "xiyue",
            "canonical_output_dir": str(run_dir),
            "input_path": str(input_path),
            "source_sha256": sha256(input_path),
            "status": "ready",
        }
        (pipeline / "run.json").write_text(json.dumps(run), encoding="utf-8")
        (pipeline / "preflight.json").write_text(
            json.dumps({"fallback_order": ["openai-native"]}), encoding="utf-8"
        )
        verdict = "success" if status == "success" else "api_error"
        manifest = {
            "schema_version": 2,
            "protocol_version": "2026-07-13-001",
            "run_id": "sample-run",
            "mode": "newspic",
            "canonical_output_dir": str(run_dir),
            "source": {
                "original_path": str(input_path),
                "original_sha256": sha256(input_path),
                "publisher_text_sha256": sha256(input_path),
            },
            "skill_contract": {
                "skill_name": "baoyu-xhs-images",
                "skill_path": str(skill_path),
                "skill_sha256": sha256(skill_path),
                "files_read": [str(skill_path), str(extend_path)],
                "preferences": {
                    "source": "extend",
                    "style": "sketch-notes",
                    "extend_path": str(extend_path),
                    "extend_sha256": sha256(extend_path),
                },
            },
            "images": [{
                "id": "01",
                "kind": "card",
                "source_skill": "baoyu-xhs-images",
                "prompt_path": str(prompt_path),
                "prompt_sha256": sha256(prompt_path),
                "prompt_written_at": written.isoformat(),
                "output_path": str(image_path),
                "output_sha256": sha256(image_path) if status == "success" else None,
                "aspect": "3:4",
                "attempts": [{
                    "scope": "image",
                    "backend": "openai-native",
                    "prompt_sha256": sha256(prompt_path),
                    "started_at": started.isoformat(),
                    "finished_at": finished.isoformat(),
                    "verdict": verdict,
                    "error_summary": "" if status == "success" else "model unavailable",
                }],
                "status": status,
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
            self.assertIn("not publish-ready", result.stdout)

    def test_attempt_cannot_predate_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest = self.make_run(Path(temp), status="success", attempt_before_prompt=True)
            result = self.run_script("validate_designer_manifest.py", str(manifest), "--phase", "plan")
            self.assertEqual(result.returncode, 1)
            self.assertIn("started before its prompt was written", result.stdout)

    def test_publish_ready_accepts_valid_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest = self.make_run(Path(temp), status="success")
            result = self.run_script(
                "validate_designer_manifest.py", str(manifest), "--phase", "publish-ready"
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_backend_alias_must_resolve_to_a_configured_preflight_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest_path = self.make_run(Path(temp), status="success")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["images"][0]["attempts"][0]["backend"] = "imagegen"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            accepted = self.run_script(
                "validate_designer_manifest.py", str(manifest_path), "--phase", "publish-ready"
            )
            self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)

            manifest["images"][0]["attempts"][0]["backend"] = "unknown-provider"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            rejected = self.run_script(
                "validate_designer_manifest.py", str(manifest_path), "--phase", "publish-ready"
            )
            self.assertEqual(rejected.returncode, 1)
            self.assertIn("was not configured by preflight", rejected.stdout)

    def test_extend_style_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest_path = self.make_run(Path(temp), status="success")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["skill_contract"]["preferences"]["style"] = "fresh"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            result = self.run_script("validate_designer_manifest.py", str(manifest_path), "--phase", "plan")
            self.assertEqual(result.returncode, 1)
            self.assertIn("does not match EXTEND.md preferred_style", result.stdout)

    def test_publish_ready_rejects_missing_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest_path = self.make_run(Path(temp), status="success")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["images"][0]["attempts"] = []
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            result = self.run_script(
                "validate_designer_manifest.py", str(manifest_path), "--phase", "publish-ready"
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("last attempt must have verdict success", result.stdout)

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
            self.assertIn("output hash mismatch", result.stdout)

    def test_publish_ready_rejects_wrong_aspect_ratio(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest_path = self.make_run(Path(temp), status="success")
            manifest = json.loads(manifest_path.read_text())
            image_path = Path(manifest["images"][0]["output_path"])
            wrong = (
                b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
                + struct.pack(">II", 400, 300) + b"\x08\x06\x00\x00\x00payload"
            )
            image_path.write_bytes(wrong)
            manifest["images"][0]["output_sha256"] = sha256(image_path)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            result = self.run_script(
                "validate_designer_manifest.py", str(manifest_path), "--phase", "publish-ready"
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("do not match aspect", result.stdout)

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

    def test_plan_rejects_prompt_outside_canonical_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest_path = self.make_run(root, status="success")
            outside = root / "outside-prompt.md"
            outside.write_text("outside\n", encoding="utf-8")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["images"][0]["prompt_path"] = str(outside)
            manifest["images"][0]["prompt_sha256"] = sha256(outside)
            manifest["images"][0]["attempts"][0]["prompt_sha256"] = sha256(outside)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            result = self.run_script(
                "validate_designer_manifest.py", str(manifest_path), "--phase", "plan"
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("prompt_path must stay inside canonical_output_dir", result.stdout)

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
        html_path = run_dir / "article-body.html"
        visible = "{{作者名}}" if placeholder else "正文。"
        html_path.write_text(
            f'<section><p><span leaf="">{visible}</span></p></section>', encoding="utf-8"
        )
        cover = run_dir / "cover.png"
        cover.write_bytes(PNG)
        gzh = ROOT / "skills" / "gzh-design"
        lock = json.loads(
            (ROOT / "third_party" / "gzh-design.lock.json").read_text(encoding="utf-8")
        )
        original = run_dir / ".pipeline" / "input.md"
        markdown = run_dir / "article-source.md"
        markdown.write_bytes(original.read_bytes())
        layout = {
            "schema_version": 1,
            "protocol_version": "2026-07-13-001",
            "run_id": run["run_id"],
            "mode": "news",
            "canonical_output_dir": str(run_dir),
            "source": {
                "markdown_path": str(markdown),
                "markdown_sha256": sha256(markdown),
                "original_path": str(original),
                "original_sha256": sha256(original),
            },
            "skill_contract": {
                "skill_name": "gzh-design",
                "skill_path": str(gzh / "SKILL.md"),
                "skill_sha256": sha256(gzh / "SKILL.md"),
                "tree_sha256": lock["tree_sha256"],
                "files_read": [
                    str(gzh / "SKILL.md"),
                    str(gzh / "references" / "theme-index.md"),
                    str(gzh / "references" / "theme-moyu-green.md"),
                    str(gzh / "references" / "common-components.md"),
                ],
                "upstream_commit": lock["commit"],
            },
            "decision": {
                "theme": "摸鱼绿",
                "theme_source": "auto",
                "article_type": "观点/深度分析",
                "content_policy": "preserve-visible-text",
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
            },
        }
        manifest = run_dir / ".pipeline" / "layout.json"
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


if __name__ == "__main__":
    unittest.main()
