"""
OAuth flow integration tests.

Run with:
    pip install pytest pytest-asyncio httpx aiosqlite mcp
    pytest tests/test_oauth.py -v

These tests spin up the full FastMCP ASGI app in-process using
httpx.AsyncClient with ASGITransport (async).
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
import os
import secrets
import time
import urllib.parse

import pytest
from httpx import ASGITransport, AsyncClient

# Must be set before importing any project modules so config picks them up.
os.environ.setdefault("OAUTH_DB_PATH", ":memory:")
os.environ.setdefault("MCP_PUBLIC_BASE_URL", "http://localhost")
os.environ.setdefault("MCP_RESOURCE_URL", "http://localhost/mcp")
os.environ.setdefault("OAUTH_ISSUER", "http://localhost")
os.environ.setdefault("XELTA_JWT_TOKEN", "")
# Enable manual JWT on the consent page so existing flow tests work without Xelta IdP.
os.environ["ALLOW_MANUAL_JWT_CONSENT"] = "true"
# Leave Xelta IdP unconfigured — tests that need it configure it explicitly.
os.environ.setdefault("XELTA_AUTH_AUTHORIZE_URL", "")
os.environ.setdefault("XELTA_AUTH_TOKEN_URL", "")
os.environ.setdefault("XELTA_AUTH_USERINFO_URL", "")
os.environ.setdefault("XELTA_AUTH_CLIENT_ID", "")
os.environ.setdefault("XELTA_AUTH_CLIENT_SECRET", "")
os.environ.setdefault("XELTA_AUTH_REDIRECT_URI", "http://localhost/auth/xelta/callback")
os.environ.setdefault("XELTA_AUTH_SCOPES", "openid email profile")


# ── Build test app ────────────────────────────────────────────────────────────

async def _make_app():
    """Return (asgi_app, storage, provider) with all routes registered."""
    from pydantic import AnyHttpUrl
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    from oauth.storage import OAuthStorage
    from oauth.provider import XeltaOAuthProvider
    from oauth.consent import handle_consent
    from oauth.xelta_auth import handle_xelta_callback, handle_xelta_logout, handle_xelta_start

    storage = OAuthStorage(":memory:")
    # Initialize directly — ASGITransport does not trigger ASGI lifespan events
    await storage.initialize()

    provider = XeltaOAuthProvider(storage, "http://localhost")

    app = FastMCP(
        "xelta-test",
        host="127.0.0.1",
        port=9999,
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl("http://localhost"),
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=["xelta.read", "xelta.write", "offline_access"],
                default_scopes=["xelta.read", "xelta.write", "offline_access"],
            ),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=["xelta.read"],
            resource_server_url=AnyHttpUrl("http://localhost/mcp"),
        ),
    )

    @app.custom_route("/consent", methods=["GET", "POST"])
    async def consent(request: Request):
        return await handle_consent(request, provider)

    @app.custom_route("/auth/xelta/start", methods=["GET"])
    async def xelta_start(request: Request):
        return await handle_xelta_start(request, storage)

    @app.custom_route("/auth/xelta/callback", methods=["GET"])
    async def xelta_callback(request: Request):
        return await handle_xelta_callback(request, storage)

    @app.custom_route("/auth/xelta/logout", methods=["GET"])
    async def xelta_logout(request: Request):
        return await handle_xelta_logout(request, storage)

    @app.custom_route("/health", methods=["GET"])
    async def health(request: Request):
        return JSONResponse({"status": "ok"})

    @app.tool()
    async def ping() -> str:
        """Ping tool for testing."""
        return "pong"

    return app.streamable_http_app(), storage, provider


# ── Helpers ───────────────────────────────────────────────────────────────────

def pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge_S256)."""
    verifier = secrets.token_urlsafe(40)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


FAKE_JWT = (
    "eyJhbGciOiJIUzI1NiJ9"
    "." + base64.urlsafe_b64encode(
        json.dumps({"id": "user123", "email": "test@xelta.ai"}).encode()
    ).rstrip(b"=").decode()
    + ".fakesig"
)


