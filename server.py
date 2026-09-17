import json
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp.server.auth.provider import ProviderTokenVerifier
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

from config import (
    MCP_PUBLIC_BASE_URL,
    MCP_RESOURCE_URL,
    OAUTH_DB_PATH,
    OAUTH_ISSUER,
)
from oauth.consent import handle_consent
from oauth.device_auth import handle_device_authorize, handle_device_page, handle_device_token
from oauth.provider import XeltaOAuthProvider
from oauth.storage_postgres import OAuthStorage
from oauth.xelta_auth import handle_xelta_callback, handle_xelta_logout, handle_xelta_start

from tools.credits import check_balance as _check_balance
from tools.connection_status import xelta_connection_status as _xelta_connection_status
from tools.reel_creator import create_reel as _create_reel
from tools.image_gen import generate_image as _generate_image
from tools.media_upload import (
    VIEW_CSP as UPLOAD_VIEW_CSP,
    VIEW_HTML as UPLOAD_VIEW_HTML,
    VIEW_MIME_TYPE as UPLOAD_VIEW_MIME_TYPE,
    VIEW_URI as UPLOAD_VIEW_URI,
    handle_media_upload,
    require_media_url,
    upload_media as _upload_media,
)
from tools.utils import format_tool_error


# ── ASGI middleware: normalize grant_types on POST /register ─────────────────

class GrantTypeNormalizerMiddleware:
    """
    Injects 'refresh_token' into grant_types on POST /register when it is absent.

    FastMCP's RegistrationHandler requires both 'authorization_code' AND
    'refresh_token' in grant_types (RFC 7591). Several MCP clients (e.g. ChatGPT)
    omit 'refresh_token', so we add it transparently before the handler sees it.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope.get("path") == "/register"
            and scope.get("method") == "POST"
        ):
            # Accumulate the full request body
            chunks: list[bytes] = []
            while True:
                event = await receive()
                chunks.append(event.get("body", b""))
                if not event.get("more_body", False):
                    break

            raw = b"".join(chunks)

            try:
                body = json.loads(raw)
                grant_types = body.get("grant_types", ["authorization_code"])
                if isinstance(grant_types, list) and "refresh_token" not in grant_types:
                    body["grant_types"] = grant_types + ["refresh_token"]
                    raw = json.dumps(body).encode()
                    print(
                        f"[register] grant_types normalized for "
                        f"client={body.get('client_name', 'unknown')}",
                        flush=True,
                    )
            except Exception:
                pass  # pass through; FastMCP will return a descriptive 400

            # Rebuild headers with correct content-length
            headers = [
                (k, v)
                for k, v in scope.get("headers", [])
                if k.lower() != b"content-length"
            ]
            headers.append((b"content-length", str(len(raw)).encode()))
            scope = {**scope, "headers": headers}

            body_delivered = False

            async def patched_receive():
                nonlocal body_delivered
                if not body_delivered:
                    body_delivered = True
                    return {"type": "http.request", "body": raw, "more_body": False}
                # Drain the original stream if the app asks again (unlikely)
                return await receive()

            await self._app(scope, patched_receive, send)
        else:
            await self._app(scope, receive, send)


# ── ASGI middleware: rewrite POST / (and MCP-like GET /) to /mcp ─────────────

class RootToMcpMiddleware:
    """
    Rewrites requests at root path "/" to "/mcp" so Claude can POST to either.

    Triggers on:
      - POST /  (Claude probes root after token exchange)
      - GET /   with Accept: text/event-stream  (SSE probe)

    All other GET / requests pass through to the custom root_handler below.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") == "/":
            method = scope.get("method", "")
            headers = dict(scope.get("headers", []))
            accept = headers.get(b"accept", b"").decode("utf-8", errors="ignore")

            if method == "POST" or "text/event-stream" in accept:
                print(f"[server] root MCP request routed to /mcp method={method}", flush=True)
                scope = dict(scope)
                scope["path"] = "/mcp"
                scope["raw_path"] = b"/mcp"

        await self._app(scope, receive, send)

# ── OAuth storage + provider ──────────────────────────────────────────────────

storage = OAuthStorage(OAUTH_DB_PATH)
provider = XeltaOAuthProvider(storage, MCP_PUBLIC_BASE_URL)


