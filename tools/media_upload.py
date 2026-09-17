"""
Image uploads for MCP clients that can't pass files to tools.

Claude can see an image the user attaches to the chat, but has no way to hand
its bytes to an MCP tool. So upload_media renders an MCP Apps upload box
(media_upload_view.html) that sends the file to POST /media/upload; the server
stores it in R2 under the existing ad-multiplier folder, and the box posts the
public media.xelta.ai URL back into the conversation for Claude to pass to
create_reel as image_url.

/media/upload sits outside OAuth — the box runs in a sandboxed iframe with no
access to the MCP session — so each upload is authorised by a short-lived HMAC
token that upload_media mints for the calling user.
"""

import sys
import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse

from mcp.types import CallToolResult, TextContent
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from config import (
    MCP_PUBLIC_BASE_URL,
    MEDIA_UPLOAD_MAX_MB,
    MEDIA_VIDEO_MAX_MB,
    MEDIA_VIDEO_MAX_SECONDS,
    MEDIA_UPLOAD_SECRET,
    MEDIA_UPLOAD_TOKEN_TTL_SECONDS,
    R2_ACCESS_KEY_ID,
    R2_ACCOUNT_ID,
    R2_BUCKET_NAME,
    R2_PUBLIC_BASE_URL,
    R2_SECRET_ACCESS_KEY,
    R2_UPLOAD_PREFIX,
    _get_active_jwt,
)

VIEW_URI = "ui://xelta/upload-media.html"
VIEW_MIME_TYPE = "text/html;profile=mcp-app"
VIEW_HTML = Path(__file__).with_name("media_upload_view.html").read_text(encoding="utf-8")

# The view loads the ext-apps SDK from unpkg and uploads to this server; the
# host's iframe CSP blocks every other origin.
VIEW_CSP = {
    "resourceDomains": ["https://unpkg.com"],
    "connectDomains": [
        "{0.scheme}://{0.netloc}".format(urlparse(MCP_PUBLIC_BASE_URL)),
    ],
}

_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "600",
}

_r2_client = None

# Called as hook(user_id, url, kind) after each successful upload. Empty by
# default; the local server registers its recent-media recorder here, so the
# hosted multi-user server never writes a machine-local history file.
UPLOAD_HOOKS: list = []


def r2_configured() -> bool:
    return bool(R2_ACCOUNT_ID and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_BUCKET_NAME)


def _r2():
    global _r2_client
    if _r2_client is None:
        import boto3
        from botocore.config import Config

        _r2_client = boto3.client(
            "s3",
            endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
            region_name="auto",
            # boto3 >= 1.36 adds CRC checksums by default; R2 doesn't need them.
            config=Config(
                signature_version="s3v4",
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )
    return _r2_client


# ── Upload tokens ─────────────────────────────────────────────────────────────

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: str) -> str:
    return _b64(hmac.new(MEDIA_UPLOAD_SECRET.encode(), payload.encode(), hashlib.sha256).digest())


def mint_upload_token(user_id: str) -> str:
    claims = {"u": user_id, "e": int(time.time()) + MEDIA_UPLOAD_TOKEN_TTL_SECONDS}
    payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
    return f"{payload}.{_sign(payload)}"


def verify_upload_token(token: str) -> str | None:
    """Return the user id the token was minted for, or None if invalid or expired."""
    try:
        payload, signature = token.split(".", 1)
        if not hmac.compare_digest(signature, _sign(payload)):
            return None
        claims = json.loads(_unb64(payload))
        if claims["e"] < time.time():
            return None
        return claims["u"]
    except Exception:
        return None


def _current_user_id() -> str:
    """Xelta user id of the caller, reduced to characters safe in an R2 key."""
    try:
        claims = json.loads(_unb64(_get_active_jwt().split(".")[1]))
        user_id = str(claims.get("id") or claims.get("sub") or "")
    except Exception:
        user_id = ""
    return re.sub(r"[^A-Za-z0-9_-]", "", user_id)[:64]


# ── Validation ────────────────────────────────────────────────────────────────

