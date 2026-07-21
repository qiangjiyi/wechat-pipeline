"""Newspic (image-text) mode: short post with 1-20 images, plain text content.

Behavior matches the original publish_newspic.py 1:1. Moved here as part of
the wechat-publisher refactor so that the CLI can dispatch to either mode.
"""

from __future__ import annotations

import json
import hashlib
import subprocess
import sys
from pathlib import Path

from lib.account import resolve_account
from lib.env_loader import merged_env
from lib.errors import PublishError, RetryablePublishError
from lib.draft_verifier import verify_newspic_draft
from lib.proxy_client import (
    DEFAULT_API_BASE,
    add_draft,
    get_access_token,
    get_draft,
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
from lib.source_loader import load_source, markdown_title, validate_newspic_source
from lib.pipeline_snapshot import load_pipeline_snapshot

SKILL_DIR = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = SKILL_DIR.parent.parent
MANIFEST_VALIDATOR = PLUGIN_ROOT / "scripts" / "validate_designer_manifest.py"


def _derive_title(text: str) -> str:
    """Derive the pipeline title without changing the sealed publication text."""
    value = markdown_title(text)
    if not value:
        raise PublishError("sealed pipeline input does not contain a title")
    return value


def _load_pipeline_manifest(args, result_path: Path | None) -> tuple[dict, Path, dict]:
    manifest_path = Path(args.manifest).expanduser().resolve()
    command = [sys.executable, str(MANIFEST_VALIDATOR), str(manifest_path)]
    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=60)
    if result.returncode != 0:
        detail = (result.stdout or result.stderr).strip()
        raise PublishError(f"pipeline manifest validation failed: {detail}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise PublishError(f"unable to read pipeline manifest: {err}") from err
    if manifest.get("mode") != "newspic":
        raise PublishError("newspic publishing requires a newspic manifest")
    canonical = Path(str(manifest.get("canonical_output_dir", ""))).expanduser().resolve()
    expected_manifest = canonical / ".pipeline" / "manifest.json"
    expected_result = canonical / ".pipeline" / "publish-result.json"
    if manifest_path != expected_manifest:
        raise PublishError(f"pipeline manifest must be {expected_manifest}")
    if result_path != expected_result:
        raise PublishError(f"pipeline newspic publishing requires {expected_result}")
    snapshot = load_pipeline_snapshot(getattr(args, "snapshot", None), canonical, "newspic")
    if args.source or args.content is not None or args.image:
        raise PublishError("--manifest cannot be combined with source, --content, or --image")
    source_path = Path(str(manifest["source"]["original_path"])).expanduser().resolve()
    sealed_text = source_path.read_text(encoding="utf-8").strip()
    publication = snapshot["data"].get("publication") or {}
    title = str(publication.get("title") or "").strip()
    if not title:
        raise PublishError("publish snapshot is missing newspic publication.title")
    if args.title is not None and args.title.strip() != title:
        raise PublishError("--title does not match publish snapshot")
    if args.author is not None and args.author.strip() != str(publication.get("author") or ""):
        raise PublishError("--author does not match publish snapshot")
    if args.digest is not None and args.digest.strip() != str(publication.get("digest") or ""):
        raise PublishError("--digest does not match publish snapshot")
    images = [str(Path(str(item["output_path"])).expanduser().resolve()) for item in manifest["images"]]
    source = {
        "title": title,
        "content": sealed_text,
        "author": publication.get("author") or "",
        "digest": publication.get("digest") or "",
        "images": images,
    }
    return source, canonical, {
        "path": manifest_path,
        "sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
        "snapshot": snapshot,
    }


def _confirm_newspic(account: str, title: str, content: str, images: list[Path]) -> None:
    print("Ready to publish WeChat newspic draft:")
    print(f"  account: {account}")
    print(f"  title: {title}")
    print(f"  content: {content}")
    print(f"  images: {len(images)}")
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
    # Resolve source file (positional arg) the same way the original script did.
    result_path = resolve_result_path(getattr(args, "result_output", None))
    manifest_binding = None
    if getattr(args, "manifest", None):
        source, base_dir, manifest_binding = _load_pipeline_manifest(args, result_path)
    else:
        direct = args.title is not None or args.content is not None or bool(args.image)
        if args.source:
            source_path = Path(args.source).expanduser().resolve()
            source = load_source(source_path)
            base_dir = source_path.parent
        elif direct:
            source = {}
            base_dir = Path.cwd()
        else:
            source_path = (Path.cwd() / "source.md").resolve()
            if not source_path.exists():
                raise PublishError("no source given and source.md not found in current directory")
            source = load_source(source_path)
            base_dir = source_path.parent

    # CLI overrides source fields.
    if manifest_binding is None and args.title is not None:
        source["title"] = args.title
    if manifest_binding is None and args.content is not None:
        source["content"] = args.content
    if manifest_binding is None and args.author is not None:
        source["author"] = args.author
    if manifest_binding is None and args.digest is not None:
        source["digest"] = args.digest
    if manifest_binding is None and args.image:
        source["images"] = list(args.image)

    env, used_env = merged_env(base_dir, args.env_file, SKILL_DIR)
    account = resolve_account(args.account, source, env)
    if manifest_binding and manifest_binding["snapshot"]["account"] != account:
        raise PublishError("selected account does not match publish snapshot")
    title, content, author, digest, images = validate_newspic_source(source, base_dir)
    api_base = env.get("WECHAT_API_BASE") or DEFAULT_API_BASE
    proxy_url = env.get("WECHAT_PROXY_URL", "")
    verify_draft = bool(getattr(args, "verify_draft", False))
    if verify_draft and result_path is None:
        raise PublishError("--verify-draft requires --result-output so a created draft cannot be duplicated")
    publish_fingerprint = fingerprint({
        "mode": "newspic",
        "account": account,
        "title": title,
        "content": content,
        "author": author,
        "digest": digest,
        "images": [{"path": str(path), "sha256": sha256_file(path)} for path in images],
        "manifest_sha256": manifest_binding["sha256"] if manifest_binding else None,
        "source_sha256": manifest_binding["source_sha256"] if manifest_binding else None,
        "snapshot_sha256": manifest_binding["snapshot"]["sha256"] if manifest_binding else None,
        "snapshot_fingerprint": manifest_binding["snapshot"]["fingerprint"] if manifest_binding else None,
        "run_identity": run_identity(result_path),
    })

    if args.dry_run:
        print(json.dumps({
            "mode": "newspic",
            "account": account,
            "env_file": str(used_env) if used_env else None,
            "api_base": api_base,
            "proxy": bool(proxy_url),
            "draft": {
                "article_type": "newspic",
                "title": title,
                "author": author,
                "digest": digest,
                "content": content,
                "images": [str(p) for p in images],
            },
            "publish_fingerprint": publish_fingerprint,
            "result_output": str(result_path) if result_path else None,
            "verify_draft": verify_draft,
        }, ensure_ascii=False, indent=2))
        return 0

    if manifest_binding and manifest_binding["snapshot"]["run_status"] != "publishing":
        raise PublishError("real pipeline publishing requires run status publishing")

    if not args.yes:
        _confirm_newspic(account, title, content, images)

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
            verification = verify_newspic_draft(
                get_draft(api_base, proxy_url, token, str(existing["draft_media_id"])),
                title=title,
                content=content,
                expected_image_count=len(images),
                expected_image_media_ids=list(existing.get("uploaded_image_media_ids") or []),
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

    print("[publish 1/3] resolve access token", flush=True)
    token = get_access_token(env, account, api_base, proxy_url)

    binding_fields = {
        "manifest_sha256": manifest_binding["sha256"] if manifest_binding else None,
        "source_sha256": manifest_binding["source_sha256"] if manifest_binding else None,
        "snapshot_sha256": manifest_binding["snapshot"]["sha256"] if manifest_binding else None,
        "snapshot_fingerprint": manifest_binding["snapshot"]["fingerprint"] if manifest_binding else None,
        "images": [{"path": str(path), "sha256": sha256_file(path)} for path in images],
    }
    media_ids: list[str] = list((checkpoint or {}).get("uploaded_image_media_ids") or [])
    if len(media_ids) > len(images):
        raise PublishError("publish checkpoint contains more image media IDs than manifest images")
    if checkpoint:
        print(f"resume: reusing {len(media_ids)} uploaded image(s)", flush=True)
    else:
        write_receipt(result_path, {
            "ok": False,
            "mode": "newspic",
            "account": account,
            "publish_fingerprint": publish_fingerprint,
            "draft_media_id": None,
            "creation_status": "uploading",
            "title": title,
            "image_count": len(images),
            "uploaded_image_media_ids": [],
            **binding_fields,
            "verification": {"ok": False, "status": "pending"},
        })
    for i, image in enumerate(images[len(media_ids):], start=len(media_ids) + 1):
        print(f"[publish 2/3][{i}/{len(images)}] upload {image}", flush=True)
        media_ids.append(upload_image(proxy_url, api_base, token, image))
        write_receipt(result_path, {
            "ok": False,
            "mode": "newspic",
            "account": account,
            "publish_fingerprint": publish_fingerprint,
            "draft_media_id": None,
            "creation_status": "uploading",
            "title": title,
            "image_count": len(images),
            "uploaded_image_media_ids": media_ids,
            **binding_fields,
            "verification": {"ok": False, "status": "pending"},
        })

    item: dict = {
        "article_type": "newspic",
        "title": title,
        "content": content,
        "image_info": {"image_list": [{"image_media_id": mid} for mid in media_ids]},
    }
    if author:
        item["author"] = author
    if digest:
        item["digest"] = digest

    print("[publish 3/3] create one WeChat draft", flush=True)
    try:
        draft_media_id = add_draft(api_base, proxy_url, token, [item])
    except RetryablePublishError as err:
        write_receipt(result_path, {
            "ok": False,
            "mode": "newspic",
            "account": account,
            "publish_fingerprint": publish_fingerprint,
            "draft_media_id": None,
            "creation_status": "unknown",
            "title": title,
            "image_count": len(images),
            "uploaded_image_media_ids": media_ids,
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
        "mode": "newspic",
        "account": account,
        "publish_fingerprint": publish_fingerprint,
        "draft_media_id": draft_media_id,
        "creation_status": "created",
        "title": title,
        "image_count": len(images),
        "uploaded_image_media_ids": media_ids,
        **binding_fields,
        "verification": {"ok": False, "status": "pending"} if verify_draft else {
            "ok": False, "status": "skipped"
        },
    })
    if verify_draft:
        print("[verify] read back the created draft", flush=True)
        verification = verify_newspic_draft(
            get_draft(api_base, proxy_url, token, draft_media_id),
            title=title,
            content=content,
            expected_image_count=len(images),
            expected_image_media_ids=media_ids,
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