async def _register_client(client: AsyncClient) -> tuple[str, str]:
    """Register a test OAuth client; return (client_id, client_secret)."""
    r = await client.post(
        "/register",
        json={
            "client_name": "Test Client",
            "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
            "scope": "xelta.read xelta.write offline_access",
        },
    )
    assert r.status_code == 201
    return r.json()["client_id"], r.json()["client_secret"]


async def _get_pending_id(client: AsyncClient, client_id: str, challenge: str) -> str:
    """Run /authorize and return the pending_id from the consent redirect."""
    r = await client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "teststate",
            "scope": "xelta.read xelta.write offline_access",
        },
    )
    assert r.status_code == 302
    location = r.headers["location"]
    return urllib.parse.parse_qs(urllib.parse.urlparse(location).query)["pending_id"][0]


async def _insert_session(storage, session_id: str, user_jwt: str = FAKE_JWT) -> None:
    """Insert a login session directly into storage (bypasses IdP)."""
    await storage.save_login_session(
        session_id=session_id,
        user_id="user123",
        email="test@xelta.ai",
        user_jwt=user_jwt,
        expires_at=int(time.time()) + 86400,
    )


# ── Infrastructure tests ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health():
    asgi_app, _, _ = await _make_app()
    async with AsyncClient(transport=ASGITransport(app=asgi_app), base_url="http://localhost") as client:
        r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_protected_resource_metadata():
    asgi_app, _, _ = await _make_app()
    async with AsyncClient(transport=ASGITransport(app=asgi_app), base_url="http://localhost") as client:
        r = await client.get("/.well-known/oauth-protected-resource/mcp")
    assert r.status_code == 200
    data = r.json()
    assert data["resource"] == "http://localhost/mcp"
    assert any(s.rstrip("/") == "http://localhost" for s in data["authorization_servers"])


@pytest.mark.asyncio
async def test_authorization_server_metadata():
    asgi_app, _, _ = await _make_app()
    async with AsyncClient(transport=ASGITransport(app=asgi_app), base_url="http://localhost") as client:
        r = await client.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    data = r.json()
    assert data["issuer"].rstrip("/") == "http://localhost"
    assert "/authorize" in data["authorization_endpoint"]
    assert "/token" in data["token_endpoint"]
    assert "/register" in data["registration_endpoint"]
    assert "S256" in data["code_challenge_methods_supported"]


@pytest.mark.asyncio
async def test_dynamic_client_registration():
    asgi_app, _, _ = await _make_app()
    async with AsyncClient(transport=ASGITransport(app=asgi_app), base_url="http://localhost") as client:
        client_id, client_secret = await _register_client(client)
    assert client_id
    assert client_secret


@pytest.mark.asyncio
async def test_authorize_redirects_to_consent():
    asgi_app, _, _ = await _make_app()
    _, challenge = pkce_pair()

    async with AsyncClient(
        transport=ASGITransport(app=asgi_app),
        base_url="http://localhost",
        follow_redirects=False,
    ) as client:
        client_id, _ = await _register_client(client)
        r = await client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "response_type": "code",
                "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "teststate",
                "scope": "xelta.read xelta.write offline_access",
            },
        )

    assert r.status_code == 302
    location = r.headers["location"]
    assert "/consent" in location
    assert "pending_id=" in location


