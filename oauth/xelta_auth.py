"""
Xelta IdP login routes — /auth/xelta/start, /auth/xelta/callback, /auth/xelta/logout.

These sit between the Claude /authorize request and the Xelta consent page.
They perform a server-side OAuth exchange with the Xelta backend so the MCP
server can authenticate users without asking them to paste a JWT.

Required env vars (see .env.example):
  XELTA_AUTH_AUTHORIZE_URL
  XELTA_AUTH_TOKEN_URL
  XELTA_AUTH_USERINFO_URL   (optional — user info decoded from JWT if absent)
  XELTA_AUTH_CLIENT_ID
  XELTA_AUTH_CLIENT_SECRET
  XELTA_AUTH_REDIRECT_URI
  XELTA_AUTH_SCOPES

What Xelta's backend provides:
  1. Authorization endpoint  https://api.xelta.ai/api/auth/oauth/authorize
  2. Token endpoint           https://api.xelta.ai/api/auth/oauth/token
     Response must include access_token (the Xelta JWT used by MCP tools).
  3. Userinfo endpoint        https://api.xelta.ai/api/auth/oauth/userinfo
     Response must include sub/id and email.
  4. The redirect URI https://mcp.xelta.ai/auth/xelta/callback is registered
     as MCP_REDIRECT_URI in the Xelta backend .env.
"""

from __future__ import annotations

import secrets
from urllib.parse import urlencode

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from oauth.provider import decode_jwt_payload
from oauth.storage import OAuthStorage


# ── public entry points ───────────────────────────────────────────────────────

async def handle_xelta_start(request: Request, storage: OAuthStorage) -> Response:
    """GET /auth/xelta/start?pending_id=<id>

    Begins the Xelta login flow.  Stores the Claude-side pending_id against
    a fresh Xelta OAuth state, then redirects to Xelta's authorization URL.
    """
    from config import (
        ALLOW_MANUAL_JWT_CONSENT,
        XELTA_AUTH_AUTHORIZE_URL,
        XELTA_AUTH_CLIENT_ID,
        XELTA_AUTH_REDIRECT_URI,
        XELTA_AUTH_SCOPES,
    )

    pending_id = request.query_params.get("pending_id", "")
    device_code = request.query_params.get("device_code", "")

    if not pending_id and not device_code:
        return _auth_error(
            "Missing pending_id or device_code. Please return to your agent and try connecting again."
        )

    if not XELTA_AUTH_AUTHORIZE_URL:
        if ALLOW_MANUAL_JWT_CONSENT:
            # Dev shortcut: skip Xelta login, go straight to the approval UI.
            if device_code:
                return RedirectResponse(f"/device?{urlencode({'code': device_code})}", status_code=302)
            return RedirectResponse(f"/consent?{urlencode({'pending_id': pending_id})}", status_code=302)
        return _auth_error(
            "Xelta authentication endpoints are not configured. "
            "The server administrator must set XELTA_AUTH_AUTHORIZE_URL "
            "and related environment variables.",
            title="Authentication not configured",
        )

    # Generate a Xelta-specific OAuth state (separate from the Claude OAuth state)
    xelta_state = secrets.token_urlsafe(24)

    await storage.save_login_state(
        state=xelta_state,
        pending_id=pending_id or f"device:{device_code}",
        expires_at=_now() + 1800,  # 30 minutes for the user to complete login
    )

    params = {
        "response_type": "code",
        "client_id": XELTA_AUTH_CLIENT_ID,
        "redirect_uri": XELTA_AUTH_REDIRECT_URI,
        "scope": XELTA_AUTH_SCOPES,
        "state": xelta_state,
    }
    
    redirect_url = f"{XELTA_AUTH_AUTHORIZE_URL}?{urlencode(params)}"
    
    # Safe debug logging
    from urllib.parse import urlparse
    parsed = urlparse(XELTA_AUTH_AUTHORIZE_URL)
    print(f"[oauth] xelta auth start redirect host={parsed.netloc} path={parsed.path} "
          f"pending_id={(pending_id or device_code)[:8]}... client_id={XELTA_AUTH_CLIENT_ID} "
          f"redirect_uri={XELTA_AUTH_REDIRECT_URI} scopes={XELTA_AUTH_SCOPES} "
          f"state_len={len(xelta_state)}", flush=True)
    
    return RedirectResponse(redirect_url, status_code=302)


