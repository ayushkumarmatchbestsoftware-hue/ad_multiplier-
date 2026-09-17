"""Device-code authorization flow for redirectless MCP agents.

This mirrors the public shape Higgsfield exposes for OpenClaw/Hermes-style
agents while reusing the existing Xelta session and MCP bearer-token storage.
"""

from __future__ import annotations

import html
import secrets
import time
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from oauth.consent import _validate_session_jwt
from oauth.provider import XeltaOAuthProvider, decode_jwt_payload, validate_jwt_structure


async def handle_device_authorize(request: Request, provider: XeltaOAuthProvider) -> Response:
    from config import (
        MCP_PUBLIC_BASE_URL,
        MCP_RESOURCE_URL,
        OAUTH_DEVICE_CODE_TTL_SECONDS,
        OAUTH_DEVICE_POLL_INTERVAL_SECONDS,
    )

    data = await _read_request_data(request)
    client_id = str(data.get("client_id") or "xelta-device-agent")
    scope = str(data.get("scope") or "xelta.read xelta.write offline_access")
    resource = str(data.get("resource") or MCP_RESOURCE_URL)

    device_code = secrets.token_urlsafe(32)
    user_code = secrets.token_urlsafe(16)
    expires_at = int(time.time()) + OAUTH_DEVICE_CODE_TTL_SECONDS

    await provider._storage.save_device_code(
        device_code=device_code,
        user_code=user_code,
        client_id=client_id,
        scope=scope,
        resource=resource,
        interval=OAUTH_DEVICE_POLL_INTERVAL_SECONDS,
        expires_at=expires_at,
    )

    verification_uri = f"{MCP_PUBLIC_BASE_URL.rstrip('/')}/device?{urlencode({'code': user_code})}"
    return JSONResponse(
        {
            "device_code": device_code,
            "user_code": user_code,
            "verification_uri": verification_uri,
            "verification_uri_complete": verification_uri,
            "expires_in": OAUTH_DEVICE_CODE_TTL_SECONDS,
            "interval": OAUTH_DEVICE_POLL_INTERVAL_SECONDS,
        }
    )


async def handle_device_page(request: Request, provider: XeltaOAuthProvider) -> Response:
    if request.method == "GET":
        return await _get_device_page(request, provider)
    return await _post_device_page(request, provider)


async def handle_device_token(request: Request, provider: XeltaOAuthProvider) -> Response:
    from config import OAUTH_ACCESS_TOKEN_TTL_SECONDS, OAUTH_REFRESH_TOKEN_TTL_DAYS

    data = await _read_request_data(request)
    device_code = str(data.get("device_code") or "")
    if not device_code:
        return _oauth_error("invalid_request", "Missing device_code")

    row = await provider._storage.get_device_code(device_code)
    if row is None:
        return _oauth_error("invalid_grant", "Unknown device_code")

    now = int(time.time())
    if int(row["expires_at"]) < now:
        return _oauth_error("expired_token", "Device code expired")

    status = row["status"]
    if status == "pending":
        return _oauth_error("authorization_pending", "User has not approved the device code yet")
    if status == "denied":
        return _oauth_error("access_denied", "User denied access")
    if status == "consumed":
        return _oauth_error("invalid_grant", "Device code already consumed")
    if status != "approved" or not row.get("user_jwt"):
        return _oauth_error("invalid_grant", "Device code is not ready")

    access_token = secrets.token_urlsafe(32)
    refresh_token = secrets.token_urlsafe(32)
    scope = row.get("scope")
    resource = row.get("resource")
    user_jwt = row["user_jwt"]

    await provider._storage.save_access_token(
        token=access_token,
        client_id=row["client_id"],
        scope=scope,
        resource=resource,
        user_jwt=user_jwt,
        expires_at=now + OAUTH_ACCESS_TOKEN_TTL_SECONDS,
    )
    await provider._storage.save_refresh_token(
        token=refresh_token,
        client_id=row["client_id"],
        scope=scope,
        resource=resource,
        user_jwt=user_jwt,
        expires_at=now + OAUTH_REFRESH_TOKEN_TTL_DAYS * 86400,
    )
    await provider._storage.consume_device_code(device_code)

    return JSONResponse(
        {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": OAUTH_ACCESS_TOKEN_TTL_SECONDS,
            "refresh_token": refresh_token,
            "refresh_expires_in": OAUTH_REFRESH_TOKEN_TTL_DAYS * 86400,
            "scope": scope,
        }
    )