def _sniff_media(head: bytes) -> tuple[str, str, str] | None:
    """(content_type, extension, kind) from magic bytes, or None if unsupported.

    `kind` is "image" or "video" and decides both the R2 folder and the size cap.
    """
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png", "image"
    if head.startswith(b"\xff\xd8\xff"):
        # ".jpeg", not ".jpg": create_reel derives the MIME type from the
        # extension, and "image/jpg" isn't a real type.
        return "image/jpeg", "jpeg", "image"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", "webp", "image"
    # ISO base media (MP4/MOV): a 'ftyp' box at offset 4. The ad-multiplier
    # source clip goes through this same route.
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"qt  ",):
            return "video/quicktime", "mov", "video"
        return "video/mp4", "mp4", "video"
    return None


# Kept as an alias so any caller still expecting the image-only check keeps working.
def _sniff_image(head: bytes) -> tuple[str, str] | None:
    sniffed = _sniff_media(head)
    if sniffed and sniffed[2] == "image":
        return sniffed[0], sniffed[1]
    return None


def mp4_duration_seconds(data: bytes) -> float | None:
    """Duration in seconds read from an MP4/MOV 'mvhd' box, or None.

    The video edit models cap source length (Wan repainting at 5s), and there is
    no ffprobe available here — but the whole file is already in memory during
    upload, so the header can simply be read. Scans for the box rather than
    walking the tree, since 'moov' may sit at either end of the file.
    """
    index = data.find(b"mvhd")
    if index < 0:
        return None
    # find() lands on the 4-byte box TYPE; the body starts immediately after it.
    body = index + 4
    try:
        version = data[body]
        if version == 1:
            # version | flags(3) | created(8) | modified(8) | timescale(4) | duration(8)
            timescale = int.from_bytes(data[body + 20:body + 24], "big")
            duration = int.from_bytes(data[body + 24:body + 32], "big")
        else:
            # version | flags(3) | created(4) | modified(4) | timescale(4) | duration(4)
            timescale = int.from_bytes(data[body + 12:body + 16], "big")
            duration = int.from_bytes(data[body + 16:body + 20], "big")
        if timescale > 0 and duration > 0:
            return duration / timescale
    except Exception:
        pass
    return None


def mp4_dimensions(data: bytes) -> tuple[int, int] | None:
    """Displayed (width, height) of the first video track in an MP4/MOV, or None.

    Reads each 'tkhd' box; audio tracks have zero width/height and are skipped. A
    90/270-degree rotation matrix (common on phone recordings) swaps the axes, so
    the result is what a viewer actually sees.
    """
    start = 0
    while True:
        index = data.find(b"tkhd", start)
        if index < 0:
            return None
        start = index + 4
        body = index + 4
        try:
            version = data[body]
            # version|flags(3) created modified track_id(4) reserved(4) duration
            # reserved(8) layer(2) group(2) volume(2) reserved(2) matrix(36) w h
            times = 8 if version == 1 else 4
            matrix = body + 4 + times * 2 + 4 + 4 + times + 8 + 8
            width = int.from_bytes(data[matrix + 36:matrix + 40], "big") >> 16
            height = int.from_bytes(data[matrix + 40:matrix + 44], "big") >> 16
            if not width or not height:
                continue
            b = int.from_bytes(data[matrix + 4:matrix + 8], "big", signed=True)
            return (height, width) if b != 0 else (width, height)
        except Exception:
            continue


def require_media_url(url: str, field: str = "image_url", kind: str = "image") -> str:
    """
    Reject anything but an https URL on the R2 public domain.

    The server downloads this URL itself, so an arbitrary value would let a
    caller make it read local files (load_file treats non-URLs as paths) or
    fetch internal addresses. `field`/`kind` only shape the error text, so a
    rejected video does not get told it should have been an image.
    """
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != urlparse(R2_PUBLIC_BASE_URL).hostname:
        article = "an" if kind[:1] in "aeiou" else "a"
        raise ValueError(
            f"{field} must be {article} {kind} uploaded to {R2_PUBLIC_BASE_URL} — "
            f"call upload_media so the user can upload their {kind} first."
        )
    return url


# ── MCP tool ──────────────────────────────────────────────────────────────────

def _upload_link(user_id: str) -> str:
    query = urlencode({"token": mint_upload_token(user_id)})
    return f"{MCP_PUBLIC_BASE_URL.rstrip('/')}/media/upload?{query}"


