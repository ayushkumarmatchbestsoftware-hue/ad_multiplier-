"""
Consent page for the Xelta MCP OAuth flow.

GET  /consent?pending_id=<id>
  - If a valid Xelta session cookie exists AND the stored JWT passes userinfo
    validation → show Higgsfield-style consent page.
  - If session is stale/expired/invalid → delete it, redirect to /auth/xelta/start.
  - If no session and Xelta IdP is configured → redirect to /auth/xelta/start.
  - If no session and ALLOW_MANUAL_JWT_CONSENT=true → show dev-mode JWT paste form.
  - Otherwise → show a configuration error page.

POST /consent
  - action=allow → read user_jwt from server-side session (or pasted JWT in dev mode)
                   → create OAuth auth code → redirect to Claude callback.
  - action=deny  → redirect to Claude callback with error=access_denied.
"""

from __future__ import annotations

import secrets
import time
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from oauth.provider import XeltaOAuthProvider, validate_jwt_structure


async def _validate_session_jwt(user_jwt: str) -> bool:
    """
    Verify the stored Xelta JWT is still accepted by the userinfo endpoint.
    Returns True if valid, False if expired/revoked/unreachable.
    Non-fatal: network errors return False so the user is sent to re-login.
    """
    from config import ALLOW_MANUAL_JWT_CONSENT, XELTA_AUTH_USERINFO_URL

    # In dev mode, skip remote validation — trust the JWT structure only
    if ALLOW_MANUAL_JWT_CONSENT:
        return validate_jwt_structure(user_jwt)

    if not XELTA_AUTH_USERINFO_URL or not user_jwt:
        return validate_jwt_structure(user_jwt)

    try:
        from oauth.xelta_auth import _fetch_userinfo
        await _fetch_userinfo(user_jwt, XELTA_AUTH_USERINFO_URL)
        return True
    except Exception:
        return False


# ── public dispatcher ─────────────────────────────────────────────────────────

async def handle_consent(request: Request, provider: XeltaOAuthProvider) -> Response:
    if request.method == "GET":
        return await _get_consent(request, provider)
    return await _post_consent(request, provider)


# ── GET /consent ──────────────────────────────────────────────────────────────

async def _get_consent(request: Request, provider: XeltaOAuthProvider) -> Response:
    from config import ALLOW_MANUAL_JWT_CONSENT, XELTA_AUTH_AUTHORIZE_URL

    pending_id = request.query_params.get("pending_id", "")
    if not pending_id:
        return _error_page("Missing pending_id parameter.")

    entry = provider.peek_pending(pending_id)
    if entry is None:
        return _error_page(
            "Authorization request not found or expired. "
            "Please return to Claude and try connecting again."
        )

    # ── Session validation ────────────────────────────────────────────────────
    session_id = request.cookies.get("xelta_mcp_session", "")
    session = await provider._storage.get_login_session(session_id) if session_id else None

    if session:
        user_jwt = session.get("user_jwt", "")
        print(f"[oauth] consent session found user_id={session.get('user_id', '')[:8]}...", flush=True)

        # Validate the stored JWT is still accepted by Xelta backend
        jwt_valid = await _validate_session_jwt(user_jwt)

        if not jwt_valid:
            # Stale session — delete it and force re-login
            print(f"[oauth] consent session userinfo validation failed — clearing session, redirecting to Xelta login", flush=True)
            await provider._storage.delete_login_session(session_id)

            start_url = f"/auth/xelta/start?{urlencode({'pending_id': pending_id})}"
            response = RedirectResponse(start_url, status_code=302)
            response.delete_cookie("xelta_mcp_session", path="/")
            return response

        # Valid session — show consent
        print(f"[oauth] consent session valid, showing consent page", flush=True)
        return HTMLResponse(
            _render_consent(
                pending_id=pending_id,
                entry=entry,
                email=session.get("email") or "",
                user_id=session.get("user_id") or "",
                dev_mode=False,
                error=None,
            )
        )

    # ── No session ────────────────────────────────────────────────────────────
    print(f"[oauth] consent no session found, pending_id={pending_id[:8]}...", flush=True)

    if ALLOW_MANUAL_JWT_CONSENT:
        return HTMLResponse(
            _render_consent(
                pending_id=pending_id,
                entry=entry,
                email="",
                user_id="",
                dev_mode=True,
                error=None,
            )
        )

    if XELTA_AUTH_AUTHORIZE_URL:
        print(f"[oauth] consent redirecting to Xelta login", flush=True)
        return RedirectResponse(
            f"/auth/xelta/start?{urlencode({'pending_id': pending_id})}",
            status_code=302,
        )

    return _error_page(
        "Xelta authentication is not configured. "
        "The administrator must set XELTA_AUTH_AUTHORIZE_URL, "
        "or enable ALLOW_MANUAL_JWT_CONSENT for local development."
    )


