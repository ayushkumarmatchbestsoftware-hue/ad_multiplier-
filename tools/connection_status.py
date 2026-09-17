"""Safe diagnostic tool — confirms auth is working without leaking credentials."""

from __future__ import annotations

import base64
import json


async def xelta_connection_status() -> str:
    """
    Check whether the Xelta MCP connection is authenticated and working.

    Returns MCP connection status, authenticated client info, Xelta user ID
    and email (if available from the session). Never returns the raw JWT,
    access token, refresh token, auth code, or client secret.
    """
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token
        token = get_access_token()
    except Exception:
        token = None

    lines: list[str] = ["Xelta MCP — connection status", ""]

    if token is None:
        lines.append("Claude OAuth: no active session (running in local/stdio mode)")
        # Try env-var fallback
        try:
            from config import JWT_TOKEN
            if JWT_TOKEN:
                user_id, email = _extract_user_info(JWT_TOKEN)
                lines.append(f"Fallback JWT: present")
                if user_id:
                    lines.append(f"Xelta user_id: {user_id}")
                if email:
                    lines.append(f"Xelta email:   {email}")
            else:
                lines.append("Fallback JWT: not set")
        except Exception:
            lines.append("Fallback JWT: unavailable")
    else:
        lines.append("Claude OAuth: active session")
        lines.append(f"Client ID:    {token.client_id}")
        lines.append(f"Scopes:       {', '.join(token.scopes)}")

        if token.expires_at:
            import time
            remaining = int(token.expires_at) - int(time.time())
            if remaining > 0:
                lines.append(f"Token expiry: {remaining}s remaining")
            else:
                lines.append("Token:        expired")

        if hasattr(token, "user_jwt") and token.user_jwt:
            user_id, email = _extract_user_info(token.user_jwt)
            if user_id:
                lines.append(f"Xelta user_id: {user_id}")
            if email:
                lines.append(f"Xelta email:   {email}")

    lines.append("")
    lines.append("MCP server: reachable")
    return "\n".join(lines)


def _extract_user_info(jwt: str) -> tuple[str, str]:
    """Decode JWT payload and return (user_id, email). Never raises."""
    try:
        payload = jwt.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        user_id = str(data.get("id") or data.get("sub") or "")
        email = str(data.get("email") or "")
        return user_id, email
    except Exception:
        return "", ""
