"""OAuth 2.1 and RFC 9728 Protected Resource Implementation for ChatGPT and Remote Agents.

Implements MCP 2025-11-25 Authorization Specification:
- RFC 9728: OAuth 2.0 Protected Resource Metadata
- RFC 8414: OAuth 2.0 Authorization Server Metadata
- RFC 7591: Dynamic Client Registration (DCR)
- RFC 7636: Proof Key for Code Exchange (PKCE S256)
- Embedded Local Authorization Server & SQLite Persistent Storage
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import html
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .env import get_runtime_dir


def base64url_encode(data: bytes) -> str:
    """Encode bytes to base64url string without padding."""
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def verify_pkce(code_verifier: str, code_challenge: str, method: str = "S256") -> bool:
    """Verify PKCE code_verifier against code_challenge."""
    if method == "plain":
        return code_verifier == code_challenge
    elif method == "S256":
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        computed = base64url_encode(digest)
        return computed == code_challenge
    return False


def get_client_ip(request: Request) -> str:
    """Extract real client IP considering reverse proxy and Cloudflare headers."""
    cf_connecting_ip = request.headers.get("cf-connecting-ip")
    if cf_connecting_ip:
        return cf_connecting_ip.strip()
    x_forwarded_for = request.headers.get("x-forwarded-for")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else "127.0.0.1"


class AuthRateLimiter:
    """Sliding-window IP rate limiter with lockout penalty for failed authentications."""

    def __init__(
        self,
        max_attempts: int = 5,
        window_seconds: float = 600.0,
        lockout_seconds: float = 900.0,
    ) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.lockout_seconds = lockout_seconds
        self._lockouts: dict[str, float] = {}
        self._failures: dict[str, list[float]] = {}
        self._dcr_counts: dict[str, list[float]] = {}

    def is_locked_out(self, ip: str) -> tuple[bool, int]:
        now = time.time()
        if ip in self._lockouts:
            unlock_at = self._lockouts[ip]
            if now < unlock_at:
                return True, int(unlock_at - now)
            else:
                del self._lockouts[ip]
                self._failures.pop(ip, None)
        return False, 0

    def record_failure(self, ip: str) -> tuple[bool, int, int]:
        now = time.time()
        attempts = self._failures.setdefault(ip, [])
        attempts = [t for t in attempts if now - t < self.window_seconds]
        attempts.append(now)
        self._failures[ip] = attempts

        if len(attempts) >= self.max_attempts:
            self._lockouts[ip] = now + self.lockout_seconds
            return True, int(self.lockout_seconds), 0
        return False, 0, max(0, self.max_attempts - len(attempts))

    def record_success(self, ip: str) -> None:
        self._failures.pop(ip, None)
        self._lockouts.pop(ip, None)

    def check_dcr_rate_limit(self, ip: str, max_dcr_per_hour: int = 15) -> bool:
        now = time.time()
        records = self._dcr_counts.setdefault(ip, [])
        records = [t for t in records if now - t < 3600.0]
        if len(records) >= max_dcr_per_hour:
            self._dcr_counts[ip] = records
            return False
        records.append(now)
        self._dcr_counts[ip] = records
        return True


class LocalOAuthServer:
    """Embedded OAuth 2.1 server with native SQLite persistence, DCR, and PKCE verification."""

    def __init__(self, admin_password: str, db_path: Path | str | None = None) -> None:
        self.admin_password = admin_password
        if db_path:
            self.db_path = str(db_path)
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        else:
            runtime_dir = get_runtime_dir()
            self.db_path = str(runtime_dir / "oauth_store.db")

        self.rate_limiter = AuthRateLimiter()
        self._init_db()

    @contextmanager
    def _db_conn(self):
        """Context manager yielding SQLite connection with auto-commit and guaranteed close."""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._db_conn() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS oauth_clients (
                client_id TEXT PRIMARY KEY,
                client_secret TEXT,
                client_name TEXT,
                redirect_uris TEXT,
                grant_types TEXT,
                response_types TEXT,
                created_at REAL
            );
            CREATE TABLE IF NOT EXISTS oauth_tokens (
                access_token TEXT PRIMARY KEY,
                token_type TEXT,
                expires_in INTEGER,
                expires_at REAL,
                created_at REAL,
                scope TEXT,
                client_id TEXT
            );
            CREATE TABLE IF NOT EXISTS oauth_codes (
                code TEXT PRIMARY KEY,
                client_id TEXT,
                redirect_uri TEXT,
                code_challenge TEXT,
                code_challenge_method TEXT,
                scope TEXT,
                expires_at REAL,
                created_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_tokens_expires_at ON oauth_tokens(expires_at);
            CREATE INDEX IF NOT EXISTS idx_codes_expires_at ON oauth_codes(expires_at);
            """)

            now = time.time()
            conn.execute("DELETE FROM oauth_codes WHERE expires_at < ?", (now,))
            conn.execute("DELETE FROM oauth_tokens WHERE expires_at < ?", (now,))

            conn.execute("""
            INSERT OR IGNORE INTO oauth_clients (client_id, client_secret, client_name, redirect_uris, grant_types, response_types, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                "chatgpt",
                self.admin_password,
                "ChatGPT Pre-registered",
                "[]",
                '["authorization_code", "refresh_token"]',
                '["code"]',
                now,
            ))

    def register_client(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """RFC 7591 Dynamic Client Registration with automatic storage in SQLite."""
        client_id = f"mcp_client_{secrets.token_urlsafe(12)}"
        client_secret = f"mcp_sec_{secrets.token_urlsafe(24)}"
        record = {
            "client_id": client_id,
            "client_secret": client_secret,
            "client_name": metadata.get("client_name", "ChatGPT"),
            "redirect_uris": metadata.get("redirect_uris", []),
            "grant_types": metadata.get("grant_types", ["authorization_code", "refresh_token"]),
            "response_types": metadata.get("response_types", ["code"]),
            "created_at": time.time(),
        }
        with self._db_conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0]
            if total >= 200:
                conn.execute("""
                DELETE FROM oauth_clients WHERE client_id != 'chatgpt'
                AND client_id IN (
                    SELECT client_id FROM oauth_clients WHERE client_id != 'chatgpt' ORDER BY created_at ASC LIMIT 20
                )
                """)

            conn.execute("""
            INSERT INTO oauth_clients (client_id, client_secret, client_name, redirect_uris, grant_types, response_types, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                client_id,
                client_secret,
                record["client_name"],
                json.dumps(record["redirect_uris"]),
                json.dumps(record["grant_types"]),
                json.dumps(record["response_types"]),
                record["created_at"],
            ))
        return record

    def create_auth_code(
        self,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        code_challenge_method: str = "S256",
        scope: str = "",
    ) -> str:
        """Issue an authorization code stored safely in SQLite."""
        code = f"auth_code_{secrets.token_urlsafe(24)}"
        now = time.time()
        expires_at = now + 600  # 10 minutes
        with self._db_conn() as conn:
            conn.execute("""
            INSERT INTO oauth_codes (code, client_id, redirect_uri, code_challenge, code_challenge_method, scope, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (code, client_id, redirect_uri, code_challenge, code_challenge_method, scope, expires_at, now))
        return code

    def exchange_code_for_token(
        self,
        code: str,
        client_id: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> dict[str, Any]:
        """Exchange authorization code and PKCE verifier for a persistent access token."""
        now = time.time()
        with self._db_conn() as conn:
            row = conn.execute("SELECT * FROM oauth_codes WHERE code = ?", (code,)).fetchone()
            if not row:
                raise ValueError("Invalid or expired authorization code")
            auth_data = dict(row)

            if now > auth_data["expires_at"]:
                conn.execute("DELETE FROM oauth_codes WHERE code = ?", (code,))
                raise ValueError("Authorization code has expired")

            if client_id and auth_data["client_id"] != client_id:
                raise ValueError("client_id mismatch")

            challenge = auth_data["code_challenge"]
            method = auth_data.get("code_challenge_method", "S256")
            if challenge and not verify_pkce(code_verifier, challenge, method):
                raise ValueError("PKCE code verification failed")

            # Invalidate used code
            conn.execute("DELETE FROM oauth_codes WHERE code = ?", (code,))

            access_token = f"mcp_tok_{secrets.token_urlsafe(32)}"
            expires_in = 86400 * 30  # 30 days
            expires_at = now + expires_in
            scope = auth_data.get("scope") or "mcp:read mcp:write"

            conn.execute("""
            INSERT INTO oauth_tokens (access_token, token_type, expires_in, expires_at, created_at, scope, client_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (access_token, "Bearer", expires_in, expires_at, now, scope, auth_data["client_id"]))

            return {
                "access_token": access_token,
                "token_type": "Bearer",
                "expires_in": expires_in,
                "created_at": now,
                "scope": scope,
                "client_id": auth_data["client_id"],
            }

    def is_token_valid(self, token: str) -> bool:
        """Check whether an issued access token or static admin password is valid."""
        if not token:
            return False
        if self.admin_password and token == self.admin_password:
            return True
        now = time.time()
        with self._db_conn() as conn:
            row = conn.execute("SELECT expires_at FROM oauth_tokens WHERE access_token = ?", (token,)).fetchone()
            if row:
                if now < row["expires_at"]:
                    return True
                else:
                    conn.execute("DELETE FROM oauth_tokens WHERE access_token = ?", (token,))
        return False


def get_server_public_url(request: Request) -> str:
    """Determine public base URL honoring reverse proxy headers and env overrides."""
    override = os.getenv("PUBLIC_BASE_URL")
    if override:
        return override.rstrip("/")

    forwarded_proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    forwarded_host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{forwarded_proto}://{forwarded_host}".rstrip("/")


async def oauth_protected_resource_endpoint(request: Request) -> Response:
    """RFC 9728 OAuth Protected Resource Metadata document for ChatGPT."""
    public_url = get_server_public_url(request)
    auth0_domain = os.getenv("AUTH0_DOMAIN", "").replace("https://", "").rstrip("/")
    issuer = f"https://{auth0_domain}" if auth0_domain else public_url

    req_resource = request.query_params.get("resource")
    if req_resource:
        resource_uri = req_resource
    elif request.url.path.rstrip("/").endswith("/mcp"):
        resource_uri = f"{public_url}/mcp"
    else:
        resource_uri = f"{public_url}/mcp"

    metadata = {
        "resource": resource_uri,
        "authorization_servers": [issuer],
        "scopes_supported": ["openid", "profile", "email", "mcp:read", "mcp:write"],
        "resource_documentation": f"{public_url}/",
    }
    return JSONResponse(metadata, headers={"Access-Control-Allow-Origin": "*"})


async def oauth_authorization_server_metadata_endpoint(request: Request) -> Response:
    """RFC 8414 OAuth 2.0 Authorization Server Metadata document for ChatGPT."""
    public_url = get_server_public_url(request)

    metadata = {
        "issuer": public_url,
        "authorization_endpoint": f"{public_url}/oauth/authorize",
        "token_endpoint": f"{public_url}/oauth/token",
        "registration_endpoint": f"{public_url}/oauth/register",
        "jwks_uri": f"{public_url}/oauth/jwks.json",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256", "plain"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
        "scopes_supported": ["openid", "profile", "email", "mcp:read", "mcp:write"],
        "subject_types_supported": ["public"],
    }
    return JSONResponse(metadata, headers={"Access-Control-Allow-Origin": "*"})


async def oauth_jwks_endpoint(request: Request) -> Response:
    """JWKS endpoint returning empty or public keys for OIDC validation."""
    return JSONResponse({"keys": []}, headers={"Access-Control-Allow-Origin": "*"})


async def oauth_register_endpoint(request: Request, oauth_server: LocalOAuthServer) -> Response:
    """RFC 7591 Dynamic Client Registration endpoint for ChatGPT with rate limiting."""
    client_ip = get_client_ip(request)
    if not oauth_server.rate_limiter.check_dcr_rate_limit(client_ip):
        return JSONResponse(
            {"error": "rate_limit_exceeded", "error_description": "Too many client registration requests. Please wait."},
            status_code=429,
            headers={"Access-Control-Allow-Origin": "*", "Retry-After": "3600"},
        )
    try:
        body = await request.json()
    except Exception:
        body = {}

    client_info = oauth_server.register_client(body)
    return JSONResponse(client_info, status_code=201, headers={"Access-Control-Allow-Origin": "*"})


async def oauth_authorize_endpoint(request: Request, oauth_server: LocalOAuthServer) -> Response:
    """Handle OAuth 2.1 authorization consent page and code generation with brute-force defense."""
    client_ip = get_client_ip(request)
    is_locked, remaining_seconds = oauth_server.rate_limiter.is_locked_out(client_ip)
    if is_locked:
        err_msg = f"❌ 连续输入错误密码过多，IP 已被安全锁定。请在 {remaining_seconds} 秒后再试！"
        html_page = _render_consent_html(
            error_msg=err_msg,
            client_id=request.query_params.get("client_id", ""),
            redirect_uri=request.query_params.get("redirect_uri", ""),
            scope=request.query_params.get("scope", ""),
            state=request.query_params.get("state", ""),
            code_challenge=request.query_params.get("code_challenge", ""),
            code_challenge_method=request.query_params.get("code_challenge_method", "S256"),
        )
        return HTMLResponse(html_page, status_code=429, headers={"Retry-After": str(remaining_seconds)})

    params = request.query_params
    response_type = params.get("response_type", "code")
    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    scope = params.get("scope", "")
    state = params.get("state", "")
    code_challenge = params.get("code_challenge", "")
    code_challenge_method = params.get("code_challenge_method", "S256")

    if request.method == "POST":
        form = await request.form()
        password = str(form.get("password", "")).strip()
        f_client_id = str(form.get("client_id", client_id))
        f_redirect_uri = str(form.get("redirect_uri", redirect_uri))
        f_scope = str(form.get("scope", scope))
        f_state = str(form.get("state", state))
        f_challenge = str(form.get("code_challenge", code_challenge))
        f_method = str(form.get("code_challenge_method", code_challenge_method))

        if oauth_server.admin_password and password != oauth_server.admin_password:
            is_locked, lockout_secs, remaining_attempts = oauth_server.rate_limiter.record_failure(client_ip)
            if is_locked:
                err_text = f"❌ 密码错误次数超限！IP 已触发锁定保护，请在 {lockout_secs} 秒后重试。"
                status_code = 429
                headers = {"Retry-After": str(lockout_secs)}
            else:
                err_text = f"❌ 访问密码错误！还可尝试 {remaining_attempts} 次。"
                status_code = 401
                headers = {}

            error_html = _render_consent_html(
                error_msg=err_text,
                client_id=f_client_id,
                redirect_uri=f_redirect_uri,
                scope=f_scope,
                state=f_state,
                code_challenge=f_challenge,
                code_challenge_method=f_method,
            )
            return HTMLResponse(error_html, status_code=status_code, headers=headers)

        oauth_server.rate_limiter.record_success(client_ip)

        code = oauth_server.create_auth_code(
            client_id=f_client_id,
            redirect_uri=f_redirect_uri,
            code_challenge=f_challenge,
            code_challenge_method=f_method,
            scope=f_scope,
        )

        query_parts = {"code": code}
        if f_state:
            query_parts["state"] = f_state

        sep = "&" if "?" in f_redirect_uri else "?"
        callback_url = f"{f_redirect_uri}{sep}{urlencode(query_parts)}"
        return RedirectResponse(callback_url, status_code=302)

    content = _render_consent_html(
        error_msg="",
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=scope,
        state=state,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
    )
    return HTMLResponse(content)


async def oauth_token_endpoint(request: Request, oauth_server: LocalOAuthServer) -> Response:
    """RFC 6749 & RFC 7636 Token Endpoint with PKCE verification."""
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            data = await request.json()
        except Exception:
            data = {}
    else:
        form = await request.form()
        data = dict(form)

    grant_type = data.get("grant_type")
    code = data.get("code", "")
    client_id = data.get("client_id", "")
    redirect_uri = data.get("redirect_uri", "")
    code_verifier = data.get("code_verifier", "")

    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            if ":" in decoded:
                client_id = decoded.split(":")[0]
        except Exception:
            pass

    if grant_type != "authorization_code":
        return JSONResponse(
            {"error": "unsupported_grant_type", "error_description": "Only authorization_code is supported"},
            status_code=400,
            headers={"Access-Control-Allow-Origin": "*"},
        )

    try:
        token_info = oauth_server.exchange_code_for_token(
            code=code,
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
        )
        return JSONResponse(token_info, headers={"Access-Control-Allow-Origin": "*"})
    except ValueError as e:
        return JSONResponse(
            {"error": "invalid_grant", "error_description": str(e)},
            status_code=400,
            headers={"Access-Control-Allow-Origin": "*"},
        )


def _render_consent_html(
    error_msg: str,
    client_id: str,
    redirect_uri: str,
    scope: str,
    state: str,
    code_challenge: str,
    code_challenge_method: str,
) -> str:
    """Render clean, responsive HTML consent page for ChatGPT."""
    err_block = f'<div style="color: #ef4444; background: #fee2e2; border: 1px solid #f87171; padding: 10px; border-radius: 6px; margin-bottom: 16px; font-size: 14px;">{html.escape(error_msg)}</div>' if error_msg else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ChatGPT / MCP 授权登录</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background: #f3f4f6;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            margin: 0;
            padding: 16px;
        }}
        .card {{
            background: #ffffff;
            border-radius: 12px;
            box-shadow: 0 10px 25px rgba(0, 0, 0, 0.08);
            width: 100%;
            max-width: 440px;
            padding: 32px;
            box-sizing: border-box;
        }}
        h2 {{
            margin-top: 0;
            color: #111827;
            font-size: 22px;
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        p {{
            color: #4b5563;
            font-size: 14px;
            line-height: 1.5;
        }}
        .badge {{
            background: #e0f2fe;
            color: #0369a1;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 12px;
            font-weight: 600;
        }}
        label {{
            display: block;
            margin-bottom: 6px;
            font-size: 14px;
            color: #374151;
            font-weight: 500;
        }}
        input[type="password"] {{
            width: 100%;
            padding: 10px 12px;
            border: 1px solid #d1d5db;
            border-radius: 6px;
            font-size: 15px;
            box-sizing: border-box;
            outline: none;
            transition: border-color 0.2s;
        }}
        input[type="password"]:focus {{
            border-color: #2563eb;
            box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.15);
        }}
        button {{
            width: 100%;
            background: #10a37f;
            color: #ffffff;
            border: none;
            padding: 12px;
            border-radius: 6px;
            font-size: 15px;
            font-weight: 600;
            cursor: pointer;
            margin-top: 20px;
            transition: background 0.2s;
        }}
        button:hover {{
            background: #0e8c6d;
        }}
        .footer {{
            margin-top: 20px;
            font-size: 12px;
            color: #9ca3af;
            text-align: center;
        }}
    </style>
</head>
<body>
    <div class="card">
        <h2>🚀 ChatGPT / MCP 安全授权</h2>
        <p>远程 <b>ChatGPT</b> / AI Agent 客户端正在请求连接本机 <b>MCP Server</b>，请验证管理员访问密码以完成授权绑定。</p>
        <p><span class="badge">安全隔离</span> 权限仅对经过授权的会话生效。</p>
        
        {err_block}
        
        <form method="POST">
            <input type="hidden" name="client_id" value="{html.escape(client_id)}">
            <input type="hidden" name="redirect_uri" value="{html.escape(redirect_uri)}">
            <input type="hidden" name="scope" value="{html.escape(scope)}">
            <input type="hidden" name="state" value="{html.escape(state)}">
            <input type="hidden" name="code_challenge" value="{html.escape(code_challenge)}">
            <input type="hidden" name="code_challenge_method" value="{html.escape(code_challenge_method)}">
            
            <label for="password">请输入本机安全访问密钥 (API Key):</label>
            <input type="password" id="password" name="password" required autofocus placeholder="输入控制台或 .env 中的访问密钥">
            
            <button type="submit">确认授权客户端访问</button>
        </form>
        
        <div class="footer">
            Local MCP Server • RFC 9728 & OAuth 2.1
        </div>
    </div>
</body>
</html>"""