@asynccontextmanager
async def lifespan(app: FastMCP) -> AsyncGenerator[None, None]:
    # Initialize storage (idempotent — safe even if already initialized)
    await storage.initialize()
    print(f"[startup] OAuth storage initialized", flush=True)
    print(f"[startup] DB path: {OAUTH_DB_PATH}", flush=True)
    print(f"[startup] MCP public base URL: {MCP_PUBLIC_BASE_URL}", flush=True)
    print(f"[startup] Resource URL: {MCP_RESOURCE_URL}", flush=True)
    yield
    await storage.close()


# ── FastMCP server ────────────────────────────────────────────────────────────

mcp = FastMCP(
    "xelta",
    host="0.0.0.0",
    port=8000,
    auth_server_provider=provider,
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(OAUTH_ISSUER),
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=["xelta.read", "xelta.write", "offline_access"],
            default_scopes=["xelta.read", "xelta.write", "offline_access"],
        ),
        revocation_options=RevocationOptions(enabled=True),
        # /mcp requires at minimum the read scope
        required_scopes=["xelta.read"],
        # Enables /.well-known/oauth-protected-resource/mcp endpoint
        resource_server_url=AnyHttpUrl(MCP_RESOURCE_URL),
    ),
    lifespan=lifespan,
)


# ── Bot-facing FastMCP server (WhatsApp/Telegram/etc backends, not Claude) ────
#
# Shares the SAME XeltaOAuthProvider/OAuthStorage as `mcp` above via `token_verifier`
# instead of `auth_server_provider` — this deliberately skips create_auth_routes()
# (see FastMCP.streamable_http_app()), so this instance registers NO /authorize,
# /token or /register routes of its own. A user connects once via the existing
# device-code flow on the primary app; the resulting token is looked up in the
# shared storage regardless of which surface it's presented to (this SDK version's
# RequireAuthMiddleware checks token presence/expiry/scope only — it does not
# enforce the stored `resource` as an audience restriction), so one login covers
# both tool lists.
bot_mcp = FastMCP(
    "xelta-bot",
    host="0.0.0.0",
    port=8000,
    token_verifier=ProviderTokenVerifier(provider),
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(OAUTH_ISSUER),
        required_scopes=["xelta.read"],
        resource_server_url=AnyHttpUrl(f"{MCP_PUBLIC_BASE_URL.rstrip('/')}/bot-mcp/mcp"),
    ),
    lifespan=lifespan,
)


# ── Consent page (public — no auth required) ──────────────────────────────────

@mcp.custom_route("/consent", methods=["GET", "POST"])
async def consent_handler(request: Request) -> Response:
    return await handle_consent(request, provider)


# ── Xelta IdP login routes (public — no auth required) ────────────────────────

@mcp.custom_route("/auth/xelta/start", methods=["GET"])
async def xelta_start_handler(request: Request) -> Response:
    return await handle_xelta_start(request, storage)


@mcp.custom_route("/auth/xelta/callback", methods=["GET"])
async def xelta_callback_handler(request: Request) -> Response:
    return await handle_xelta_callback(request, storage)


@mcp.custom_route("/auth/xelta/logout", methods=["GET"])
async def xelta_logout_handler(request: Request) -> Response:
    return await handle_xelta_logout(request, storage)


# ── Health check (public, used by Docker/Cloudflare) ─────────────────────────

@mcp.custom_route("/health", methods=["GET"])
async def health_handler(request: Request) -> Response:
    from starlette.responses import JSONResponse
    
    try:
        health_data = await storage.health_check()
        status_code = 200 if health_data.get("oauth_db_initialized") else 500
        
        return JSONResponse(
            {
                "status": "ok" if status_code == 200 else "error",
                "service": "xelta-mcp",
                **health_data,
            },
            status_code=status_code,
        )
    except Exception as e:
        return JSONResponse(
            {
                "status": "error",
                "service": "xelta-mcp",
                "oauth_db_initialized": False,
                "error": "Health check failed",
            },
            status_code=500,
        )


# ── Root route (public, for probes/debugging) ────────────────────────────────
# NOTE: POST / is handled by RootToMcpMiddleware (rewrites to /mcp).
# GET / returns service info for health probes.

