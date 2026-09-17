"""
Local stdio entry point for Claude Desktop.

Built on the same pattern as Higgsfield's MCP:
  - media is referred to by media_id (or a prior job_id), never by raw URL;
  - the upload widget runs media_upload -> PUT -> media_confirm itself and hands
    Claude a confirmed media_id in a visible message;
  - generation returns a job_id at once and the generation widget polls
    job_status, so no tool call sits open while a provider works;
  - sign-in is a widget too, and failed calls name a recovery_tool.

Auth: the browser sign-in saved by xelta_login (the Xelta Figma plugin's session
flow), falling back to XELTA_JWT_TOKEN.

Uploads: the xelta-ai bucket's CORS policy doesn't admit the widget's origin, so
media_upload's upload_url points at a loopback listener this process starts,
which streams the bytes to R2.
"""
import socket
import sys
import threading

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

import config

# This process belongs to whoever is at this machine, so the browser sign-in
# saved by xelta_login may stand in for OAuth. Must precede any tool call.
config.LOCAL_SESSION_AUTH = True

import tools.media_upload as media_upload
from tools import generation, jobs, media_tools, widgets
from tools.credits import check_balance as _check_balance
from tools.game_development import game_development_route as _game_development_route
from tools.media_library import MediaError
from tools.media_upload import _current_user_id
from tools.reel_creator import create_reel as _create_reel
from tools.utils import format_tool_error
from tools.xelta_session import login_step as _login_step, logout as _xelta_logout

mcp = FastMCP("xelta")


# ── Loopback upload listener ──────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# The port has to be settled HERE, before the @mcp.resource decorators below
# run: they capture the upload widget's CSP at import time, and a policy that
# doesn't name this port silently blocks the upload PUT ("Network error").
_UPLOAD_PORT = _free_port()
_UPLOAD_ORIGIN = f"http://127.0.0.1:{_UPLOAD_PORT}"
media_upload.MCP_PUBLIC_BASE_URL = _UPLOAD_ORIGIN
UPLOAD_CSP = widgets.upload_csp(_UPLOAD_ORIGIN)


def _start_upload_listener() -> None:
    import uvicorn
    from starlette.applications import Starlette
    from starlette.routing import Route

    app = Starlette(routes=[
        Route("/media/upload/{media_id}", media_tools.handle_media_put, methods=["PUT", "OPTIONS"]),
    ])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=_UPLOAD_PORT, log_level="critical"))
    threading.Thread(target=server.run, daemon=True).start()


# ── results ───────────────────────────────────────────────────────────────────

def _auth_problem(e: Exception) -> bool:
    if isinstance(e, httpx.HTTPStatusError):
        return e.response.status_code in (401, 403)
    return isinstance(e, ValueError) and "No Xelta sign-in" in str(e)


def _error(e: Exception) -> CallToolResult:
    """Failed call. Auth problems name the tool that fixes them, like Higgsfield's."""
    recovery = ""
    if isinstance(e, MediaError):
        text = str(e)
    elif _auth_problem(e):
        recovery = "xelta_login"
        text = "Xelta sign-in is required. Call xelta_login now, without asking or explaining first."
    else:
        text = format_tool_error(e)
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent={"error": text, "recovery_tool": recovery},
        isError=True,
    )


def _ok(text: str, structured: dict) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], structuredContent=structured)


def _user() -> str:
    user_id = _current_user_id()
    if not user_id:
        raise ValueError("No Xelta sign-in available.")
    return user_id


_JOBS_NOTE = (
    "The generation widget shows progress and swaps in the result by itself — don't call "
    "job_status unless the user asks. To build on a finished result, pass its job_id as a "
    "media value. If this call timed out on your side, the outcome is unknown: don't "
    "resubmit; check with job_status."
)


