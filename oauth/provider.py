"""XeltaOAuthProvider — implements OAuthAuthorizationServerProvider for FastMCP.

Flow:
  GET /authorize        → provider.authorize() → redirect to /consent?pending_id=<id>
  GET /consent          → check Xelta session → if none: redirect to /auth/xelta/start
  GET /auth/xelta/start → build Xelta OAuth URL → redirect to Xelta login
  GET /auth/xelta/callback → validate state, exchange code, create session, set cookie
  GET /consent (w/ session) → show Higgsfield-style consent page
  POST /consent         → read user_jwt from session, create auth code, redirect to Claude
  POST /token           → FastMCP built-in handler calls provider methods below
"""

from __future__ import annotations

import base64
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from oauth.storage import OAuthStorage


# ── Extended token models (carry user_jwt through the flow) ───────────────────

class XeltaAccessToken(AccessToken):
    user_jwt: str = ""


class XeltaAuthorizationCode(AuthorizationCode):
    user_jwt: str = ""


class XeltaRefreshToken(RefreshToken):
    user_jwt: str = ""


# ── In-memory pending auth request (lives only during /authorize → /consent) ─

@dataclass
class _PendingAuth:
    client_id: str
    params: AuthorizationParams
    # 15 minutes — enough time for the user to complete Xelta login
    expires_at: float = field(default_factory=lambda: time.time() + 900)