async def handle_xelta_callback(request: Request, storage: OAuthStorage) -> Response:
    """GET /auth/xelta/callback?code=<code>&state=<state>

    Handles the redirect back from Xelta's OAuth server.
    Exchanges the code for a token, fetches user info, creates a session,
    sets the session cookie, and redirects to the MCP consent page.
    """
    from config import (
        ALLOW_MANUAL_JWT_CONSENT,
        MCP_PUBLIC_BASE_URL,
        XELTA_AUTH_CLIENT_ID,
        XELTA_AUTH_CLIENT_SECRET,
        XELTA_AUTH_REDIRECT_URI,
        XELTA_AUTH_TOKEN_URL,
        XELTA_AUTH_USERINFO_URL,
    )

    error = request.query_params.get("error")
    code = request.query_params.get("code")
    state = request.query_params.get("state", "")

    if error:
        desc = request.query_params.get("error_description", error)
        return _auth_error(f"Xelta login was not completed: {desc}. Please try again.")

    if not code or not state:
        return _auth_error("Invalid callback: missing code or state. Please try again.")

    # Validate state and retrieve pending_id
    state_row = await storage.get_login_state(state)
    if state_row is None:
        return _auth_error(
            "Login session expired or invalid state. Please return to Claude and try again."
        )

    pending_id = state_row["pending_id"]
    await storage.delete_login_state(state)

    # Exchange code for Xelta access token
    if not XELTA_AUTH_TOKEN_URL:
        return _auth_error("Token endpoint not configured. Contact the administrator.")

    try:
        token_data = await _exchange_code(
            code=code,
            token_url=XELTA_AUTH_TOKEN_URL,
            client_id=XELTA_AUTH_CLIENT_ID,
            client_secret=XELTA_AUTH_CLIENT_SECRET,
            redirect_uri=XELTA_AUTH_REDIRECT_URI,
        )
    except httpx.HTTPStatusError as exc:
        # Log response body to diagnose 403/401 errors (no secrets in body expected)
        try:
            body_text = exc.response.text[:500]
        except Exception:
            body_text = "<unreadable>"
        print(
            f"[oauth] token exchange failed status={exc.response.status_code} "
            f"url={XELTA_AUTH_TOKEN_URL} client_id={XELTA_AUTH_CLIENT_ID} "
            f"redirect_uri={XELTA_AUTH_REDIRECT_URI} body={body_text!r}",
            flush=True,
        )
        return _auth_error(
            f"Token exchange failed (HTTP {exc.response.status_code}). Please try again."
        )
    except Exception as exc:
        print(f"[oauth] token exchange exception: {type(exc).__name__}: {exc}", flush=True)
        return _auth_error("Token exchange failed. Please try again.")

    # The Xelta access_token IS the JWT that MCP tools use for downstream calls.
    user_jwt = token_data.get("access_token", "")
    if not user_jwt:
        return _auth_error("No access token received from Xelta. Please try again.")

    # Try userinfo endpoint first; fall back to decoding the JWT payload.
    user_id, email = "", ""
    if XELTA_AUTH_USERINFO_URL:
        try:
            info = await _fetch_userinfo(user_jwt, XELTA_AUTH_USERINFO_URL)
            user_id = str(info.get("id") or info.get("sub") or "")
            email = str(info.get("email") or "")
        except Exception:
            pass  # non-fatal — proceed without userinfo

    if not user_id or not email:
        payload = decode_jwt_payload(user_jwt)
        user_id = user_id or str(payload.get("id") or payload.get("sub") or "")
        email = email or str(payload.get("email") or "")

    # Create server-side session
    session_id = secrets.token_urlsafe(32)
    session_ttl = 86400 * 30  # 30 days

    await storage.save_login_session(
        session_id=session_id,
        user_id=user_id,
        email=email,
        user_jwt=user_jwt,
        expires_at=_now() + session_ttl,
    )

    # Redirect to consent page and set the session cookie
    if str(pending_id).startswith("device:"):
        device_code = str(pending_id).split(":", 1)[1]
        redirect_target = f"/device?{urlencode({'code': device_code})}"
    else:
        redirect_target = f"/consent?{urlencode({'pending_id': pending_id})}"

    response = RedirectResponse(redirect_target, status_code=302)
    is_secure = MCP_PUBLIC_BASE_URL.startswith("https://")
    response.set_cookie(
        key="xelta_mcp_session",
        value=session_id,
        httponly=True,
        secure=is_secure,
        samesite="lax",
        max_age=session_ttl,
        path="/",
    )
    return response