async def upload_media() -> CallToolResult:
    if not r2_configured():
        raise RuntimeError("Image uploads are not configured on this server (R2 credentials missing).")
    user_id = _current_user_id()
    if not user_id:
        raise RuntimeError("Could not identify your Xelta account. Reconnect Xelta and try again.")

    link = _upload_link(user_id)
    return CallToolResult(
        content=[TextContent(
            type="text",
            text=(
                "An upload box is showing in the chat. After the user uploads an image, "
                "its public URL is posted into the conversation — pass that URL as image_url "
                "to generate_image (to edit, restyle or upscale it) or to create_reel. "
                "If the upload box isn't visible, the user can upload "
                f"at this link instead and paste the resulting URL back: {link}"
            ),
        )],
        structuredContent={
            "upload_url": link,
            "max_mb": MEDIA_UPLOAD_MAX_MB,
            "max_video_mb": MEDIA_VIDEO_MAX_MB,
            "expires_in_seconds": MEDIA_UPLOAD_TOKEN_TTL_SECONDS,
        },
    )


# ── HTTP route: /media/upload ─────────────────────────────────────────────────

def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers=_CORS_HEADERS)


async def handle_media_upload(request: Request) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=_CORS_HEADERS)
    if request.method == "GET":
        # Standalone page for clients that don't render MCP Apps views.
        return HTMLResponse(VIEW_HTML)

    user_id = verify_upload_token(request.query_params.get("token", ""))
    if not user_id:
        return _error(401, "This upload link has expired. Ask for a new upload box.")
    if not r2_configured():
        return _error(503, "Image uploads are not configured on this server.")

    # Video needs a bigger ceiling than a reference image; cap at whichever
    # applies once the type is known, but stream-guard at the larger of the two.
    max_bytes = max(MEDIA_UPLOAD_MAX_MB, MEDIA_VIDEO_MAX_MB) * 1024 * 1024
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > max_bytes:
        return _error(413, f"File is too large (max {MEDIA_VIDEO_MAX_MB} MB).")

    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > max_bytes:
            return _error(413, f"File is too large (max {MEDIA_VIDEO_MAX_MB} MB).")

    sniffed = _sniff_media(bytes(body[:16]))
    if not sniffed:
        return _error(415, "Only PNG, JPEG, WebP images or MP4/MOV video are supported.")
    content_type, ext, kind = sniffed

    limit_mb = MEDIA_VIDEO_MAX_MB if kind == "video" else MEDIA_UPLOAD_MAX_MB
    if len(body) > limit_mb * 1024 * 1024:
        return _error(413, f"{kind.capitalize()} is too large (max {limit_mb} MB).")

    duration = mp4_duration_seconds(bytes(body)) if kind == "video" else None
    if duration is not None and duration > MEDIA_VIDEO_MAX_SECONDS:
        return _error(
            413,
            f"Video is {duration:.1f}s — the editor accepts clips up to "
            f"{MEDIA_VIDEO_MAX_SECONDS:g} seconds. Trim it and upload again.",
        )

    # src/ for source clips, ref/ for reference images — the layout already in
    # use under this prefix.
    folder = "src" if kind == "video" else "ref"
    key = f"{R2_UPLOAD_PREFIX}/u/{user_id}/{folder}/{secrets.token_urlsafe(16)}.{ext}"
    try:
        await asyncio.to_thread(
            _r2().put_object,
            Bucket=R2_BUCKET_NAME,
            Key=key,
            Body=bytes(body),
            ContentType=content_type,
        )
    except Exception as e:
        print(f"[media] R2 upload failed key={key}: {e}", file=sys.stderr, flush=True)
        return _error(502, "Upload failed. Please try again.")

    print(
        f"[media] uploaded user={user_id} key={key} bytes={len(body)}"
        + (f" duration={duration:.2f}s" if duration else ""),
        file=sys.stderr,
        flush=True,
    )
    payload = {
        "url": f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{key}",
        "content_type": content_type,
        "size": len(body),
        "kind": kind,
    }
    if duration is not None:
        payload["duration_seconds"] = round(duration, 2)
    for hook in UPLOAD_HOOKS:
        try:
            hook(user_id, payload["url"], kind)
        except Exception as e:
            print(f"[media] upload hook failed: {e}", file=sys.stderr, flush=True)
    return JSONResponse(payload, headers=_CORS_HEADERS)
