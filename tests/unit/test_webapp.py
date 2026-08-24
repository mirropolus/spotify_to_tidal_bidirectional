import csv
import io
from types import SimpleNamespace
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
from spotify_to_tidal.webapp.tidal_auth import ReadOnlyTidalSession


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
    state.tidal_session = MagicMock()
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


def test_tidal_device_authorization_requests_read_only_scope():
    response = MagicMock(ok=True)
    response.json.return_value = {
        "expiresIn": 300,
        "userCode": "ABCD",
        "verificationUri": "link.tidal.com",
        "verificationUriComplete": "link.tidal.com/ABCD",
        "interval": 2,
        "deviceCode": "device-code",
    }
    session = object.__new__(ReadOnlyTidalSession)
    session.request_session = MagicMock()
    session.request_session.post.return_value = response
    session.config = SimpleNamespace(client_id="client-id")

    link = session.get_link_login()

    assert link.user_code == "ABCD"
    _, payload = session.request_session.post.call_args.args
    assert payload["scope"] == "r_usr"
    assert "w_usr" not in payload["scope"]


def test_tidal_token_poll_requests_read_only_scope(mocker):
    pending = MagicMock(ok=False)
    pending.json.return_value = {"error": "authorization_pending"}
    complete = MagicMock(ok=True)
    complete.json.return_value = {
        "access_token": "not-rendered",
        "refresh_token": "not-rendered",
        "expires_in": 3600,
        "token_type": "Bearer",
    }
    session = object.__new__(ReadOnlyTidalSession)
    session.request_session = MagicMock()
    session.request_session.post.side_effect = [pending, complete]
    session.config = SimpleNamespace(
        client_id="client-id",
        client_secret="client-secret",
        api_oauth2_token="https://auth.tidal.com/v1/oauth2/token",
    )
    link = SimpleNamespace(expires_in=10, interval=1, device_code="device-code")
    mocker.patch("spotify_to_tidal.webapp.tidal_auth.time.sleep")

    result = session._check_link_login(link)

    assert result["access_token"] == "not-rendered"
    for call in session.request_session.post.call_args_list:
        assert call.args[1]["scope"] == "r_usr"
        assert "w_usr" not in call.args[1]["scope"]


def test_web_audit_is_read_only_and_downloads_csv(mocker):
    client, _, state = connected_client()
    rows = [{
        "tidal_id": "tidal-1",
        "spotify_id": "spotify-1",
        "isrc": "ISRC1",
        "artist": "Artist",
        "title": "Old favorite",
        "tidal_date_added": "2021-05-12T14:32:10Z",
        "spotify_added_at": "2026-04-27T08:00:00Z",
        "status": "timestamp_mismatch",
        "timestamp_delta_seconds": "156000000",
        "spotify_added_cluster_size": "8",
        "timestamp_suspect_reason": "spotify_added_much_later_than_tidal;clustered_spotify_added_at",
    }]
    collect = mocker.patch(
        "spotify_to_tidal.webapp.app.collect_favorites_audit_rows",
        new=mocker.AsyncMock(return_value=rows),
    )

    response = client.post(
        "/audit",
        data={"csrf_token": state.csrf_token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    collect.assert_awaited_once_with(
        state.spotify_session,
        state.tidal_session,
        {
            "audit_timestamp_mismatch_days": 30,
            "audit_timestamp_cluster_size": 3,
        },
    )
    for session in (state.spotify_session, state.tidal_session):
        session.current_user_saved_tracks_add.assert_not_called()
        session.current_user_saved_tracks_delete.assert_not_called()
        session.playlist_add_items.assert_not_called()
        session.playlist_replace_items.assert_not_called()
        session.user.favorites.add_track.assert_not_called()
        session.user.favorites.remove_track.assert_not_called()

    download = client.get("/favorites_audit.csv")
    assert download.status_code == 200
    assert "favorites_audit.csv" in download.headers["content-disposition"]
    parsed = list(csv.DictReader(io.StringIO(download.content.decode("utf-8-sig"))))
    assert parsed == rows


def test_web_audit_requires_csrf():
    client, _, _ = connected_client()
    response = client.post("/audit", data={"csrf_token": "wrong"})
    assert response.status_code == 403


def test_web_app_exposes_no_sync_or_delete_library_routes():
    app = create_app(configured_settings())
    route_paths = {route.path for route in app.routes}
    assert not any("sync" in path for path in route_paths)
    assert not any("playlist" in path for path in route_paths)
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
    assert new_state.tidal_session is None
    assert new_state.audit_csv is None
