import asyncio
import csv
import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from spotify_to_tidal import __main__
from spotify_to_tidal.dry_run import (
    collect_favorites_dry_run_rows,
    favorites_dry_run,
)
from spotify_to_tidal.sync import tidal_search


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
        "album": {
            "name": f"Album {isrc}",
            "artists": [{"name": f"Artist {isrc}"}],
        },
        "track_number": 1,
    }


def saved_item(track_id, isrc, added_at="2026-01-01T00:00:00Z"):
    return {"added_at": added_at, "track": spotify_track(track_id, isrc)}


def test_bidirectional_dry_run_outputs_only_semantic_differences_and_never_writes(mocker):
    existing_tidal = tidal_track(
        "tidal-existing",
        "ISRC1",
        datetime.datetime(2024, 1, 1, tzinfo=UTC),
    )
    tidal_only_later = tidal_track(
        "tidal-only",
        "ISRC2",
        datetime.datetime(2025, 1, 1, tzinfo=UTC),
    )
    tidal_only_earlier = tidal_track(
        "tidal-only",
        "ISRC2",
        datetime.datetime(2021, 5, 12, 14, 32, 10, tzinfo=UTC),
    )
    spotify_existing = saved_item("spotify-existing", "ISRC1")
    spotify_alternative = saved_item("spotify-alternative", "ISRC1")
    spotify_only = saved_item("spotify-only", "ISRC3")
    spotify = MagicMock()
    favorites = MagicMock()
    tidal = SimpleNamespace(user=SimpleNamespace(favorites=favorites))
    mocker.patch(
        "spotify_to_tidal.dry_run.get_all_favorites",
        new=mocker.AsyncMock(return_value=[
            existing_tidal,
            tidal_only_later,
            tidal_only_earlier,
        ]),
    )
    mocker.patch(
        "spotify_to_tidal.dry_run.get_spotify_saved_track_items",
        new=mocker.AsyncMock(return_value=[
            spotify_existing,
            spotify_alternative,
            spotify_only,
        ]),
    )
    spotify_candidate = spotify_track("spotify-candidate", "ISRC2")
    spotify_catalog = mocker.patch(
        "spotify_to_tidal.dry_run._catalog_matches_for_unmatched",
        new=mocker.AsyncMock(return_value={"tidal-only": spotify_candidate}),
    )
    tidal_candidate = tidal_track("tidal-candidate", "ISRC3", None)
    tidal_catalog = mocker.patch(
        "spotify_to_tidal.dry_run._tidal_catalog_matches",
        new=mocker.AsyncMock(return_value=[tidal_candidate]),
    )

    rows = asyncio.run(
        collect_favorites_dry_run_rows(spotify, tidal, {}, "bidirectional")
    )

    assert len(rows) == 2
    assert {(row["direction"], row["action"]) for row in rows} == {
        ("tidal_to_spotify", "would_add"),
        ("spotify_to_tidal", "would_add"),
    }
    tidal_row = next(row for row in rows if row["source_service"] == "tidal")
    assert tidal_row["source_id"] == "tidal-only"
    assert tidal_row["source_date_added"] == "2021-05-12T14:32:10.000Z"
    spotify_catalog.assert_awaited_once()
    assert [track.id for track in spotify_catalog.await_args.args[1]] == ["tidal-only"]
    tidal_catalog.assert_awaited_once_with(tidal, [spotify_only], {})
    spotify._put.assert_not_called()
    spotify.current_user_saved_tracks_add.assert_not_called()
    spotify.current_user_saved_tracks_delete.assert_not_called()
    spotify.playlist_add_items.assert_not_called()
    favorites.add_track.assert_not_called()
    favorites.remove_track.assert_not_called()


