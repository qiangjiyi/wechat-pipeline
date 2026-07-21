from __future__ import annotations

import io
import hashlib
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from argparse import Namespace
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1] / "plugins" / "wechat-pipeline"
PUBLISHER_SCRIPTS = ROOT / "skills" / "wechat-publisher" / "scripts"
sys.path.insert(0, str(PUBLISHER_SCRIPTS))

from lib.errors import PublishError  # noqa: E402
from lib import proxy_client  # noqa: E402
from lib.account import account_value, resolve_account  # noqa: E402
from lib.env_loader import load_dotenv, merged_env  # noqa: E402
from lib.html_article import inspect_html, rewrite_image_sources, upload_html_images  # noqa: E402
from lib.draft_verifier import verify_article_draft  # noqa: E402
from lib.source_loader import load_source, parse_markdown, parse_simple_yaml, validate_newspic_source  # noqa: E402
import mode_article  # noqa: E402
import mode_newspic  # noqa: E402


class FakeResponse:
    def __init__(self, payload: dict):
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return self.body


class ProxyClientTests(unittest.TestCase):
    def test_network_failures_retry_with_required_backoff(self) -> None:
        calls = [
            urllib.error.URLError("reset"),
            TimeoutError("timeout"),
            FakeResponse({"ok": True}),
        ]
        sleeps: list[float] = []
        with mock.patch.object(proxy_client.urllib.request, "urlopen", side_effect=calls):
            result = proxy_client.request_json(
                "https://proxy.example/",
                "POST",
                {"hello": "world"},
                sleeper=sleeps.append,
            )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(sleeps, [30.0, 60.0])

    def test_http_5xx_retries_but_business_error_does_not(self) -> None:
        http_error = urllib.error.HTTPError(
            "https://proxy.example/", 503, "unavailable", {}, io.BytesIO(b"temporary")
        )
        sleeps: list[float] = []
        with mock.patch.object(
            proxy_client.urllib.request,
            "urlopen",
            side_effect=[http_error, FakeResponse({"media_id": "ok"})],
        ):
            result = proxy_client.request_json(
                "https://proxy.example/", "POST", {}, sleeper=sleeps.append
            )
        self.assertEqual(result["media_id"], "ok")
        self.assertEqual(sleeps, [30.0])

        with mock.patch.object(
            proxy_client.urllib.request,
            "urlopen",
            return_value=FakeResponse({"errcode": 40001, "errmsg": "invalid credential"}),
        ) as urlopen:
            with self.assertRaisesRegex(PublishError, "WeChat error 40001"):
                proxy_client.request_json(
                    "https://proxy.example/", "POST", {}, sleeper=sleeps.append
                )
        self.assertEqual(urlopen.call_count, 1)

    def test_proxy_envelope_is_preserved(self) -> None:
        captured: list[dict] = []

        def open_request(request, timeout):
            captured.append(json.loads(request.data.decode("utf-8")))
            return FakeResponse({"access_token": "token"})

        with mock.patch.object(proxy_client.urllib.request, "urlopen", side_effect=open_request):
            result = proxy_client.proxy_json(
                "https://proxy.example/",
                "https://api.weixin.qq.com/cgi-bin/token",
                "GET",
            )
        self.assertEqual(result["access_token"], "token")
        self.assertEqual(
            captured,
            [{
                "url": "https://api.weixin.qq.com/cgi-bin/token",
                "method": "GET",
            }],
        )

    def test_direct_upload_retries_transport_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            image = Path(temp) / "cover.png"
            image.write_bytes(b"png")
            with (
                mock.patch.object(
                    proxy_client.urllib.request,
                    "urlopen",
                    side_effect=[urllib.error.URLError("reset"), FakeResponse({"media_id": "m1"})],
                ),
                mock.patch.object(proxy_client.time, "sleep") as sleep,
            ):
                media_id = proxy_client.upload_image_direct(
                    "https://api.weixin.qq.com", "token", image
                )
        self.assertEqual(media_id, "m1")
        sleep.assert_called_once_with(30.0)

    def test_body_upload_uses_a_fresh_multipart_boundary(self) -> None:
        requests = []

        def open_request(request, timeout):
            requests.append(request)
            return FakeResponse({"url": "https://mmbiz.example/image"})

        with tempfile.TemporaryDirectory() as temp:
            image = Path(temp) / 'body"image.png'
            image.write_bytes(b"png")
            with mock.patch.object(proxy_client.urllib.request, "urlopen", side_effect=open_request):
                first = proxy_client.upload_body_image_direct(
                    "https://api.weixin.qq.com", "token", image
                )
                second = proxy_client.upload_body_image_direct(
                    "https://api.weixin.qq.com", "token", image
                )
        self.assertEqual(first, "https://mmbiz.example/image")
        self.assertEqual(second, first)
        content_types = [request.get_header("Content-type") for request in requests]
        self.assertNotEqual(content_types[0], content_types[1])
        self.assertNotIn(b'filename="body"image.png"', requests[0].data)

    def test_token_and_draft_final_paths(self) -> None:
        self.assertEqual(
            proxy_client.get_access_token(
                {"WECHAT_PERSONAL_ACCESS_TOKEN": "existing-token"},
                "personal",
                "https://api.weixin.qq.com",
                "",
            ),
            "existing-token",
        )
        with mock.patch.object(
            proxy_client, "proxy_json", return_value={"media_id": "draft-1"}
        ) as proxy_json:
            draft = proxy_client.add_draft(
                "https://api.weixin.qq.com",
                "https://proxy.example",
                "token",
                [{"title": "Title"}],
            )
        self.assertEqual(draft, "draft-1")
        self.assertEqual(proxy_json.call_args.args[2:], ("POST", {"articles": [{"title": "Title"}]}))

        with mock.patch.object(
            proxy_client, "proxy_json", return_value={"news_item": [{"title": "Title"}]}
        ) as proxy_json:
            fetched = proxy_client.get_draft(
                "https://api.weixin.qq.com", "https://proxy.example", "token", "draft-1"
            )
        self.assertEqual(fetched["news_item"][0]["title"], "Title")
        self.assertEqual(proxy_json.call_args.args[2:], ("POST", {"media_id": "draft-1"}))

    def test_draft_add_never_retries_an_ambiguous_network_failure(self) -> None:
        with (
            mock.patch.object(
                proxy_client.urllib.request,
                "urlopen",
                side_effect=urllib.error.URLError("response lost after commit"),
            ) as urlopen,
            mock.patch.object(proxy_client.time, "sleep") as sleep,
        ):
            with self.assertRaisesRegex(PublishError, "network error"):
                proxy_client.add_draft(
                    "https://api.weixin.qq.com", "", "token", [{"title": "Title"}]
                )
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()