# ── POST /consent ─────────────────────────────────────────────────────────────

async def _post_consent(request: Request, provider: XeltaOAuthProvider) -> Response:
    from config import ALLOW_MANUAL_JWT_CONSENT, OAUTH_AUTH_CODE_TTL_SECONDS

    form = await request.form()
    pending_id = str(form.get("pending_id", ""))
    action = str(form.get("action", ""))

    # Pop the entry (single-use)
    entry = provider.pop_pending(pending_id)
    if entry is None:
        return _error_page(
            "Authorization session expired. Please return to Claude and try again."
        )

    redirect_uri = str(entry.params.redirect_uri)
    state = entry.params.state

    def _deny_redirect() -> RedirectResponse:
        qs = {"error": "access_denied", "error_description": "User denied access"}
        if state:
            qs["state"] = state
        return RedirectResponse(f"{redirect_uri}?{urlencode(qs)}", status_code=302)

    if action == "deny":
        return _deny_redirect()

    # ── Resolve user JWT ─────────────────────────────────────────────────────

    session_id = request.cookies.get("xelta_mcp_session", "")
    session = await provider._storage.get_login_session(session_id) if session_id else None

    user_jwt: str | None = None

    if session:
        user_jwt = session["user_jwt"]
    elif ALLOW_MANUAL_JWT_CONSENT:
        # Dev-mode: read from form
        pasted = str(form.get("xelta_jwt", "")).strip()
        if not pasted:
            provider._pending[pending_id] = entry  # put back
            return HTMLResponse(
                _render_consent(
                    pending_id=pending_id,
                    entry=entry,
                    email="",
                    user_id="",
                    dev_mode=True,
                    error="Please paste your Xelta JWT to continue.",
                ),
                status_code=422,
            )
        if not validate_jwt_structure(pasted):
            provider._pending[pending_id] = entry
            return HTMLResponse(
                _render_consent(
                    pending_id=pending_id,
                    entry=entry,
                    email="",
                    user_id="",
                    dev_mode=True,
                    error=(
                        "Invalid token — expected three base64 segments separated by dots. "
                        "Copy the full Bearer value from DevTools."
                    ),
                ),
                status_code=422,
            )
        user_jwt = pasted

    if not user_jwt:
        return _error_page(
            "No Xelta session found. Please return to Claude and try again."
        )

    # ── Create authorization code ────────────────────────────────────────────

    scopes = entry.params.scopes or []
    auth_code = secrets.token_urlsafe(32)

    await provider._storage.save_auth_code(
        code=auth_code,
        client_id=entry.client_id,
        redirect_uri=redirect_uri,
        redirect_uri_provided_explicitly=entry.params.redirect_uri_provided_explicitly,
        scope=" ".join(scopes) if scopes else None,
        resource=entry.params.resource,
        code_challenge=entry.params.code_challenge,
        code_challenge_method="S256",
        user_jwt=user_jwt,
        expires_at=time.time() + OAUTH_AUTH_CODE_TTL_SECONDS,
    )

    qs: dict[str, str] = {"code": auth_code}
    if state:
        qs["state"] = state
    return RedirectResponse(f"{redirect_uri}?{urlencode(qs)}", status_code=302)


# ── HTML rendering ────────────────────────────────────────────────────────────

_SCOPE_LABELS = {
    "xelta.read":     "Read required Xelta workspace data",
    "xelta.write":    "Create and update items in your Xelta account",
    "offline_access": "Maintain access without re-authenticating",
}

_FIXED_PERMS = [
    "Verify your Xelta identity",
    "Access your enabled Xelta MCP tools",
]