class XeltaOAuthProvider:
    """
    OAuthAuthorizationServerProvider implementation for Xelta MCP.

    Clients, codes, and tokens are stored in SQLite via OAuthStorage.
    Pending authorization requests (between /authorize and /consent) are
    kept in memory — they expire after 5 minutes.
    """

    def __init__(self, storage: OAuthStorage, base_url: str) -> None:
        self._storage = storage
        self._base_url = base_url.rstrip("/")
        self._pending: dict[str, _PendingAuth] = {}

    # ── pending request helpers ───────────────────────────────────────────────

    def store_pending(self, client_id: str, params: AuthorizationParams) -> str:
        self._evict_expired_pending()
        pending_id = secrets.token_urlsafe(24)
        self._pending[pending_id] = _PendingAuth(client_id=client_id, params=params)
        return pending_id

    def pop_pending(self, pending_id: str) -> _PendingAuth | None:
        entry = self._pending.pop(pending_id, None)
        if entry and entry.expires_at < time.time():
            return None
        return entry

    def peek_pending(self, pending_id: str) -> _PendingAuth | None:
        entry = self._pending.get(pending_id)
        if entry and entry.expires_at < time.time():
            self._pending.pop(pending_id, None)
            return None
        return entry

    def _evict_expired_pending(self) -> None:
        now = time.time()
        expired = [k for k, v in self._pending.items() if v.expires_at < now]
        for k in expired:
            del self._pending[k]

    # ── OAuthAuthorizationServerProvider protocol ─────────────────────────────

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return await self._storage.get_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await self._storage.save_client(client_info)

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        """Called by FastMCP's /authorize handler. Returns URL to redirect to."""
        from config import MCP_RESOURCE_URL
        
        # Get resource, default to MCP_RESOURCE_URL if missing
        resource = getattr(params, "resource", None)
        if not resource:
            resource = MCP_RESOURCE_URL
            print(f"[oauth] authorize: resource missing, defaulting to {MCP_RESOURCE_URL}", flush=True)
        
        print(f"[oauth] authorize client={client.client_id!r} resource={resource!r}", flush=True)
        
        pending_id = self.store_pending(client.client_id, params)
        qs = urlencode({"pending_id": pending_id})
        return f"{self._base_url}/consent?{qs}"

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> XeltaAuthorizationCode | None:
        row = await self._storage.get_auth_code(authorization_code)
        if row is None:
            return None
        if row["client_id"] != client.client_id:
            return None
        if row["used"]:
            return None

        from pydantic import AnyUrl

        return XeltaAuthorizationCode(
            code=row["code"],
            client_id=row["client_id"],
            scopes=(row["scope"] or "").split() if row["scope"] else [],
            expires_at=float(row["expires_at"]),
            code_challenge=row["code_challenge"],
            redirect_uri=AnyUrl(row["redirect_uri"]),
            redirect_uri_provided_explicitly=bool(row["redirect_uri_provided_explicitly"]),
            resource=row["resource"],
            user_jwt=row["user_jwt"],
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: XeltaAuthorizationCode,
    ) -> OAuthToken:
        """FastMCP token handler has already verified PKCE. Issue tokens."""
        await self._storage.mark_code_used(authorization_code.code)

        scopes = authorization_code.scopes or []
        scope_str = " ".join(scopes)
        resource = authorization_code.resource
        user_jwt = authorization_code.user_jwt

        from config import OAUTH_ACCESS_TOKEN_TTL_SECONDS, OAUTH_REFRESH_TOKEN_TTL_DAYS

        access_token = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + OAUTH_ACCESS_TOKEN_TTL_SECONDS

        await self._storage.save_access_token(
            token=access_token,
            client_id=client.client_id,
            scope=scope_str or None,
            resource=resource,
            user_jwt=user_jwt,
            expires_at=expires_at,
        )

        refresh_token = secrets.token_urlsafe(32)
        rt_expires_at = int(time.time()) + OAUTH_REFRESH_TOKEN_TTL_DAYS * 86400

        await self._storage.save_refresh_token(
            token=refresh_token,
            client_id=client.client_id,
            scope=scope_str or None,
            resource=resource,
            user_jwt=user_jwt,
            expires_at=rt_expires_at,
        )

        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=OAUTH_ACCESS_TOKEN_TTL_SECONDS,
            refresh_token=refresh_token,
            scope=scope_str or None,
        )

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> XeltaRefreshToken | None:
        row = await self._storage.get_refresh_token(refresh_token)
        if row is None:
            return None
        if row["client_id"] != client.client_id:
            return None

        return XeltaRefreshToken(
            token=row["token"],
            client_id=row["client_id"],
            scopes=(row["scope"] or "").split() if row["scope"] else [],
            expires_at=row["expires_at"],
            user_jwt=row["user_jwt"],
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: XeltaRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """Rotate both tokens on refresh."""
        from config import OAUTH_ACCESS_TOKEN_TTL_SECONDS, OAUTH_REFRESH_TOKEN_TTL_DAYS

        # Revoke old refresh token
        await self._storage.revoke_refresh_token(refresh_token.token)

        effective_scopes = scopes if scopes else refresh_token.scopes
        scope_str = " ".join(effective_scopes)
        user_jwt = refresh_token.user_jwt

        access_token = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + OAUTH_ACCESS_TOKEN_TTL_SECONDS

        await self._storage.save_access_token(
            token=access_token,
            client_id=client.client_id,
            scope=scope_str or None,
            resource=None,
            user_jwt=user_jwt,
            expires_at=expires_at,
        )

        new_refresh_token = secrets.token_urlsafe(32)
        rt_expires_at = int(time.time()) + OAUTH_REFRESH_TOKEN_TTL_DAYS * 86400

        await self._storage.save_refresh_token(
            token=new_refresh_token,
            client_id=client.client_id,
            scope=scope_str or None,
            resource=None,
            user_jwt=user_jwt,
            expires_at=rt_expires_at,
        )

        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=OAUTH_ACCESS_TOKEN_TTL_SECONDS,
            refresh_token=new_refresh_token,
            scope=scope_str or None,
        )

    async def load_access_token(self, token: str) -> XeltaAccessToken | None:
        """Called by ProviderTokenVerifier to authenticate bearer tokens on /mcp."""
        row = await self._storage.get_access_token(token)
        if row is None:
            return None

        expires_at = row["expires_at"]
        if expires_at is not None and int(expires_at) < int(time.time()):
            return None

        scopes = (row["scope"] or "").split() if row["scope"] else []

        return XeltaAccessToken(
            token=row["token"],
            client_id=row["client_id"],
            scopes=scopes,
            expires_at=expires_at,
            resource=row["resource"],
            user_jwt=row["user_jwt"],
        )

    async def revoke_token(
        self,
        token: XeltaAccessToken | XeltaRefreshToken,
    ) -> None:
        if isinstance(token, XeltaAccessToken):
            await self._storage.delete_access_token(token.token)
        elif isinstance(token, XeltaRefreshToken):
            await self._storage.revoke_refresh_token(token.token)


# ── JWT utility ───────────────────────────────────────────────────────────────

def decode_jwt_payload(jwt: str) -> dict[str, Any]:
    """Decode JWT payload without verifying signature. Returns {} on failure."""
    try:
        parts = jwt.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1]
        payload += "=" * (4 - len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def validate_jwt_structure(jwt: str) -> bool:
    """Return True if jwt has three base64url segments (structure only, no sig check)."""
    parts = jwt.strip().split(".")
    if len(parts) != 3:
        return False
    for part in parts:
        if not part:
            return False
    try:
        padded = parts[1] + "=" * (4 - len(parts[1]) % 4)
        json.loads(base64.urlsafe_b64decode(padded))
        return True
    except Exception:
        return False
