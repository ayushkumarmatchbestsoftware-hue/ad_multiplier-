"""
Xelta sign-in for the local (stdio) server — the same session flow the Figma
plugin uses (new_figma/figma_plugin: ui.html + backend/server.py).

  POST {API}/session/start    {"clientVersion"}     -> authUrl, flowId, readKey, expiresAt, pollIntervalMs
  (the user opens authUrl in a browser and signs in)
  POST {API}/session/poll     {"flowId","readKey"}  -> status, session.accessToken
  POST {API}/session/refresh  Bearer <jwt>          -> refreshed token
  POST {API}/session/logout   Bearer <jwt>

The plugin keeps its token in figma.clientStorage and re-validates it on open;
here it lives in .xelta_session.json next to config.py, so every server process
on this machine — Claude Desktop's and each Claude Code session's — shares one
sign-in.

The plugin never calls /session/refresh (its backend only proxies it), so that
response shape is unverified. Refresh is therefore strictly best-effort: tried
only when the token is close to expiry, and the stored token is replaced only
by a structurally valid JWT that outlives it.

api.xelta.ai sits behind Cloudflare, which rejects non-browser user agents with
403 / error 1010, so every call here carries browser-like headers.
"""

from __future__ import annotations

import sys
import asyncio
import base64
import json
import os
import time
from pathlib import Path

import httpx

SESSION_API_URL = os.getenv(
    "XELTA_SESSION_API_URL", "https://api.xelta.ai/api/figma-plugin"
).rstrip("/")
SESSION_FILE = Path(
    os.getenv("XELTA_SESSION_FILE", "")
    or Path(__file__).resolve().parent.parent / ".xelta_session.json"
)
CLIENT_VERSION = "0.1.0"

# Refresh no earlier than this before expiry, and at most once per interval per
# process, so a refresh endpoint that misbehaves can't be hammered on every call.
_REFRESH_WINDOW_SECONDS = 24 * 3600
_REFRESH_RETRY_SECONDS = 3600
_last_refresh_attempt = 0.0

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://www.xelta.ai",
    "Referer": "https://www.xelta.ai/",
}


def _headers(jwt: str = "") -> dict:
    return {**_BROWSER_HEADERS, **({"Authorization": f"Bearer {jwt}"} if jwt else {})}


# ── token file ────────────────────────────────────────────────────────────────

def _load() -> dict:
    try:
        return json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(state: dict) -> None:
    # Write-then-replace so a concurrent reader in another server process never
    # sees a half-written file.
    tmp = SESSION_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, SESSION_FILE)


def _clear() -> None:
    try:
        SESSION_FILE.unlink()
    except FileNotFoundError:
        pass


def jwt_claims(token: str) -> dict:
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:
        return {}


def _is_live(token: str, *, margin: int = 60) -> bool:
    exp = jwt_claims(token).get("exp")
    return bool(token) and token.count(".") == 2 and (not exp or exp - margin > time.time())