def _jobs_result(out: dict, summary: str) -> CallToolResult:
    lines = [summary] + [
        f"- job_id {j['job_id']}{' (' + j['label'] + ')' if j['label'] else ''}: {j['status']}"
        for j in out["jobs"]
    ]
    return _ok("\n".join(lines + ["", _JOBS_NOTE]), {"jobs": out["jobs"]})


# ── account ───────────────────────────────────────────────────────────────────

@mcp.tool(annotations={"title": "Check Xelta Balance", "readOnlyHint": True, "destructiveHint": False,
                       "idempotentHint": True, "openWorldHint": True})
async def check_balance() -> CallToolResult:
    """Check your Xelta credit balance."""
    try:
        data = await _check_balance()
    except Exception as e:
        return _error(e)
    balance = data.get("data", 0)
    return _ok(f"Your Xelta balance: {balance} credits", {"balance": balance})


@mcp.tool(
    meta=widgets.ui_meta(widgets.LOGIN_URI),
    annotations={"title": "Sign in to Xelta", "readOnlyHint": False, "destructiveHint": False,
                 "idempotentHint": True, "openWorldHint": True},
)
async def xelta_login(switch_account: bool = False) -> CallToolResult:
    """
    Sign in to Xelta. Shows a sign-in widget with a button; it waits for the user
    in the background and sends "I've signed in" when done — so after calling
    this, stop and wait for that message. Don't call it repeatedly yourself.

    Call it immediately (without asking first) whenever a tool reports
    recovery_tool "xelta_login".

    Args:
        switch_account: Sign out first so the user can pick a different account.
    """
    try:
        step = await _login_step(switch_account)
    except Exception as e:
        return _error(e)
    if step["signed_in"]:
        return _ok(step["message"], step)
    return _ok(
        "A sign-in button is showing. Wait for the user — the widget sends a message when "
        "sign-in completes. Don't call xelta_login again or ask them to paste anything.",
        step,
    )


@mcp.tool(annotations={"title": "Sign out of Xelta", "readOnlyHint": False, "destructiveHint": True,
                       "idempotentHint": True, "openWorldHint": True})
async def xelta_logout() -> str:
    """Sign out of Xelta on this machine and end the session on Xelta's side."""
    try:
        return await _xelta_logout()
    except Exception as e:
        return format_tool_error(e)


# ── media ─────────────────────────────────────────────────────────────────────

@mcp.tool(
    meta=widgets.ui_meta(widgets.UPLOAD_URI),
    annotations={"title": "Upload Media", "readOnlyHint": False, "destructiveHint": False,
                 "idempotentHint": False, "openWorldHint": False},
)
async def media_upload_widget(type: str = "auto", multiple: bool = False, max_files: int = 1,
                              min_files: int = 1, label: str = "") -> CallToolResult:
    """
    Required local-media intake. Call this immediately, as the only tool in the turn,
    when the user refers to an attached or local photo or video but you have no
    confirmed media_id for it yet.

    Claude chat attachments can't be passed to Xelta tools, so don't ask the user to
    attach files to the chat. This widget is the upload surface: the user selects the
    file, the widget uploads and confirms it, then sends a message containing the
    confirmed media_id. Stop after calling this and wait for that message.

    Before calling, check whether the file already exists: if the user refers to
    something uploaded or generated earlier, use show_medias instead.

    Args:
        type: 'image', 'video' or 'auto'.
        multiple: Allow several files (e.g. an ad plus reference images).
        max_files: Most files accepted (1-20).
        min_files: Fewest confirmed files before Continue is enabled.
        label: Optional short heading, e.g. "Upload the ad to multiply".
    """
    return _ok(
        "An upload box is showing. Wait for the user — a message with the confirmed "
        "media_id arrives when the upload finishes.",
        {"type": type if type in ("image", "video", "auto") else "auto",
         "multiple": bool(multiple), "max_files": max(1, min(max_files, 20)),
         "min_files": max(1, min(min_files, 20)), "label": label[:80],
         "limits": {"image_mb": config.MEDIA_UPLOAD_MAX_MB, "video_mb": config.MEDIA_INTAKE_VIDEO_MAX_MB,
                    "video_seconds": config.MEDIA_INTAKE_VIDEO_MAX_SECONDS}},
    )


