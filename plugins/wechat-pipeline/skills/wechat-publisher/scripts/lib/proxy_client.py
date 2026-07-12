"""HTTP client for the WeChat API, with optional HTTP proxy envelope."""

from __future__ import annotations

import base64
import json
import mimetypes
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, TypeVar

from .errors import DEFAULT_API_BASE, PublishError, RetryablePublishError, USER_AGENT


NETWORK_BACKOFF_SECONDS = (30.0, 60.0, 120.0)
T = TypeVar("T")


def _with_network_retry(
    operation: Callable[[], T],
    *,
    backoff: tuple[float, ...] = NETWORK_BACKOFF_SECONDS,
    sleeper: Callable[[float], None] | None = None,
) -> T:
    sleep = sleeper or time.sleep
    for attempt in range(len(backoff) + 1):
        try:
            return operation()
        except RetryablePublishError:
            if attempt == len(backoff):
                raise
            sleep(backoff[attempt])
    raise AssertionError("retry loop exhausted unexpectedly")


def _read_json_response(req: urllib.request.Request, *, timeout: int) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as err:
        raw = err.read()
        message = f"HTTP {err.code}: {raw.decode('utf-8', errors='replace')}"
        error_type = RetryablePublishError if err.code in {408, 429} or 500 <= err.code < 600 else PublishError
        raise error_type(message) from err
    except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
        raise RetryablePublishError(f"network error: {err}") from err
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise PublishError(f"response was not valid JSON: {err}") from err
    if not isinstance(data, dict):
        raise PublishError("response JSON must be an object")
    if data.get("errcode"):
        raise PublishError(f"WeChat error {data.get('errcode')}: {data.get('errmsg')}")
    return data


def request_json(
    url: str,
    method: str,
    payload: dict | None = None,
    *,
    backoff: tuple[float, ...] = NETWORK_BACKOFF_SECONDS,
    sleeper: Callable[[float], None] | None = None,
) -> dict:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def operation() -> dict:
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("User-Agent", USER_AGENT)
        if payload is not None:
            req.add_header("Content-Type", "application/json")
        return _read_json_response(req, timeout=30)

    return _with_network_retry(operation, backoff=backoff, sleeper=sleeper)


def proxy_json(proxy_url: str, url: str, method: str, payload: dict | None = None) -> dict:
    """Call the WeChat API through the HTTP proxy envelope: POST {url, method, data}."""
    envelope = {"url": url, "method": method}
    if payload is not None:
        envelope["data"] = payload
    return request_json(proxy_url, "POST", envelope)


def get_access_token(env: dict[str, str], account: str, api_base: str, proxy_url: str) -> str:
    from .account import account_value

    direct_token = account_value(env, account, "ACCESS_TOKEN")
    if direct_token:
        return direct_token
    app_id = account_value(env, account, "APP_ID")
    app_secret = account_value(env, account, "APP_SECRET")
    if not app_id or not app_secret:
        raise PublishError(f"missing app id/secret for account: {account}")
    query = urllib.parse.urlencode({
        "grant_type": "client_credential",
        "appid": app_id,
        "secret": app_secret,
    })
    url = f"{api_base}/cgi-bin/token?{query}"
    data = proxy_json(proxy_url, url, "GET") if proxy_url else request_json(url, "GET")
    token = data.get("access_token")
    if not token:
        raise PublishError(f"access_token missing in response: {data}")
    return str(token)


def with_token(url: str, token: str) -> str:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    query["access_token"] = [token]
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query, doseq=True)))


def _multipart_image(image: Path, prefix: str) -> tuple[str, bytes]:
    boundary = f"----wechat-publisher-{prefix}-{secrets.token_hex(16)}"
    mime_type = mimetypes.guess_type(str(image))[0] or "image/jpeg"
    safe_name = image.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="media"; filename="{safe_name}"\r\n'.encode(),
        f"Content-Type: {mime_type}\r\n\r\n".encode(),
        image.read_bytes(),
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    return boundary, body


