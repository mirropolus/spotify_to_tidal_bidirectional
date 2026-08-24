import asyncio
import csv
import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from spotify_to_tidal import __main__
from spotify_to_tidal.audit import (
    audit_favorites,
    build_favorites_audit_rows,
    collect_favorites_audit_rows_from_tracks,
)
from spotify_to_tidal.sync import spotify_search


UTC = datetime.timezone.utc


def tidal_track(track_id, isrc, date_added):
    return SimpleNamespace(
        id=track_id,
        isrc=isrc,
        name=f"Title {isrc}",
        duration=200,
        artists=[SimpleNamespace(name=f"Artist {isrc}")],
        version=None,
        available=True,
        date_added=date_added,
        user_date_added=date_added,
    )


def spotify_track(track_id, isrc):
    return {
        "id": track_id,
        "name": f"Title {isrc}",
        "duration_ms": 200000,
        "artists": [{"name": f"Artist {isrc}"}],
        "external_ids": {"isrc": isrc},
    }


def saved_item(track_id, isrc, added_at):
    return {"added_at": added_at, "track": spotify_track(track_id, isrc)}


def test_audit_output_contains_all_statuses():
    tidal = [
        tidal_track("tidal-matched", "ISRC1", datetime.datetime(2024, 1, 1, tzinfo=UTC)),
        tidal_track("tidal-mismatch", "ISRC2", datetime.datetime(2021, 1, 1, tzinfo=UTC)),
        tidal_track("tidal-only", "ISRC3", datetime.datetime(2022, 1, 1, tzinfo=UTC)),
        tidal_track("tidal-failed", "ISRC4", datetime.datetime(2023, 1, 1, tzinfo=UTC)),
    ]
    spotify = [
        saved_item("spotify-matched", "ISRC1", "2024-01-02T00:00:00Z"),
        saved_item("spotify-mismatch", "ISRC2", "2026-04-27T00:00:00Z"),
        saved_item("spotify-only", "ISRC5", "2025-01-01T00:00:00Z"),
    ]
    catalog = {
        "tidal-only": spotify_track("spotify-catalog", "ISRC3"),
        "tidal-failed": None,
    }

    rows = build_favorites_audit_rows(tidal, spotify, catalog)
    by_tidal_id = {row["tidal_id"]: row for row in rows if row["tidal_id"]}
    by_spotify_id = {row["spotify_id"]: row for row in rows if row["spotify_id"]}

    assert by_tidal_id["tidal-matched"]["status"] == "matched"
    assert by_tidal_id["tidal-mismatch"]["status"] == "timestamp_mismatch"
    assert by_tidal_id["tidal-mismatch"]["timestamp_delta_seconds"] > 0
    assert by_tidal_id["tidal-only"]["status"] == "tidal_only"
    assert by_tidal_id["tidal-failed"]["status"] == "match_failed"
    assert by_spotify_id["spotify-only"]["status"] == "spotify_only"


def test_audit_flags_clustered_suspicious_timestamps():
    tidal = [
        tidal_track(f"tidal-{index}", f"ISRC{index}", datetime.datetime(2020, 1, index + 1, tzinfo=UTC))
        for index in range(3)
    ]
    spotify = [
        saved_item(f"spotify-{index}", f"ISRC{index}", "2026-04-27T12:00:00Z")
        for index in range(3)
    ]

    rows = build_favorites_audit_rows(tidal, spotify)

    assert {row["status"] for row in rows} == {"timestamp_mismatch"}
    assert {row["spotify_added_cluster_size"] for row in rows} == {3}
    assert all("clustered_spotify_added_at" in row["timestamp_suspect_reason"] for row in rows)


