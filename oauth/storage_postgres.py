"""PostgreSQL/SQLite-backed persistence for OAuth clients, codes, and tokens.

Supports both SQLite (for local dev) and PostgreSQL (for production).
Database type is auto-detected from OAUTH_DB_PATH:
  - postgresql:// or postgres:// → PostgreSQL
  - anything else → SQLite
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# SQL statements compatible with both SQLite and PostgreSQL
_TABLES_SQLITE = [
    """
    CREATE TABLE IF NOT EXISTS xelta_login_sessions (
        session_id TEXT    PRIMARY KEY,
        user_id    TEXT,
        email      TEXT,
        user_jwt   TEXT    NOT NULL,
        expires_at INTEGER NOT NULL,
        created_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS xelta_login_states (
        state      TEXT    PRIMARY KEY,
        pending_id TEXT    NOT NULL,
        expires_at INTEGER NOT NULL,
        created_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS oauth_clients (
        client_id                TEXT PRIMARY KEY,
        client_secret            TEXT,
        client_id_issued_at      INTEGER NOT NULL,
        client_secret_expires_at INTEGER,
        redirect_uris            TEXT    NOT NULL,
        grant_types              TEXT    NOT NULL,
        response_types           TEXT    NOT NULL,
        scope                    TEXT,
        token_endpoint_auth_method TEXT,
        client_name              TEXT,
        client_uri               TEXT,
        created_at               INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS oauth_auth_codes (
        code                         TEXT  PRIMARY KEY,
        client_id                    TEXT  NOT NULL,
        redirect_uri                 TEXT  NOT NULL,
        redirect_uri_provided_explicitly INTEGER NOT NULL DEFAULT 0,
        scope                        TEXT,
        resource                     TEXT,
        code_challenge               TEXT  NOT NULL,
        code_challenge_method        TEXT  NOT NULL DEFAULT 'S256',
        user_jwt                     TEXT  NOT NULL,
        expires_at                   REAL  NOT NULL,
        used                         INTEGER NOT NULL DEFAULT 0,
        created_at                   INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS oauth_access_tokens (
        token      TEXT    PRIMARY KEY,
        client_id  TEXT    NOT NULL,
        scope      TEXT,
        resource   TEXT,
        user_jwt   TEXT    NOT NULL,
        expires_at INTEGER,
        created_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
        token      TEXT    PRIMARY KEY,
        client_id  TEXT    NOT NULL,
        scope      TEXT,
        resource   TEXT,
        user_jwt   TEXT    NOT NULL,
        expires_at INTEGER,
        revoked    INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS oauth_device_codes (
        device_code TEXT PRIMARY KEY,
        user_code   TEXT NOT NULL UNIQUE,
        client_id   TEXT NOT NULL,
        scope       TEXT,
        resource    TEXT,
        user_jwt    TEXT,
        status      TEXT NOT NULL,
        interval    INTEGER NOT NULL,
        expires_at  INTEGER NOT NULL,
        created_at  INTEGER NOT NULL,
        updated_at  INTEGER NOT NULL
    )
    """,
]

_TABLES_POSTGRES = [
    """
    CREATE TABLE IF NOT EXISTS xelta_login_sessions (
        session_id TEXT    PRIMARY KEY,
        user_id    TEXT,
        email      TEXT,
        user_jwt   TEXT    NOT NULL,
        expires_at BIGINT NOT NULL,
        created_at BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS xelta_login_states (
        state      TEXT    PRIMARY KEY,
        pending_id TEXT    NOT NULL,
        expires_at BIGINT NOT NULL,
        created_at BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS oauth_clients (
        client_id                TEXT PRIMARY KEY,
        client_secret            TEXT,
        client_id_issued_at      BIGINT NOT NULL,
        client_secret_expires_at BIGINT,
        redirect_uris            TEXT    NOT NULL,
        grant_types              TEXT    NOT NULL,
        response_types           TEXT    NOT NULL,
        scope                    TEXT,
        token_endpoint_auth_method TEXT,
        client_name              TEXT,
        client_uri               TEXT,
        created_at               BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS oauth_auth_codes (
        code                         TEXT  PRIMARY KEY,
        client_id                    TEXT  NOT NULL,
        redirect_uri                 TEXT  NOT NULL,
        redirect_uri_provided_explicitly INTEGER NOT NULL DEFAULT 0,
        scope                        TEXT,
        resource                     TEXT,
        code_challenge               TEXT  NOT NULL,
        code_challenge_method        TEXT  NOT NULL DEFAULT 'S256',
        user_jwt                     TEXT  NOT NULL,
        expires_at                   DOUBLE PRECISION  NOT NULL,
        used                         INTEGER NOT NULL DEFAULT 0,
        created_at                   BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS oauth_access_tokens (
        token      TEXT    PRIMARY KEY,
        client_id  TEXT    NOT NULL,
        scope      TEXT,
        resource   TEXT,
        user_jwt   TEXT    NOT NULL,
        expires_at BIGINT,
        created_at BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
        token      TEXT    PRIMARY KEY,
        client_id  TEXT    NOT NULL,
        scope      TEXT,
        resource   TEXT,
        user_jwt   TEXT    NOT NULL,
        expires_at BIGINT,
        revoked    INTEGER NOT NULL DEFAULT 0,
        created_at BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS oauth_device_codes (
        device_code TEXT PRIMARY KEY,
        user_code   TEXT NOT NULL UNIQUE,
        client_id   TEXT NOT NULL,
        scope       TEXT,
        resource    TEXT,
        user_jwt    TEXT,
        status      TEXT NOT NULL,
        interval    INTEGER NOT NULL,
        expires_at  BIGINT NOT NULL,
        created_at  BIGINT NOT NULL,
        updated_at  BIGINT NOT NULL
    )
    """,
]


class OAuthStorage:
    """
    OAuth storage supporting both SQLite and PostgreSQL.
    
    Database type is auto-detected from db_path:
      - postgresql:// or postgres:// → PostgreSQL
      - anything else → SQLite
    """
    
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: Any = None  # aiosqlite.Connection or asyncpg.Pool
        self._init_lock = asyncio.Lock()
        self._initialized = False
        self._is_postgres = self._detect_postgres(db_path)
        
    def _detect_postgres(self, db_path: str) -> bool:
        """Detect if db_path is a PostgreSQL connection string."""
        try:
            parsed = urlparse(db_path)
            return parsed.scheme in ('postgresql', 'postgres', 'postgres+asyncpg')
        except Exception:
            return False

    async def ensure_initialized(self) -> None:
        """
        Idempotent lazy initialization of the database.
        
        Safe to call multiple times — returns immediately if already initialized.
        Uses an asyncio.Lock to prevent concurrent initialization races.
        """
        if self._initialized and self._db is not None:
            return

        async with self._init_lock:
            # Double-check after acquiring lock
            if self._initialized and self._db is not None:
                return

            if self._is_postgres:
                await self._init_postgres()
            else:
                await self._init_sqlite()
            
            self._initialized = True
            db_type = "PostgreSQL" if self._is_postgres else "SQLite"
            logger.info(f"OAuth {db_type} initialized at {self._db_path if not self._is_postgres else '[PostgreSQL]'}")

    async def _init_sqlite(self) -> None:
        """Initialize SQLite database."""
        import aiosqlite
        
        # Create parent directory if it doesn't exist
        db_path = Path(self._db_path)
        if str(db_path) != ":memory:":
            db_path.parent.mkdir(parents=True, exist_ok=True)

        # Connect to SQLite
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row

        # Enable WAL mode for better concurrency (except for :memory:)
        if str(db_path) != ":memory:":
            await self._db.execute("PRAGMA journal_mode=WAL")
        
        # Enable foreign keys
        await self._db.execute("PRAGMA foreign_keys=ON")

        # Create tables
        for stmt in _TABLES_SQLITE:
            await self._db.execute(stmt)
        await self._db.commit()

    async def _init_postgres(self) -> None:
        """Initialize PostgreSQL connection pool."""
        import asyncpg
        
        # Create connection pool
        self._db = await asyncpg.create_pool(
            self._db_path,
            min_size=2,
            max_size=10,
            command_timeout=60,
        )
        
        # Create tables
        async with self._db.acquire() as conn:
            for stmt in _TABLES_POSTGRES:
                await conn.execute(stmt)

    async def initialize(self) -> None:
        """Legacy initialization method — delegates to ensure_initialized()."""
        await self.ensure_initialized()

    async def close(self) -> None:
        """Close database connection."""
        if self._db:
            if self._is_postgres:
                await self._db.close()
            else:
                await self._db.close()
            self._db = None
            self._initialized = False

    # ── internal helpers ──────────────────────────────────────────────────────

    async def _fetchone(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        """Fetch one row from database (works for both SQLite and PostgreSQL)."""
        await self.ensure_initialized()
        
        if self._is_postgres:
            async with self._db.acquire() as conn:
                row = await conn.fetchrow(sql, *params)
                return dict(row) if row else None
        else:
            async with self._db.execute(sql, params) as cur:
                row = await cur.fetchone()
            return dict(row) if row else None

    async def _execute(self, sql: str, params: tuple = ()) -> None:
        """Execute a SQL statement (works for both SQLite and PostgreSQL)."""
        await self.ensure_initialized()
        
        if self._is_postgres:
            async with self._db.acquire() as conn:
                await conn.execute(sql, *params)
        else:
            await self._db.execute(sql, params)
            await self._db.commit()

    # ── clients ───────────────────────────────────────────────────────────────

    async def save_client(self, client_info: Any) -> None:
        await self.ensure_initialized()
        c = client_info
        
        if self._is_postgres:
            sql = """
                INSERT INTO oauth_clients
                    (client_id, client_secret, client_id_issued_at,
                     client_secret_expires_at, redirect_uris, grant_types,
                     response_types, scope, token_endpoint_auth_method,
                     client_name, client_uri, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                ON CONFLICT (client_id) DO UPDATE SET
                    client_secret = EXCLUDED.client_secret,
                    client_id_issued_at = EXCLUDED.client_id_issued_at,
                    client_secret_expires_at = EXCLUDED.client_secret_expires_at,
                    redirect_uris = EXCLUDED.redirect_uris,
                    grant_types = EXCLUDED.grant_types,
                    response_types = EXCLUDED.response_types,
                    scope = EXCLUDED.scope,
                    token_endpoint_auth_method = EXCLUDED.token_endpoint_auth_method,
                    client_name = EXCLUDED.client_name,
                    client_uri = EXCLUDED.client_uri
            """
        else:
            sql = """
                INSERT OR REPLACE INTO oauth_clients
                    (client_id, client_secret, client_id_issued_at,
                     client_secret_expires_at, redirect_uris, grant_types,
                     response_types, scope, token_endpoint_auth_method,
                     client_name, client_uri, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
        
        params = (
            c.client_id,
            c.client_secret,
            c.client_id_issued_at or int(time.time()),
            c.client_secret_expires_at,
            json.dumps([str(u) for u in (c.redirect_uris or [])]),
            json.dumps(list(c.grant_types or ["authorization_code", "refresh_token"])),
            json.dumps(list(c.response_types or ["code"])),
            c.scope,
            c.token_endpoint_auth_method,
            c.client_name,
            str(c.client_uri) if c.client_uri else None,
            int(time.time()),
        )
        
        await self._execute(sql, params)

    async def get_client(self, client_id: str) -> Any | None:
        from mcp.shared.auth import OAuthClientInformationFull
        from pydantic import AnyUrl

        if self._is_postgres:
            sql = "SELECT * FROM oauth_clients WHERE client_id = $1"
        else:
            sql = "SELECT * FROM oauth_clients WHERE client_id = ?"
        
        row = await self._fetchone(sql, (client_id,))
        if row is None:
            return None
        
        return OAuthClientInformationFull(
            client_id=row["client_id"],
            client_secret=row["client_secret"],
            client_id_issued_at=row["client_id_issued_at"],
            client_secret_expires_at=row["client_secret_expires_at"],
            redirect_uris=[AnyUrl(u) for u in json.loads(row["redirect_uris"])],
            grant_types=json.loads(row["grant_types"]),
            response_types=json.loads(row["response_types"]),
            scope=row["scope"],
            token_endpoint_auth_method=row["token_endpoint_auth_method"],
            client_name=row["client_name"],
            client_uri=row["client_uri"],
        )

    # ── auth codes ────────────────────────────────────────────────────────────

    async def save_auth_code(
        self,
        *,
        code: str,
        client_id: str,
        redirect_uri: str,
        redirect_uri_provided_explicitly: bool,
        scope: str | None,
        resource: str | None,
        code_challenge: str,
        code_challenge_method: str,
        user_jwt: str,
        expires_at: float,
    ) -> None:
        await self.ensure_initialized()
        
        if self._is_postgres:
            sql = """
                INSERT INTO oauth_auth_codes
                    (code, client_id, redirect_uri,
                     redirect_uri_provided_explicitly, scope, resource,
                     code_challenge, code_challenge_method,
                     user_jwt, expires_at, used, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 0, $11)
            """
        else:
            sql = """
                INSERT INTO oauth_auth_codes
                    (code, client_id, redirect_uri,
                     redirect_uri_provided_explicitly, scope, resource,
                     code_challenge, code_challenge_method,
                     user_jwt, expires_at, used, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """
        
        params = (
            code, client_id, redirect_uri,
            1 if redirect_uri_provided_explicitly else 0,
            scope, resource,
            code_challenge, code_challenge_method,
            user_jwt, expires_at, int(time.time()),
        )
        
        await self._execute(sql, params)

    async def get_auth_code(self, code: str) -> dict[str, Any] | None:
        if self._is_postgres:
            sql = "SELECT * FROM oauth_auth_codes WHERE code = $1"
        else:
            sql = "SELECT * FROM oauth_auth_codes WHERE code = ?"
        return await self._fetchone(sql, (code,))

    async def mark_code_used(self, code: str) -> None:
        if self._is_postgres:
            sql = "UPDATE oauth_auth_codes SET used = 1 WHERE code = $1"
        else:
            sql = "UPDATE oauth_auth_codes SET used = 1 WHERE code = ?"
        await self._execute(sql, (code,))

    # ── access tokens ─────────────────────────────────────────────────────────

    async def save_access_token(
        self,
        *,
        token: str,
        client_id: str,
        scope: str | None,
        resource: str | None,
        user_jwt: str,
        expires_at: int | None,
    ) -> None:
        await self.ensure_initialized()
        
        if self._is_postgres:
            sql = """
                INSERT INTO oauth_access_tokens
                    (token, client_id, scope, resource, user_jwt, expires_at, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
            """
        else:
            sql = """
                INSERT INTO oauth_access_tokens
                    (token, client_id, scope, resource, user_jwt, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """
        
        params = (token, client_id, scope, resource, user_jwt, expires_at, int(time.time()))
        await self._execute(sql, params)

    async def get_access_token(self, token: str) -> dict[str, Any] | None:
        if self._is_postgres:
            sql = "SELECT * FROM oauth_access_tokens WHERE token = $1"
        else:
            sql = "SELECT * FROM oauth_access_tokens WHERE token = ?"
        return await self._fetchone(sql, (token,))

    # ── refresh tokens ────────────────────────────────────────────────────────

    async def save_refresh_token(
        self,
        *,
        token: str,
        client_id: str,
        scope: str | None,
        resource: str | None,
        user_jwt: str,
        expires_at: int | None,
    ) -> None:
        await self.ensure_initialized()
        
        if self._is_postgres:
            sql = """
                INSERT INTO oauth_refresh_tokens
                    (token, client_id, scope, resource,
                     user_jwt, expires_at, revoked, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, 0, $7)
            """
        else:
            sql = """
                INSERT INTO oauth_refresh_tokens
                    (token, client_id, scope, resource,
                     user_jwt, expires_at, revoked, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?)
            """
        
        params = (token, client_id, scope, resource, user_jwt, expires_at, int(time.time()))
        await self._execute(sql, params)

    async def get_refresh_token(self, token: str) -> dict[str, Any] | None:
        if self._is_postgres:
            sql = "SELECT * FROM oauth_refresh_tokens WHERE token = $1 AND revoked = 0"
        else:
            sql = "SELECT * FROM oauth_refresh_tokens WHERE token = ? AND revoked = 0"
        return await self._fetchone(sql, (token,))

    async def revoke_refresh_token(self, token: str) -> None:
        if self._is_postgres:
            sql = "UPDATE oauth_refresh_tokens SET revoked = 1 WHERE token = $1"
        else:
            sql = "UPDATE oauth_refresh_tokens SET revoked = 1 WHERE token = ?"
        await self._execute(sql, (token,))

    async def delete_access_token(self, token: str) -> None:
        if self._is_postgres:
            sql = "DELETE FROM oauth_access_tokens WHERE token = $1"
        else:
            sql = "DELETE FROM oauth_access_tokens WHERE token = ?"
        await self._execute(sql, (token,))

    # ── Xelta login sessions ──────────────────────────────────────────────────

    async def save_device_code(
        self,
        *,
        device_code: str,
        user_code: str,
        client_id: str,
        scope: str | None,
        resource: str | None,
        interval: int,
        expires_at: int,
    ) -> None:
        now = int(time.time())
        if self._is_postgres:
            sql = """
                INSERT INTO oauth_device_codes
                    (device_code, user_code, client_id, scope, resource, user_jwt,
                     status, interval, expires_at, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, NULL, 'pending', $6, $7, $8, $9)
            """
        else:
            sql = """
                INSERT INTO oauth_device_codes
                    (device_code, user_code, client_id, scope, resource, user_jwt,
                     status, interval, expires_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, NULL, 'pending', ?, ?, ?, ?)
            """
        await self._execute(
            sql,
            (device_code, user_code, client_id, scope, resource, interval, expires_at, now, now),
        )

    async def get_device_code(self, device_code: str) -> dict[str, Any] | None:
        if self._is_postgres:
            sql = "SELECT * FROM oauth_device_codes WHERE device_code = $1"
        else:
            sql = "SELECT * FROM oauth_device_codes WHERE device_code = ?"
        return await self._fetchone(sql, (device_code,))

    async def get_device_code_by_user_code(self, user_code: str) -> dict[str, Any] | None:
        if self._is_postgres:
            sql = "SELECT * FROM oauth_device_codes WHERE user_code = $1"
        else:
            sql = "SELECT * FROM oauth_device_codes WHERE user_code = ?"
        return await self._fetchone(sql, (user_code,))

    async def approve_device_code(self, user_code: str, user_jwt: str) -> None:
        if self._is_postgres:
            sql = """
                UPDATE oauth_device_codes
                SET status = 'approved', user_jwt = $1, updated_at = $2
                WHERE user_code = $3 AND status = 'pending'
            """
        else:
            sql = """
                UPDATE oauth_device_codes
                SET status = 'approved', user_jwt = ?, updated_at = ?
                WHERE user_code = ? AND status = 'pending'
            """
        await self._execute(sql, (user_jwt, int(time.time()), user_code))

    async def deny_device_code(self, user_code: str) -> None:
        if self._is_postgres:
            sql = """
                UPDATE oauth_device_codes
                SET status = 'denied', updated_at = $1
                WHERE user_code = $2 AND status = 'pending'
            """
        else:
            sql = """
                UPDATE oauth_device_codes
                SET status = 'denied', updated_at = ?
                WHERE user_code = ? AND status = 'pending'
            """
        await self._execute(sql, (int(time.time()), user_code))

    async def consume_device_code(self, device_code: str) -> None:
        if self._is_postgres:
            sql = """
                UPDATE oauth_device_codes
                SET status = 'consumed', updated_at = $1
                WHERE device_code = $2
            """
        else:
            sql = """
                UPDATE oauth_device_codes
                SET status = 'consumed', updated_at = ?
                WHERE device_code = ?
            """
        await self._execute(sql, (int(time.time()), device_code))

    async def save_login_session(
        self,
        *,
        session_id: str,
        user_id: str,
        email: str,
        user_jwt: str,
        expires_at: int,
    ) -> None:
        await self.ensure_initialized()
        
        if self._is_postgres:
            sql = """
                INSERT INTO xelta_login_sessions
                    (session_id, user_id, email, user_jwt, expires_at, created_at)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (session_id) DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    email = EXCLUDED.email,
                    user_jwt = EXCLUDED.user_jwt,
                    expires_at = EXCLUDED.expires_at
            """
        else:
            sql = """
                INSERT OR REPLACE INTO xelta_login_sessions
                    (session_id, user_id, email, user_jwt, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """
        
        params = (session_id, user_id, email, user_jwt, expires_at, int(time.time()))
        await self._execute(sql, params)

    async def get_login_session(self, session_id: str) -> dict[str, Any] | None:
        if not session_id:
            return None
        
        if self._is_postgres:
            sql = "SELECT * FROM xelta_login_sessions WHERE session_id = $1 AND expires_at > $2"
        else:
            sql = "SELECT * FROM xelta_login_sessions WHERE session_id = ? AND expires_at > ?"
        
        return await self._fetchone(sql, (session_id, int(time.time())))

    async def delete_login_session(self, session_id: str) -> None:
        if self._is_postgres:
            sql = "DELETE FROM xelta_login_sessions WHERE session_id = $1"
        else:
            sql = "DELETE FROM xelta_login_sessions WHERE session_id = ?"
        await self._execute(sql, (session_id,))

    # ── Xelta login states (one-time OAuth state tokens) ─────────────────────

    async def save_login_state(
        self,
        *,
        state: str,
        pending_id: str,
        expires_at: int,
    ) -> None:
        await self.ensure_initialized()
        
        if self._is_postgres:
            sql = """
                INSERT INTO xelta_login_states
                    (state, pending_id, expires_at, created_at)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (state) DO UPDATE SET
                    pending_id = EXCLUDED.pending_id,
                    expires_at = EXCLUDED.expires_at
            """
        else:
            sql = """
                INSERT OR REPLACE INTO xelta_login_states
                    (state, pending_id, expires_at, created_at)
                VALUES (?, ?, ?, ?)
            """
        
        params = (state, pending_id, expires_at, int(time.time()))
        await self._execute(sql, params)

    async def get_login_state(self, state: str) -> dict[str, Any] | None:
        if self._is_postgres:
            sql = "SELECT * FROM xelta_login_states WHERE state = $1 AND expires_at > $2"
        else:
            sql = "SELECT * FROM xelta_login_states WHERE state = ? AND expires_at > ?"
        return await self._fetchone(sql, (state, int(time.time())))

    async def delete_login_state(self, state: str) -> None:
        if self._is_postgres:
            sql = "DELETE FROM xelta_login_states WHERE state = $1"
        else:
            sql = "DELETE FROM xelta_login_states WHERE state = ?"
        await self._execute(sql, (state,))

    # ── Health check ──────────────────────────────────────────────────────────

    async def health_check(self) -> dict[str, Any]:
        """
        Check database health and return status information.
        
        Returns:
            dict with keys:
                - oauth_db_initialized: bool
                - oauth_db_type: str (sqlite or postgresql)
                - oauth_db_path: str (only for SQLite)
        """
        try:
            await self.ensure_initialized()
            
            # Simple query to verify DB is working
            if self._is_postgres:
                async with self._db.acquire() as conn:
                    await conn.fetchval("SELECT 1")
            else:
                async with self._db.execute("SELECT 1") as cur:
                    await cur.fetchone()
            
            result = {
                "oauth_db_initialized": True,
                "oauth_db_type": "postgresql" if self._is_postgres else "sqlite",
            }
            
            if not self._is_postgres:
                result["oauth_db_path"] = self._db_path if self._db_path != ":memory:" else ":memory:"
            
            return result
            
        except Exception as e:
            logger.error(f"OAuth storage health check failed: {e}")
            return {
                "oauth_db_initialized": False,
                "error": str(e),
            }
