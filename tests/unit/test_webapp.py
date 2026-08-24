import csv
import datetime
import io
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from spotify_to_tidal.webapp.app import (
    COOKIE_NAME,
    MemorySessionStore,
    SPOTIFY_AUDIT_SCOPE,
    WebSettings,
    create_app,
)
from spotify_to_tidal.webapp.tidal_auth import TidalCredentials
from spotify_to_tidal.webapp.tidal_auth import TidalAPIError


def configured_settings(**overrides):
    values = {
        "base_url": "https://audit.example.test",
        "spotify_client_id": "spotify-client-id",
        "tidal_client_id": "tidal-client-id",
        "tidal_client_secret": "tidal-client-secret",
    }
    values.update(overrides)
    return WebSettings(**values)


def connected_client():
    store = MemorySessionStore(7200)
    app = create_app(configured_settings(), store=store)
    client = TestClient(app, base_url="https://audit.example.test")
    response = client.get("/")
    assert response.status_code == 200
    session_id = client.cookies.get(COOKIE_NAME)
    _, state, created = store.get_or_create(session_id)
    assert not created
    state.spotify_session = MagicMock()
    state.tidal_client = MagicMock()
    return client, store, state


def test_home_is_installable_and_explicitly_read_only():
    app = create_app(configured_settings())
    client = TestClient(app, base_url="https://audit.example.test")

    response = client.get("/")

    assert response.status_code == 200
    assert "Favorite Bridge Audit" in response.text
    assert "Read-only" in response.text
    assert 'rel="manifest"' in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_remote_base_url_requires_https():
    with pytest.raises(ValueError, match="must use HTTPS"):
        WebSettings(base_url="http://audit.example.test")


def test_loopback_http_is_allowed():
    settings = WebSettings(base_url="http://127.0.0.1:8765")
    assert settings.base_url == "http://127.0.0.1:8765"