async def _get_device_page(request: Request, provider: XeltaOAuthProvider) -> Response:
    from config import ALLOW_MANUAL_JWT_CONSENT, XELTA_AUTH_AUTHORIZE_URL

    user_code = request.query_params.get("code", "")
    if not user_code:
        return _device_error_page("Missing device code.")

    row = await provider._storage.get_device_code_by_user_code(user_code)
    if row is None:
        return _device_error_page("Device authorization request not found.")
    if int(row["expires_at"]) < int(time.time()):
        return _device_error_page("Device authorization request expired.")
    if row["status"] != "pending":
        return _device_done_page(row["status"])

    session_id = request.cookies.get("xelta_mcp_session", "")
    session = await provider._storage.get_login_session(session_id) if session_id else None
    if session and await _validate_session_jwt(session.get("user_jwt", "")):
        return HTMLResponse(_render_device_page(user_code, row, session, dev_mode=False, error=None))

    if session_id:
        await provider._storage.delete_login_session(session_id)

    if ALLOW_MANUAL_JWT_CONSENT:
        return HTMLResponse(_render_device_page(user_code, row, None, dev_mode=True, error=None))

    if XELTA_AUTH_AUTHORIZE_URL:
        return RedirectResponse(
            f"/auth/xelta/start?{urlencode({'device_code': user_code})}",
            status_code=302,
        )

    return _device_error_page("Xelta authentication is not configured.")


async def _post_device_page(request: Request, provider: XeltaOAuthProvider) -> Response:
    from config import ALLOW_MANUAL_JWT_CONSENT

    form = await request.form()
    user_code = str(form.get("code") or "")
    action = str(form.get("action") or "")

    row = await provider._storage.get_device_code_by_user_code(user_code)
    if row is None:
        return _device_error_page("Device authorization request not found.")
    if int(row["expires_at"]) < int(time.time()):
        return _device_error_page("Device authorization request expired.")
    if row["status"] != "pending":
        return _device_done_page(row["status"])

    if action == "deny":
        await provider._storage.deny_device_code(user_code)
        return _device_done_page("denied")

    session_id = request.cookies.get("xelta_mcp_session", "")
    session = await provider._storage.get_login_session(session_id) if session_id else None
    user_jwt = session.get("user_jwt", "") if session else ""

    if not user_jwt and ALLOW_MANUAL_JWT_CONSENT:
        pasted = str(form.get("xelta_jwt") or "").strip()
        if not pasted:
            return HTMLResponse(
                _render_device_page(user_code, row, None, dev_mode=True, error="Paste your Xelta JWT to approve this agent."),
                status_code=422,
            )
        if not validate_jwt_structure(pasted):
            return HTMLResponse(
                _render_device_page(user_code, row, None, dev_mode=True, error="Invalid JWT format."),
                status_code=422,
            )
        user_jwt = pasted

    if not user_jwt:
        return _device_error_page("No Xelta session found. Please sign in and try again.")

    await provider._storage.approve_device_code(user_code, user_jwt)
    return _device_done_page("approved")


async def _read_request_data(request: Request) -> dict:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    try:
        form = await request.form()
        return dict(form)
    except Exception:
        return {}


def _oauth_error(error: str, description: str) -> JSONResponse:
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=400,
    )


