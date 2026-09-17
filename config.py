import base64
import json
import os
from pathlib import Path

from dotenv import load_dotenv

# Load from the working directory first (existing behaviour), then from the
# .env sitting next to this file. An MCP client launching server_local.py over
# stdio sets its own working directory, so the cwd lookup alone can silently
# miss the project's .env and leave every tool unauthenticated. Neither call
# overrides variables already present in the environment, so Docker's env_file
# still wins in the container.
load_dotenv()
load_dotenv(Path(__file__).resolve().with_name(".env"))

# ── Xelta service config ──────────────────────────────────────────────────────

JWT_TOKEN = os.getenv("XELTA_JWT_TOKEN", "")

CREDITS_BASE_URL  = os.getenv("XELTA_CREDITS_BASE_URL",  "https://pmt.xelta.ai")

# Production — uncomment these and comment out the staging block below when
# pushing to production.
# MIXBOARD_BASE_URL   = os.getenv("XELTA_MIXBOARD_BASE_URL",   "https://mixboard.xelta.ai")
# STREETAD_BASE_URL   = os.getenv("XELTA_STREETAD_BASE_URL",   "https://grf.xelta.ai")
# WEBBUILDER_BASE_URL = os.getenv("XELTA_WEBBUILDER_BASE_URL", "https://webgen.xelta.ai")
# REEL_BASE_URL       = os.getenv("XELTA_REEL_BASE_URL",       "https://reel.xelta.ai")

# Staging (active)
MIXBOARD_BASE_URL   = os.getenv("XELTA_MIXBOARD_BASE_URL",   "https://mixboard.xelta.ai")
STREETAD_BASE_URL   = os.getenv("XELTA_STREETAD_BASE_URL",   "https://grf.xelta.ai")
WEBBUILDER_BASE_URL = os.getenv("XELTA_WEBBUILDER_BASE_URL", "https://webdesign.stg.xelta.ai")
REEL_BASE_URL       = os.getenv("XELTA_REEL_BASE_URL",       "https://reel.xelta.ai")

IMAGE_GEN_BASE_URL = os.getenv("XELTA_IMAGE_GEN_BASE_URL", "https://vygxif9kfe.execute-api.ap-south-1.amazonaws.com")

MUSIC_BASE_URL = os.getenv("XELTA_MUSIC_BASE_URL", "https://music.xelta.ai")

# Real chat/assistant service (xelta_chat) — a LangGraph react-agent (GPT-4o-mini)
# wrapped in FastAPI. Uses the SAME Xelta platform JWT as every other tool
# (bearer_headers()), unlike the earlier bot.xelta.ai integration this replaced,
# which used a separate project-scoped session-token auth. Stateless: no
# server-side memory, no admin/project bootstrap needed — the caller passes
# prior turns via `context` on each call.
# Production URL (chatbotus.xelta.ai) now authenticates against prod's own DB,
# replacing the earlier staging deployment (xeltachatbot.stg2.xelta.ai), which
# checked prod-issued JWTs against a staging DB and rejected them.
CHAT_BASE_URL = os.getenv("XELTA_CHAT_BASE_URL", "https://chatbotus.xelta.ai")

# ── OAuth server config ───────────────────────────────────────────────────────

MCP_PUBLIC_BASE_URL = os.getenv("MCP_PUBLIC_BASE_URL", "https://mcp.xelta.ai")
MCP_RESOURCE_URL    = os.getenv("MCP_RESOURCE_URL",    "https://mcp.xelta.ai/mcp")
OAUTH_ISSUER        = os.getenv("OAUTH_ISSUER",        "https://mcp.xelta.ai")
OAUTH_DB_PATH       = os.getenv("OAUTH_DB_PATH",       "oauth.db")

OAUTH_ACCESS_TOKEN_TTL_SECONDS = int(os.getenv("OAUTH_ACCESS_TOKEN_TTL_SECONDS", "3600"))
OAUTH_AUTH_CODE_TTL_SECONDS    = int(os.getenv("OAUTH_AUTH_CODE_TTL_SECONDS",    "300"))
OAUTH_REFRESH_TOKEN_TTL_DAYS   = int(os.getenv("OAUTH_REFRESH_TOKEN_TTL_DAYS",   "30"))
OAUTH_DEVICE_CODE_TTL_SECONDS  = int(os.getenv("OAUTH_DEVICE_CODE_TTL_SECONDS",  "900"))
OAUTH_DEVICE_POLL_INTERVAL_SECONDS = int(os.getenv("OAUTH_DEVICE_POLL_INTERVAL_SECONDS", "3"))

CONSENT_SESSION_SECRET = os.getenv("CONSENT_SESSION_SECRET", "change-me-in-production")

# ── Media uploads (Cloudflare R2) ─────────────────────────────────────────────
# Claude can't hand a user's attached image to an MCP tool, so upload_media
# shows an upload box that sends the file to /media/upload; the server stores
# it in R2 and the public URL is what tools like create_reel receive.