def test_spotify_start_uses_pkce_and_read_only_scope(mocker):
    captured = {}

    class FakeSpotifyPKCE:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def get_authorize_url(self):
            return "https://accounts.spotify.com/authorize?safe=test"

    mocker.patch("spotify_to_tidal.webapp.app.SpotifyPKCE", FakeSpotifyPKCE)
    client = TestClient(
        create_app(configured_settings()),
        base_url="https://audit.example.test",
    )

    response = client.get("/auth/spotify/start", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("https://accounts.spotify.com/")
    assert captured["scope"] == SPOTIFY_AUDIT_SCOPE == "user-library-read"
    assert captured["redirect_uri"] == "https://audit.example.test/auth/spotify/callback"
    assert captured["open_browser"] is False


def test_spotify_callback_rejects_invalid_state_without_token_exchange(mocker):
    client = TestClient(
        create_app(configured_settings()),
        base_url="https://audit.example.test",
    )
    client.get("/auth/spotify/start", follow_redirects=False)
    exchange = mocker.patch("spotipy.oauth2.SpotifyPKCE.get_access_token")

    response = client.get(
        "/auth/spotify/callback?code=one-time-code&state=wrong",
        follow_redirects=False,
    )

    assert response.status_code == 303
    exchange.assert_not_called()


def test_tidal_start_requires_csrf_and_redirects_to_authorization_code_pkce():
    store = MemorySessionStore(7200)
    client = TestClient(
        create_app(configured_settings(), store=store),
        base_url="https://audit.example.test",
    )
    client.get("/")
    session_id = client.cookies.get(COOKIE_NAME)
    _, state, _ = store.get_or_create(session_id)

    rejected = client.post(
        "/auth/tidal/start",
        data={"csrf_token": "wrong"},
        follow_redirects=False,
    )
    response = client.post(
        "/auth/tidal/start",
        data={"csrf_token": state.csrf_token},
        follow_redirects=False,
    )

    assert rejected.status_code == 403
    assert response.status_code == 303
    assert response.headers["location"].startswith("https://login.tidal.com/authorize?")
    assert state.tidal_state
    assert state.tidal_code_verifier
    assert state.tidal_client is None


def test_tidal_callback_rejects_invalid_state_without_exchange(mocker):
    store = MemorySessionStore(7200)
    client = TestClient(
        create_app(configured_settings(), store=store),
        base_url="https://audit.example.test",
    )
    client.get("/")
    session_id = client.cookies.get(COOKIE_NAME)
    _, browser, _ = store.get_or_create(session_id)
    client.post(
        "/auth/tidal/start",
        data={"csrf_token": browser.csrf_token},
        follow_redirects=False,
    )
    exchange = mocker.patch(
        "spotify_to_tidal.webapp.app.exchange_authorization_code",
        new=mocker.AsyncMock(),
    )

    response = client.get(
        "/auth/tidal/callback?code=one-time-code&state=wrong",
        follow_redirects=False,
    )

    assert response.status_code == 303
    exchange.assert_not_awaited()
    assert browser.tidal_client is None
    assert browser.tidal_state is not None
    assert browser.tidal_code_verifier is not None


def test_unsolicited_tidal_callback_does_not_disconnect_existing_session(mocker):
    client, _, browser = connected_client()
    existing_client = browser.tidal_client
    exchange = mocker.patch(
        "spotify_to_tidal.webapp.app.exchange_authorization_code",
        new=mocker.AsyncMock(),
    )

    response = client.get(
        "/auth/tidal/callback?error=access_denied&state=untrusted",
        follow_redirects=False,
    )

    assert response.status_code == 303
    exchange.assert_not_awaited()
    assert browser.tidal_client is existing_client


def test_tidal_callback_exchanges_code_and_keeps_token_only_in_memory(mocker):
    store = MemorySessionStore(7200)
    client = TestClient(
        create_app(configured_settings(), store=store),
        base_url="https://audit.example.test",
    )
    client.get("/")
    session_id = client.cookies.get(COOKIE_NAME)
    _, browser, _ = store.get_or_create(session_id)
    client.post(
        "/auth/tidal/start",
        data={"csrf_token": browser.csrf_token},
        follow_redirects=False,
    )
    expected_state = browser.tidal_state
    expected_verifier = browser.tidal_code_verifier
    credentials = TidalCredentials(
        access_token="private-access-token",
        refresh_token="private-refresh-token",
        token_type="Bearer",
        expires_at=datetime.datetime.now(datetime.timezone.utc),
        scope="collection.read",
    )
    exchange = mocker.patch(
        "spotify_to_tidal.webapp.app.exchange_authorization_code",
        new=mocker.AsyncMock(return_value=credentials),
    )

    response = client.get(
        f"/auth/tidal/callback?code=one-time-code&state={expected_state}",
        follow_redirects=False,
    )
    home = client.get("/")

    assert response.status_code == 303
    exchange.assert_awaited_once_with(
        client_id="tidal-client-id",
        client_secret="tidal-client-secret",
        code="one-time-code",
        code_verifier=expected_verifier,
        redirect_uri="https://audit.example.test/auth/tidal/callback",
    )
    assert browser.tidal_client is not None
    assert browser.tidal_state is None
    assert browser.tidal_code_verifier is None
    assert "private-access-token" not in response.headers.get("set-cookie", "")
    assert "private-access-token" not in home.text
    assert "private-refresh-token" not in home.text


def test_tidal_callback_handles_provider_error_without_exchange(mocker):
    store = MemorySessionStore(7200)
    client = TestClient(
        create_app(configured_settings(), store=store),
        base_url="https://audit.example.test",
    )
    client.get("/")
    session_id = client.cookies.get(COOKIE_NAME)
    _, browser, _ = store.get_or_create(session_id)
    client.post(
        "/auth/tidal/start",
        data={"csrf_token": browser.csrf_token},
        follow_redirects=False,
    )
    exchange = mocker.patch(
        "spotify_to_tidal.webapp.app.exchange_authorization_code",
        new=mocker.AsyncMock(),
    )

    response = client.get(
        f"/auth/tidal/callback?error=access_denied&state={browser.tidal_state}",
        follow_redirects=False,
    )

    assert response.status_code == 303
    exchange.assert_not_awaited()
    assert browser.tidal_client is None


def test_web_audit_is_read_only_and_downloads_csv(mocker):
    client, _, state = connected_client()
    rows = [{
        "tidal_id": "tidal-1",
        "spotify_id": "spotify-1",
        "spotify_catalog_candidate_id": "",
        "isrc": "ISRC1",
        "artist": "Artist",
        "title": "Old favorite",
        "tidal_date_added": "2021-05-12T14:32:10Z",
        "spotify_added_at": "2026-04-27T08:00:00Z",
        "status": "timestamp_mismatch",
        "timestamp_delta_seconds": "156000000",
        "spotify_added_cluster_size": "8",
        "timestamp_suspect_reason": "spotify_added_much_later_than_tidal;clustered_spotify_added_at",
        "tidal_source_occurrences": "2",
        "tidal_source_duplicate_conflict": "date_added_conflict",
        "spotify_source_occurrences": "1",
        "spotify_source_duplicate_conflict": "",
    }]
    collect = mocker.patch(
        "spotify_to_tidal.webapp.app.collect_favorites_audit_rows_from_tracks",
        new=mocker.AsyncMock(return_value=rows),
    )
    tidal_tracks = [MagicMock()]
    state.tidal_client.favorite_tracks = mocker.AsyncMock(return_value=tidal_tracks)

    response = client.post(
        "/audit",
        data={"csrf_token": state.csrf_token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    collect.assert_awaited_once_with(
        state.spotify_session,
        tidal_tracks,
        {
            "audit_timestamp_mismatch_days": 30,
            "audit_timestamp_cluster_size": 3,
            "audit_timestamp_cluster_min_delta_hours": 24,
        },
    )
    state.spotify_session.current_user_saved_tracks_add.assert_not_called()
    state.spotify_session.current_user_saved_tracks_delete.assert_not_called()
    state.spotify_session.playlist_add_items.assert_not_called()
    state.spotify_session.playlist_replace_items.assert_not_called()

    download = client.get("/favorites_audit.csv")
    assert download.status_code == 200
    assert "favorites_audit.csv" in download.headers["content-disposition"]
    parsed = list(csv.DictReader(io.StringIO(download.content.decode("utf-8-sig"))))
    assert parsed == rows
    home = client.get("/")
    assert "Collapsed 1 duplicate Tidal collection records across 1 IDs" in home.text
    assert "conflicting source data" in home.text


def test_web_audit_requires_csrf():
    client, _, _ = connected_client()
    response = client.post("/audit", data={"csrf_token": "wrong"})
    assert response.status_code == 403


def test_web_audit_reports_safe_tidal_stage_error(mocker):
    client, _, state = connected_client()
    state.tidal_client.favorite_tracks = mocker.AsyncMock(
        side_effect=TidalAPIError("Tidal collection pagination failed")
    )

    response = client.post(
        "/audit",
        data={"csrf_token": state.csrf_token},
        follow_redirects=False,
    )
    home = client.get("/")

    assert response.status_code == 303
    assert "Tidal collection pagination failed" in home.text
    assert "No library changes were made" in home.text


def test_web_app_exposes_no_sync_or_delete_library_routes():
    app = create_app(configured_settings())
    route_paths = {route.path for route in app.routes}
    assert not any("sync" in path for path in route_paths)
    assert not any("playlist" in path for path in route_paths)
    assert "/auth/tidal/status" not in route_paths
    assert "/auth/tidal/callback" in route_paths
    assert route_paths >= {"/audit", "/favorites_audit.csv", "/session/delete"}


def test_delete_session_removes_in_memory_tokens_and_report():
    client, store, state = connected_client()
    state.audit_csv = b"private report"
    session_id = client.cookies.get(COOKIE_NAME)

    response = client.post(
        "/session/delete",
        data={"csrf_token": state.csrf_token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    _, new_state, created = store.get_or_create(session_id)
    assert created
    assert new_state.spotify_session is None
    assert new_state.tidal_client is None
    assert new_state.audit_csv is None


def test_expired_session_drops_in_memory_provider_credentials(mocker):
    store = MemorySessionStore(300)
    session_id, state, _ = store.get_or_create(None)
    state.spotify_session = MagicMock()
    state.tidal_client = MagicMock()
    mocker.patch(
        "spotify_to_tidal.webapp.app.time.monotonic",
        return_value=state.created_monotonic + 301,
    )

    new_id, new_state, created = store.get_or_create(session_id)

    assert created
    assert new_id != session_id
    assert new_state.spotify_session is None
    assert new_state.tidal_client is None