def test_dry_run_csv_contains_blocked_and_failed_rows_without_writes(mocker, tmp_path):
    missing_date = tidal_track("tidal-missing", "ISRC1", None)
    no_match = tidal_track(
        "tidal-no-match",
        "ISRC2",
        datetime.datetime(2022, 1, 1, tzinfo=UTC),
    )
    spotify = MagicMock()
    favorites = MagicMock()
    tidal = SimpleNamespace(user=SimpleNamespace(favorites=favorites))
    mocker.patch(
        "spotify_to_tidal.dry_run.get_all_favorites",
        new=mocker.AsyncMock(return_value=[missing_date, no_match]),
    )
    mocker.patch(
        "spotify_to_tidal.dry_run.get_spotify_saved_track_items",
        new=mocker.AsyncMock(return_value=[]),
    )
    mocker.patch(
        "spotify_to_tidal.dry_run._catalog_matches_for_unmatched",
        new=mocker.AsyncMock(return_value={
            "tidal-missing": spotify_track("spotify-candidate", "ISRC1"),
            "tidal-no-match": None,
        }),
    )
    output = tmp_path / "plan.csv"

    asyncio.run(favorites_dry_run(spotify, tidal, {}, "tidal_to_spotify", output))

    with output.open(newline="", encoding="utf-8") as report:
        rows = list(csv.DictReader(report))
    assert {(row["source_id"], row["action"]) for row in rows} == {
        ("tidal-missing", "blocked"),
        ("tidal-no-match", "match_failed"),
    }
    spotify._put.assert_not_called()
    favorites.add_track.assert_not_called()


def test_dry_run_cli_uses_read_only_spotify_scope_and_no_sync_wrappers(mocker, tmp_path):
    config = tmp_path / "config.yml"
    config.write_text(
        "spotify:\n  username: test\n  client_id: id\n  client_secret: secret\n"
        "  redirect_uri: http://127.0.0.1/callback\n",
        encoding="utf-8",
    )
    spotify = MagicMock()
    tidal = MagicMock()
    tidal.check_login.return_value = True
    open_spotify = mocker.patch(
        "spotify_to_tidal.__main__._auth.open_spotify_session",
        return_value=spotify,
    )
    mocker.patch(
        "spotify_to_tidal.__main__._auth.open_tidal_session",
        return_value=tidal,
    )
    dry_run = mocker.patch(
        "spotify_to_tidal.__main__._dry_run.favorites_dry_run_wrapper"
    )
    forward = mocker.patch("spotify_to_tidal.__main__._sync.sync_favorites_wrapper")
    reverse = mocker.patch(
        "spotify_to_tidal.__main__._sync.sync_favorites_tidal_to_spotify_wrapper"
    )
    output = tmp_path / "plan.csv"
    mocker.patch("sys.argv", [
        "spotify_to_tidal",
        "--config", str(config),
        "--dry-run",
        "--sync-direction", "bidirectional",
        "--dry-run-output", str(output),
    ])

    __main__.main()

    open_spotify.assert_called_once_with(
        mocker.ANY,
        sync_direction="spotify_to_tidal",
    )
    dry_run.assert_called_once_with(
        spotify,
        tidal,
        mocker.ANY,
        "bidirectional",
        str(output),
    )
    forward.assert_not_called()
    reverse.assert_not_called()


def test_read_only_tidal_catalog_search_does_not_modify_match_failure_cache(mocker):
    spotify = spotify_track("spotify-one", "ISRC1")
    candidate = tidal_track("tidal-one", "ISRC1", None)
    tidal = MagicMock()
    tidal.search.side_effect = [
        {"albums": []},
        {"tracks": [candidate]},
    ]
    remove_failure = mocker.patch(
        "spotify_to_tidal.sync.failure_cache.remove_match_failure"
    )
    cache_failure = mocker.patch(
        "spotify_to_tidal.sync.failure_cache.cache_match_failure"
    )

    result = asyncio.run(tidal_search(
        spotify,
        asyncio.Semaphore(2),
        tidal,
        record_failure=False,
    ))

    assert result is candidate
    remove_failure.assert_not_called()
    cache_failure.assert_not_called()


@pytest.mark.parametrize(
    ("extra_args", "message"),
    [
        (["--audit-favorites"], "Choose either --audit-favorites or --dry-run"),
        (["--uri", "spotify:playlist:test"], "--dry-run is favorites-only"),
    ],
)
def test_dry_run_rejects_incompatible_cli_modes_before_authentication(
    mocker,
    tmp_path,
    extra_args,
    message,
):
    config = tmp_path / "config.yml"
    config.write_text("spotify:\n  client_id: test\n", encoding="utf-8")
    open_spotify = mocker.patch(
        "spotify_to_tidal.__main__._auth.open_spotify_session"
    )
    open_tidal = mocker.patch(
        "spotify_to_tidal.__main__._auth.open_tidal_session"
    )
    mocker.patch(
        "sys.argv",
        [
            "spotify_to_tidal",
            "--config",
            str(config),
            "--dry-run",
            *extra_args,
        ],
    )

    with pytest.raises(SystemExit, match=message):
        __main__.main()

    open_spotify.assert_not_called()
    open_tidal.assert_not_called()