def test_audit_is_account_read_only_and_writes_csv(mocker, tmp_path):
    tidal_favorite = tidal_track(
        "tidal-matched",
        "ISRC1",
        datetime.datetime(2024, 1, 1, tzinfo=UTC),
    )
    favorites = MagicMock()
    tidal_session = SimpleNamespace(user=SimpleNamespace(favorites=favorites))
    spotify_session = MagicMock()
    spotify_session.current_user_saved_tracks.return_value = {
        "items": [saved_item("spotify-matched", "ISRC1", "2024-01-02T00:00:00Z")],
        "next": None,
        "limit": 50,
        "total": 1,
    }
    mocker.patch(
        "spotify_to_tidal.audit.get_all_favorites",
        new=mocker.AsyncMock(return_value=[tidal_favorite]),
    )
    output = tmp_path / "audit.csv"

    result = asyncio.run(audit_favorites(spotify_session, tidal_session, {}, output))

    assert result == output
    with output.open(newline="", encoding="utf-8") as report:
        rows = list(csv.DictReader(report))
    assert rows[0]["status"] == "matched"
    spotify_session.current_user_saved_tracks_add.assert_not_called()
    spotify_session.current_user_saved_tracks_delete.assert_not_called()
    spotify_session.playlist_add_items.assert_not_called()
    spotify_session.playlist_replace_items.assert_not_called()
    favorites.add_track.assert_not_called()
    favorites.remove_track.assert_not_called()


def test_audit_cli_dispatches_only_to_audit(mocker, tmp_path):
    config = tmp_path / "config.yml"
    config.write_text(
        "spotify:\n  username: test\n  client_id: id\n  client_secret: secret\n"
        "  redirect_uri: http://127.0.0.1/callback\n",
        encoding="utf-8",
    )
    spotify_session = MagicMock()
    tidal_session = MagicMock()
    tidal_session.check_login.return_value = True
    mocker.patch("spotify_to_tidal.__main__._auth.open_spotify_session", return_value=spotify_session)
    mocker.patch("spotify_to_tidal.__main__._auth.open_tidal_session", return_value=tidal_session)
    audit_wrapper = mocker.patch("spotify_to_tidal.__main__._audit.audit_favorites_wrapper")
    sync_favorites = mocker.patch("spotify_to_tidal.__main__._sync.sync_favorites_wrapper")
    reverse_favorites = mocker.patch("spotify_to_tidal.__main__._sync.sync_favorites_tidal_to_spotify_wrapper")
    sync_playlists = mocker.patch("spotify_to_tidal.__main__._sync.sync_playlists_wrapper")
    output = tmp_path / "report.csv"
    mocker.patch(
        "sys.argv",
        [
            "spotify_to_tidal",
            "--config", str(config),
            "--audit-favorites",
            "--audit-output", str(output),
        ],
    )

    __main__.main()

    audit_wrapper.assert_called_once_with(
        spotify_session,
        tidal_session,
        mocker.ANY,
        str(output),
    )
    sync_favorites.assert_not_called()
    reverse_favorites.assert_not_called()
    sync_playlists.assert_not_called()


def test_audit_catalog_search_does_not_modify_failure_cache(mocker):
    track = tidal_track(
        "tidal-catalog",
        "ISRCCATALOG",
        datetime.datetime(2024, 1, 1, tzinfo=UTC),
    )
    spotify_session = MagicMock()
    spotify_session.search.return_value = {
        "tracks": {"items": [spotify_track("spotify-catalog", "ISRCCATALOG")]}
    }
    remove_failure = mocker.patch(
        "spotify_to_tidal.sync.failure_cache.remove_match_failure"
    )
    cache_failure = mocker.patch(
        "spotify_to_tidal.sync.failure_cache.cache_match_failure"
    )

    result = asyncio.run(
        spotify_search(
            track,
            asyncio.Semaphore(1),
            spotify_session,
            record_failure=False,
        )
    )

    assert result["id"] == "spotify-catalog"
    remove_failure.assert_not_called()
    cache_failure.assert_not_called()


def test_web_audit_handles_tidal_track_without_included_artist_metadata():
    track = tidal_track(
        "tidal-no-artist",
        "ISRCNOARTIST",
        datetime.datetime(2024, 1, 1, tzinfo=UTC),
    )
    track.artists = []
    spotify_session = MagicMock()
    spotify_session.current_user_saved_tracks.return_value = {
        "items": [],
        "next": None,
        "limit": 50,
        "total": 0,
    }
    spotify_session.search.return_value = {"tracks": {"items": []}}

    rows = asyncio.run(
        collect_favorites_audit_rows_from_tracks(
            spotify_session,
            [track],
            {},
        )
    )

    assert rows[0]["status"] == "match_failed"
    spotify_session.search.assert_called_once_with(
        q="isrc:ISRCNOARTIST",
        type="track",
    )
