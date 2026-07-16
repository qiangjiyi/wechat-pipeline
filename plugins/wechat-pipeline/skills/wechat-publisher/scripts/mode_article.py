"""Article (news) mode: render markdown to themed HTML, upload body images,
rewrite the HTML with mmbiz URLs, upload a cover, and POST a news draft."""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from lib.account import account_value, resolve_account
from lib.env_loader import merged_env
from lib.errors import PublishError, RetryablePublishError
from lib.html_article import inspect_html, upload_html_images
from lib.draft_verifier import verify_article_draft
from lib.proxy_client import (
    add_draft,
    get_draft,
    get_access_token,
    upload_body_image,
    upload_image,
)
from lib.result_store import (
    fingerprint,
    load_matching_receipt,
    publish_lock,
    resolve_result_path,
    run_identity,
    sha256_file,
    write_receipt,
)

SKILL_DIR = Path(__file__).resolve().parent.parent
RENDER_SCRIPT = SKILL_DIR / "scripts" / "render_markdown.mjs"
DEPENDENCY_SCRIPT = SKILL_DIR / "scripts" / "ensure_dependencies.py"
PLUGIN_ROOT = SKILL_DIR.parent.parent
LAYOUT_VALIDATOR = PLUGIN_ROOT / "scripts" / "validate_article_layout.py"

# Body image <img> replacement style. We borrow this from baoyu-post-to-wechat's
# wechat-api.ts to keep the article's body look consistent with theirs.
BODY_IMG_STYLE = "display: block; width: 100%; margin: 1.5em auto;"