@mcp.resource(widgets.UPLOAD_URI, mime_type=widgets.MIME_TYPE, meta={"ui": {"csp": UPLOAD_CSP}})
def upload_widget() -> str:
    """Upload widget."""
    return widgets.UPLOAD_HTML


@mcp.tool(annotations={"title": "Create Upload Slot", "readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": False})
async def media_upload(filename: str = "", content_type: str = "", files: list[dict] | None = None) -> CallToolResult:
    """
    Reserve upload slots. Returns a media_id and an upload_url per file; PUT the file
    bytes to upload_url, then call media_confirm. Used by the upload widget. For a
    user's own photo or video, call media_upload_widget instead.

    Args:
        filename: Single file name, e.g. 'ad.mp4'.
        content_type: Its MIME type, e.g. 'image/png'.
        files: Alternatively 1-20 {filename, content_type} objects.
    """
    try:
        specs = files or [{"filename": filename, "content_type": content_type}]
        out = media_tools.media_upload(_user(), specs)
    except Exception as e:
        return _error(e)
    return _ok(f"Reserved {len(out['uploads'])} upload slot(s). PUT each file to its upload_url, then call media_confirm.", out)


@mcp.tool(annotations={"title": "Confirm Upload", "readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": True, "openWorldHint": False})
async def media_confirm(media_id: str = "", media_ids: list[str] | None = None, type: str = "") -> CallToolResult:
    """
    Confirm uploads after their bytes were PUT to upload_url. Only confirmed media can
    be used in generation.

    Args:
        media_id: Single media_id to confirm.
        media_ids: Or several.
        type: 'image' or 'video' (informational).
    """
    try:
        ids = media_ids or ([media_id] if media_id else [])
        if not ids:
            raise MediaError("Pass media_id or media_ids.")
        out = await media_tools.media_confirm(_user(), ids)
    except Exception as e:
        return _error(e)
    ok = [r for r in out["results"] if r["status"] == "confirmed"]
    return _ok(f"Confirmed {len(ok)} of {len(out['results'])}: " + ", ".join(r["media_id"] for r in ok), out)


@mcp.tool(annotations={"title": "Import Media From URL", "readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": True})
async def media_import_url(url: str) -> CallToolResult:
    """
    Import an https image or video from the web and get a confirmed media_id. Use this
    whenever the user gives a web link to media — generation tools take media_ids, not URLs.
    Max 50 MB; video up to 10 seconds.

    Args:
        url: https URL of a PNG, JPEG, WebP image or MP4/MOV video.
    """
    try:
        out = await media_tools.media_import_url(_user(), url)
    except Exception as e:
        return _error(e)
    return _ok(f"Imported as media_id {out['media_id']} ({out['type']}).", out)


@mcp.tool(annotations={"title": "Show Media", "readOnlyHint": True, "destructiveHint": False,
                       "idempotentHint": True, "openWorldHint": False})
async def show_medias(type: str = "image", size: int = 24, cursor: int = 0) -> CallToolResult:
    """
    List the user's uploaded, imported and generated media, newest first, with media_ids.
    Use it when the user refers to something from earlier ("my cat photo", "the ad I
    uploaded yesterday") so they don't have to upload it again. Call once with the one
    type they mean. Paginated via next_cursor.

    Args:
        type: 'image' or 'video'.
        size: Page size (1-100).
        cursor: next_cursor from the previous page.
    """
    try:
        out = media_tools.show_medias(_user(), type, size, cursor)
    except Exception as e:
        return _error(e)
    lines = [f"{len(out['medias'])} {type}(s), newest first:"] + [
        f"- media_id {m['media_id']} · {m['source']}"
        + (f" · {m['filename']}" if m['filename'] else "")
        + (f' · "{m["caption"]}"' if m["caption"] else "")
        for m in out["medias"]
    ]
    if out["next_cursor"] is not None:
        lines.append(f"More available: cursor={out['next_cursor']}")
    return _ok("\n".join(lines), out)