@pytest.mark.asyncio
async def test_authorize_rejects_invalid_client():
    asgi_app, _, _ = await _make_app()
    _, challenge = pkce_pair()

    async with AsyncClient(
        transport=ASGITransport(app=asgi_app),
        base_url="http://localhost",
        follow_redirects=False,
    ) as client:
        r = await client.get(
            "/authorize",
            params={
                "client_id": "nonexistent",
                "response_type": "code",
                "redirect_uri": "https://evil.com/callback",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )

    assert r.status_code in (400, 302)


# ── Full flow — manual JWT (dev mode, ALLOW_MANUAL_JWT_CONSENT=true) ──────────

@pytest.mark.asyncio
async def test_full_auth_code_flow_manual_jwt():
    asgi_app, _, _ = await _make_app()
    verifier, challenge = pkce_pair()

    async with AsyncClient(
        transport=ASGITransport(app=asgi_app),
        base_url="http://localhost",
        follow_redirects=False,
    ) as client:
        client_id, client_secret = await _register_client(client)

        pending_id = await _get_pending_id(client, client_id, challenge)

        consent_r = await client.post(
            "/consent",
            data={"pending_id": pending_id, "action": "approve", "xelta_jwt": FAKE_JWT},
        )
        assert consent_r.status_code == 302
        callback_url = consent_r.headers["location"]
        assert "code=" in callback_url
        assert "state=teststate" in callback_url

        code = urllib.parse.parse_qs(urllib.parse.urlparse(callback_url).query)["code"][0]

        token_r = await client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "code_verifier": verifier,
                "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            },
        )
        assert token_r.status_code == 200
        tokens = token_r.json()
        assert "access_token" in tokens
        assert tokens["token_type"].lower() == "bearer"
        assert "refresh_token" in tokens
        assert tokens["expires_in"] == 3600


@pytest.mark.asyncio
async def test_invalid_pkce_rejected():
    asgi_app, _, _ = await _make_app()
    verifier, challenge = pkce_pair()
    wrong_verifier = secrets.token_urlsafe(40)

    async with AsyncClient(
        transport=ASGITransport(app=asgi_app),
        base_url="http://localhost",
        follow_redirects=False,
    ) as client:
        client_id, client_secret = await _register_client(client)
        pending_id = await _get_pending_id(client, client_id, challenge)

        consent_r = await client.post(
            "/consent",
            data={"pending_id": pending_id, "action": "approve", "xelta_jwt": FAKE_JWT},
        )
        code = urllib.parse.parse_qs(
            urllib.parse.urlparse(consent_r.headers["location"]).query
        )["code"][0]

        token_r = await client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "code_verifier": wrong_verifier,
                "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            },
        )
    assert token_r.status_code == 400
    assert token_r.json()["error"] == "invalid_grant"


@pytest.mark.asyncio
async def test_auth_code_cannot_be_reused():
    asgi_app, _, _ = await _make_app()
    verifier, challenge = pkce_pair()

    async with AsyncClient(
        transport=ASGITransport(app=asgi_app),
        base_url="http://localhost",
        follow_redirects=False,
    ) as client:
        client_id, client_secret = await _register_client(client)
        pending_id = await _get_pending_id(client, client_id, challenge)

        consent_r = await client.post(
            "/consent",
            data={"pending_id": pending_id, "action": "approve", "xelta_jwt": FAKE_JWT},
        )
        code = urllib.parse.parse_qs(
            urllib.parse.urlparse(consent_r.headers["location"]).query
        )["code"][0]

        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "code_verifier": verifier,
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
        }
        first = await client.post("/token", data=payload)
        assert first.status_code == 200

        second = await client.post("/token", data=payload)
        assert second.status_code == 400


# ── Bearer token / MCP access tests ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_unauthenticated_mcp_returns_401():
    asgi_app, _, _ = await _make_app()
    async with AsyncClient(transport=ASGITransport(app=asgi_app), base_url="http://localhost") as client:
        r = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={"Content-Type": "application/json"},
        )
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers


@pytest.mark.asyncio
async def test_valid_bearer_token_accepted():
    asgi_app, _, _ = await _make_app()
    verifier, challenge = pkce_pair()

    async with AsyncClient(
        transport=ASGITransport(app=asgi_app),
        base_url="http://localhost",
        follow_redirects=False,
    ) as client:
        client_id, client_secret = await _register_client(client)
        pending_id = await _get_pending_id(client, client_id, challenge)

        consent_r = await client.post(
            "/consent",
            data={"pending_id": pending_id, "action": "approve", "xelta_jwt": FAKE_JWT},
        )
        code = urllib.parse.parse_qs(
            urllib.parse.urlparse(consent_r.headers["location"]).query
        )["code"][0]

        token_r = await client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "code_verifier": verifier,
                "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            },
        )
        access_token = token_r.json()["access_token"]

    # Use raise_app_exceptions=False: the session manager needs a running task group
    # (ASGI lifespan) which ASGITransport doesn't trigger. The test intent is to
    # verify the bearer token is accepted (not 401/403), not to exercise full MCP.
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app, raise_app_exceptions=False),
        base_url="http://localhost",
    ) as client:
        r = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0.1"},
            }},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2024-11-05",
            },
        )
    # Bearer token was accepted by auth middleware (not 401/403)
    assert r.status_code not in (401, 403)