def _run_renderer(args, markdown_path: Path, base_dir: Path) -> dict:
    """Invoke render_markdown.mjs and return its parsed JSON output."""
    dependency_proc = subprocess.run(
        [sys.executable, str(DEPENDENCY_SCRIPT), "--quiet"],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    if dependency_proc.returncode != 0:
        raise PublishError(dependency_proc.stderr.strip() or dependency_proc.stdout.strip())
    dependency_dir = dependency_proc.stdout.strip().splitlines()[-1]
    cmd = ["node", str(RENDER_SCRIPT)]
    if args.theme:
        cmd += ["--theme", args.theme]
    if args.color:
        cmd += ["--color", args.color]
    if args.no_cite:
        cmd += ["--no-cite"]
    cmd += ["--markdown", str(markdown_path)]
    try:
        render_env = dict(os.environ)
        render_env["WECHAT_PUBLISHER_NODE_DIR"] = dependency_dir
        proc = subprocess.run(
            cmd,
            cwd=str(base_dir),
            env=render_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except FileNotFoundError as err:
        raise PublishError(f"node runtime not found: {err}") from err
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        raise PublishError(f"render_markdown.mjs failed: {err or 'exit ' + str(proc.returncode)}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as err:
        raise PublishError(f"render_markdown.mjs returned non-JSON: {err}") from err


def _resolve_cover_path(cover_hint: str | None, base_dir: Path) -> Path | None:
    if not cover_hint:
        return None
    p = Path(cover_hint).expanduser()
    if not p.is_absolute():
        p = base_dir / p
    return p if p.exists() and p.is_file() else None


def _resolve_fallback_cover(base_dir: Path) -> Path | None:
    p = base_dir / "imgs" / "cover.png"
    return p if p.exists() and p.is_file() else None


def _rewrite_html_with_mmbiz(html: str, replacements: list[tuple[str, str]]) -> str:
    """Replace each `WECHATIMGPH_N` plain-text occurrence with a styled <img> tag.

    baoyu-md renders image placeholders as raw text inside <p> blocks (browser
    path replaces them via paste); for the API path we substitute the full tag.
    """
    out = html
    for placeholder, mmbiz_url in replacements:
        tag = f'<img src="{mmbiz_url}" style="{BODY_IMG_STYLE}" />'
        # Escape regex metachars in the placeholder, then replace ALL occurrences.
        pattern = re.escape(placeholder)
        out = re.sub(pattern, tag, out)
    return out


def _confirm_article(account: str, title: str, summary: str, image_count: int, cover: Path | None) -> None:
    print("Ready to publish WeChat article (news) draft:")
    print(f"  account:  {account}")
    print(f"  title:    {title}")
    print(f"  summary:  {summary}")
    print(f"  body images: {image_count}")
    if cover:
        print(f"  cover:    {cover}")
    try:
        answer = input("Publish now? Type 'yes' to continue: ").strip().lower()
    except EOFError as err:
        raise PublishError("cancelled: no interactive stdin; pass --yes to publish non-interactively") from err
    if answer != "yes":
        raise PublishError("cancelled")


def run(args) -> int:
    result_path = resolve_result_path(getattr(args, "result_output", None))
    with publish_lock(None if getattr(args, "dry_run", False) else result_path):
        return _run(args)


def _run(args) -> int:
    html_arg = getattr(args, "html", None)
    if html_arg:
        if args.markdown or args.markdown_pos:
            raise PublishError("--html is mutually exclusive with Markdown input")
        if args.theme or args.color or args.no_cite:
            raise PublishError("--theme, --color, and --no-cite apply only to Markdown rendering")
        return _run_html(args, Path(html_arg).expanduser().resolve())

    markdown_arg = args.markdown or args.markdown_pos
    if not markdown_arg:
        raise PublishError("article mode requires a markdown path (positional or --markdown)")

    markdown_path = Path(markdown_arg).expanduser().resolve()
    if not markdown_path.exists() or not markdown_path.is_file():
        raise PublishError(f"markdown not found: {markdown_path}")
    base_dir = markdown_path.parent

    # 1. Render markdown → HTML + metadata
    rendered = _run_renderer(args, markdown_path, base_dir)
    temporary_directory = rendered.pop("temporaryDirectory", None)
    try:
        return _run_rendered(args, markdown_path, base_dir, rendered)
    finally:
        _cleanup_render_temp(temporary_directory)


def _load_layout_manifest(args, html_path: Path) -> tuple[dict, Path | None]:
    value = getattr(args, "layout_manifest", None)
    manifest_path = Path(value).expanduser().resolve() if value else None
    command = [sys.executable, str(LAYOUT_VALIDATOR), str(html_path)]
    if manifest_path:
        command += ["--manifest", str(manifest_path)]
    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=60)
    if result.returncode != 0:
        detail = (result.stdout or result.stderr).strip()
        raise PublishError(f"gzh-design layout validation failed: {detail}")
    if not manifest_path:
        return {}, None
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8")), manifest_path
    except (OSError, json.JSONDecodeError) as err:
        raise PublishError(f"unable to read layout manifest: {err}") from err


def _resolve_html_cover(args, metadata: dict, base_dir: Path) -> tuple[Path | None, str]:
    if args.cover:
        explicit = Path(args.cover).expanduser()
        if not explicit.is_absolute():
            explicit = Path.cwd() / explicit
        explicit = explicit.resolve()
        return (explicit, "--cover") if explicit.is_file() else (None, "")
    hinted = _resolve_cover_path(metadata.get("cover_path"), base_dir)
    if hinted:
        return hinted, "layout-manifest"
    fallback = _resolve_fallback_cover(base_dir)
    return (fallback, "imgs/cover.png") if fallback else (None, "")


def _run_html(args, html_path: Path) -> int:
    if not html_path.is_file():
        raise PublishError(f"HTML not found: {html_path}")
    html = html_path.read_text(encoding="utf-8")
    sources = inspect_html(html)
    manifest, manifest_path = _load_layout_manifest(args, html_path)
    metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}

    title = (args.title or metadata.get("title") or "").strip()
    author = (args.author or metadata.get("author") or "").strip()
    summary = (args.summary or metadata.get("summary") or "").strip()
    if not title:
        raise PublishError("pre-typeset HTML requires --title or layout manifest metadata.title")

    base_dir = html_path.parent
    env, used_env = merged_env(base_dir, args.env_file, SKILL_DIR)
    account = resolve_account(args.account, {}, env)
    api_base = env.get("WECHAT_API_BASE", "").strip() or "https://api.weixin.qq.com"
    proxy_url = env.get("WECHAT_PROXY_URL", "").strip()
    cover_path, cover_source = _resolve_html_cover(args, metadata, base_dir)
    result_path = resolve_result_path(getattr(args, "result_output", None))
    verify_draft = bool(getattr(args, "verify_draft", False))
    if verify_draft and result_path is None:
        raise PublishError("--verify-draft requires --result-output so a created draft cannot be duplicated")
    identity = run_identity(result_path)
    if manifest_path and result_path and result_path != manifest_path.parent / "publish-result.json":
        raise PublishError("pipeline HTML publishing requires .pipeline/publish-result.json")
    publish_fingerprint = fingerprint({
        "mode": "article-html",
        "account": account,
        "title": title,
        "author": author,
        "summary": summary,
        "html_sha256": hashlib.sha256(html.encode("utf-8")).hexdigest(),
        "layout_sha256": sha256_file(manifest_path) if manifest_path else None,
        "cover_sha256": sha256_file(cover_path) if cover_path else None,
        "run_identity": identity,
    })

    if args.dry_run:
        print(json.dumps({
            "mode": "article-html",
            "account": account,
            "env_file": str(used_env) if used_env else None,
            "api_base": api_base,
            "proxy": bool(proxy_url),
            "html": str(html_path),
            "layout_manifest": str(manifest_path) if manifest_path else None,
            "title": title,
            "author": author,
            "summary": summary,
            "body_image_count": len(sources),
            "cover": str(cover_path) if cover_path else None,
            "cover_source": cover_source,
            "html_bytes": len(html.encode("utf-8")),
            "publish_fingerprint": publish_fingerprint,
            "result_output": str(result_path) if result_path else None,
            "verify_draft": verify_draft,
        }, ensure_ascii=False, indent=2))
        return 0

    if cover_path is None:
        raise PublishError("pre-typeset HTML requires --cover or layout manifest metadata.cover_path")
    if not args.yes:
        _confirm_article(account, title, summary, len(sources), cover_path)

    existing = load_matching_receipt(result_path, publish_fingerprint)
    checkpoint = None
    if existing:
        if existing.get("creation_status") == "unknown":
            recovered = str(getattr(args, "recover_draft_media_id", None) or "").strip()
            if not recovered:
                raise PublishError(
                    "draft creation outcome is unknown; refuse to call draft/add again. "
                    "Inspect the WeChat draft list, then pass --recover-draft-media-id to verify "
                    f"the confirmed draft: {result_path}"
                )
            existing["draft_media_id"] = recovered
            existing["creation_status"] = "recovered"
            existing["verification"] = {"ok": False, "status": "pending"}
            existing = write_receipt(result_path, existing)
        if not existing.get("draft_media_id"):
            checkpoint = existing
        elif verify_draft and not existing.get("verification", {}).get("ok"):
            print("resume: draft already exists; retrying draft/get verification only", flush=True)
            token = get_access_token(env, account, api_base, proxy_url)
            verification = verify_article_draft(
                get_draft(api_base, proxy_url, token, str(existing["draft_media_id"])),
                title=title,
                summary=summary,
                source_html=html,
                expected_image_count=len(sources),
            )
            existing["verification"] = verification
            existing["ok"] = verification["ok"]
            if verification["ok"]:
                existing.pop("error", None)
            existing = write_receipt(result_path, existing)
            if not verification["ok"]:
                raise PublishError(
                    f"draft exists but read-back verification failed; receipt preserved at {result_path}: "
                    + "; ".join(verification["errors"])
                )
        if existing.get("draft_media_id"):
            print(json.dumps({**existing, "reused": True}, ensure_ascii=False))
            return 0

    print("[publish 1/4] resolve access token", flush=True)
    token = get_access_token(env, account, api_base, proxy_url)
    print(f"[publish 2/4] upload and rewrite {len(sources)} body image(s)", flush=True)
    binding_fields = {
        "layout_sha256": sha256_file(manifest_path) if manifest_path else None,
        "html_sha256": sha256_file(html_path),
    }
    uploaded_body_images = dict((checkpoint or {}).get("uploaded_body_images") or {})

    def persist_body_checkpoint() -> None:
        write_receipt(result_path, {
            "ok": False,
            "mode": "article-html",
            "account": account,
            "publish_fingerprint": publish_fingerprint,
            "draft_media_id": None,
            "creation_status": "uploading",
            "title": title,
            "summary": summary,
            "body_image_count": len(sources),
            "uploaded_body_images": uploaded_body_images,
            "cover_path": str(cover_path),
            "cover_source": cover_source,
            **binding_fields,
            "verification": {"ok": False, "status": "pending"},
        })

    def save_body_checkpoint(source: str, url: str) -> None:
        uploaded_body_images[source] = url
        persist_body_checkpoint()

    if not checkpoint:
        persist_body_checkpoint()
    elif uploaded_body_images:
        print(f"resume: reusing {len(uploaded_body_images)} uploaded body image(s)", flush=True)
    html, uploaded_count = upload_html_images(
        html,
        base_dir,
        lambda image: upload_body_image(proxy_url, api_base, token, image),
        existing=uploaded_body_images,
        on_uploaded=save_body_checkpoint,
    )
    # Re-run the same fragment checks after source rewriting. The upstream layout
    # validator ran before upload and the replacement changes only img[src].
    inspect_html(html)

    thumb_media_id = str((checkpoint or {}).get("thumb_media_id") or "")
    if thumb_media_id:
        print("[publish 3/4] reuse uploaded cover", flush=True)
    else:
        print(f"[publish 3/4] upload cover ({cover_source}): {cover_path}", flush=True)
        thumb_media_id = upload_image(proxy_url, api_base, token, cover_path)
        write_receipt(result_path, {
            "ok": False,
            "mode": "article-html",
            "account": account,
            "publish_fingerprint": publish_fingerprint,
            "draft_media_id": None,
            "creation_status": "ready_to_create",
            "title": title,
            "summary": summary,
            "body_image_count": len(sources),
            "uploaded_body_images": uploaded_body_images,
            "cover_path": str(cover_path),
            "cover_source": cover_source,
            "thumb_media_id": thumb_media_id,
            **binding_fields,
            "verification": {"ok": False, "status": "pending"},
        })
    article: dict = {
        "article_type": "news",
        "title": title,
        "content": html,
        "thumb_media_id": thumb_media_id,
        "need_open_comment": 1,
        "only_fans_can_comment": 0,
    }
    if author:
        article["author"] = author
    if summary:
        article["digest"] = summary[:120]
    print("[publish 4/4] create one WeChat draft", flush=True)
    try:
        draft_media_id = add_draft(api_base, proxy_url, token, [article])
    except RetryablePublishError as err:
        write_receipt(result_path, {
            "ok": False,
            "mode": "article-html",
            "account": account,
            "publish_fingerprint": publish_fingerprint,
            "draft_media_id": None,
            "creation_status": "unknown",
            "title": title,
            "summary": summary,
            "body_image_count": len(sources),
            "uploaded_body_image_count": uploaded_count,
            "cover_path": str(cover_path),
            "cover_source": cover_source,
            "thumb_media_id": thumb_media_id,
            "uploaded_body_images": uploaded_body_images,
            **binding_fields,
            "verification": {"ok": False, "status": "blocked"},
            "error": str(err),
        })
        raise PublishError(
            "draft/add returned an ambiguous network failure; a safety receipt was preserved "
            f"at {result_path} and automatic recreation is disabled"
        ) from err
    receipt = write_receipt(result_path, {
        "ok": True,
        "mode": "article-html",
        "account": account,
        "publish_fingerprint": publish_fingerprint,
        "draft_media_id": draft_media_id,
        "creation_status": "created",
        **binding_fields,
        "title": title,
        "summary": summary,
        "body_image_count": len(sources),
        "uploaded_body_image_count": uploaded_count,
        "uploaded_body_images": uploaded_body_images,
        "thumb_media_id": thumb_media_id,
        "cover_path": str(cover_path),
        "cover_source": cover_source,
        "verification": {"ok": False, "status": "pending"} if verify_draft else {
            "ok": False, "status": "skipped"
        },
    })
    if verify_draft:
        print("[verify] read back the created draft", flush=True)
        verification = verify_article_draft(
            get_draft(api_base, proxy_url, token, draft_media_id),
            title=title,
            summary=summary,
            source_html=html,
            expected_image_count=len(sources),
        )
        receipt["verification"] = verification
        receipt = write_receipt(result_path, receipt)
        if not verification["ok"]:
            raise PublishError(
                f"draft was created but read-back verification failed; receipt preserved at {result_path}: "
                + "; ".join(verification["errors"])
            )
    print(json.dumps(receipt, ensure_ascii=False))
    return 0


def _cleanup_render_temp(value: object) -> None:
    if not value:
        return
    candidate = Path(str(value)).expanduser().resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if candidate.parent != temp_root or not candidate.name.startswith("wechat-publisher-render-"):
        print(f"warning: refused to clean unexpected renderer temp path: {candidate}", file=sys.stderr)
        return
    try:
        shutil.rmtree(candidate)
    except FileNotFoundError:
        pass
    except OSError as err:
        print(f"warning: unable to clean renderer temp path {candidate}: {err}", file=sys.stderr)


def _run_rendered(args, markdown_path: Path, base_dir: Path, rendered: dict) -> int:
    title = (args.title or rendered.get("title") or "").strip()
    author = (args.author or rendered.get("author") or "").strip()
    summary = (args.summary or rendered.get("summary") or "").strip()
    html = rendered.get("html") or ""
    content_images = rendered.get("contentImages") or []
    verification_source_html = re.sub(r"WECHATIMGPH_\d+", "", html)

    if not title:
        raise PublishError("title is empty (provide --title or a markdown H1/frontmatter.title)")
    if not html:
        raise PublishError("rendered HTML is empty")

    # 2. Resolve account + load env
    # Article mode has no source-file account; default to CLI flag or single-configured.
    env, used_env = merged_env(base_dir, args.env_file, SKILL_DIR)
    account = resolve_account(args.account, {}, env)
    api_base = env.get("WECHAT_API_BASE", "").strip() or "https://api.weixin.qq.com"
    proxy_url = env.get("WECHAT_PROXY_URL", "").strip()

    # 3. Resolve cover (priority: --cover > frontmatter hint > imgs/cover.png > first body image)
    explicit_cover = Path(args.cover).expanduser() if args.cover else None
    if explicit_cover and not explicit_cover.is_absolute():
        explicit_cover = (Path.cwd() / explicit_cover).resolve()
    cover_path: Path | None = None
    cover_source = ""
    if explicit_cover and explicit_cover.exists() and explicit_cover.is_file():
        cover_path = explicit_cover
        cover_source = "--cover"
    else:
        hinted = _resolve_cover_path(rendered.get("coverHint"), base_dir)
        if hinted:
            cover_path = hinted
            cover_source = "frontmatter"
        else:
            fb = _resolve_fallback_cover(base_dir)
            if fb:
                cover_path = fb
                cover_source = "imgs/cover.png"

    result_path = resolve_result_path(getattr(args, "result_output", None))
    verify_draft = bool(getattr(args, "verify_draft", False))
    if verify_draft and result_path is None:
        raise PublishError("--verify-draft requires --result-output so a created draft cannot be duplicated")
    publish_fingerprint = fingerprint({
        "mode": "article",
        "account": account,
        "title": title,
        "author": author,
        "summary": summary,
        "html_sha256": hashlib.sha256(html.encode("utf-8")).hexdigest(),
        "markdown_sha256": sha256_file(markdown_path),
        "content_images": [
            sha256_file(Path(item.get("localPath") or ""))
            for item in content_images
            if Path(item.get("localPath") or "").is_file()
        ],
        "cover_sha256": sha256_file(cover_path) if cover_path else None,
        "run_identity": run_identity(result_path),
    })

    # 4. Dry-run: print the resolved plan and return
    if args.dry_run:
        print(json.dumps({
            "mode": "article",
            "account": account,
            "env_file": str(used_env) if used_env else None,
            "api_base": api_base,
            "proxy": bool(proxy_url),
            "markdown": str(markdown_path),
            "theme": args.theme or "default",
            "color": args.color,
            "cite_status": not args.no_cite,
            "title": title,
            "author": author,
            "summary": summary,
            "body_image_count": len(content_images),
            "cover": str(cover_path) if cover_path else None,
            "cover_source": cover_source,
            "html_bytes": len(html),
            "publish_fingerprint": publish_fingerprint,
            "result_output": str(result_path) if result_path else None,
            "verify_draft": verify_draft,
        }, ensure_ascii=False, indent=2))
        return 0

    # 5. Interactive confirmation
    if not args.yes:
        _confirm_article(account, title, summary, len(content_images), cover_path)

    existing = load_matching_receipt(result_path, publish_fingerprint)
    if existing:
        if existing.get("creation_status") == "unknown":
            recovered = str(getattr(args, "recover_draft_media_id", None) or "").strip()
            if not recovered:
                raise PublishError(
                    "draft creation outcome is unknown; refuse to call draft/add again. "
                    "Inspect the WeChat draft list, then pass --recover-draft-media-id to verify "
                    f"the confirmed draft: {result_path}"
                )
            existing["draft_media_id"] = recovered
            existing["creation_status"] = "recovered"
            existing["verification"] = {"ok": False, "status": "pending"}
            existing = write_receipt(result_path, existing)
        if verify_draft and not existing.get("verification", {}).get("ok"):
            print("resume: draft already exists; retrying draft/get verification only", flush=True)
            token = get_access_token(env, account, api_base, proxy_url)
            verification = verify_article_draft(
                get_draft(api_base, proxy_url, token, str(existing["draft_media_id"])),
                title=title,
                summary=summary,
                source_html=verification_source_html,
                expected_image_count=len(content_images),
            )
            existing["verification"] = verification
            existing["ok"] = verification["ok"]
            if verification["ok"]:
                existing.pop("error", None)
            existing = write_receipt(result_path, existing)
            if not verification["ok"]:
                raise PublishError(
                    f"draft exists but read-back verification failed; receipt preserved at {result_path}: "
                    + "; ".join(verification["errors"])
                )
        print(json.dumps({**existing, "reused": True}, ensure_ascii=False))
        return 0

    # 6. Get access token
    print("[publish 1/4] resolve access token", flush=True)
    token = get_access_token(env, account, api_base, proxy_url)

    # 7. Upload body images, collect mmbiz URLs, then rewrite HTML
    replacements: list[tuple[str, str]] = []
    for idx, img in enumerate(content_images, start=1):
        local_path = Path(img.get("localPath") or "")
        if not local_path or not local_path.exists():
            raise PublishError(f"body image not found: {img.get('originalPath')}")
        print(
            f"[publish 2/4][{idx}/{len(content_images)}] upload body image {local_path.name}",
            flush=True,
        )
        mmbiz_url = upload_body_image(proxy_url, api_base, token, local_path)
        replacements.append((img["placeholder"], mmbiz_url))
    html = _rewrite_html_with_mmbiz(html, replacements)

    # 8. Upload cover (material) → thumb_media_id
    if cover_path is None and content_images:
        # Fall back to using the first body image as cover (uploaded as material)
        first = Path(content_images[0].get("localPath") or "")
        if first and first.exists():
            cover_path = first
            cover_source = "first-body-image"
    if cover_path is None:
        raise PublishError("no cover image (provide --cover, frontmatter.coverImage, or imgs/cover.png)")

    print(f"[publish 3/4] upload cover ({cover_source}): {cover_path}", flush=True)
    thumb_media_id = upload_image(proxy_url, api_base, token, cover_path)

    # 9. Compose article payload
    article: dict = {
        "article_type": "news",
        "title": title,
        "content": html,
        "thumb_media_id": thumb_media_id,
        "need_open_comment": 1,
        "only_fans_can_comment": 0,
    }
    if author:
        article["author"] = author
    if summary:
        # WeChat accepts `digest` for news type (max ~120 chars). Truncate to be safe.
        article["digest"] = summary[:120]

    # 10. Submit
    print("[publish 4/4] create one WeChat draft", flush=True)
    try:
        draft_media_id = add_draft(api_base, proxy_url, token, [article])
    except RetryablePublishError as err:
        write_receipt(result_path, {
            "ok": False,
            "mode": "article",
            "account": account,
            "publish_fingerprint": publish_fingerprint,
            "draft_media_id": None,
            "creation_status": "unknown",
            "title": title,
            "summary": summary,
            "body_image_count": len(content_images),
            "cover_path": str(cover_path),
            "cover_source": cover_source,
            "thumb_media_id": thumb_media_id,
            "verification": {"ok": False, "status": "blocked"},
            "error": str(err),
        })
        raise PublishError(
            "draft/add returned an ambiguous network failure; a safety receipt was preserved "
            f"at {result_path} and automatic recreation is disabled"
        ) from err
    receipt = write_receipt(result_path, {
        "ok": True,
        "mode": "article",
        "account": account,
        "publish_fingerprint": publish_fingerprint,
        "draft_media_id": draft_media_id,
        "creation_status": "created",
        "title": title,
        "summary": summary,
        "body_image_count": len(content_images),
        "cover_path": str(cover_path),
        "cover_source": cover_source,
        "verification": {"ok": False, "status": "pending"} if verify_draft else {
            "ok": False, "status": "skipped"
        },
    })
    if verify_draft:
        print("[verify] read back the created draft", flush=True)
        verification = verify_article_draft(
            get_draft(api_base, proxy_url, token, draft_media_id),
            title=title,
            summary=summary,
            source_html=verification_source_html,
            expected_image_count=len(content_images),
        )
        receipt["verification"] = verification
        receipt = write_receipt(result_path, receipt)
        if not verification["ok"]:
            raise PublishError(
                f"draft was created but read-back verification failed; receipt preserved at {result_path}: "
                + "; ".join(verification["errors"])
            )
    print(json.dumps(receipt, ensure_ascii=False))
    return 0
