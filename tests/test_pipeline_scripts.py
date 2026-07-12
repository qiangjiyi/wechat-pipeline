from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "plugins" / "wechat-pipeline"
PYTHON = sys.executable
PNG = b"\x89PNG\r\n\x1a\n" + b"test-png-payload"


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
            invalid = self.run_script("run_context.py", "status", run_dir, "published")
            self.assertNotEqual(invalid.returncode, 0)
            self.assertIn("invalid run status transition", invalid.stderr)
            valid = self.run_script("run_context.py", "status", run_dir, "planning")
            self.assertEqual(valid.returncode, 0, valid.stderr)
            failed = self.run_script("run_context.py", "status", run_dir, "failed")
            self.assertEqual(failed.returncode, 0, failed.stderr)
            wrong_resume = self.run_script("run_context.py", "status", run_dir, "ready")
            self.assertNotEqual(wrong_resume.returncode, 0)
            resumed = self.run_script("run_context.py", "status", run_dir, "planning")
            self.assertEqual(resumed.returncode, 0, resumed.stderr)

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
            result = self.run_script("run_context.py", "seal", str(run_dir))
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
            planning = self.run_script("run_context.py", "status", run_dir, "planning")
            self.assertEqual(planning.returncode, 0, planning.stderr)
            result = self.run_script("run_context.py", "seal", run_dir)
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
            "protocol_version": "2026-07-11-002",
            "run_id": "sample-run",
            "mode": "newspic",
            "account": "xiyue",
            "canonical_output_dir": str(run_dir),
            "input_path": str(input_path),
            "source_sha256": sha256(input_path),
            "status": "ready",
        }
        (pipeline / "run.json").write_text(json.dumps(run), encoding="utf-8")
        verdict = "success" if status == "success" else "api_error"
        manifest = {
            "schema_version": 2,
            "protocol_version": "2026-07-11-002",
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


if __name__ == "__main__":
    unittest.main()
