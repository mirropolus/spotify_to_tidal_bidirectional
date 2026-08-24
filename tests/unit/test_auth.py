# tests/unit/test_auth.py

import pytest
import spotipy
import tidalapi
import yaml
import sys
from unittest import mock
from spotify_to_tidal.auth import open_spotify_session, open_tidal_session, SPOTIFY_SCOPES, SPOTIFY_WRITE_SCOPES


def test_open_spotify_session(mocker):
    # Mock the SpotifyOAuth class
    mock_spotify_oauth = mocker.patch(
        "spotify_to_tidal.auth.spotipy.SpotifyOAuth", autospec=True
    )
    mock_spotify_instance = mocker.patch(
        "spotify_to_tidal.auth.spotipy.Spotify", autospec=True
    )

    # Define a mock configuration
    mock_config = {
        "username": "test_user",
        "client_id": "test_client_id",
        "client_secret": "test_client_secret",
        "redirect_uri": "http://127.0.0.1/",
        "open_browser": True,
    }

    # Create a mock SpotifyOAuth instance
    mock_oauth_instance = mock_spotify_oauth.return_value
    mock_oauth_instance.get_access_token.return_value = "mock_access_token"

    # Call the function under test
    spotify_instance = open_spotify_session(mock_config)

    # Assert that the SpotifyOAuth was called with correct parameters
    mock_spotify_oauth.assert_called_once_with(
        username="test_user",
        scope=SPOTIFY_SCOPES,
        client_id="test_client_id",
        client_secret="test_client_secret",
        redirect_uri="http://127.0.0.1/",
        requests_timeout=2,
        open_browser=True,
    )

    # Assert that the Spotify instance was created
    mock_spotify_instance.assert_called_once_with(oauth_manager=mock_oauth_instance)
    assert spotify_instance == mock_spotify_instance.return_value


def test_open_spotify_session_oauth_error(mocker):
    # Mock the SpotifyOAuth class and simulate an OAuth error
    mock_spotify_oauth = mocker.patch(
        "spotify_to_tidal.auth.spotipy.SpotifyOAuth", autospec=True
    )
    mock_spotify_oauth.return_value.get_access_token.side_effect = (
        spotipy.SpotifyOauthError("mock error")
    )

    # Define a mock configuration
    mock_config = {
        "username": "test_user",
        "client_id": "test_client_id",
        "client_secret": "test_client_secret",
        "redirect_uri": "http://127.0.0.1/",
    }

    # Mock sys.exit to prevent the test from exiting
    mock_sys_exit = mocker.patch("sys.exit")

    # Call the function under test and assert sys.exit is called
    open_spotify_session(mock_config)
    mock_sys_exit.assert_called_once()


@pytest.mark.parametrize("sync_direction", ["tidal_to_spotify", "bidirectional"])
def test_open_spotify_session_write_scopes(mocker, sync_direction):
    """When sync_direction requires writing to Spotify, write scopes are appended."""
    mock_spotify_oauth = mocker.patch(
        "spotify_to_tidal.auth.spotipy.SpotifyOAuth", autospec=True
    )
    mocker.patch("spotify_to_tidal.auth.spotipy.Spotify", autospec=True)
    mock_spotify_oauth.return_value.get_access_token.return_value = "mock_access_token"

    mock_config = {
        "username": "test_user",
        "client_id": "test_client_id",
        "client_secret": "test_client_secret",
        "redirect_uri": "http://127.0.0.1/",
    }

    open_spotify_session(mock_config, sync_direction=sync_direction)

    expected_scope = SPOTIFY_SCOPES + ', ' + SPOTIFY_WRITE_SCOPES
    mock_spotify_oauth.assert_called_once_with(
        username="test_user",
        scope=expected_scope,
        client_id="test_client_id",
        client_secret="test_client_secret",
        redirect_uri="http://127.0.0.1/",
        requests_timeout=2,
        open_browser=True,
    )