def _render_device_page(
    user_code: str,
    row: dict,
    session: dict | None,
    *,
    dev_mode: bool,
    error: str | None,
) -> str:
    payload = decode_jwt_payload(session.get("user_jwt", "")) if session else {}
    email = session.get("email") if session else payload.get("email", "")
    client_name = html.escape(str(row.get("client_id") or "device agent"))
    subtitle = (
        f"<strong>{client_name}</strong> wants to access Xelta"
        + (f" as <strong>{html.escape(str(email))}</strong>" if email else "")
    )
    error_html = f'<div class="error">{html.escape(error)}</div>' if error else ""
    dev_section = ""
    if dev_mode:
        dev_section = """
      <label for="xelta_jwt">Xelta JWT token</label>
      <textarea id="xelta_jwt" name="xelta_jwt" placeholder="eyJhbGciOi..." spellcheck="false"></textarea>
"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Authorize Xelta Agent</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f5f5f7; min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 24px; }}
    .card {{ width: 100%; max-width: 420px; background: #fff; border-radius: 20px; box-shadow: 0 4px 32px rgba(0,0,0,.10); padding: 36px; }}
    .badge {{ width: 54px; height: 54px; border-radius: 15px; background: linear-gradient(135deg, #6c5ce7, #2dd4bf); color: #fff; display: flex; align-items: center; justify-content: center; font-weight: 800; margin: 0 auto 22px; }}
    h1 {{ font-size: 20px; color: #111; margin-bottom: 8px; text-align: center; }}
    .subtitle {{ color: #555; font-size: 14px; line-height: 1.5; margin-bottom: 22px; text-align: center; }}
    .code {{ background: #f7f7fa; border: 1px solid #ececf1; border-radius: 10px; padding: 12px; font-family: Consolas, monospace; font-size: 13px; color: #333; margin-bottom: 18px; word-break: break-all; }}
    ul {{ list-style: none; display: grid; gap: 8px; margin-bottom: 20px; color: #333; font-size: 14px; }}
    li::before {{ content: "\\2713"; color: #16a34a; font-weight: 700; margin-right: 8px; }}
    label {{ display: block; font-size: 12px; font-weight: 700; color: #555; margin-bottom: 6px; }}
    textarea {{ width: 100%; min-height: 78px; border: 1px solid #ddd; border-radius: 10px; padding: 10px; font-family: Consolas, monospace; font-size: 12px; margin-bottom: 16px; }}
    .error {{ background: #fef2f2; border: 1px solid #fecaca; color: #b91c1c; border-radius: 10px; padding: 10px 12px; font-size: 13px; margin-bottom: 14px; }}
    .actions {{ display: flex; gap: 10px; }}
    button {{ flex: 1; border-radius: 10px; padding: 13px; cursor: pointer; font-weight: 700; }}
    .deny {{ border: 1.5px solid #ddd; background: #fff; color: #555; }}
    .allow {{ border: 0; background: #6c5ce7; color: #fff; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="badge">X</div>
    <h1>Authorize Xelta Agent</h1>
    <p class="subtitle">{subtitle}</p>
    <div class="code">Code: {html.escape(user_code)}</div>
    <ul>
      <li>Verify your Xelta identity</li>
      <li>Access your enabled Xelta MCP tools</li>
      <li>Maintain access until tokens expire or are revoked</li>
    </ul>
    {error_html}
    <form method="POST" action="/device" autocomplete="off">
      <input type="hidden" name="code" value="{html.escape(user_code)}" />
      {dev_section}
      <div class="actions">
        <button class="deny" type="submit" name="action" value="deny">Deny</button>
        <button class="allow" type="submit" name="action" value="approve">Allow</button>
      </div>
    </form>
  </div>
</body>
</html>"""


def _device_done_page(status: str) -> HTMLResponse:
    title = "Agent authorized" if status == "approved" else "Authorization finished"
    body = "You can return to the agent." if status == "approved" else f"Status: {html.escape(status)}"
    return HTMLResponse(
        f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>{title}</title><style>body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f5f5f7;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}}.card{{background:#fff;border-radius:18px;padding:34px;box-shadow:0 4px 28px rgba(0,0,0,.1);max-width:380px;text-align:center}}h1{{font-size:20px;margin-bottom:10px}}p{{color:#555}}</style></head>
<body><div class="card"><h1>{title}</h1><p>{body}</p></div></body></html>"""
    )


def _device_error_page(message: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Device Authorization Error</title><style>body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f5f5f7;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}}.card{{background:#fff;border-radius:18px;padding:34px;box-shadow:0 4px 28px rgba(0,0,0,.1);max-width:390px;text-align:center}}h1{{font-size:20px;margin-bottom:10px}}p{{color:#555;line-height:1.6}}</style></head>
<body><div class="card"><h1>Device Authorization Error</h1><p>{html.escape(message)}</p></div></body></html>""",
        status_code=400,
    )