class PublisherInputTests(unittest.TestCase):
    def newspic_args(self, root: Path, images: list[Path]) -> Namespace:
        return Namespace(
            source=None,
            manifest=None,
            title="Title",
            content="Body",
            image=[str(path) for path in images],
            author=None,
            digest=None,
            account="personal",
            env_file=None,
            dry_run=False,
            yes=True,
            result_output=str(root / ".pipeline" / "publish-result.json"),
            verify_draft=True,
            recover_draft_media_id=None,
        )

    def test_named_account_never_falls_back_to_global_credentials(self) -> None:
        env = {
            "WECHAT_APP_ID": "global-id",
            "WECHAT_PERSONAL_APP_ID": "personal-id",
        }
        self.assertEqual(account_value(env, "personal", "APP_ID"), "personal-id")
        self.assertEqual(account_value(env, "company", "APP_ID"), "")
        self.assertEqual(account_value(env, "default", "APP_ID"), "global-id")

    def test_multiple_accounts_require_an_explicit_selection(self) -> None:
        with self.assertRaisesRegex(PublishError, "multiple accounts configured"):
            resolve_account(None, {}, {"WECHAT_ACCOUNTS": "personal,company"})

    def test_env_precedence_is_process_over_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env_file = root / ".env"
            env_file.write_text("WECHAT_API_BASE=https://file.example\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"WECHAT_API_BASE": "https://process.example"}, clear=False):
                env, used = merged_env(root, str(env_file), root)
        self.assertEqual(used, env_file)
        self.assertEqual(env["WECHAT_API_BASE"], "https://process.example")

    def test_dotenv_parser_has_one_deterministic_flat_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            env_file = Path(temp) / ".env"
            env_file.write_text(
                "# comment\nexport EMPTY=\nSINGLE='one'\nDOUBLE=\"two\"\nMIXED=\"three'\n",
                encoding="utf-8",
            )
            values = load_dotenv(env_file)
        self.assertEqual(
            values,
            {"EMPTY": "", "SINGLE": "one", "DOUBLE": "two", "MIXED": '\"three\''},
        )

    def test_simple_source_parser_supports_declared_flat_contract(self) -> None:
        parsed = parse_simple_yaml('account: personal\nimages:\n  - "one.png"\n  - two.png\n')
        self.assertEqual(parsed, {"account": "personal", "images": ["one.png", "two.png"]})
        markdown = parse_markdown("---\naccount: personal\n---\n# Title\n\nBody\n")
        self.assertEqual(markdown["title"], "Title")
        self.assertEqual(markdown["content"], "Body")
        with self.assertRaisesRegex(PublishError, "unsupported YAML line"):
            parse_simple_yaml("nested:\n  child: value\n")

    def test_load_source_dispatches_json_and_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            json_path = root / "source.json"
            yaml_path = root / "source.yaml"
            json_path.write_text('{"title": "JSON"}\n', encoding="utf-8")
            yaml_path.write_text("title: YAML\nimages:\n  - one.png\n", encoding="utf-8")
            self.assertEqual(load_source(json_path), {"title": "JSON"})
            self.assertEqual(load_source(yaml_path)["title"], "YAML")

    def test_newspic_content_is_rejected_instead_of_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            image = root / "card.png"
            image.write_bytes(b"png")
            source = {"title": "Title", "content": "x" * 1201, "images": [str(image)]}
            with self.assertRaisesRegex(PublishError, "refuse to truncate user text"):
                validate_newspic_source(source, root)

    def test_article_dry_run_uses_rendered_metadata_without_network(self) -> None:
        render_temp = Path(tempfile.mkdtemp(prefix="wechat-publisher-render-"))
        (render_temp / "downloaded.png").write_bytes(b"png")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            markdown = root / "article.md"
            markdown.write_text("# Title\n\nBody\n", encoding="utf-8")
            args = Namespace(
                markdown=str(markdown),
                markdown_pos=None,
                theme="simple",
                color="blue",
                no_cite=False,
                title=None,
                author=None,
                summary=None,
                cover=None,
                account="personal",
                env_file=None,
                dry_run=True,
                yes=True,
            )
            rendered = {
                "title": "Rendered title",
                "author": "Author",
                "summary": "Summary",
                "html": "<p>Body</p>",
                "contentImages": [],
                "coverHint": None,
                "temporaryDirectory": str(render_temp),
            }
            output = io.StringIO()
            with (
                mock.patch.object(mode_article, "_run_renderer", return_value=rendered),
                mock.patch.object(
                    mode_article,
                    "merged_env",
                    return_value=({"WECHAT_ACCOUNTS": "personal"}, None),
                ),
                redirect_stdout(output),
            ):
                result = mode_article.run(args)
        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["mode"], "article")
        self.assertEqual(payload["title"], "Rendered title")
        self.assertEqual(payload["theme"], "simple")
        self.assertFalse(render_temp.exists())

    def test_noninteractive_confirmation_returns_publish_error(self) -> None:
        with mock.patch("builtins.input", side_effect=EOFError):
            with self.assertRaisesRegex(PublishError, "pass --yes"):
                mode_article._confirm_article("personal", "Title", "Summary", 0, None)
            with self.assertRaisesRegex(PublishError, "pass --yes"):
                mode_newspic._confirm_newspic("personal", "Title", "Body", [])

    def test_html_placeholder_rewrite_replaces_all_occurrences(self) -> None:
        html = "<p>WECHATIMGPH_1</p><p>WECHATIMGPH_1</p>"
        rewritten = mode_article._rewrite_html_with_mmbiz(
            html, [("WECHATIMGPH_1", "https://mmbiz.example/image")]
        )
        self.assertNotIn("WECHATIMGPH_1", rewritten)
        self.assertEqual(rewritten.count("https://mmbiz.example/image"), 2)

    def test_gzh_html_inspection_rejects_documents_and_placeholders(self) -> None:
        with self.assertRaisesRegex(PublishError, "body fragment"):
            inspect_html("<html><body><section></section></body></html>")
        with self.assertRaisesRegex(PublishError, "unresolved placeholder"):
            inspect_html('<section><p><span leaf="">{{作者名}}</span></p></section>')
        with self.assertRaisesRegex(PublishError, "non-empty src"):
            inspect_html("<section><img></section>")

    def test_gzh_image_rewrite_preserves_non_image_markup(self) -> None:
        source = (
            '<section><svg viewBox="0 0 10 10"></svg>'
            '<span leaf=""><img src="image%20one.png" style="max-width:100%;"></span></section>'
        )
        rewritten = rewrite_image_sources(
            source, {"image%20one.png": "https://mmbiz.qpic.cn/new?a=1&b=2"}
        )
        self.assertIn('<svg viewBox="0 0 10 10"></svg>', rewritten)
        self.assertIn('style="max-width:100%;"', rewritten)
        self.assertIn("https://mmbiz.qpic.cn/new?a=1&amp;b=2", rewritten)

    def test_gzh_html_uploads_local_images_once_and_keeps_mmbiz(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            image = root / "body image.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\nbody")
            html = (
                '<section><span leaf=""><img src="body%20image.png"></span>'
                '<span leaf=""><img src="body%20image.png"></span>'
                '<span leaf=""><img src="https://mmbiz.qpic.cn/already"></span></section>'
            )
            uploaded: list[Path] = []

            def uploader(path: Path) -> str:
                uploaded.append(path)
                return "https://mmbiz.qpic.cn/uploaded"

            rewritten, count = upload_html_images(html, root, uploader)
            self.assertEqual(count, 1)
            self.assertEqual(uploaded, [image.resolve()])
            self.assertEqual(rewritten.count("https://mmbiz.qpic.cn/uploaded"), 2)
            self.assertIn("https://mmbiz.qpic.cn/already", rewritten)

    def test_draft_readback_verifies_text_title_and_wechat_images(self) -> None:
        source = '<section><p><span leaf="">正文。</span></p><img src="local.png"></section>'
        draft = {
            "news_item": [{
                "title": "Title",
                "digest": "Summary",
                "content": '<section><p><span leaf="">正文。</span></p>'
                '<img src="https://mmbiz.qpic.cn/body"></section>',
            }]
        }
        result = verify_article_draft(
            draft,
            title="Title",
            summary="Summary",
            source_html=source,
            expected_image_count=1,
        )
        self.assertTrue(result["ok"], result)

    def test_draft_readback_accepts_wechat_data_src_images(self) -> None:
        source = '<section><p><span leaf="">正文。</span></p><img src="local.png"></section>'
        draft = {
            "news_item": [{
                "title": "Title",
                "digest": "Summary",
                "content": '<section><p><span leaf="">正文。</span></p>'
                '<img data-src="https://mmbiz.qpic.cn/body"></section>',
            }]
        }
        result = verify_article_draft(
            draft,
            title="Title",
            summary="Summary",
            source_html=source,
            expected_image_count=1,
        )
        self.assertTrue(result["ok"], result)

    def test_article_html_dry_run_does_not_use_markdown_renderer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            article = root / "article-body.html"
            article.write_text(
                '<section><p><span leaf="">正文。</span></p></section>', encoding="utf-8"
            )
            env_file = root / ".env"
            env_file.write_text("WECHAT_ACCOUNTS=personal\n", encoding="utf-8")
            args = Namespace(
                markdown=None,
                markdown_pos=None,
                html=str(article),
                layout_manifest=None,
                theme=None,
                color=None,
                no_cite=False,
                title="HTML title",
                author="Author",
                summary="Summary",
                cover=None,
                account="personal",
                env_file=str(env_file),
                dry_run=True,
                yes=True,
            )
            output = io.StringIO()
            with (
                mock.patch.object(mode_article, "_run_renderer") as renderer,
                redirect_stdout(output),
            ):
                result = mode_article.run(args)
            self.assertEqual(result, 0)
            renderer.assert_not_called()
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["mode"], "article-html")
            self.assertEqual(payload["title"], "HTML title")

    def test_article_html_dry_run_consumes_valid_layout_manifest_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "article-run"
            pipeline = run_dir / ".pipeline"
            pipeline.mkdir(parents=True)
            original = pipeline / "input.md"
            original.write_text("# 标题\n\n正文。\n", encoding="utf-8")
            markdown = run_dir / "content.md"
            markdown.write_bytes(original.read_bytes())
            typesetting_at = datetime.now(timezone.utc)
            (pipeline / "events.jsonl").write_text(
                json.dumps({
                    "event": "status.changed",
                    "occurred_at": typesetting_at.isoformat(),
                    "details": {"to": "typesetting"},
                }) + "\n",
                encoding="utf-8",
            )
            article = run_dir / "article-body.html"
            article.write_text(
                '<section><p><span leaf="">正文。</span></p></section>', encoding="utf-8"
            )
            images = run_dir / "images"
            images.mkdir()
            cover = images / "cover.png"
            cover.write_bytes(b"\x89PNG\r\n\x1a\ncover")
            (pipeline / "manifest.json").write_text(
                json.dumps({
                    "layout_input": {
                        "path": str(markdown),
                        "sha256": hashlib.sha256(markdown.read_bytes()).hexdigest(),
                    },
                    "images": [{"id": "00", "kind": "cover", "output_path": str(cover)}],
                }),
                encoding="utf-8",
            )
            run = {
                "protocol_version": "2026-07-20-001",
                "run_id": "run",
                "canonical_output_dir": str(run_dir),
                "status": "typesetting",
            }
            (pipeline / "run.json").write_text(json.dumps(run), encoding="utf-8")
            gzh = ROOT / "skills" / "gzh-design"
            lock = json.loads(
                (ROOT / "third_party" / "gzh-design.lock.json").read_text(encoding="utf-8")
            )
            sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
            native_workspace = run_dir / "gzh-design"
            native_workspace.mkdir(parents=True)
            native_html = native_workspace / "natural.html"
            native_html.write_bytes(article.read_bytes())
            layout_skill_run = {
                "schema_version": 1,
                "protocol_version": "2026-07-20-001",
                "run_id": "run",
                "skill_identifier": "wechat-pipeline:gzh-design",
                "skill_path": str(gzh / "SKILL.md"),
                "skill_sha256": sha(gzh / "SKILL.md"),
                "invocation_method": "native-skill",
                "input_path": str(markdown),
                "input_sha256": sha(markdown),
                "workspace": str(native_workspace),
                "status": "success",
                "returned_output": {
                    "path": str(native_html), "sha256": sha(native_html),
                },
            }
            layout_skill_run_path = pipeline / "layout-skill-run.json"
            layout_skill_run_path.write_text(json.dumps(layout_skill_run), encoding="utf-8")
            layout = {
                "schema_version": 1,
                "protocol_version": "2026-07-20-001",
                "run_id": "run",
                "mode": "news",
                "canonical_output_dir": str(run_dir),
                "source": {
                    "markdown_path": str(markdown),
                    "markdown_sha256": sha(markdown),
                    "original_path": str(original),
                    "original_sha256": sha(original),
                },
                "skill_run": {
                    "path": str(layout_skill_run_path),
                    "sha256": sha(layout_skill_run_path),
                },
                "skill_contract": {
                    "skill_name": "gzh-design",
                    "skill_identifier": "wechat-pipeline:gzh-design",
                    "skill_path": str(gzh / "SKILL.md"),
                    "skill_sha256": sha(gzh / "SKILL.md"),
                    "tree_sha256": lock["tree_sha256"],
                    "upstream_commit": lock["commit"],
                    "invocation_method": "native-skill",
                },
                "decision": {
                    "content_policy": "preserve-visible-text",
                    "engagement_footer_policy": "no-generated-engagement-footer",
                },
                "metadata": {
                    "title": "Manifest title",
                    "author": "Manifest author",
                    "summary": "Manifest summary",
                    "cover_path": str(cover),
                },
                "output": {
                    "html_path": str(article),
                    "html_sha256": sha(article),
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "native_output_path": str(native_html),
                    "native_output_sha256": sha(native_html),
                },
            }
            layout_path = pipeline / "layout.json"
            layout_path.write_text(json.dumps(layout), encoding="utf-8")
            env_file = run_dir / ".env"
            env_file.write_text("WECHAT_ACCOUNTS=personal\n", encoding="utf-8")
            args = Namespace(
                markdown=None, markdown_pos=None, html=str(article),
                layout_manifest=str(layout_path), theme=None, color=None, no_cite=False,
                snapshot=str(pipeline / "publish-snapshot.json"),
                title=None, author=None, summary=None, cover=None, account="personal",
                env_file=str(env_file), dry_run=True, yes=True,
            )
            output = io.StringIO()
            with (
                mock.patch.object(
                    mode_article,
                    "load_pipeline_snapshot",
                    return_value={
                        "account": "personal", "sha256": "a" * 64, "fingerprint": "b" * 64,
                        "data": {
                            "publication": {
                                "title": "Manifest title",
                                "author": "Manifest author",
                                "summary": "Manifest summary",
                            },
                            "cover": {"path": str(cover)},
                        },
                    },
                ),
                redirect_stdout(output),
            ):
                result = mode_article.run(args)
            self.assertEqual(result, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["title"], "Manifest title")
            self.assertEqual(payload["cover"], str(cover))

    def test_article_html_publish_rewrites_images_and_submits_fragment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            body_image = root / "body.png"
            body_image.write_bytes(b"\x89PNG\r\n\x1a\nbody")
            cover = root / "cover.png"
            cover.write_bytes(b"png")
            article = root / "article-body.html"
            article.write_text(
                '<section><p><span leaf="">正文。</span></p>'
                '<span leaf=""><img src="body.png" style="max-width:100%;"></span></section>',
                encoding="utf-8",
            )
            args = Namespace(
                markdown=None,
                markdown_pos=None,
                html=str(article),
                layout_manifest=None,
                theme=None,
                color=None,
                no_cite=False,
                title="HTML title",
                author="Author",
                summary="Summary",
                cover=str(cover),
                account="personal",
                env_file=None,
                dry_run=False,
                yes=True,
                result_output=str(root / ".pipeline" / "publish-result.json"),
                verify_draft=True,
            )
            captured: list[dict] = []

            def add_draft(_api, _proxy, _token, articles):
                captured.extend(articles)
                return "draft-media"

            with (
                mock.patch.object(mode_article, "_load_layout_manifest", return_value=({}, None)),
                mock.patch.object(mode_article, "merged_env", return_value=({"WECHAT_ACCOUNTS": "personal"}, None)),
                mock.patch.object(mode_article, "get_access_token", return_value="token"),
                mock.patch.object(mode_article, "upload_body_image", return_value="https://mmbiz.qpic.cn/body"),
                mock.patch.object(mode_article, "upload_image", return_value="cover-media"),
                mock.patch.object(mode_article, "add_draft", side_effect=add_draft),
                mock.patch.object(
                    mode_article,
                    "get_draft",
                    return_value={
                        "news_item": [{
                            "title": "HTML title",
                            "digest": "Summary",
                            "content": '<section><p><span leaf="">正文。</span></p>'
                            '<span leaf=""><img src="https://mmbiz.qpic.cn/body" '
                            'style="max-width:100%;"></span></section>',
                        }]
                    },
                ) as get_draft,
            ):
                result = mode_article.run(args)
                resumed = mode_article.run(args)
            self.assertEqual(result, 0)
            self.assertEqual(resumed, 0)
            self.assertEqual(len(captured), 1)
            self.assertIn("https://mmbiz.qpic.cn/body", captured[0]["content"])
            self.assertNotIn('src="body.png"', captured[0]["content"])
            self.assertTrue(captured[0]["content"].startswith("<section>"))
            self.assertEqual(captured[0]["thumb_media_id"], "cover-media")
            self.assertEqual(get_draft.call_count, 1)
            receipt = json.loads((root / ".pipeline" / "publish-result.json").read_text())
            self.assertEqual(receipt["draft_media_id"], "draft-media")
            self.assertTrue(receipt["verification"]["ok"])

    def test_verification_resume_never_creates_a_second_draft(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cover = root / "cover.png"
            cover.write_bytes(b"png")
            article = root / "article-body.html"
            article.write_text(
                '<section><p><span leaf="">正文。</span></p></section>', encoding="utf-8"
            )
            args = Namespace(
                markdown=None, markdown_pos=None, html=str(article), layout_manifest=None,
                theme=None, color=None, no_cite=False, title="HTML title", author="",
                summary="Summary", cover=str(cover), account="personal", env_file=None,
                dry_run=False, yes=True,
                result_output=str(root / ".pipeline" / "publish-result.json"),
                verify_draft=True,
            )
            bad = {"news_item": [{"title": "Wrong", "digest": "Summary", "content": ""}]}
            good = {
                "news_item": [{
                    "title": "HTML title",
                    "digest": "Summary",
                    "content": '<section><p><span leaf="">正文。</span></p></section>',
                }]
            }
            with (
                mock.patch.object(mode_article, "_load_layout_manifest", return_value=({}, None)),
                mock.patch.object(
                    mode_article, "merged_env",
                    return_value=({"WECHAT_ACCOUNTS": "personal"}, None),
                ),
                mock.patch.object(mode_article, "get_access_token", return_value="token"),
                mock.patch.object(mode_article, "upload_image", return_value="cover-media"),
                mock.patch.object(mode_article, "add_draft", return_value="draft-media") as add,
                mock.patch.object(mode_article, "get_draft", side_effect=[bad, good]),
            ):
                with self.assertRaisesRegex(PublishError, "draft was created"):
                    mode_article.run(args)
                resumed = mode_article.run(args)
            self.assertEqual(resumed, 0)
            self.assertEqual(add.call_count, 1)
            receipt = json.loads((root / ".pipeline" / "publish-result.json").read_text())
            self.assertTrue(receipt["verification"]["ok"])

    def test_newspic_ambiguous_draft_creation_is_never_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            image = root / "card.png"
            image.write_bytes(b"png")
            args = self.newspic_args(root, [image])
            with (
                mock.patch.object(mode_newspic, "merged_env", return_value=({}, None)),
                mock.patch.object(mode_newspic, "get_access_token", return_value="token"),
                mock.patch.object(mode_newspic, "upload_image", return_value="image-1"),
                mock.patch.object(
                    mode_newspic,
                    "add_draft",
                    side_effect=proxy_client.RetryablePublishError("response lost"),
                ) as add,
            ):
                with self.assertRaisesRegex(PublishError, "ambiguous network failure"):
                    mode_newspic.run(args)
                with self.assertRaisesRegex(PublishError, "outcome is unknown"):
                    mode_newspic.run(args)
            self.assertEqual(add.call_count, 1)
            receipt = json.loads((root / ".pipeline" / "publish-result.json").read_text())
            self.assertEqual(receipt["creation_status"], "unknown")
            args.recover_draft_media_id = "confirmed-draft"
            with (
                mock.patch.object(mode_newspic, "merged_env", return_value=({}, None)),
                mock.patch.object(mode_newspic, "get_access_token", return_value="token"),
                mock.patch.object(mode_newspic, "add_draft") as add_again,
                mock.patch.object(
                    mode_newspic,
                    "get_draft",
                    return_value={
                        "news_item": [{
                            "title": "Title",
                            "content": "Body",
                            "image_info": {"image_list": [
                                {"image_media_id": "image-1"},
                            ]},
                        }]
                    },
                ),
            ):
                self.assertEqual(mode_newspic.run(args), 0)
            add_again.assert_not_called()
            recovered = json.loads((root / ".pipeline" / "publish-result.json").read_text())
            self.assertEqual(recovered["creation_status"], "recovered")
            self.assertTrue(recovered["verification"]["ok"])

    def test_newspic_upload_checkpoint_resumes_without_reuploading_completed_images(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            images = [root / "one.png", root / "two.png"]
            for image in images:
                image.write_bytes(b"png")
            args = self.newspic_args(root, images)
            with (
                mock.patch.object(mode_newspic, "merged_env", return_value=({}, None)),
                mock.patch.object(mode_newspic, "get_access_token", return_value="token"),
                mock.patch.object(
                    mode_newspic,
                    "upload_image",
                    side_effect=["image-1", PublishError("upload stopped")],
                ),
            ):
                with self.assertRaisesRegex(PublishError, "upload stopped"):
                    mode_newspic.run(args)
            with (
                mock.patch.object(mode_newspic, "merged_env", return_value=({}, None)),
                mock.patch.object(mode_newspic, "get_access_token", return_value="token"),
                mock.patch.object(mode_newspic, "upload_image", return_value="image-2") as upload,
                mock.patch.object(mode_newspic, "add_draft", return_value="draft-1"),
                mock.patch.object(
                    mode_newspic,
                    "get_draft",
                    return_value={
                        "news_item": [{
                            "title": "Title",
                            "content": "Body",
                            "image_info": {"image_list": [
                                {"image_media_id": "image-1"},
                                {"image_media_id": "image-2"},
                            ]},
                        }]
                    },
                ),
            ):
                self.assertEqual(mode_newspic.run(args), 0)
            upload.assert_called_once()

    def test_article_html_upload_checkpoint_resumes_body_images(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("one.png", "two.png"):
                (root / name).write_bytes(b"\x89PNG\r\n\x1a\npayload")
            cover = root / "cover.png"
            cover.write_bytes(b"png")
            article = root / "article-body.html"
            article.write_text(
                '<section><p><span leaf="">正文。</span></p>'
                '<img src="one.png"><img src="two.png"></section>',
                encoding="utf-8",
            )
            args = Namespace(
                markdown=None, markdown_pos=None, html=str(article), layout_manifest=None,
                theme=None, color=None, no_cite=False, title="Title", author="",
                summary="", cover=str(cover), account="personal", env_file=None,
                dry_run=False, yes=True,
                result_output=str(root / ".pipeline" / "publish-result.json"),
                verify_draft=True, recover_draft_media_id=None,
            )
            with (
                mock.patch.object(mode_article, "_load_layout_manifest", return_value=({}, None)),
                mock.patch.object(mode_article, "merged_env", return_value=({}, None)),
                mock.patch.object(mode_article, "get_access_token", return_value="token"),
                mock.patch.object(
                    mode_article,
                    "upload_body_image",
                    side_effect=["https://mmbiz.qpic.cn/one", PublishError("upload stopped")],
                ),
            ):
                with self.assertRaisesRegex(PublishError, "upload stopped"):
                    mode_article.run(args)
            final_html = (
                '<section><p><span leaf="">正文。</span></p>'
                '<img src="https://mmbiz.qpic.cn/one">'
                '<img src="https://mmbiz.qpic.cn/two"></section>'
            )
            with (
                mock.patch.object(mode_article, "_load_layout_manifest", return_value=({}, None)),
                mock.patch.object(mode_article, "merged_env", return_value=({}, None)),
                mock.patch.object(mode_article, "get_access_token", return_value="token"),
                mock.patch.object(
                    mode_article, "upload_body_image", return_value="https://mmbiz.qpic.cn/two"
                ) as upload,
                mock.patch.object(mode_article, "upload_image", return_value="cover-media"),
                mock.patch.object(mode_article, "add_draft", return_value="draft-1"),
                mock.patch.object(
                    mode_article,
                    "get_draft",
                    return_value={"news_item": [{"title": "Title", "content": final_html}]},
                ),
            ):
                self.assertEqual(mode_article.run(args), 0)
            upload.assert_called_once()


if __name__ == "__main__":
    unittest.main()