def _extract_token(payload: dict) -> str:
    """Same fallbacks the plugin uses (ui.html) when reading a completed session."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    session = data.get("session") if isinstance(data.get("session"), dict) else {}
    for value in (
        session.get("accessToken"), session.get("token"),
        data.get("accessToken"), data.get("token"), data.get("jwt"),
    ):
        if isinstance(value, str) and value.count(".") == 2:
            return value
    return ""


def _store_token(token: str) -> dict:
    claims = jwt_claims(token)
    state = {
        "token": token,
        "user_id": str(claims.get("id") or claims.get("sub") or ""),
        "email": str(claims.get("email") or ""),
        "expires_at": claims.get("exp"),
        "saved_at": int(time.time()),
    }
    _save(state)
    return state


# ── used by config._get_active_jwt ────────────────────────────────────────────

def session_token() -> str:
    """The signed-in token if one is stored and unexpired, else ''."""
    token = _load().get("token", "")
    if not _is_live(token):
        return ""
    _maybe_refresh(token)
    return _load().get("token", token)


def _maybe_refresh(token: str) -> None:
    global _last_refresh_attempt
    claims = jwt_claims(token)
    exp, iat = claims.get("exp"), claims.get("iat")
    now = time.time()
    # Refresh in the last fifth of the token's life (capped at 24h). A fixed 24h
    # window meant a freshly issued 24-hour sign-in tried to refresh at once.
    window = min(_REFRESH_WINDOW_SECONDS, (exp - iat) * 0.2) if exp and iat else _REFRESH_WINDOW_SECONDS
    if not exp or exp - now > window:
        return
    if now - _last_refresh_attempt < _REFRESH_RETRY_SECONDS:
        return
    _last_refresh_attempt = now
    try:
        r = httpx.post(f"{SESSION_API_URL}/session/refresh", headers=_headers(token), timeout=10)
        if r.status_code != 200:
            print(f"[session] refresh declined: HTTP {r.status_code}", file=sys.stderr, flush=True)
            return
        fresh = _extract_token(r.json())
        if fresh and _is_live(fresh) and (jwt_claims(fresh).get("exp") or 0) > exp:
            _store_token(fresh)
            print("[session] token refreshed", file=sys.stderr, flush=True)
        else:
            print("[session] refresh returned no usable token; keeping the current one", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[session] refresh failed: {e}", file=sys.stderr, flush=True)


# ── sign-in / sign-out ────────────────────────────────────────────────────────

async def _validate(client: httpx.AsyncClient, token: str) -> bool | None:
    """True/False from Xelta, or None if the check itself couldn't run.

    The plugin validates a restored token against /api/me; the equivalent here
    is the credits endpoint, which is free and needs no other state.
    """
    from config import CREDITS_BASE_URL
    try:
        r = await client.get(
            f"{CREDITS_BASE_URL}/api/users/credits",
            headers={"Authorization": f"Bearer {token}"},  # no Origin: pmt 500s on it
            timeout=10,
        )
    except Exception:
        return None
    if r.status_code in (401, 403):
        return False
    return True if r.status_code == 200 else None


def _describe(state: dict) -> str:
    who = state.get("email") or state.get("user_id") or "unknown account"
    exp = state.get("expires_at")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(exp)) if exp else "no expiry"
    return f"Signed in to Xelta as {who} (session valid until {when})."


async def login_step(switch_account: bool = False) -> dict:
    """One non-blocking step of sign-in. Never waits on the user.

    Returns {signed_in, email, user_id, expires_at, auth_url, message}. When not
    yet signed in it starts (or reuses) a browser flow, checks it exactly once,
    and returns the link. The sign-in widget calls this repeatedly while the
    user signs in; a tool call never sits polling, which is what looked stuck.
    """
    state = _load()
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        if switch_account and state.get("token"):
            await logout()
            state = {}

        token = state.get("token", "")
        if token and _is_live(token):
            valid = await _validate(client, token)
            if valid is not False:
                return {**_identity(state), "signed_in": True, "auth_url": "",
                        "message": _describe(state)}
            state.pop("token", None)
            _save(state) if state else _clear()

        # Reuse an unexpired pending flow, so repeated calls wait on the same tab.
        pending = state.get("pending") or {}
        if not pending or pending.get("expires_ts", 0) <= time.time():
            r = await client.post(f"{SESSION_API_URL}/session/start",
                                  json={"clientVersion": CLIENT_VERSION}, headers=_headers())
            r.raise_for_status()
            body = r.json()
            data = body.get("data") if isinstance(body.get("data"), dict) else body
            auth_url = data.get("authUrl") or data.get("auth_url") or data.get("url")
            if not auth_url:
                return {"signed_in": False, "auth_url": "",
                        "message": f"Xelta didn't return a sign-in link: {str(body)[:160]}"}
            # Force the account picker instead of silently reusing a browser
            # session, as the Figma plugin does.
            sep = "&" if "?" in auth_url else "?"
            pending = {
                "flowId": data.get("flowId") or data.get("flow_id"),
                "readKey": data.get("readKey") or data.get("read_key"),
                "authUrl": f"{auth_url}{sep}prompt=select_account&max_age=0",
                "expires_ts": time.time() + 15 * 60,
            }
            _save({"pending": pending})
            return {"signed_in": False, "auth_url": pending["authUrl"],
                    "message": "Waiting for the user to sign in to Xelta in their browser."}

        try:
            r = await client.post(f"{SESSION_API_URL}/session/poll",
                                  json={"flowId": pending["flowId"], "readKey": pending["readKey"]},
                                  headers=_headers())
            gone = r.status_code == 410
            body = r.json() if r.content else {}
        except Exception:
            gone, body = False, {}
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        status = str(data.get("status", "")).lower()

        if status in ("complete", "completed", "success"):
            token = _extract_token(body)
            if not token:
                _clear()
                return {"signed_in": False, "auth_url": "",
                        "message": "Sign-in finished but Xelta returned no token. Try again."}
            stored = _store_token(token)
            return {**_identity(stored), "signed_in": True, "auth_url": "", "message": _describe(stored)}
        if gone or status in ("expired", "denied", "cancelled", "error"):
            _clear()
            return await login_step()
        return {"signed_in": False, "auth_url": pending["authUrl"],
                "message": "Waiting for the user to sign in to Xelta in their browser."}


def _identity(state: dict) -> dict:
    return {"email": state.get("email", ""), "user_id": state.get("user_id", ""),
            "expires_at": state.get("expires_at")}


async def login(switch_account: bool = False) -> str:
    """Text form of login_step, for callers that only need a sentence."""
    step = await login_step(switch_account)
    if step["signed_in"]:
        return step["message"]
    return f"Open this link to sign in to Xelta: {step['auth_url']}" if step["auth_url"] else step["message"]


async def logout() -> str:
    state = _load()
    token = state.get("token", "")
    if token:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(f"{SESSION_API_URL}/session/logout", headers=_headers(token))
        except Exception:
            pass  # best-effort, as in the plugin; the local session is cleared regardless
    _clear()
    return "Signed out of Xelta." if token else "No Xelta session was stored."


def current_identity() -> dict:
    state = _load()
    return state if _is_live(state.get("token", "")) else {}