# ── Session-based (Higgsfield-style) consent tests ───────────────────────────

@pytest.mark.asyncio
async def test_consent_get_with_valid_session_shows_form():
    """GET /consent with a valid session cookie renders the consent HTML."""
    asgi_app, storage, _ = await _make_app()
    _, challenge = pkce_pair()
    session_id = secrets.token_urlsafe(32)

    async with AsyncClient(
        transport=ASGITransport(app=asgi_app),
        base_url="http://localhost",
        follow_redirects=False,
    ) as client:
        client_id, _ = await _register_client(client)
        pending_id = await _get_pending_id(client, client_id, challenge)

        # Insert a session directly (simulating completed Xelta login)
        await _insert_session(storage, session_id)

        r = await client.get(
            "/consent",
            params={"pending_id": pending_id},
            headers={"Cookie": f"xelta_mcp_session={session_id}"},
        )

    assert r.status_code == 200
    html = r.text
    assert "test@xelta.ai" in html
    assert "Allow" in html or "allow" in html.lower()


@pytest.mark.asyncio
async def test_consent_get_without_session_shows_dev_jwt_field():
    """GET /consent with no session and ALLOW_MANUAL_JWT_CONSENT=true shows JWT textarea."""
    asgi_app, _, _ = await _make_app()
    _, challenge = pkce_pair()

    async with AsyncClient(
        transport=ASGITransport(app=asgi_app),
        base_url="http://localhost",
        follow_redirects=False,
    ) as client:
        client_id, _ = await _register_client(client)
        pending_id = await _get_pending_id(client, client_id, challenge)

        r = await client.get("/consent", params={"pending_id": pending_id})

    assert r.status_code == 200
    assert "xelta_jwt" in r.text or "JWT" in r.text


@pytest.mark.asyncio
async def test_consent_post_uses_session_jwt_not_form():
    """POST /consent with a valid session reads the JWT from the session, not the form."""
    asgi_app, storage, _ = await _make_app()
    verifier, challenge = pkce_pair()
    session_id = secrets.token_urlsafe(32)

    async with AsyncClient(
        transport=ASGITransport(app=asgi_app),
        base_url="http://localhost",
        follow_redirects=False,
    ) as client:
        client_id, client_secret = await _register_client(client)
        pending_id = await _get_pending_id(client, client_id, challenge)

        await _insert_session(storage, session_id)

        # POST with session cookie but NO xelta_jwt form field
        consent_r = await client.post(
            "/consent",
            data={"pending_id": pending_id, "action": "approve"},
            headers={"Cookie": f"xelta_mcp_session={session_id}"},
        )

    assert consent_r.status_code == 302
    callback_url = consent_r.headers["location"]
    assert "code=" in callback_url


@pytest.mark.asyncio
async def test_consent_post_with_session_jwt_not_in_redirect():
    """The redirect URL after consent approval must not contain the JWT value."""
    asgi_app, storage, _ = await _make_app()
    _, challenge = pkce_pair()
    session_id = secrets.token_urlsafe(32)

    async with AsyncClient(
        transport=ASGITransport(app=asgi_app),
        base_url="http://localhost",
        follow_redirects=False,
    ) as client:
        client_id, _ = await _register_client(client)
        pending_id = await _get_pending_id(client, client_id, challenge)
        await _insert_session(storage, session_id)

        consent_r = await client.post(
            "/consent",
            data={"pending_id": pending_id, "action": "approve"},
            headers={"Cookie": f"xelta_mcp_session={session_id}"},
        )

    assert consent_r.status_code == 302
    redirect = consent_r.headers["location"]
    assert FAKE_JWT not in redirect
    assert "fakesig" not in redirect