def _render_consent(
    *,
    pending_id: str,
    entry,
    email: str,
    user_id: str,
    dev_mode: bool,
    error: str | None,
) -> str:
    scopes = entry.params.scopes or []
    perm_items = "".join(
        f'<li><span class="check">&#10003;</span> {label}</li>'
        for label in _FIXED_PERMS
    ) + "".join(
        f'<li><span class="check">&#10003;</span> {_SCOPE_LABELS.get(s, s)}</li>'
        for s in scopes if s in _SCOPE_LABELS
    )

    # Subtitle: show email or generic text
    if email:
        subtitle = f'Claude wants to access Xelta on behalf of <strong>{email}</strong>'
        switch_html = (
            f'<p class="switch">'
            f'<a href="/auth/xelta/logout?{urlencode({"pending_id": pending_id})}">'
            f'Switch account</a></p>'
        )
    else:
        subtitle = "Claude wants to access your Xelta account"
        switch_html = ""

    error_html = f'<div class="error">{error}</div>' if error else ""

    dev_section = ""
    if dev_mode:
        dev_section = f"""
        <details class="dev-box" open>
          <summary>&#x1F6E0; Developer mode — paste Xelta JWT</summary>
          <p class="dev-hint">
            <strong>ALLOW_MANUAL_JWT_CONSENT=true</strong> is active.
            This section is hidden in production.
            Go to <a href="https://www.xelta.ai" target="_blank">xelta.ai</a> → DevTools (F12) →
            Network → copy the <code>Authorization: Bearer &lt;token&gt;</code> header.
          </p>
          <label for="xelta_jwt">Xelta JWT token</label>
          <textarea
            id="xelta_jwt" name="xelta_jwt"
            placeholder="eyJhbGciOiJIUzI1NiIs..."
            spellcheck="false" autocomplete="off"
          ></textarea>
        </details>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Claude wants to access Xelta</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #f5f5f7;
      display: flex; align-items: center; justify-content: center;
      min-height: 100vh; padding: 24px;
    }}
    .card {{
      background: #fff; border-radius: 20px;
      box-shadow: 0 4px 32px rgba(0,0,0,.10);
      max-width: 420px; width: 100%; padding: 40px 36px 32px;
    }}
    /* App connection graphic */
    .apps {{
      display: flex; align-items: center; justify-content: center;
      gap: 14px; margin-bottom: 28px;
    }}
    .app-icon {{
      width: 52px; height: 52px; border-radius: 14px;
      display: flex; align-items: center; justify-content: center;
      font-weight: 700; font-size: 20px; color: #fff; flex-shrink: 0;
    }}
    .app-claude {{ background: linear-gradient(135deg, #c77dff, #7b2ff7); }}
    .app-xelta  {{ background: linear-gradient(135deg, #6c5ce7, #a29bfe); }}
    .connector {{
      display: flex; align-items: center; gap: 4px; color: #ccc;
    }}
    .connector-dot {{ width: 6px; height: 6px; border-radius: 50%; background: #ddd; }}
    .connector-line {{ width: 20px; height: 2px; background: #ddd; }}

    h1 {{ font-size: 20px; font-weight: 700; color: #111; margin-bottom: 6px; line-height: 1.3; }}
    .subtitle {{ font-size: 14px; color: #555; margin-bottom: 24px; line-height: 1.5; }}

    .perms-box {{
      background: #f9f9fb; border: 1px solid #eee; border-radius: 12px;
      padding: 16px 20px; margin-bottom: 20px;
    }}
    .perms-box p {{
      font-size: 12px; font-weight: 600; color: #888; text-transform: uppercase;
      letter-spacing: .06em; margin-bottom: 10px;
    }}
    .perms-box ul {{ list-style: none; display: flex; flex-direction: column; gap: 9px; }}
    .perms-box li {{ font-size: 14px; color: #333; display: flex; align-items: flex-start; gap: 8px; }}
    .check {{ color: #27ae60; font-weight: 700; flex-shrink: 0; }}

    .switch {{ font-size: 13px; text-align: center; margin-bottom: 20px; }}
    .switch a {{ color: #6c5ce7; text-decoration: none; }}
    .switch a:hover {{ text-decoration: underline; }}

    .error {{
      background: #fef2f2; border: 1px solid #fecaca; border-radius: 10px;
      color: #b91c1c; font-size: 13px; padding: 10px 14px; margin-bottom: 16px;
    }}

    /* Dev mode section */
    .dev-box {{
      border: 1.5px dashed #f59e0b; border-radius: 10px;
      padding: 14px 16px; margin-bottom: 20px; background: #fffbeb;
    }}
    .dev-box summary {{
      cursor: pointer; font-size: 13px; font-weight: 600; color: #92400e;
      list-style: none; margin-bottom: 10px;
    }}
    .dev-hint {{ font-size: 12px; color: #78350f; line-height: 1.6; margin-bottom: 10px; }}
    .dev-hint code {{ background: #fde68a; padding: 1px 4px; border-radius: 4px; font-size: 11px; }}
    .dev-box label {{
      display: block; font-size: 12px; font-weight: 600; color: #78350f;
      margin-bottom: 4px;
    }}
    .dev-box textarea {{
      width: 100%; height: 72px; border: 1px solid #fcd34d; border-radius: 8px;
      padding: 8px 10px; font-size: 12px; font-family: "SF Mono", Consolas, monospace;
      resize: vertical; background: #fffef0; color: #333;
    }}
    .dev-box textarea:focus {{ outline: none; border-color: #f59e0b; }}

    .actions {{
      display: flex; gap: 10px; margin-top: 20px;
    }}
    .btn-allow {{
      flex: 1; background: #6c5ce7; color: #fff; border: none;
      border-radius: 10px; padding: 13px; font-size: 15px; font-weight: 600;
      cursor: pointer; transition: background .15s;
    }}
    .btn-allow:hover {{ background: #5a4bd1; }}
    .btn-deny {{
      flex: 1; background: transparent; color: #555; border: 1.5px solid #e0e0e0;
      border-radius: 10px; padding: 12px; font-size: 14px; font-weight: 500;
      cursor: pointer; transition: border-color .15s;
    }}
    .btn-deny:hover {{ border-color: #999; color: #333; }}

    .footer {{
      font-size: 12px; color: #999; text-align: center;
      margin-top: 20px; line-height: 1.6;
    }}
    .footer a {{ color: #6c5ce7; text-decoration: none; }}
  </style>
</head>
<body>
  <div class="card">

    <!-- App connection graphic -->
    <div class="apps">
      <div class="app-icon app-claude" title="Claude">C</div>
      <div class="connector">
        <div class="connector-dot"></div>
        <div class="connector-line"></div>
        <div class="connector-dot"></div>
        <div class="connector-line"></div>
        <div class="connector-dot"></div>
      </div>
      <div class="app-icon app-xelta" title="Xelta">X</div>
    </div>

    <h1>Claude wants to access Xelta</h1>
    <p class="subtitle">{subtitle}</p>

    <!-- Permissions box -->
    <div class="perms-box">
      <p>This will allow Claude to:</p>
      <ul>{perm_items}</ul>
    </div>

    {switch_html}
    {error_html}

    <form method="POST" action="/consent" autocomplete="off">
      <input type="hidden" name="pending_id" value="{pending_id}" />
      {dev_section}

      <div class="actions">
        <button type="submit" name="action" value="deny" class="btn-deny">Deny</button>
        <button type="submit" name="action" value="allow" class="btn-allow">Allow</button>
      </div>
    </form>

    <p class="footer">
      If you allow access, this app will redirect you to Claude.
      You'll stay signed in until you
      <a href="/auth/xelta/logout">sign out</a> or revoke access.
    </p>

  </div>
</body>
</html>"""


def _error_page(message: str) -> HTMLResponse:
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Authorization Error</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, sans-serif;
      background: #f5f5f7; display: flex; align-items: center;
      justify-content: center; min-height: 100vh; padding: 24px;
    }}
    .card {{
      background: #fff; border-radius: 16px; padding: 40px 36px;
      box-shadow: 0 4px 24px rgba(0,0,0,.08); max-width: 420px; text-align: center;
    }}
    .icon {{ font-size: 36px; margin-bottom: 16px; }}
    h1 {{ color: #111; font-size: 20px; margin-bottom: 12px; }}
    p {{ color: #555; font-size: 14px; line-height: 1.7; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">&#9888;&#65039;</div>
    <h1>Authorization Error</h1>
    <p>{message}</p>
  </div>
</body>
</html>"""
    return HTMLResponse(html, status_code=400)
