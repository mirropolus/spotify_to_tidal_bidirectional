"""FastAPI application for a mobile, read-only favorites audit."""

from __future__ import annotations

import argparse
from collections import Counter
import csv
from dataclasses import dataclass, field
import datetime
import hmac
import io
import os
from pathlib import Path
import secrets
import threading
import time
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import spotipy
from spotipy.cache_handler import MemoryCacheHandler
from spotipy.oauth2 import SpotifyPKCE
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..audit import (
    AUDIT_FIELDS,
    AuditStageError,
    collect_favorites_audit_rows_from_tracks,
)
from .tidal_auth import (
    TidalAPIError,
    TidalOpenAPIClient,
    build_authorization_url,
    exchange_authorization_code,
    generate_pkce_verifier,
)


COOKIE_NAME = "favorite_bridge_session"
SPOTIFY_AUDIT_SCOPE = "user-library-read"
DEFAULT_SESSION_TTL_SECONDS = 2 * 60 * 60
PACKAGE_ROOT = Path(__file__).resolve().parent


def _validated_base_url(value: str) -> str:
    parsed = urlparse(value.rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("AUDIT_WEB_BASE_URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("AUDIT_WEB_BASE_URL cannot contain credentials, query, or fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("AUDIT_WEB_BASE_URL cannot contain a path")
    loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    if parsed.scheme != "https" and not loopback:
        raise ValueError("AUDIT_WEB_BASE_URL must use HTTPS except on loopback")
    return value.rstrip("/")


@dataclass(frozen=True)
class WebSettings:
    base_url: str = "http://127.0.0.1:8765"
    spotify_client_id: str | None = None
    tidal_client_id: str | None = None
    tidal_client_secret: str | None = None
    session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS
    mismatch_days: int = 30
    cluster_size: int = 3

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", _validated_base_url(self.base_url))
        if self.session_ttl_seconds < 300:
            raise ValueError("Session lifetime must be at least five minutes")

    @classmethod
    def from_environment(cls) -> "WebSettings":
        return cls(
            base_url=os.getenv("AUDIT_WEB_BASE_URL", "http://127.0.0.1:8765"),
            spotify_client_id=os.getenv("SPOTIFY_CLIENT_ID"),
            tidal_client_id=os.getenv("TIDAL_CLIENT_ID"),
            tidal_client_secret=os.getenv("TIDAL_CLIENT_SECRET"),
            session_ttl_seconds=int(
                os.getenv("AUDIT_WEB_SESSION_TTL_SECONDS", str(DEFAULT_SESSION_TTL_SECONDS))
            ),
            mismatch_days=int(os.getenv("AUDIT_TIMESTAMP_MISMATCH_DAYS", "30")),
            cluster_size=int(os.getenv("AUDIT_TIMESTAMP_CLUSTER_SIZE", "3")),
        )


@dataclass
class BrowserSession:
    created_monotonic: float = field(default_factory=time.monotonic)
    csrf_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    spotify_state: str | None = None
    spotify_oauth: SpotifyPKCE | None = None
    spotify_session: Any | None = None
    tidal_state: str | None = None
    tidal_code_verifier: str | None = None
    tidal_client: TidalOpenAPIClient | None = None
    audit_csv: bytes | None = None
    audit_counts: dict[str, int] = field(default_factory=dict)
    audit_preview: list[dict] = field(default_factory=list)
    audit_generated_at: str | None = None
    notice: str | None = None
    error: str | None = None

    def clear_report(self) -> None:
        self.audit_csv = None
        self.audit_counts = {}
        self.audit_preview = []
        self.audit_generated_at = None


class MemorySessionStore:
    """Opaque, process-local sessions; provider tokens never reach the browser."""

    def __init__(self, ttl_seconds: int) -> None:
        self.ttl_seconds = ttl_seconds
        self._sessions: dict[str, BrowserSession] = {}
        self._lock = threading.Lock()

    def get_or_create(self, session_id: str | None) -> tuple[str, BrowserSession, bool]:
        now = time.monotonic()
        with self._lock:
            expired = [
                key for key, value in self._sessions.items()
                if now - value.created_monotonic > self.ttl_seconds
            ]
            for key in expired:
                self._sessions.pop(key, None)

            if session_id and session_id in self._sessions:
                return session_id, self._sessions[session_id], False

            new_id = secrets.token_urlsafe(32)
            state = BrowserSession()
            self._sessions[new_id] = state
            return new_id, state, True

    def delete(self, session_id: str | None) -> None:
        if not session_id:
            return
        with self._lock:
            self._sessions.pop(session_id, None)


def _csv_bytes(rows: list[dict]) -> bytes:
    report = io.StringIO(newline="")
    writer = csv.DictWriter(report, fieldnames=AUDIT_FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    return report.getvalue().encode("utf-8-sig")


def create_app(
    settings: WebSettings | None = None,
    *,
    store: MemorySessionStore | None = None,
) -> FastAPI:
    settings = settings or WebSettings.from_environment()
    store = store or MemorySessionStore(settings.session_ttl_seconds)
    templates = Jinja2Templates(directory=str(PACKAGE_ROOT / "templates"))
    app = FastAPI(
        title="Favorite Bridge Audit",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    hostname = urlparse(settings.base_url).hostname or "127.0.0.1"
    allowed_hosts = [hostname, "127.0.0.1", "localhost", "testserver"]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(dict.fromkeys(allowed_hosts)))
    app.mount("/static", StaticFiles(directory=str(PACKAGE_ROOT / "static")), name="static")
    app.state.settings = settings
    app.state.session_store = store

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def browser_state(request: Request) -> tuple[str, BrowserSession, bool]:
        return store.get_or_create(request.cookies.get(COOKIE_NAME))

    def attach_cookie(response: Response, session_id: str) -> None:
        response.set_cookie(
            COOKIE_NAME,
            session_id,
            max_age=settings.session_ttl_seconds,
            httponly=True,
            secure=settings.base_url.startswith("https://"),
            samesite="lax",
            path="/",
        )

    async def require_csrf(request: Request, state: BrowserSession) -> None:
        form = await request.form()
        supplied = str(form.get("csrf_token", ""))
        if not hmac.compare_digest(supplied, state.csrf_token):
            raise HTTPException(status_code=403, detail="Invalid request token")

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        session_id, state, _ = browser_state(request)
        tidal_status = "connected" if state.tidal_client is not None else "disconnected"
        notice, error = state.notice, state.error
        state.notice = None
        state.error = None
        response = templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "csrf_token": state.csrf_token,
                "spotify_connected": state.spotify_session is not None,
                "spotify_configured": bool(settings.spotify_client_id),
                "tidal_status": tidal_status,
                "tidal_configured": bool(settings.tidal_client_id and settings.tidal_client_secret),
                "can_audit": state.spotify_session is not None and tidal_status == "connected",
                "audit_counts": state.audit_counts,
                "audit_preview": state.audit_preview,
                "audit_generated_at": state.audit_generated_at,
                "notice": notice,
                "error": error,
            },
        )
        attach_cookie(response, session_id)
        return response

    @app.get("/auth/spotify/start")
    async def spotify_start(request: Request):
        if not settings.spotify_client_id:
            raise HTTPException(status_code=503, detail="Spotify is not configured")
        session_id, state, _ = browser_state(request)
        oauth_state = secrets.token_urlsafe(32)
        oauth = SpotifyPKCE(
            client_id=settings.spotify_client_id,
            redirect_uri=f"{settings.base_url}/auth/spotify/callback",
            state=oauth_state,
            scope=SPOTIFY_AUDIT_SCOPE,
            cache_handler=MemoryCacheHandler(),
            open_browser=False,
            requests_timeout=10,
        )
        state.spotify_state = oauth_state
        state.spotify_oauth = oauth
        state.spotify_session = None
        state.clear_report()
        response = RedirectResponse(oauth.get_authorize_url(), status_code=303)
        attach_cookie(response, session_id)
        return response

    @app.get("/auth/spotify/callback")
    async def spotify_callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ):
        session_id, browser, _ = browser_state(request)
        if (
            error
            or not code
            or not state
            or not browser.spotify_state
            or not hmac.compare_digest(state, browser.spotify_state)
            or browser.spotify_oauth is None
        ):
            browser.spotify_oauth = None
            browser.spotify_state = None
            browser.error = "Spotify authorization was cancelled or invalid."
        else:
            try:
                browser.spotify_oauth.get_access_token(code, check_cache=False)
                spotify_session = spotipy.Spotify(
                    auth_manager=browser.spotify_oauth,
                    requests_timeout=10,
                )
                spotify_session.current_user_saved_tracks(limit=1)
                browser.spotify_session = spotify_session
                browser.notice = "Spotify connected with read-only authorization."
                browser.clear_report()
            except Exception:
                browser.spotify_session = None
                browser.error = "Spotify authorization failed. Start it again."
            finally:
                browser.spotify_state = None
        response = RedirectResponse("/", status_code=303)
        attach_cookie(response, session_id)
        return response

    @app.post("/auth/spotify/disconnect")
    async def spotify_disconnect(request: Request):
        session_id, state, _ = browser_state(request)
        await require_csrf(request, state)
        state.spotify_state = None
        state.spotify_oauth = None
        state.spotify_session = None
        state.clear_report()
        state.notice = "Spotify disconnected."
        response = RedirectResponse("/", status_code=303)
        attach_cookie(response, session_id)
        return response

    @app.post("/auth/tidal/start")
    async def tidal_start(request: Request):
        if not settings.tidal_client_id or not settings.tidal_client_secret:
            raise HTTPException(status_code=503, detail="Tidal is not configured")
        session_id, state, _ = browser_state(request)
        await require_csrf(request, state)
        oauth_state = secrets.token_urlsafe(32)
        code_verifier = generate_pkce_verifier()
        redirect_uri = f"{settings.base_url}/auth/tidal/callback"
        state.tidal_state = oauth_state
        state.tidal_code_verifier = code_verifier
        state.tidal_client = None
        state.clear_report()
        response = RedirectResponse(
            build_authorization_url(
                settings.tidal_client_id,
                redirect_uri,
                oauth_state,
                code_verifier,
            ),
            status_code=303,
        )
        attach_cookie(response, session_id)
        return response

    @app.get("/auth/tidal/callback")
    async def tidal_callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ):
        session_id, browser, _ = browser_state(request)
        valid_state = (
            state is not None
            and browser.tidal_state is not None
            and hmac.compare_digest(state, browser.tidal_state)
        )
        if not valid_state:
            browser.error = "Tidal authorization was cancelled or invalid."
        elif error or not code or not browser.tidal_code_verifier:
            browser.tidal_client = None
            browser.error = "Tidal authorization was cancelled or invalid."
        else:
            try:
                credentials = await exchange_authorization_code(
                    client_id=settings.tidal_client_id or "",
                    client_secret=settings.tidal_client_secret or "",
                    code=code,
                    code_verifier=browser.tidal_code_verifier,
                    redirect_uri=f"{settings.base_url}/auth/tidal/callback",
                )
                browser.tidal_client = TidalOpenAPIClient(
                    credentials,
                    settings.tidal_client_id or "",
                    settings.tidal_client_secret or "",
                )
                browser.notice = "Tidal connected with read-only authorization."
                browser.clear_report()
            except Exception:
                browser.tidal_client = None
                browser.error = "Tidal authorization failed. Start it again."
        if valid_state:
            browser.tidal_state = None
            browser.tidal_code_verifier = None
        response = RedirectResponse("/", status_code=303)
        attach_cookie(response, session_id)
        return response

    @app.post("/auth/tidal/disconnect")
    async def tidal_disconnect(request: Request):
        session_id, state, _ = browser_state(request)
        await require_csrf(request, state)
        state.tidal_state = None
        state.tidal_code_verifier = None
        state.tidal_client = None
        state.clear_report()
        state.notice = "Tidal disconnected."
        response = RedirectResponse("/", status_code=303)
        attach_cookie(response, session_id)
        return response

    @app.post("/audit")
    async def run_audit(request: Request):
        session_id, state, _ = browser_state(request)
        await require_csrf(request, state)
        if state.spotify_session is None or state.tidal_client is None:
            raise HTTPException(status_code=409, detail="Connect both services first")
        try:
            tidal_tracks = await state.tidal_client.favorite_tracks()
            rows = await collect_favorites_audit_rows_from_tracks(
                state.spotify_session,
                tidal_tracks,
                {
                    "audit_timestamp_mismatch_days": settings.mismatch_days,
                    "audit_timestamp_cluster_size": settings.cluster_size,
                },
            )
            state.audit_csv = _csv_bytes(rows)
            state.audit_counts = dict(Counter(row["status"] for row in rows))
            state.audit_preview = [
                row for row in rows
                if row["status"] in {"timestamp_mismatch", "match_failed"}
            ][:20]
            state.audit_generated_at = datetime.datetime.now(
                datetime.timezone.utc
            ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            state.notice = "Audit completed. No library changes were made."
        except TidalAPIError as exc:
            state.clear_report()
            state.error = f"{exc}. No library changes were made."
        except AuditStageError as exc:
            state.clear_report()
            state.error = f"{exc}. No library changes were made."
        except Exception:
            state.clear_report()
            state.error = "The audit could not be completed. No library changes were made."
        response = RedirectResponse("/", status_code=303)
        attach_cookie(response, session_id)
        return response

    @app.get("/favorites_audit.csv")
    async def download_audit(request: Request):
        session_id, state, _ = browser_state(request)
        if state.audit_csv is None:
            raise HTTPException(status_code=404, detail="No audit report is available")
        response = Response(
            state.audit_csv,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="favorites_audit.csv"',
            },
        )
        attach_cookie(response, session_id)
        return response

    @app.post("/session/delete")
    async def delete_session(request: Request):
        session_id, state, _ = browser_state(request)
        await require_csrf(request, state)
        store.delete(session_id)
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @app.get("/healthz")
    async def healthcheck():
        return {"status": "ok"}

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the read-only mobile favorites audit")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    try:
        settings = WebSettings.from_environment()
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"Invalid audit web configuration: {exc}") from None

    import uvicorn

    uvicorn.run(
        create_app(settings),
        host=args.host,
        port=args.port,
        access_log=False,
        log_level="info",
    )


__all__ = [
    "BrowserSession",
    "COOKIE_NAME",
    "MemorySessionStore",
    "SPOTIFY_AUDIT_SCOPE",
    "WebSettings",
    "create_app",
    "main",
]
