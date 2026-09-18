"""
Media intake tools — the same protocol as Higgsfield's MCP.

    media_upload   -> {uploads: [{media_id, upload_url, ...}]}
    PUT <upload_url> with the file bytes
    media_confirm  -> {results: [{media_id, type, url, ...}]}

The upload widget drives this itself through the host (callServerTool), exactly
as Higgsfield's does, then hands Claude the confirmed media_id.

One deliberate difference: Higgsfield's upload_url is a presigned storage URL
the browser PUTs to directly. The xelta-ai R2 bucket's CORS policy only admits
https://www.xelta.ai (checked 2026-09-17: a widget origin gets 403 on preflight),
so upload_url points at this server, which streams the bytes to R2. The widget
treats upload_url as opaque — if the bucket ever admits the widget origin,
media_upload can return a presigned URL with no widget change.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import socket
import sys
import re
import time
from urllib.parse import unquote, urlencode, urlparse

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

import tools.media_upload as upload_module
from config import (
    MEDIA_INTAKE_VIDEO_MAX_MB,
    MEDIA_INTAKE_VIDEO_MAX_SECONDS,
    MEDIA_UPLOAD_MAX_MB,
    MEDIA_UPLOAD_SECRET,
    MEDIA_UPLOAD_TOKEN_TTL_SECONDS,
    R2_BUCKET_NAME,
)
from tools import media_library as library
from tools import video_prep
from tools.media_library import MediaError
from tools.media_upload import (
    _b64, _unb64, _sniff_media, mp4_dimensions, mp4_duration_seconds, _r2, r2_configured,
)

IMPORT_MAX_MB = 50

_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "PUT, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "600",
}

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
# Any of these is accepted: tools/video_prep converts a clip to what the chosen
# engine takes when it's used, so intake only checks that it is a readable video.
_VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi")


def _limit_bytes(kind: str) -> int:
    return (MEDIA_INTAKE_VIDEO_MAX_MB if kind == "video" else MEDIA_UPLOAD_MAX_MB) * 1024 * 1024


def _sniff(head: bytes) -> tuple[str, str, str] | None:
    return _sniff_media(head) or video_prep.sniff_container(head)


def _measure_video(body: bytes, ext: str) -> tuple[float | None, int, int, str]:
    """(duration, width, height, problem) for an uploaded clip; problem is '' when usable."""
    info = video_prep.probe_bytes(body, ext)
    if info is None and video_prep.ffmpeg_paths():
        return None, 0, 0, "That file couldn't be read as a video."
    if info is None:
        if ext not in ("mp4", "mov"):
            return None, 0, 0, f"{ext.upper()} video needs ffmpeg on this computer. Upload MP4 or MOV instead."
        duration, (width, height) = mp4_duration_seconds(body), mp4_dimensions(body) or (0, 0)
    else:
        duration, width, height = info.duration, info.width, info.height
    if duration is not None and duration > MEDIA_INTAKE_VIDEO_MAX_SECONDS:
        return duration, width, height, (
            f"Video is {duration:.0f}s long; the most that can be uploaded is "
            f"{MEDIA_INTAKE_VIDEO_MAX_SECONDS:g}s. Video edits only use the first 10 seconds, "
            "so trim it and upload again."
        )
    return duration, width, height, ""


def kind_for(filename: str = "", content_type: str = "") -> str:
    ct, name = (content_type or "").lower(), (filename or "").lower()
    if ct.startswith("image/") or name.endswith(_IMAGE_EXTS):
        return "image"
    if ct.startswith("video/") or name.endswith(_VIDEO_EXTS):
        return "video"
    return ""


# ── per-slot upload tokens ────────────────────────────────────────────────────
# Bound to one media_id, so a leaked upload_url can only ever fill that slot.

def _sign(payload: str) -> str:
    return _b64(hmac.new(MEDIA_UPLOAD_SECRET.encode(), payload.encode(), hashlib.sha256).digest())


def mint_slot_token(user_id: str, media_id: str) -> str:
    claims = {"u": user_id, "m": media_id, "e": int(time.time()) + MEDIA_UPLOAD_TOKEN_TTL_SECONDS}
    payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
    return f"{payload}.{_sign(payload)}"


def verify_slot_token(token: str, media_id: str) -> str | None:
    try:
        payload, signature = token.split(".", 1)
        if not hmac.compare_digest(signature, _sign(payload)):
            return None
        claims = json.loads(_unb64(payload))
        if claims["e"] < time.time() or claims.get("m") != media_id:
            return None
        return claims["u"]
    except Exception:
        return None


# ── media_upload ──────────────────────────────────────────────────────────────

def media_upload(user_id: str, files: list[dict]) -> dict:
    if not r2_configured():
        raise MediaError("Uploads aren't configured on this server (R2 credentials missing).")
    if not user_id:
        raise MediaError("Could not identify your Xelta account. Run xelta_login, then try again.")
    uploads = []
    for f in files[:20]:
        filename = str(f.get("filename") or "upload")
        content_type = str(f.get("content_type") or "")
        kind = kind_for(filename, content_type)
        if not kind:
            raise MediaError(f"{filename}: only images (PNG, JPEG, WebP) and video (MP4, MOV, WebM, MKV, AVI) can be uploaded.")
        entry = library.new_upload(user_id, filename, kind)
        query = urlencode({"token": mint_slot_token(user_id, entry["media_id"])})
        base = upload_module.MCP_PUBLIC_BASE_URL.rstrip("/")
        uploads.append({
            "media_id": entry["media_id"],
            "upload_url": f"{base}/media/upload/{entry['media_id']}?{query}",
            "method": "PUT",
            "content_type": content_type,
            "type": kind,
            "max_bytes": _limit_bytes(kind),
            "expires_in_seconds": MEDIA_UPLOAD_TOKEN_TTL_SECONDS,
        })
    return {"uploads": uploads}


# ── PUT /media/upload/{media_id} ──────────────────────────────────────────────

def _err(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers=_CORS)


async def _store(user_id: str, media_id: str, body: bytes) -> tuple[dict | None, JSONResponse | None]:
    sniffed = _sniff(body[:64])
    if not sniffed:
        return None, _err(415, "Only PNG, JPEG, WebP images or video (MP4, MOV, WebM, MKV, AVI) are supported.")
    content_type, ext, kind = sniffed
    if len(body) > _limit_bytes(kind):
        return None, _err(413, f"{kind.capitalize()} is too large (max {_limit_bytes(kind) // 2**20} MB).")
    duration, width, height = None, 0, 0
    if kind == "video":
        duration, width, height, problem = await asyncio.to_thread(_measure_video, body, ext)
        if problem:
            return None, _err(413 if duration else 415, problem)

    key = library.upload_key(user_id, media_id, kind, ext)
    try:
        await asyncio.to_thread(_r2().put_object, Bucket=R2_BUCKET_NAME, Key=key,
                                Body=body, ContentType=content_type)
    except Exception as e:
        print(f"[media] R2 put failed key={key}: {e}", file=sys.stderr, flush=True)
        return None, _err(502, "Upload failed. Please try again.")
    entry = library.mark_uploaded(user_id, media_id, key=key, kind=kind, size=len(body),
                                  duration=duration, width=width, height=height)
    return entry, None


async def handle_media_put(request: Request) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=_CORS)
    media_id = request.path_params.get("media_id", "")
    user_id = verify_slot_token(request.query_params.get("token", ""), media_id)
    if not user_id:
        return _err(401, "This upload link has expired. Ask for a new upload box.")
    entry = library.get(user_id, media_id)
    if not entry or entry["status"] != "pending":
        return _err(409, "This upload slot is no longer open.")

    ceiling = _limit_bytes("video")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > ceiling:
        return _err(413, f"File is too large (max {ceiling // 2**20} MB).")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > ceiling:
            return _err(413, f"File is too large (max {ceiling // 2**20} MB).")

    stored, error = await _store(user_id, media_id, bytes(body))
    if error:
        return error
    return JSONResponse({"media_id": media_id, "type": stored["type"], "size": stored["size"]}, headers=_CORS)


# ── media_confirm ─────────────────────────────────────────────────────────────

async def media_confirm(user_id: str, media_ids: list[str]) -> dict:
    results = []
    for media_id in media_ids[:20]:
        entry = library.get(user_id, media_id)
        if not entry:
            results.append({"media_id": media_id, "status": "failed", "error": "Unknown media_id."})
            continue
        if entry["status"] == "uploaded":
            try:
                await asyncio.to_thread(_r2().head_object, Bucket=R2_BUCKET_NAME, Key=entry["key"])
            except Exception:
                results.append({"media_id": media_id, "status": "failed", "error": "The file never arrived."})
                continue
            entry = library.confirm(user_id, media_id)
        elif entry["status"] == "pending":
            results.append({"media_id": media_id, "status": "failed",
                            "error": "No file was uploaded to this slot yet."})
            continue
        results.append({"media_id": media_id, "status": "confirmed", "type": entry["type"],
                        "url": entry["url"], "filename": entry["filename"]})
    return {"results": results}


# ── media_import_url ──────────────────────────────────────────────────────────

async def _assert_public_host(host: str) -> None:
    """Refuse hosts that resolve anywhere but the public internet (SSRF guard)."""
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, 443, 0, socket.SOCK_STREAM)
    except socket.gaierror:
        raise MediaError(f"Couldn't resolve {host}.")
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise MediaError("That URL points at a private or local address and can't be imported.")


async def media_import_url(user_id: str, url: str) -> dict:
    url = (url or "").strip()
    if library.is_own_url(url):
        kind = "video" if urlparse(url).path.lower().endswith(_VIDEO_EXTS + (".webm",)) else "image"
        entry = library.add(user_id, url, kind, "imported")
        return {"media_id": entry["media_id"], "type": entry["type"], "url": entry["url"]}

    cap = IMPORT_MAX_MB * 1024 * 1024
    current = url
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        for _hop in range(4):
            parsed = urlparse(current)
            if parsed.scheme != "https" or not parsed.hostname:
                raise MediaError("Only https URLs can be imported.")
            await _assert_public_host(parsed.hostname)
            async with client.stream("GET", current) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    current = str(httpx.URL(current).join(r.headers["location"]))
                    continue
                if r.status_code != 200:
                    raise MediaError(f"The URL returned HTTP {r.status_code}.")
                body = bytearray()
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) > cap:
                        raise MediaError(f"That file is larger than {IMPORT_MAX_MB} MB.")
                break
        else:
            raise MediaError("Too many redirects.")

    media_id = library.new_id()
    sniffed = _sniff(bytes(body[:64]))
    if not sniffed:
        raise MediaError("That URL isn't a PNG, JPEG, WebP image or a video (MP4, MOV, WebM, MKV, AVI).")
    content_type, ext, kind = sniffed
    duration, width, height = None, 0, 0
    if kind == "video":
        duration, width, height, problem = await asyncio.to_thread(_measure_video, bytes(body), ext)
        if problem:
            raise MediaError(problem)
    key = library.upload_key(user_id, media_id, kind, ext)
    await asyncio.to_thread(_r2().put_object, Bucket=R2_BUCKET_NAME, Key=key,
                            Body=bytes(body), ContentType=content_type)
    entry = library.add(user_id, library.public_url(key), kind, "imported", key=key,
                        filename=urlparse(url).path.rsplit("/", 1)[-1][:120],
                        size=len(body), duration=duration, media_id=media_id,
                        width=width, height=height)
    return {"media_id": entry["media_id"], "type": entry["type"], "url": entry["url"]}


# ── download ──────────────────────────────────────────────────────────────────

DOWNLOAD_LINK_TTL_SECONDS = 3600


def download_link(user_id: str, media_id: str, filename: str = "") -> dict:
    """A short-lived link that downloads the file instead of playing it in the browser.

    Public media URLs open inline; a presigned GET can set Content-Disposition, so
    the browser saves it under a readable name.
    """
    entry = library.get(user_id, media_id)
    if not entry or not entry.get("url"):
        raise MediaError(f"No media with id {media_id}.")
    url = entry["url"]
    if not library.is_own_url(url) or not r2_configured():
        return {"url": url, "filename": ""}
    key = entry.get("key") or unquote(urlparse(url).path.lstrip("/"))
    ext = key.rsplit(".", 1)[-1].lower() if "." in key.rsplit("/", 1)[-1] else ""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", filename or f"xelta-{media_id[:8]}").strip("-.") or "xelta"
    name = f"{stem}.{ext}" if ext and not stem.lower().endswith("." + ext) else stem
    signed = _r2().generate_presigned_url(
        "get_object",
        Params={"Bucket": R2_BUCKET_NAME, "Key": key,
                "ResponseContentDisposition": f'attachment; filename="{name}"'},
        ExpiresIn=DOWNLOAD_LINK_TTL_SECONDS,
    )
    return {"url": signed, "filename": name}


# ── show_medias ───────────────────────────────────────────────────────────────

def show_medias(user_id: str, kind: str = "image", size: int = 24, cursor: int = 0) -> dict:
    entries, next_cursor = library.list_media(user_id, kind, max(1, min(size, 100)), max(0, cursor))
    return {
        "medias": [{
            "media_id": e["media_id"], "type": e["type"], "source": e["source"],
            "url": e["url"], "filename": e["filename"], "caption": e["caption"],
            "created_at": e["created"],
        } for e in entries],
        "next_cursor": next_cursor,
    }