def test_open_spotify_session_read_only_scopes_by_default(mocker):
    """Default sync_direction (spotify_to_tidal) uses read-only scopes."""
    mock_spotify_oauth = mocker.patch(
        "spotify_to_tidal.auth.spotipy.SpotifyOAuth", autospec=True
    )
    mocker.patch("spotify_to_tidal.auth.spotipy.Spotify", autospec=True)
    mock_spotify_oauth.return_value.get_access_token.return_value = "mock_access_token"

    mock_config = {
        "username": "test_user",
        "client_id": "test_client_id",
        "client_secret": "test_client_secret",
        "redirect_uri": "http://127.0.0.1/",
    }

    open_spotify_session(mock_config)

    mock_spotify_oauth.assert_called_once_with(
        username="test_user",
        scope=SPOTIFY_SCOPES,
        client_id="test_client_id",
        client_secret="test_client_secret",
        redirect_uri="http://127.0.0.1/",
        requests_timeout=2,
        open_browser=True,
    )


def test_open_spotify_session_spotify_to_tidal_read_only(mocker):
    """Explicit spotify_to_tidal direction uses read-only scopes."""
    mock_spotify_oauth = mocker.patch(
        "spotify_to_tidal.auth.spotipy.SpotifyOAuth", autospec=True
    )
    mocker.patch("spotify_to_tidal.auth.spotipy.Spotify", autospec=True)
    mock_spotify_oauth.return_value.get_access_token.return_value = "mock_access_token"

    mock_config = {
        "username": "test_user",
        "client_id": "test_client_id",
        "client_secret": "test_client_secret",
        "redirect_uri": "http://127.0.0.1/",
    }

    open_spotify_session(mock_config, sync_direction="spotify_to_tidal")

    mock_spotify_oauth.assert_called_once_with(
        username="test_user",
        scope=SPOTIFY_SCOPES,
        client_id="test_client_id",
        client_secret="test_client_secret",
        redirect_uri="http://127.0.0.1/",
        requests_timeout=2,
        open_browser=True,
    )


def test_open_spotify_session_uses_refresh_token_from_environment(mocker, monkeypatch):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "environment-client-id")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "environment-client-secret")
    monkeypatch.setenv("SPOTIFY_REFRESH_TOKEN", "environment-refresh-token")
    monkeypatch.setenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1/callback")
    mock_spotify_oauth = mocker.patch(
        "spotify_to_tidal.auth.spotipy.SpotifyOAuth", autospec=True
    )
    mocker.patch("spotify_to_tidal.auth.spotipy.Spotify", autospec=True)

    open_spotify_session({"username": "test-user"}, sync_direction="bidirectional")

    arguments = mock_spotify_oauth.call_args.kwargs
    assert arguments["client_id"] == "environment-client-id"
    assert arguments["client_secret"] == "environment-client-secret"
    assert arguments["open_browser"] is False
    assert "cache_handler" in arguments
    mock_spotify_oauth.return_value.refresh_access_token.assert_called_once_with(
        "environment-refresh-token"
    )
    mock_spotify_oauth.return_value.get_access_token.assert_not_called()


def test_open_tidal_session_uses_environment_without_session_file(mocker, monkeypatch):
    monkeypatch.setenv("TIDAL_ACCESS_TOKEN", "environment-access-token")
    monkeypatch.setenv("TIDAL_REFRESH_TOKEN", "environment-refresh-token")
    mock_session_class = mocker.patch(
        "spotify_to_tidal.auth.tidalapi.Session", autospec=True
    )
    mock_session = mock_session_class.return_value
    mock_session.load_oauth_session.return_value = True
    file_open = mocker.patch("builtins.open")

    result = open_tidal_session()

    assert result == mock_session
    mock_session.load_oauth_session.assert_called_once_with(
        token_type="Bearer",
        access_token="environment-access-token",
        refresh_token="environment-refresh-token",
        expiry_time=None,
    )
    mock_session.login_oauth.assert_not_called()
    file_open.assert_not_called()