async def handle_xelta_logout(request: Request, storage: OAuthStorage) -> Response:
    """GET /auth/xelta/logout?pending_id=<id>&next=<url>

    Clears the Xelta MCP session cookie and DB record.
    - If pending_id is provided, redirects to /auth/xelta/start?pending_id=<id>
      (used by "Switch account" on the consent page).
    - Otherwise redirects to `next` (same-origin only) or a logged-out page.
    """
    session_id = request.cookies.get("xelta_mcp_session", "")
    if session_id:
        await storage.delete_login_session(session_id)
        print(f"[oauth] logout: session cleared session_id_len={len(session_id)}", flush=True)

    pending_id = request.query_params.get("pending_id", "")
    if pending_id:
        redirect_to = f"/auth/xelta/start?{urlencode({'pending_id': pending_id})}"
    else:
        next_url = request.query_params.get("next", "")
        # Restrict next to same-origin paths only
        if next_url.startswith("/"):
            redirect_to = next_url
        else:
            redirect_to = "/"

    response = RedirectResponse(redirect_to, status_code=302)
    response.delete_cookie("xelta_mcp_session", path="/")
    return response


# ── HTTP helpers ──────────────────────────────────────────────────────────────

async def _exchange_code(
    *,
    code: str,
    token_url: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            token_url,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Origin": "https://mcp.xelta.ai",
                "Referer": "https://mcp.xelta.ai/",
            },
        )
        r.raise_for_status()
        return r.json()


async def _fetch_userinfo(access_token: str, userinfo_url: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            userinfo_url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Origin": "https://mcp.xelta.ai",
                "Referer": "https://mcp.xelta.ai/",
            },
        )
        r.raise_for_status()
        return r.json()


# ── Error page ────────────────────────────────────────────────────────────────

def _auth_error(message: str, title: str = "Login Error") -> HTMLResponse:
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{title}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #f5f5f7;
      display: flex; align-items: center; justify-content: center;
      min-height: 100vh; padding: 24px;
    }}
    .card {{
      background: #fff; border-radius: 16px; padding: 40px 36px;
      box-shadow: 0 4px 24px rgba(0,0,0,.08); max-width: 420px; text-align: center;
    }}
    .icon {{ font-size: 40px; margin-bottom: 16px; }}
    h1 {{ color: #111; font-size: 20px; margin-bottom: 12px; }}
    p {{ color: #555; font-size: 14px; line-height: 1.7; }}
    a {{ color: #6c5ce7; text-decoration: none; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">&#9888;&#65039;</div>
    <h1>{title}</h1>
    <p>{message}</p>
  </div>
</body>
</html>"""
    return HTMLResponse(html, status_code=400)


def _now() -> int:
    import time
    return int(time.time())