@pytest.mark.asyncio
async def test_logout_clears_session_and_cookie():
    """GET /auth/xelta/logout deletes the session from DB and clears the cookie."""
    asgi_app, storage, _ = await _make_app()
    session_id = secrets.token_urlsafe(32)

    async with AsyncClient(
        transport=ASGITransport(app=asgi_app),
        base_url="http://localhost",
        follow_redirects=False,
    ) as client:
        await _insert_session(storage, session_id)

        # Confirm session exists
        row = await storage.get_login_session(session_id)
        assert row is not None

        r = await client.get(
            "/auth/xelta/logout",
            headers={"Cookie": f"xelta_mcp_session={session_id}"},
        )

    assert r.status_code == 302
    # Session must be gone from DB
    row_after = await storage.get_login_session(session_id)
    assert row_after is None
    # Cookie must be cleared
    set_cookie = r.headers.get("set-cookie", "")
    assert "xelta_mcp_session" in set_cookie
    assert "Max-Age=0" in set_cookie or 'expires=' in set_cookie.lower()


@pytest.mark.asyncio
async def test_logout_same_origin_next_enforced():
    """Logout next= param is restricted to same-origin paths."""
    asgi_app, storage, _ = await _make_app()
    session_id = secrets.token_urlsafe(32)

    async with AsyncClient(
        transport=ASGITransport(app=asgi_app),
        base_url="http://localhost",
        follow_redirects=False,
    ) as client:
        await _insert_session(storage, session_id)

        r = await client.get(
            "/auth/xelta/logout",
            params={"next": "https://evil.com/steal"},
            headers={"Cookie": f"xelta_mcp_session={session_id}"},
        )

    assert r.status_code == 302
    # Must redirect to "/" not the attacker's URL
    assert r.headers["location"] == "/"


@pytest.mark.asyncio
async def test_xelta_start_without_authorize_url_and_dev_mode():
    """
    /auth/xelta/start with no XELTA_AUTH_AUTHORIZE_URL configured
    and ALLOW_MANUAL_JWT_CONSENT=true redirects straight to /consent.
    """
    import config as cfg

    asgi_app, _, _ = await _make_app()
    _, challenge = pkce_pair()

    async with AsyncClient(
        transport=ASGITransport(app=asgi_app),
        base_url="http://localhost",
        follow_redirects=False,
    ) as client:
        client_id, _ = await _register_client(client)
        pending_id = await _get_pending_id(client, client_id, challenge)

        # XELTA_AUTH_AUTHORIZE_URL is empty by default in this test env
        r = await client.get(
            "/auth/xelta/start",
            params={"pending_id": pending_id},
        )

    # Should redirect to /consent (dev shortcut) rather than error
    assert r.status_code == 302
    assert "/consent" in r.headers["location"]


@pytest.mark.asyncio
async def test_xelta_callback_rejects_bad_state():
    """GET /auth/xelta/callback with an unknown state returns an error page."""
    asgi_app, _, _ = await _make_app()

    async with AsyncClient(
        transport=ASGITransport(app=asgi_app),
        base_url="http://localhost",
        follow_redirects=False,
    ) as client:
        r = await client.get(
            "/auth/xelta/callback",
            params={"code": "somecode", "state": "totally-unknown-state"},
        )

    # Should return an HTML error page, not crash
    assert r.status_code in (400, 200)
    assert "expired" in r.text.lower() or "invalid" in r.text.lower() or "error" in r.text.lower()


@pytest.mark.asyncio
async def test_get_mcp_returns_401():
    """GET /mcp (no auth) must return 401 with WWW-Authenticate, same as POST."""
    asgi_app, _, _ = await _make_app()
    async with AsyncClient(transport=ASGITransport(app=asgi_app), base_url="http://localhost") as client:
        r = await client.get("/mcp")
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers


@pytest.mark.asyncio
async def test_manual_jwt_hidden_when_allow_manual_jwt_false():
    """
    When ALLOW_MANUAL_JWT_CONSENT=false (production), the consent GET
    response must not contain a JWT textarea / paste field.
    """
    # Temporarily flip the flag
    original = os.environ.get("ALLOW_MANUAL_JWT_CONSENT")
    os.environ["ALLOW_MANUAL_JWT_CONSENT"] = "false"

    try:
        import config as cfg
        importlib.reload(cfg)

        asgi_app, _, _ = await _make_app()
        _, challenge = pkce_pair()

        async with AsyncClient(
            transport=ASGITransport(app=asgi_app),
            base_url="http://localhost",
            follow_redirects=False,
        ) as client:
            client_id, _ = await _register_client(client)
            pending_id = await _get_pending_id(client, client_id, challenge)

            r = await client.get("/consent", params={"pending_id": pending_id})
    finally:
        if original is None:
            os.environ.pop("ALLOW_MANUAL_JWT_CONSENT", None)
        else:
            os.environ["ALLOW_MANUAL_JWT_CONSENT"] = original
        importlib.reload(cfg)

    # With no session and manual JWT disabled, the page should NOT expose a JWT
    # input field or redirect to itself — it redirects to the Xelta auth start.
    # Accept either a redirect (to /auth/xelta/start) or an error page.
    assert r.status_code in (302, 400, 200)
    if r.status_code == 200:
        html = r.text.lower()
        assert "xelta_jwt" not in html, "JWT paste field must be absent in production mode"
        assert 'name="xelta_jwt"' not in r.text


@pytest.mark.asyncio
async def test_authorize_with_xelta_configured_start_redirects_to_xelta():
    """
    /auth/xelta/start when XELTA_AUTH_AUTHORIZE_URL is configured must
    redirect to the Xelta backend authorize URL (not to /consent).
    """
    original = os.environ.get("XELTA_AUTH_AUTHORIZE_URL")
    os.environ["XELTA_AUTH_AUTHORIZE_URL"] = "https://api.xelta.ai/api/auth/oauth/authorize"

    try:
        import config as cfg
        importlib.reload(cfg)

        asgi_app, _, _ = await _make_app()
        _, challenge = pkce_pair()

        async with AsyncClient(
            transport=ASGITransport(app=asgi_app),
            base_url="http://localhost",
            follow_redirects=False,
        ) as client:
            client_id, _ = await _register_client(client)
            pending_id = await _get_pending_id(client, client_id, challenge)

            r = await client.get(
                "/auth/xelta/start",
                params={"pending_id": pending_id},
            )
    finally:
        if original is None:
            os.environ.pop("XELTA_AUTH_AUTHORIZE_URL", None)
        else:
            os.environ["XELTA_AUTH_AUTHORIZE_URL"] = original
        importlib.reload(cfg)

    assert r.status_code == 302
    location = r.headers["location"]
    assert "api.xelta.ai" in location
    assert "response_type=code" in location
    assert "/consent" not in location


