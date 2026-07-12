from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1] / "plugins" / "wechat-pipeline"
PUBLISHER_SCRIPTS = ROOT / "skills" / "wechat-publisher" / "scripts"
sys.path.insert(0, str(PUBLISHER_SCRIPTS))

from lib.errors import PublishError  # noqa: E402
from lib import proxy_client  # noqa: E402
from lib.account import account_value, resolve_account  # noqa: E402
from lib.env_loader import load_dotenv, merged_env  # noqa: E402
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


class PublisherInputTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