# ── generation ────────────────────────────────────────────────────────────────

@mcp.tool(
    meta=widgets.ui_meta(widgets.GENERATION_URI),
    annotations={"title": "Generate Image", "readOnlyHint": False, "destructiveHint": False,
                 "idempotentHint": False, "openWorldHint": True},
)
async def generate_image(prompt: str = "", model: str = "", medias: list[dict] | None = None,
                         size: str = "", optimize_prompt: bool = True) -> CallToolResult:
    """
    Generate an image, or edit one, and show it in the generation widget.

    Returns a job_id immediately; the widget shows progress and the result. Don't poll.

    Inputs are media references, never URLs: `medias` is a list of
    {"value": <media_id or job_id>, "role": "image" | "style"}.
    - role "image": the picture to edit.
    - role "style": a second image used as a style reference (style-transfer models).
    For the user's own local file call media_upload_widget first; for a web link call
    media_import_url; for something from earlier call show_medias.

    Don't ask the user for a size — edits keep the source's shape automatically.

    Args:
        prompt: What to generate, or what to change.
        model: Leave empty for Gemini 2.5 Flash (59 credits), which generates and edits.
        medias: Input media, as above.
        size: Only if the user explicitly asked for a size or aspect ratio.
        optimize_prompt: Prompt enhancement where the model supports it.
    """
    try:
        out = await generation.generate_image(_user(), prompt=prompt, model=model, medias=medias,
                                              size=size, optimize_prompt=optimize_prompt)
    except Exception as e:
        return _error(e)
    return _jobs_result(out, f"Started image job with {out['model']}.")


@mcp.tool(
    meta=widgets.ui_meta(widgets.GENERATION_URI),
    annotations={"title": "Ad Multiplier", "readOnlyHint": False, "destructiveHint": False,
                 "idempotentHint": False, "openWorldHint": True},
)
async def multiply_ad(source: str, variants: str, references: list[str] | None = None,
                      model: str = "", size: str = "") -> CallToolResult:
    """
    Turn one ad (image or video) into several independently edited variants — swapping
    people, products, outfits, objects or backgrounds while keeping the original
    composition, and for video the motion, cuts and source audio.

    Returns one job per variant immediately; the generation widget shows them all and
    fills each in as it finishes. Don't poll.

    Video edits always run on Kling O3 Pro Video Editor: 2,670 credits per variant,
    keeps the source audio, uses up to the first 10 seconds. There is no other video model.

    Any video works as the source — any resolution, frame rate, format (MP4, MOV, WebM,
    MKV, AVI), HDR or phone rotation. It is converted automatically to what the model
    accepts before sending; the result says what was changed (e.g. upscaled, trimmed),
    so tell the user that in a sentence. Don't ask the user to re-export a clip.

    Args:
        source: media_id of the ad (from media_upload_widget, media_import_url or
            show_medias), or the job_id of an earlier result.
        variants: The edits, ONE PER LINE — one output per line.
        references: Optional media_ids, matched to variants BY POSITION (reference 1 is
            for line 1). Use "" for a variant without a reference. Never combined into
            every pairing.
        model: Image only — leave empty for Gemini 2.5 Flash (59 credits each).
        size: Image only — only if the user asked for a size.
    """
    try:
        out = await generation.multiply_ad(_user(), source=source, variants=variants,
                                           references=references, model=model, size=size)
    except Exception as e:
        return _error(e)
    kind = out["jobs"][0]["type"] if out["jobs"] else "media"
    summary = f"Started {len(out['jobs'])} {kind} variant job(s) with {out['model']}."
    if out.get("source_changes"):
        summary += " The source clip is converted first so the model accepts it: " + "; ".join(out["source_changes"]) + "."
    return _jobs_result(out, summary)