def upload_image_direct(api_base: str, token: str, image: Path) -> str:
    """Direct multipart upload to /cgi-bin/material/add_material."""
    boundary, body = _multipart_image(image, "material")
    query = urllib.parse.urlencode({"type": "image"})
    url = with_token(f"{api_base}/cgi-bin/material/add_material?{query}", token)
    def operation() -> dict:
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        return _read_json_response(req, timeout=60)

    data = _with_network_retry(operation)
    if data.get("errcode"):
        raise PublishError(f"WeChat error {data.get('errcode')}: {data.get('errmsg')}")
    media_id = data.get("media_id")
    if not media_id:
        raise PublishError(f"media_id missing for {image}: {data}")
    return str(media_id)


def upload_image_proxy(proxy_url: str, api_base: str, token: str, image: Path) -> str:
    """Upload via proxy envelope: POST {url, method: UPLOAD, fileData (base64), ...}."""
    query = urllib.parse.urlencode({"type": "image"})
    url = with_token(f"{api_base}/cgi-bin/material/add_material?{query}", token)
    mime_type = mimetypes.guess_type(str(image))[0] or "image/jpeg"
    payload = {
        "url": url,
        "method": "UPLOAD",
        "fileData": base64.b64encode(image.read_bytes()).decode("ascii"),
        "fileName": image.name,
        "mimeType": mime_type,
        "fieldName": "media",
    }
    data = request_json(proxy_url, "POST", payload)
    media_id = data.get("media_id")
    if not media_id:
        raise PublishError(f"media_id missing for {image}: {data}")
    return str(media_id)


def upload_image(proxy_url: str, api_base: str, token: str, image: Path) -> str:
    """Upload an image, choosing proxy or direct path."""
    if proxy_url:
        return upload_image_proxy(proxy_url, api_base, token, image)
    return upload_image_direct(api_base, token, image)


UPLOAD_BODY_IMG_URL_SUFFIX = "/cgi-bin/media/uploadimg"


def _build_upload_envelope(proxy_url: str, url: str, image: Path) -> dict:
    mime_type = mimetypes.guess_type(str(image))[0] or "image/jpeg"
    return {
        "url": url,
        "method": "UPLOAD",
        "fileData": base64.b64encode(image.read_bytes()).decode("ascii"),
        "fileName": image.name,
        "mimeType": mime_type,
        "fieldName": "media",
    }


def upload_body_image_direct(api_base: str, token: str, image: Path) -> str:
    """Upload to /cgi-bin/media/uploadimg (temporary body image). Returns the mmbiz URL."""
    boundary, body = _multipart_image(image, "body")
    url = with_token(f"{api_base}{UPLOAD_BODY_IMG_URL_SUFFIX}", token)
    def operation() -> dict:
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        return _read_json_response(req, timeout=60)

    data = _with_network_retry(operation)
    if data.get("errcode") and data.get("errcode") != 0:
        raise PublishError(f"WeChat error {data.get('errcode')}: {data.get('errmsg')}")
    url_field = data.get("url")
    if not url_field:
        raise PublishError(f"uploadimg returned no url for {image}: {data}")
    return str(url_field)


def upload_body_image_proxy(proxy_url: str, api_base: str, token: str, image: Path) -> str:
    """Upload to media/uploadimg via the proxy envelope. Returns the mmbiz URL."""
    url = with_token(f"{api_base}{UPLOAD_BODY_IMG_URL_SUFFIX}", token)
    payload = _build_upload_envelope(proxy_url, url, image)
    data = request_json(proxy_url, "POST", payload)
    url_field = data.get("url")
    if not url_field:
        raise PublishError(f"uploadimg returned no url for {image}: {data}")
    return str(url_field)


def upload_body_image(proxy_url: str, api_base: str, token: str, image: Path) -> str:
    """Upload a body image, choosing proxy or direct path. Returns the mmbiz URL."""
    if proxy_url:
        return upload_body_image_proxy(proxy_url, api_base, token, image)
    return upload_body_image_direct(api_base, token, image)


def add_draft(api_base: str, proxy_url: str, token: str, articles: list[dict]) -> str:
    """POST /cgi-bin/draft/add with the given articles list. Returns the new draft's media_id."""
    url = with_token(f"{api_base}/cgi-bin/draft/add", token)
    data = proxy_json(proxy_url, url, "POST", {"articles": articles}) if proxy_url else request_json(url, "POST", {"articles": articles})
    media_id = data.get("media_id")
    if not media_id:
        raise PublishError(f"draft media_id missing: {data}")
    return str(media_id)