@mcp.custom_route("/", methods=["GET"])
async def root_handler(request: Request) -> Response:
    from starlette.responses import JSONResponse

    return JSONResponse({
        "service": "xelta-mcp",
        "status": "ok",
        "mcp_endpoint": "/mcp",
        "health": "/health",
        "authorization_server": "/.well-known/oauth-authorization-server",
        "protected_resource": "/.well-known/oauth-protected-resource",
        "device_authorization": "/device/authorize",
    })


# ── Protected resource metadata (bare route for Claude discovery) ─────────────

@mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
async def protected_resource_metadata_handler(request: Request) -> Response:
    from starlette.responses import JSONResponse
    
    return JSONResponse({
        "resource": MCP_RESOURCE_URL,
        "authorization_servers": [OAUTH_ISSUER, MCP_PUBLIC_BASE_URL],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["xelta.read", "xelta.write", "offline_access"],
        "xelta_auth_hints": {
            "selection": "client_capability_based",
            "options": [
                {
                    "flow": "authorization_code_pkce",
                    "authorization_server": OAUTH_ISSUER,
                    "potential_clients": ["anthropic", "claude", "claude-ai", "claude-code", "perplexity", "cursor"],
                    "use_when": "Client can complete an OAuth authorization-code redirect using a registered redirect URI.",
                    "requires": ["authorization_endpoint", "token_endpoint", "redirect_uri_receiver", "pkce"],
                },
                {
                    "flow": "device_code",
                    "authorization_server": MCP_PUBLIC_BASE_URL,
                    "device_authorization_endpoint": f"{MCP_PUBLIC_BASE_URL.rstrip('/')}/device/authorize",
                    "token_endpoint": f"{MCP_PUBLIC_BASE_URL.rstrip('/')}/device/token",
                    "potential_clients": ["openclaw", "hermes", "memoclaw"],
                    "use_when": "Client cannot receive authorization-code redirects and should ask the user to complete verification out-of-band.",
                    "requires": ["device_authorization_endpoint", "token_polling"],
                },
            ],
        },
    })


# Device-code OAuth flow for redirectless MCP agents.

@mcp.custom_route("/device/authorize", methods=["POST"])
async def device_authorize_handler(request: Request) -> Response:
    return await handle_device_authorize(request, provider)


@mcp.custom_route("/oauth2/device/code", methods=["POST"])
async def oauth2_device_code_handler(request: Request) -> Response:
    return await handle_device_authorize(request, provider)


@mcp.custom_route("/device", methods=["GET", "POST"])
async def device_page_handler(request: Request) -> Response:
    return await handle_device_page(request, provider)


@mcp.custom_route("/device/token", methods=["POST"])
async def device_token_handler(request: Request) -> Response:
    return await handle_device_token(request, provider)


# ── Media upload (public — authorised by the token upload_media puts in the URL)

@mcp.custom_route("/media/upload", methods=["GET", "POST", "OPTIONS"])
async def media_upload_handler(request: Request) -> Response:
    return await handle_media_upload(request)


# ── MCP tools ─────────────────────────────────────────────────────────────────