@pytest.mark.asyncio
async def test_xelta_callback_exchanges_code_with_backend(monkeypatch):
    """
    /auth/xelta/callback must POST to the Xelta token endpoint with the
    authorization code and redirect to /consent with the session cookie set.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    MOCK_JWT = (
        "eyJhbGciOiJIUzI1NiJ9"
        "." + base64.urlsafe_b64encode(
            json.dumps({"id": "u42", "email": "user@xelta.ai"}).encode()
        ).rstrip(b"=").decode()
        + ".mocksig"
    )

    mock_token_response = MagicMock()
    mock_token_response.raise_for_status = MagicMock()
    mock_token_response.json = MagicMock(return_value={"access_token": MOCK_JWT})

    mock_userinfo_response = MagicMock()
    mock_userinfo_response.raise_for_status = MagicMock()
    mock_userinfo_response.json = MagicMock(return_value={"id": "u42", "email": "user@xelta.ai"})

    original_authorize = os.environ.get("XELTA_AUTH_AUTHORIZE_URL")
    original_token = os.environ.get("XELTA_AUTH_TOKEN_URL")
    original_userinfo = os.environ.get("XELTA_AUTH_USERINFO_URL")
    original_client_id = os.environ.get("XELTA_AUTH_CLIENT_ID")
    original_client_secret = os.environ.get("XELTA_AUTH_CLIENT_SECRET")
    os.environ["XELTA_AUTH_AUTHORIZE_URL"] = "https://api.xelta.ai/api/auth/oauth/authorize"
    os.environ["XELTA_AUTH_TOKEN_URL"] = "https://api.xelta.ai/api/auth/oauth/token"
    os.environ["XELTA_AUTH_USERINFO_URL"] = "https://api.xelta.ai/api/auth/oauth/userinfo"
    os.environ["XELTA_AUTH_CLIENT_ID"] = "xelta-mcp-server"
    os.environ["XELTA_AUTH_CLIENT_SECRET"] = "test-secret"

    try:
        import config as cfg
        importlib.reload(cfg)

        asgi_app, storage, _ = await _make_app()

        # Insert a login state so the callback can validate it
        xelta_state = "test-xelta-state-abc"
        _, challenge = pkce_pair()
        async with AsyncClient(
            transport=ASGITransport(app=asgi_app),
            base_url="http://localhost",
            follow_redirects=False,
        ) as client:
            client_id, _ = await _register_client(client)
            pending_id = await _get_pending_id(client, client_id, challenge)

        await storage.save_login_state(
            state=xelta_state,
            pending_id=pending_id,
            expires_at=int(time.time()) + 1800,
        )

        mock_client_instance = AsyncMock()
        mock_client_instance.post = AsyncMock(return_value=mock_token_response)
        mock_client_instance.get = AsyncMock(return_value=mock_userinfo_response)
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client_instance):
            async with AsyncClient(
                transport=ASGITransport(app=asgi_app),
                base_url="http://localhost",
                follow_redirects=False,
            ) as client:
                r = await client.get(
                    "/auth/xelta/callback",
                    params={"code": "backend-auth-code", "state": xelta_state},
                )
    finally:
        for k, v in [
            ("XELTA_AUTH_AUTHORIZE_URL", original_authorize),
            ("XELTA_AUTH_TOKEN_URL", original_token),
            ("XELTA_AUTH_USERINFO_URL", original_userinfo),
            ("XELTA_AUTH_CLIENT_ID", original_client_id),
            ("XELTA_AUTH_CLIENT_SECRET", original_client_secret),
        ]:
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(cfg)

    # Should redirect to /consent with the session cookie set
    assert r.status_code == 302
    assert "/consent" in r.headers["location"]
    set_cookie = r.headers.get("set-cookie", "")
    assert "xelta_mcp_session" in set_cookie
    # JWT must not appear in the redirect URL
    assert MOCK_JWT not in r.headers["location"]
    assert "mocksig" not in r.headers["location"]


@pytest.mark.asyncio
async def test_html_response_never_contains_jwt():
    """
    Any HTML returned from /consent must not contain the raw JWT value.
    Covers both GET (render form) and error pages.
    """
    asgi_app, storage, _ = await _make_app()
    _, challenge = pkce_pair()
    session_id = secrets.token_urlsafe(32)

    async with AsyncClient(
        transport=ASGITransport(app=asgi_app),
        base_url="http://localhost",
        follow_redirects=False,
    ) as client:
        client_id, _ = await _register_client(client)
        pending_id = await _get_pending_id(client, client_id, challenge)
        await _insert_session(storage, session_id)

        r = await client.get(
            "/consent",
            params={"pending_id": pending_id},
            headers={"Cookie": f"xelta_mcp_session={session_id}"},
        )

    assert r.status_code == 200
    # The raw JWT must not appear anywhere in the rendered HTML
    assert FAKE_JWT not in r.text
    assert "fakesig" not in r.text