@mcp.tool(
    meta=widgets.ui_meta(widgets.GENERATION_URI),
    annotations={"title": "Job Status", "readOnlyHint": True, "destructiveHint": False,
                 "idempotentHint": True, "openWorldHint": True},
)
async def job_status(job_id: str, source: str = "", wait_seconds: int = 0) -> CallToolResult:
    """
    Status and results of a generation job. Returns instantly by default; for a job
    that isn't finished, the response says how long to wait before checking again.
    The generation widget calls this itself, so you rarely need to.

    Args:
        job_id: From generate_image or multiply_ad.
        source: Set to 'widget' by the generation widget.
        wait_seconds: Wait up to this long (max 15) for the job to finish.
    """
    try:
        job = await jobs.job_status(_user(), job_id, wait_seconds=wait_seconds)
        if job is None:
            raise MediaError(f"No job with id {job_id}.")
    except Exception as e:
        return _error(e)
    text = f"Job {job['job_id']}: {job['status']}"
    if job["results"]:
        text += "; result media_id " + ", ".join(r["media_id"] for r in job["results"])
    if job["error"]:
        text += f"; {job['error']}"
    if job["recovery_tool"]:
        text += f". recovery_tool: {job['recovery_tool']} — call it now."
    return _ok(text, {"job": job})


@mcp.resource(widgets.GENERATION_URI, mime_type=widgets.MIME_TYPE, meta={"ui": {"csp": widgets.GENERATION_CSP}})
def generation_widget() -> str:
    """Generation widget."""
    return widgets.GENERATION_HTML


@mcp.resource(widgets.LOGIN_URI, mime_type=widgets.MIME_TYPE, meta={"ui": {"csp": widgets.LOGIN_CSP}})
def login_widget() -> str:
    """Sign-in widget."""
    return widgets.LOGIN_HTML


# ── other tools (unchanged) ───────────────────────────────────────────────────

@mcp.tool(annotations={"title": "Create Reel", "readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": True})
async def create_reel(prompt: str = "", image_url: str = "", reel_id: str = "") -> str:
    """
    Generate a short AI video reel from a text prompt.

    This can take a few minutes. If it's still generating when this tool returns,
    the response includes a reel_id — call this tool again with that reel_id
    (and no prompt) to resume checking on the same reel instead of starting a new one.

    Args:
        prompt: Description of the reel to generate e.g. 'A cinematic product reveal for a sports shoe'.
            Required unless reel_id is given.
        image_url: Optional public URL of an image to include in the reel.
        reel_id: Resume checking an in-progress reel from a previous call instead
            of starting a new one.
    """
    try:
        return await _create_reel(prompt, image_url, reel_id)
    except Exception as e:
        return format_tool_error(e)


@mcp.tool(annotations={"title": "Game Development Guide", "readOnlyHint": True, "destructiveHint": False,
                       "idempotentHint": True, "openWorldHint": False})
async def game_development_route(topic: str = "") -> str:
    """
    Guide for building and deploying playable browser games with Xelta's MCP tools
    (generate_image, create_reel).

    Call with no topic first for the overview and pipeline routing. Call again with
    topic='build-game', 'game-design', or 'stylization' to pull a specific reference
    doc as that phase of the pipeline needs it.

    Args:
        topic: Leave empty for the overview. Otherwise one of: 'build-game', 'game-design',
            'stylization'.
    """
    try:
        return await _game_development_route(topic)
    except Exception as e:
        return format_tool_error(e)


if __name__ == "__main__":
    print(f"[xelta] upload listener on {_UPLOAD_ORIGIN}", file=sys.stderr, flush=True)
    from tools.video_prep import ffmpeg_paths
    print(f"[xelta] video conversion: {ffmpeg_paths()[0] if ffmpeg_paths() else 'UNAVAILABLE - ffmpeg not found'}",
          file=sys.stderr, flush=True)
    _start_upload_listener()
    mcp.run(transport="stdio")
