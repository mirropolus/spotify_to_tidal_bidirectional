from unittest.mock import MagicMock

import pytest

from spotify_to_tidal import __main__
from spotify_to_tidal.sync import SpotifyTimestampSaveError


def _write_config(tmp_path):
    config = tmp_path / "config.yml"
    config.write_text(
        "spotify:\n"
        "  username: test\n"
        "  client_id: id\n"
        "  client_secret: secret\n"
        "  redirect_uri: http://127.0.0.1/callback\n",
        encoding="utf-8",
    )
    return config


def test_timestamp_capability_cli_uses_write_scope_and_never_opens_tidal(
    mocker,
    tmp_path,
    capsys,
):
    config = _write_config(tmp_path)
    spotify = MagicMock()
    open_spotify = mocker.patch(
        "spotify_to_tidal.__main__._auth.open_spotify_session",
        return_value=spotify,
    )
    open_tidal = mocker.patch(
        "spotify_to_tidal.__main__._auth.open_tidal_session"
    )
    check = mocker.patch(
        "spotify_to_tidal.__main__._sync.check_spotify_timestamp_support"
    )
    mocker.patch(
        "sys.argv",
        [
            "spotify_to_tidal",
            "--config",
            str(config),
            "--check-spotify-timestamp-support",
        ],
    )

    __main__.main()

    open_spotify.assert_called_once_with(
        {
            "username": "test",
            "client_id": "id",
            "client_secret": "secret",
            "redirect_uri": "http://127.0.0.1/callback",
        },
        sync_direction="tidal_to_spotify",
    )
    check.assert_called_once_with(spotify)
    open_tidal.assert_not_called()
    assert "no library changes" in capsys.readouterr().out


def test_timestamp_capability_cli_reports_restriction_without_opening_tidal(
    mocker,
    tmp_path,
):
    config = _write_config(tmp_path)
    mocker.patch(
        "spotify_to_tidal.__main__._auth.open_spotify_session",
        return_value=MagicMock(),
    )
    open_tidal = mocker.patch(
        "spotify_to_tidal.__main__._auth.open_tidal_session"
    )
    mocker.patch(
        "spotify_to_tidal.__main__._sync.check_spotify_timestamp_support",
        side_effect=SpotifyTimestampSaveError("restricted"),
    )
    mocker.patch(
        "sys.argv",
        [
            "spotify_to_tidal",
            "--config",
            str(config),
            "--check-spotify-timestamp-support",
        ],
    )

    with pytest.raises(SystemExit, match="restricted"):
        __main__.main()

    open_tidal.assert_not_called()


def test_timestamp_capability_cli_rejects_sync_options_before_authentication(
    mocker,
    tmp_path,
):
    config = _write_config(tmp_path)
    open_spotify = mocker.patch(
        "spotify_to_tidal.__main__._auth.open_spotify_session"
    )
    mocker.patch(
        "sys.argv",
        [
            "spotify_to_tidal",
            "--config",
            str(config),
            "--check-spotify-timestamp-support",
            "--sync-favorites",
        ],
    )

    with pytest.raises(SystemExit, match="must be run by itself"):
        __main__.main()

    open_spotify.assert_not_called()
