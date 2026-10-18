"""ASGI middleware: authentication, CSRF, rate and size limits, read-only servers, audit.

Pure ASGI (no BaseHTTPMiddleware) so the context variables it sets - the
current fbadmin user and client address - reach the endpoints, which run in
a thread pool.
"""

from __future__ import annotations

import json
import re
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import urlsplit

from .readonly import blocked_request, write_statements
from .sessions import Principal, current_client_ip, current_principal

SESSION_COOKIE = "fbadmin_session"
CSRF_COOKIE = "fbadmin_csrf"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
PUBLIC_API = re.compile(r"^/api/(health$|auth/(status|login|setup|logout|oidc/.*)$)")
SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"same-origin"),
]

_DB_EXEC = re.compile(r"^/api/servers/([^/]+)/databases/([^/]+)/execute$")
_SESS = re.compile(r"^/api/query/sessions/([^/]+)/(execute|commit|rollback|options)$")
_GRID = re.compile(r"^/api/servers/([^/]+)/databases/([^/]+)/tables/([^/]+)/data$")
_DROP = re.compile(r"^/api/servers/([^/]+)/databases/([^/]+)/objects/([^/]+)/([^/]+)$")
_SRV = re.compile(r"^/api/servers/([^/]+)(?:/databases/([^/]+))?(/.*)?$")
_SELF_AUDITED = re.compile(r"^/api/servers/[^/]+/(fbusers|databases/[^/]+/security)(/|$)")
_NOT_AUDITED_ACTIONS = {"connect", "disconnect", "fetch", "explain", "params", "completion", "cancel",
                        "statistics", "preview", "search", "export"}