@mcp.tool(
    annotations={
        "title": "Xelta Connection Status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def xelta_connection_status() -> str:
    """Check whether the Xelta MCP connection is authenticated and working."""
    try:
        return await _xelta_connection_status()
    except Exception as e:
        return format_tool_error(e)


@mcp.tool(
    annotations={
        "title": "Check Xelta Balance",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def check_balance() -> str:
    """Check your Xelta credit balance."""
    try:
        data = await _check_balance()
        return f"Your Xelta balance: {data.get('data', 0)} credits"
    except Exception as e:
        return format_tool_error(e)


@mcp.tool(
    meta={
        "ui": {"resourceUri": UPLOAD_VIEW_URI},
        "ui/resourceUri": UPLOAD_VIEW_URI,  # legacy hosts
    },
    annotations={
        "title": "Upload Image",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def upload_media() -> CallToolResult:
    """
    Show an upload box in the chat so the user can upload an image (PNG, JPEG or WebP).

    Call this whenever the user wants to use their own image — to edit, restyle or
    upscale it with generate_image (image-to-image), or as the image for create_reel.
    Images attached directly to the chat can't be passed to Xelta tools, so have the
    user upload through this box instead. Once they upload, the image's public URL is
    posted into the conversation; pass that URL as image_url.

    If the box doesn't render (some MCP clients don't show tool UI), the response
    also contains a plain link the user can open in a browser to upload and copy
    the URL back.
    """
    try:
        return await _upload_media()
    except Exception as e:
        return CallToolResult(content=[TextContent(type="text", text=format_tool_error(e))], isError=True)


@mcp.resource(UPLOAD_VIEW_URI, mime_type=UPLOAD_VIEW_MIME_TYPE, meta={"ui": {"csp": UPLOAD_VIEW_CSP}})
def upload_media_view() -> str:
    """Upload box rendered in the chat by upload_media."""
    return UPLOAD_VIEW_HTML


@mcp.tool(
    annotations={
        "title": "Create Reel",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def create_reel(prompt: str = "", image_url: str = "", reel_id: str = "") -> str:
    """
    Generate a short AI video reel from a text prompt.

    This can take a few minutes. If it's still generating when this tool returns,
    the response includes a reel_id — call this tool again with that reel_id
    (and no prompt) to resume checking on the same reel instead of starting a new one.

    Args:
        prompt: Description of the reel to generate e.g. 'A cinematic product reveal for a sports shoe'.
            Required unless reel_id is given.
        image_url: Optional image to include in the reel. Must be a URL posted by
            upload_media (https://media.xelta.ai/...) — if the user wants to use
            their own image, call upload_media first.
        reel_id: Resume checking an in-progress reel from a previous call instead
            of starting a new one.
    """
    try:
        if image_url:
            image_url = require_media_url(image_url)
        return await _create_reel(prompt, image_url, reel_id)
    except Exception as e:
        return format_tool_error(e)


@mcp.tool(
    annotations={
        "title": "Generate Image",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def generate_image(
    prompt: str = "",
    model_id: str = "",
    size: str = "",
    optimize_prompt: bool = True,
    image_url: str = "",
    style_image_url: str = "",
) -> str:
    """
    Generate an image from a text prompt, or edit an existing image (image-to-image).

    This tool runs as a wizard — call it up to 2 times per generation:
    - Step 1: Call with prompt (and image_url if editing), omitting size. If no
      model_id is given it defaults to Gemini 2.5 Flash, which handles both
      text-to-image and image-to-image — no need to ask the user to pick a model
      unless they want a different one. The tool returns that model's real size
      options — present them and ask the user to pick one.
    - Step 2: Call again with the same arguments plus size. The image is generated
      and its URL returned.

    Skip the size step only if the user already gave a size. Some models (the
    upscalers, style transfer) have no size option and generate on the first call.

    Image-to-image: to edit, restyle or upscale a user's own image, first call
    upload_media so they can upload it, then pass the https://media.xelta.ai/...
    URL it posts back as image_url. Images attached directly to the chat cannot
    be used — only URLs from upload_media are accepted.

    Args:
        prompt: Description of the image to generate, or of the edit to apply.
        model_id: Model ID (leave empty to default to Gemini 2.5 Flash, 59 credits).
            Call with an unknown ID to get the full catalogue with credit costs.
        size: The size/aspect-ratio value for the chosen model. Leave empty to be
            shown that model's valid options — they differ per model, so don't guess.
        optimize_prompt: Whether to apply prompt enhancement where supported (default: true).
        image_url: Image to edit/restyle/upscale. Must be a URL posted by
            upload_media (https://media.xelta.ai/...).
        style_image_url: Second image, used only by style-transfer models that need
            both a base image and a separate style reference. Also from upload_media.
    """
    try:
        if image_url:
            image_url = require_media_url(image_url)
        if style_image_url:
            style_image_url = require_media_url(style_image_url)
        return await _generate_image(
            prompt, model_id, size, optimize_prompt, image_url, style_image_url
        )
    except Exception as e:
        return format_tool_error(e)


# ── Bot-facing tools (registered on bot_mcp, not mcp) ─────────────────────────
# Same underlying tools/*.py implementations as above — only the thin MCP-tool
# wrapper is duplicated, matching this project's existing convention between
# server.py and server_local.py.

@bot_mcp.tool(
    name="check_balance",
    annotations={
        "title": "Check Xelta Balance",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def bot_check_balance() -> str:
    """Check your Xelta credit balance."""
    try:
        data = await _check_balance()
        return f"Your Xelta balance: {data.get('data', 0)} credits"
    except Exception as e:
        return format_tool_error(e)


@bot_mcp.tool(
    name="generate_image",
    annotations={
        "title": "Generate Image",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def bot_generate_image(
    prompt: str = "",
    model_id: str = "",
    size: str = "",
    optimize_prompt: bool = True,
    image_url: str = "",
    style_image_url: str = "",
) -> str:
    """
    Generate an image from a text prompt, or edit an existing image (image-to-image).

    This tool runs as a wizard — call it up to 2 times per generation:
    - Step 1: Call with prompt (and image_url if editing), omitting size. If no
      model_id is given it defaults to Gemini 2.5 Flash, which handles both
      text-to-image and image-to-image. The tool returns that model's real size options.
    - Step 2: Call again with the same arguments plus size. The image is generated
      and its URL returned.

    Some models (the upscalers, style transfer) have no size option and generate
    on the first call.

    Args:
        prompt: Description of the image to generate, or of the edit to apply.
        model_id: Model ID (leave empty to default to Gemini 2.5 Flash, 59 credits).
        size: The size/aspect-ratio value for the chosen model. Leave empty to be
            shown that model's valid options — they differ per model, so don't guess.
        optimize_prompt: Whether to apply prompt enhancement where supported (default: true).
        image_url: Image to edit/restyle/upscale, uploaded to https://media.xelta.ai/.
        style_image_url: Second image, for style-transfer models that need both a
            base image and a separate style reference.
    """
    try:
        if image_url:
            image_url = require_media_url(image_url)
        if style_image_url:
            style_image_url = require_media_url(style_image_url)
        return await _generate_image(
            prompt, model_id, size, optimize_prompt, image_url, style_image_url
        )
    except Exception as e:
        return format_tool_error(e)


@bot_mcp.tool(
    name="create_reel",
    annotations={
        "title": "Create Reel",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def bot_create_reel(prompt: str = "", image_url: str = "", reel_id: str = "") -> str:
    """
    Generate a short AI video reel from a text prompt.

    This can take a few minutes. If it's still generating when this tool returns,
    the response includes a reel_id — call this tool again with that reel_id
    (and no prompt) to resume checking on the same reel instead of starting a new one.

    Args:
        prompt: Description of the reel to generate. Required unless reel_id is given.
        image_url: Optional image to include in the reel, uploaded to https://media.xelta.ai/.
        reel_id: Resume checking an in-progress reel from a previous call.
    """
    try:
        if image_url:
            image_url = require_media_url(image_url)
        return await _create_reel(prompt, image_url, reel_id)
    except Exception as e:
        return format_tool_error(e)


def _build_asgi_app() -> ASGIApp:
    # streamable_http_app() must be called first on each server — it lazily
    # creates that server's `session_manager`, which the combined lifespan
    # below needs to reference.
    mcp_app = mcp.streamable_http_app()
    bot_app = bot_mcp.streamable_http_app()

    # Mounting FastMCP's streamable_http_app() under another Starlette app
    # (below) does NOT forward ASGI lifespan events into it — Starlette only
    # sends "lifespan" scope to the top-level app, never into Mount'ed
    # sub-apps. Each streamable_http_app() relies on its own lifespan to
    # enter `session_manager.run()`, which starts the anyio task group the
    # streamable-HTTP transport needs; without it, every request to /mcp
    # fails with "RuntimeError: Task group is not initialized." So the
    # combined app must explicitly enter both session managers itself.
    @asynccontextmanager
    async def combined_lifespan(app: Starlette) -> AsyncGenerator[None, None]:
        from contextlib import AsyncExitStack

        async with AsyncExitStack() as stack:
            await stack.enter_async_context(lifespan(mcp))
            await stack.enter_async_context(mcp.session_manager.run())
            await stack.enter_async_context(bot_mcp.session_manager.run())
            yield

    combined = Starlette(
        routes=[
            Mount("/bot-mcp", app=bot_app),
            Mount("/", app=mcp_app),
        ],
        lifespan=combined_lifespan,
    )
    return GrantTypeNormalizerMiddleware(RootToMcpMiddleware(combined))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(_build_asgi_app(), host="0.0.0.0", port=8000)
else:
    # Module-level `app` for uvicorn server:app invocation
    app = _build_asgi_app()