R2_ACCOUNT_ID        = os.getenv("R2_ACCOUNT_ID",        "")
R2_ACCESS_KEY_ID     = os.getenv("R2_ACCESS_KEY_ID",     "")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET_NAME       = os.getenv("R2_BUCKET_NAME",       "xelta-ai")
R2_PUBLIC_BASE_URL   = os.getenv("R2_PUBLIC_BASE_URL",   "https://media.xelta.ai")
# Existing folder in the bucket — uploads go to <prefix>/u/<user_id>/ref/.
R2_UPLOAD_PREFIX     = os.getenv("R2_UPLOAD_PREFIX",     "ad-multiplier")

MEDIA_UPLOAD_MAX_MB            = int(os.getenv("MEDIA_UPLOAD_MAX_MB", "20"))
# Hosted server (server.py) upload flow: video clips are checked against these at
# upload, because that is the one point where the bytes are in hand.
MEDIA_VIDEO_MAX_MB             = int(os.getenv("MEDIA_VIDEO_MAX_MB", "200"))
MEDIA_VIDEO_MAX_SECONDS        = float(os.getenv("MEDIA_VIDEO_MAX_SECONDS", "10"))
# Local server intake (server_local.py). Any format, resolution and frame rate is
# accepted: tools/video_prep converts each clip to what Kling O3 takes, trimming
# to its maximum length, so these only bound what's worth uploading.
MEDIA_INTAKE_VIDEO_MAX_MB      = int(os.getenv("MEDIA_INTAKE_VIDEO_MAX_MB", "500"))
MEDIA_INTAKE_VIDEO_MAX_SECONDS = float(os.getenv("MEDIA_INTAKE_VIDEO_MAX_SECONDS", "120"))
MEDIA_UPLOAD_TOKEN_TTL_SECONDS = int(os.getenv("MEDIA_UPLOAD_TOKEN_TTL_SECONDS", "1800"))
MEDIA_UPLOAD_SECRET            = os.getenv("MEDIA_UPLOAD_SECRET", "") or CONSENT_SESSION_SECRET

# ── Xelta IdP (OAuth 2.0 / OIDC endpoints for user login) ────────────────────
# These are the Xelta-side endpoints used to authenticate users before showing
# the MCP consent page. Leave blank until Xelta provides them.

XELTA_AUTH_AUTHORIZE_URL = os.getenv("XELTA_AUTH_AUTHORIZE_URL", "")
XELTA_AUTH_TOKEN_URL     = os.getenv("XELTA_AUTH_TOKEN_URL",     "")
XELTA_AUTH_USERINFO_URL  = os.getenv("XELTA_AUTH_USERINFO_URL",  "")
XELTA_AUTH_CLIENT_ID     = os.getenv("XELTA_AUTH_CLIENT_ID",     "")
XELTA_AUTH_CLIENT_SECRET = os.getenv("XELTA_AUTH_CLIENT_SECRET", "")
XELTA_AUTH_REDIRECT_URI  = os.getenv(
    "XELTA_AUTH_REDIRECT_URI", "https://mcp.xelta.ai/auth/xelta/callback"
)
XELTA_AUTH_SCOPES = os.getenv("XELTA_AUTH_SCOPES", "openid email profile")

# ── Development flag ──────────────────────────────────────────────────────────
# When true, the consent page shows a "paste JWT" field so developers can test
# without a live Xelta IdP. MUST be false in production.
ALLOW_MANUAL_JWT_CONSENT = os.getenv("ALLOW_MANUAL_JWT_CONSENT", "false").lower() == "true"


# ── JWT helpers ───────────────────────────────────────────────────────────────

def _decode_jwt_payload(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        return json.loads(base64.b64decode(payload))
    except Exception:
        return {}


USER_ID = _decode_jwt_payload(JWT_TOKEN).get("id", "") if JWT_TOKEN else ""


# Set True by server_local.py only. The stored browser sign-in belongs to
# whoever is at this machine, so the hosted multi-user server must never fall
# back to it — there, a request without OAuth has no identity at all.
LOCAL_SESSION_AUTH = False

_NO_JWT = (
    "No Xelta sign-in available. Run xelta_login to sign in (local server), "
    "connect via Claude OAuth (hosted server), or set XELTA_JWT_TOKEN."
)


def _get_active_jwt() -> str:
    """
    Return the Xelta JWT for the current request.

    Priority:
      1. JWT stored on the OAuth access token for the active MCP session.
      2. Local server only: the browser sign-in saved by xelta_login.
      3. XELTA_JWT_TOKEN env var (dev fallback).
    """
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token
        token = get_access_token()
        if token is not None and hasattr(token, "user_jwt") and token.user_jwt:
            return token.user_jwt
    except Exception:
        pass
    if LOCAL_SESSION_AUTH:
        try:
            from tools.xelta_session import session_token
            stored = session_token()
            if stored:
                return stored
        except Exception:
            pass
    return JWT_TOKEN


def auth_headers() -> dict:
    jwt = _get_active_jwt()
    if not jwt:
        raise ValueError(_NO_JWT)
    return {"Authorization": f"Bearer {jwt}"}


def cookie_headers() -> dict:
    jwt = _get_active_jwt()
    if not jwt:
        raise ValueError(_NO_JWT)
    return {"Cookie": f"token={jwt}"}