def _json(status: int, detail: str, code: str, headers: dict[str, str] | None = None):
    body = json.dumps({"detail": detail, "code": code}).encode()
    hdrs = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    hdrs += [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return status, hdrs, body


def _cookie_header(name: str, value: str, *, max_age: int, http_only: bool, secure: bool, same_site: str) -> bytes:
    parts = [f"{name}={value}", "Path=/", f"Max-Age={max_age}", f"SameSite={same_site}"]
    if http_only:
        parts.append("HttpOnly")
    if secure:
        parts.append("Secure")
    return "; ".join(parts).encode()


def session_cookies(sec, session, secure: bool) -> list[tuple[bytes, bytes]]:
    age = sec.settings.session_lifetime
    return [
        (b"set-cookie", _cookie_header(SESSION_COOKIE, session.id, max_age=age, http_only=True, secure=secure,
                                       same_site="Lax")),
        (b"set-cookie", _cookie_header(CSRF_COOKIE, session.csrf, max_age=age, http_only=False, secure=secure,
                                       same_site="Strict")),
    ]


def clear_cookies(secure: bool) -> list[tuple[bytes, bytes]]:
    return [
        (b"set-cookie", _cookie_header(SESSION_COOKIE, "", max_age=0, http_only=True, secure=secure, same_site="Lax")),
        (b"set-cookie", _cookie_header(CSRF_COOKIE, "", max_age=0, http_only=False, secure=secure,
                                       same_site="Strict")),
    ]


def is_secure(sec, scheme: str) -> bool:
    v = sec.settings.cookie_secure
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return scheme in ("https", "wss")


class _Req:
    def __init__(self, scope):
        self.scope = scope
        self.method: str = scope["method"]
        self.path: str = scope["path"]
        self.headers: dict[str, str] = {}
        for k, v in scope.get("headers") or []:
            key = k.decode("latin-1").lower()
            val = v.decode("latin-1")
            self.headers[key] = self.headers[key] + "; " + val if key == "cookie" and key in self.headers else val
        client = scope.get("client")
        self.ip: str | None = client[0] if client else None
        self.scheme: str = scope.get("scheme", "http")
        c = SimpleCookie()
        try:
            c.load(self.headers.get("cookie", ""))
        except Exception:  # noqa: BLE001
            pass
        self.cookies = {k: m.value for k, m in c.items()}

    def cross_site(self) -> bool:
        site = self.headers.get("sec-fetch-site")
        if site:
            return site == "cross-site"
        origin = self.headers.get("origin")
        if not origin or origin == "null":
            return origin == "null"
        host = self.headers.get("x-forwarded-host") or self.headers.get("host", "")
        return urlsplit(origin).netloc.lower() != host.lower()


class SecurityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        from ..api.deps import get_state

        st = get_state()
        sec = st.security
        req = _Req(scope)
        tokens = [current_client_ip.set(req.ip)]
        extra_headers: list[tuple[bytes, bytes]] = list(SECURITY_HEADERS)
        try:
            await self._handle(st, sec, req, scope, receive, send, extra_headers, tokens)
        finally:
            for t in reversed(tokens):
                t.var.reset(t)

    async def _reply(self, send, response, extra_headers):
        status, hdrs, body = response
        await send({"type": "http.response.start", "status": status, "headers": hdrs + extra_headers})
        await send({"type": "http.response.body", "body": body})

    async def _handle(self, st, sec, req: _Req, scope, receive, send, extra_headers, tokens):
        is_api = req.path.startswith("/api/")
        unsafe = req.method not in SAFE_METHODS
        if is_api and unsafe and req.cross_site():
            await self._reply(send, _json(403, "Cross-site request refused", "csrf_failed"), extra_headers)
            return

        principal: Principal | None = None
        if sec.server_mode and is_api:
            length = req.headers.get("content-length")
            if length and length.isdigit() and int(length) > sec.settings.max_request_size:
                await self._reply(send, _json(413, "Request too large", "too_large"), extra_headers)
                return
            session = sec.sessions.get(req.cookies.get(SESSION_COOKIE))
            new_session = False
            if "proxy" in sec.settings.auth_sources and sec.settings.proxy.is_trusted(req.ip):
                hdr_user = req.headers.get(sec.settings.proxy.header.lower(), "").strip()
                if hdr_user and (session is None or session.principal.username.lower() != hdr_user.lower()):
                    from .proxy import principal_from_headers

                    try:
                        p = principal_from_headers(sec, req.headers, hdr_user)
                    except Exception as exc:  # noqa: BLE001
                        code = getattr(exc, "code", "forbidden")
                        await self._reply(send, _json(getattr(exc, "status", 403), str(exc), code), extra_headers)
                        return
                    if session is not None:
                        sec.end_session(session.id)
                    session = sec.start_session(p)
                    new_session = True
                    extra_headers += session_cookies(sec, session, is_secure(sec, req.scheme))
            public = bool(PUBLIC_API.match(req.path))
            if session is None and not public:
                await self._reply(send, _json(401, "Login required", "auth_required"), extra_headers)
                return
            if session is not None:
                principal = session.principal
                wait = sec.api_limiter.hit(f"u:{principal.user_id}")
                if wait:
                    await self._reply(send, _json(429, "Too many requests", "rate_limited",
                                                  {"Retry-After": str(int(wait))}), extra_headers)
                    return
                if unsafe and req.path not in ("/api/auth/login", "/api/auth/setup"):
                    token = req.headers.get(CSRF_HEADER)
                    if new_session or not token or token != session.csrf:
                        await self._reply(send, _json(403, "Missing or invalid CSRF token - reload the page",
                                                      "csrf_failed"), extra_headers)
                        return
                tokens.append(current_principal.set(principal))

        # ---- read-only servers and audit (server and query-session endpoints only)
        audit_ctx: dict[str, Any] | None = None
        body = b""
        body_read = False
        if is_api and unsafe and (req.path.startswith("/api/servers/") or req.path.startswith("/api/query/")):
            body, too_large = await _read_body(receive, sec.settings.max_request_size if sec.server_mode else None)
            body_read = True
            if too_large:
                await self._reply(send, _json(413, "Request too large", "too_large"), extra_headers)
                return
            try:
                refusal, audit_ctx = self._inspect(st, sec, req, body)
            except Exception:  # noqa: BLE001 - unknown ids etc.: let the endpoint answer
                refusal, audit_ctx = None, None
            if refusal:
                await self._reply(send, _json(403, refusal, "read_only"), extra_headers)
                return

        if body_read:
            sent = False

            async def replay():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            app_receive = replay
        elif sec.server_mode and is_api and unsafe:
            app_receive = _limited_receive(receive, sec.settings.max_request_size)
        else:
            app_receive = receive

        capture = audit_ctx is not None and audit_ctx.get("capture")
        status_box = {"status": 0}
        chunks: list[bytes] = []

        async def wrapped_send(message):
            if message["type"] == "http.response.start":
                status_box["status"] = message["status"]
                message = {**message, "headers": list(message.get("headers") or []) + extra_headers}
            elif message["type"] == "http.response.body" and capture:
                chunks.append(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, app_receive, wrapped_send)
        except _TooLarge:
            await self._reply(send, _json(413, "Request too large", "too_large"), extra_headers)
            return
        if audit_ctx is not None:
            self._audit(sec, audit_ctx, status_box["status"], b"".join(chunks), body)

    # ------------------------------------------------------------------ inspection
    def _inspect(self, st, sec, req: _Req, body: bytes) -> tuple[str | None, dict[str, Any] | None]:
        path, method = req.path, req.method
        server_id = db_id = None
        sess = None
        m = _SESS.match(path)
        if m:
            sess = st.manager.session(m.group(1))
            server_id, db_id = sess.server_id, sess.db_id
        else:
            m = _SRV.match(path)
            if m:
                server_id, db_id = m.group(1), m.group(2)
        srv = None
        if server_id:
            try:
                srv = st.store.get(server_id)
            except KeyError:
                srv = None
        if db_id and (srv is None or all(d.id != db_id for d in srv.databases)):
            db_id = None  # e.g. /databases/create

        # read-only enforcement
        if srv is not None and srv.read_only:
            _, reason = blocked_request(method, path)
            if reason:
                return reason, None
            if path.endswith("/execute"):
                sql = _json_field(body, "sql")
                bad = write_statements(sql or "")
                if bad:
                    first = bad[0].strip().splitlines()[0][:120] if bad[0].strip() else ""
                    return (f"Server is registered as read-only: only queries are allowed "
                            f"(refused: {first})"), None

        if not sec.audit.enabled or _SELF_AUDITED.match(path):
            return None, None
        if m := _SESS.match(path):
            action = m.group(2)
            if action == "execute":
                return None, {"kind": "script", "server_id": server_id, "db_id": db_id, "capture": True}
            if action in ("commit", "rollback") and sess is not None and sess.dirty:
                return None, {"kind": "tx", "server_id": server_id, "db_id": db_id, "statement": action.upper()}
            return None, None
        if m := _DB_EXEC.match(path):
            return None, {"kind": "script", "server_id": m.group(1), "db_id": m.group(2), "capture": True}
        if (m := _GRID.match(path)) and method == "POST":
            return None, {"kind": "grid", "server_id": m.group(1), "db_id": m.group(2), "table": m.group(3),
                          "capture": True}
        if (m := _DROP.match(path)) and method == "DELETE":
            return None, {"kind": "drop", "server_id": m.group(1), "db_id": m.group(2), "capture": True,
                          "statement": f"DROP {m.group(3)} {m.group(4)}"}
        if server_id and path.startswith("/api/servers/"):
            parts = [p for p in path.split("/") if p]
            last = parts[-1]
            registry = (len(parts) == 3 and method in ("PUT", "DELETE")) or \
                (parts[3:] == ["databases"] and method == "POST") or \
                (len(parts) == 5 and parts[3] == "databases" and method in ("PUT", "DELETE"))
            if registry or last in _NOT_AUDITED_ACTIONS:
                return None, None
            return None, {"kind": "action", "server_id": server_id, "db_id": db_id, "capture": True,
                          "statement": f"{method} {path}" + _body_summary(body)}
        return None, None

    def _audit(self, sec, ctx: dict[str, Any], status: int, resp: bytes, req_body: bytes) -> None:
        try:
            data = json.loads(resp) if resp else {}
        except ValueError:
            data = {}
        ok = 200 <= status < 300
        err = None if ok else (data.get("detail") if isinstance(data, dict) else None) or f"HTTP {status}"
        kind = ctx["kind"]
        sid, dbid = ctx.get("server_id"), ctx.get("db_id")
        if kind == "script":
            if ok and isinstance(data, dict):
                sec.audit.record_script(sid, dbid, data)
            else:
                sql = _json_field(req_body, "sql") or ""
                sec.audit.record(server_id=sid, db_id=dbid, statement=sql, success=False, error=str(err))
        elif kind == "grid":
            changes = {}
            try:
                changes = json.loads(req_body or b"{}")
            except ValueError:
                pass
            n = {k: len(changes.get(k) or []) for k in ("inserts", "updates", "deletes")}
            stmt = (f"-- grid edit on {ctx['table']}: {n['inserts']} insert(s), {n['updates']} update(s), "
                    f"{n['deletes']} delete(s)\n" + json.dumps(changes, default=str)[:4000])
            sec.audit.record(server_id=sid, db_id=dbid, statement=stmt, success=ok, error=err, cat="DML",
                             row_count=data.get("affected") if ok and isinstance(data, dict) else None)
        elif kind == "drop":
            stmt = (data.get("sql") if ok and isinstance(data, dict) else None) or ctx["statement"]
            sec.audit.record(server_id=sid, db_id=dbid, statement=stmt, success=ok, error=err, cat="DDL")
        elif kind == "tx":
            sec.audit.record(server_id=sid, db_id=dbid, statement=ctx["statement"], success=ok, error=err, cat="TX")
        else:
            sec.audit.record(server_id=sid, db_id=dbid, statement=ctx["statement"], success=ok, error=err,
                             cat="ACTION")


class _TooLarge(Exception):
    pass


async def _read_body(receive, limit: int | None) -> tuple[bytes, bool]:
    chunks, size = [], 0
    while True:
        msg = await receive()
        if msg["type"] != "http.request":
            break
        b = msg.get("body", b"")
        size += len(b)
        if limit is not None and size > limit:
            return b"", True
        chunks.append(b)
        if not msg.get("more_body"):
            break
    return b"".join(chunks), False


def _limited_receive(receive, limit: int):
    seen = 0

    async def inner():
        nonlocal seen
        msg = await receive()
        if msg["type"] == "http.request":
            seen += len(msg.get("body", b""))
            if seen > limit:
                raise _TooLarge()
        return msg

    return inner


def _body_summary(body: bytes) -> str:
    """Request body for the audit log, without secrets."""
    try:
        data = json.loads(body or b"null")
    except ValueError:
        return ""
    if not isinstance(data, dict) or not data:
        return ""
    clean = {k: ("***" if "password" in k.lower() or "secret" in k.lower() else v) for k, v in data.items()}
    return "\n" + json.dumps(clean, default=str)[:2000]


def _json_field(body: bytes, key: str) -> Any:
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return None
    return data.get(key) if isinstance(data, dict) else None
