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
    load_audit_crosscheck,
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

    config = {"dry_run_origin_cluster_size": 99}
    rows = asyncio.run(
        collect_favorites_dry_run_rows(
            spotify,
            tidal,
            config,
            "bidirectional",
        )
    )

    assert len(rows) == 2
    assert {(row["direction"], row["action"]) for row in rows} == {
        ("tidal_to_spotify", "would_add"),
        ("spotify_to_tidal", "would_add"),
    }
    tidal_row = next(row for row in rows if row["source_service"] == "tidal")
    assert tidal_row["source_id"] == "tidal-only"
    assert tidal_row["source_date_added"] == "2021-05-12T14:32:10.000Z"
    assert tidal_row["target_isrc"] == "ISRC2"
    assert tidal_row["target_artist"] == "Artist ISRC2"
    assert tidal_row["target_title"] == "Title ISRC2"
    assert tidal_row["source_duration_ms"] == 200000
    assert tidal_row["target_duration_ms"] == 200000
    assert tidal_row["match_method"] == "exact_isrc"
    spotify_catalog.assert_awaited_once()
    assert [track.id for track in spotify_catalog.await_args.args[1]] == ["tidal-only"]
    tidal_catalog.assert_awaited_once_with(tidal, [spotify_only], config)
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
        None,
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


def test_dry_run_coalesces_duplicate_destination_targets(mocker):
    first = saved_item(
        "spotify-one",
        "ISRC1",
        "2026-01-01T00:00:00Z",
    )
    second = saved_item(
        "spotify-two",
        "ISRC1",
        "2026-01-02T00:00:00Z",
    )
    candidate = tidal_track("tidal-one", "ISRC1", None)
    spotify = MagicMock()
    favorites = MagicMock()
    tidal = SimpleNamespace(user=SimpleNamespace(favorites=favorites))
    mocker.patch(
        "spotify_to_tidal.dry_run.get_all_favorites",
        new=mocker.AsyncMock(return_value=[]),
    )
    mocker.patch(
        "spotify_to_tidal.dry_run.get_spotify_saved_track_items",
        new=mocker.AsyncMock(return_value=[first, second]),
    )
    mocker.patch(
        "spotify_to_tidal.dry_run._tidal_catalog_matches",
        new=mocker.AsyncMock(return_value=[candidate, candidate]),
    )

    rows = asyncio.run(collect_favorites_dry_run_rows(
        spotify,
        tidal,
        {},
        "spotify_to_tidal",
    ))

    assert [row["action"] for row in rows].count("would_add") == 1
    assert [row["action"] for row in rows].count("duplicate_target") == 1
    assert {row["target_candidate_id"] for row in rows} == {"tidal-one"}
    assert {
        row["coalesced_source_ids"] for row in rows
    } == {"spotify-one;spotify-two"}
    assert all(
        "duplicate_destination_target" in row["safety_flags"]
        for row in rows
    )
    assert all(row["target_isrc"] == "ISRC1" for row in rows)
    assert all(row["match_method"] == "exact_isrc" for row in rows)
    favorites.add_track.assert_not_called()


def test_clustered_spotify_additions_are_origin_suspect_not_would_add(mocker):
    items = [
        saved_item(f"spotify-{index}", f"ISRC{index}", "2026-05-26T08:28:10Z")
        for index in range(3)
    ]
    candidates = [
        tidal_track(f"tidal-{index}", f"ISRC{index}", None)
        for index in range(3)
    ]
    spotify = MagicMock()
    favorites = MagicMock()
    tidal = SimpleNamespace(user=SimpleNamespace(favorites=favorites))
    mocker.patch(
        "spotify_to_tidal.dry_run.get_all_favorites",
        new=mocker.AsyncMock(return_value=[]),
    )
    mocker.patch(
        "spotify_to_tidal.dry_run.get_spotify_saved_track_items",
        new=mocker.AsyncMock(return_value=items),
    )
    mocker.patch(
        "spotify_to_tidal.dry_run._tidal_catalog_matches",
        new=mocker.AsyncMock(return_value=candidates),
    )

    rows = asyncio.run(collect_favorites_dry_run_rows(
        spotify,
        tidal,
        {"dry_run_origin_cluster_size": 3},
        "spotify_to_tidal",
    ))

    assert {row["action"] for row in rows} == {"origin_suspect"}
    assert {row["source_added_cluster_size"] for row in rows} == {3}
    assert all(
        row["safety_flags"] == "clustered_spotify_added_at"
        for row in rows
    )
    favorites.add_track.assert_not_called()


def test_prior_audit_match_blocks_contradictory_plan_row(mocker, tmp_path):
    audit = tmp_path / "audit.csv"
    audit.write_text(
        "tidal_id,spotify_id,status\n"
        "tidal-existing,spotify-one,timestamp_mismatch\n",
        encoding="utf-8",
    )
    spotify_item = saved_item(
        "spotify-one",
        "ISRC1",
        "2026-01-01T00:00:00Z",
    )
    alternative = tidal_track("tidal-alternative", "ISRC1", None)
    spotify = MagicMock()
    favorites = MagicMock()
    tidal = SimpleNamespace(user=SimpleNamespace(favorites=favorites))
    mocker.patch(
        "spotify_to_tidal.dry_run.get_all_favorites",
        new=mocker.AsyncMock(return_value=[]),
    )
    mocker.patch(
        "spotify_to_tidal.dry_run.get_spotify_saved_track_items",
        new=mocker.AsyncMock(return_value=[spotify_item]),
    )
    mocker.patch(
        "spotify_to_tidal.dry_run._tidal_catalog_matches",
        new=mocker.AsyncMock(return_value=[alternative]),
    )

    rows = asyncio.run(collect_favorites_dry_run_rows(
        spotify,
        tidal,
        {},
        "spotify_to_tidal",
        audit,
    ))

    assert len(rows) == 1
    row = rows[0]
    assert row["action"] == "audit_conflict"
    assert row["audit_status"] == "timestamp_mismatch"
    assert row["audit_target_ids"] == "tidal-existing"
    assert "prior_audit_cross_service_match" in row["safety_flags"]
    favorites.add_track.assert_not_called()


def test_metadata_only_catalog_match_requires_manual_review(mocker):
    spotify_item = saved_item(
        "spotify-one",
        "SOURCE-ISRC",
        "2026-01-01T00:00:00Z",
    )
    candidate = tidal_track("tidal-one", "TARGET-ISRC", None)
    candidate.name = spotify_item["track"]["name"]
    candidate.artists = [SimpleNamespace(name="Artist SOURCE-ISRC")]
    spotify = MagicMock()
    favorites = MagicMock()
    tidal = SimpleNamespace(user=SimpleNamespace(favorites=favorites))
    mocker.patch(
        "spotify_to_tidal.dry_run.get_all_favorites",
        new=mocker.AsyncMock(return_value=[]),
    )
    mocker.patch(
        "spotify_to_tidal.dry_run.get_spotify_saved_track_items",
        new=mocker.AsyncMock(return_value=[spotify_item]),
    )
    mocker.patch(
        "spotify_to_tidal.dry_run._tidal_catalog_matches",
        new=mocker.AsyncMock(return_value=[candidate]),
    )

    rows = asyncio.run(collect_favorites_dry_run_rows(
        spotify,
        tidal,
        {},
        "spotify_to_tidal",
    ))

    assert rows[0]["action"] == "metadata_review"
    assert rows[0]["match_method"] == "metadata"
    assert rows[0]["isrc"] == "SOURCE-ISRC"
    assert rows[0]["target_isrc"] == "TARGET-ISRC"
    assert "metadata_match_requires_review" in rows[0]["safety_flags"]
    favorites.add_track.assert_not_called()


def test_audit_crosscheck_does_not_treat_catalog_candidate_as_saved_id(tmp_path):
    audit = tmp_path / "audit.csv"
    audit.write_text(
        "tidal_id,spotify_id,spotify_catalog_candidate_id,status\n"
        "tidal-only,,spotify-catalog,tidal_only\n",
        encoding="utf-8",
    )

    snapshot = load_audit_crosscheck(audit)

    assert snapshot["existing_tidal_ids"] == {"tidal-only"}
    assert snapshot["existing_spotify_ids"] == set()


def test_dry_run_cli_passes_prior_audit_path(mocker, tmp_path):
    config = tmp_path / "config.yml"
    config.write_text("spotify:\n  client_id: test\n", encoding="utf-8")
    audit = tmp_path / "audit.csv"
    audit.write_text("tidal_id,spotify_id,status\n", encoding="utf-8")
    spotify = MagicMock()
    tidal = MagicMock()
    tidal.check_login.return_value = True
    mocker.patch(
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
    mocker.patch("sys.argv", [
        "spotify_to_tidal",
        "--config", str(config),
        "--dry-run",
        "--dry-run-audit-input", str(audit),
    ])

    __main__.main()

    dry_run.assert_called_once_with(
        spotify,
        tidal,
        mocker.ANY,
        "spotify_to_tidal",
        "favorites_dry_run.csv",
        str(audit),
    )


def test_dry_run_cli_rejects_missing_audit_file_before_authentication(
    mocker,
    tmp_path,
):
    config = tmp_path / "config.yml"
    config.write_text("spotify:\n  client_id: test\n", encoding="utf-8")
    missing = tmp_path / "missing.csv"
    open_spotify = mocker.patch(
        "spotify_to_tidal.__main__._auth.open_spotify_session"
    )
    open_tidal = mocker.patch(
        "spotify_to_tidal.__main__._auth.open_tidal_session"
    )
    mocker.patch("sys.argv", [
        "spotify_to_tidal",
        "--config", str(config),
        "--dry-run",
        "--dry-run-audit-input", str(missing),
    ])

    with pytest.raises(SystemExit, match="does not exist or is not a file"):
        __main__.main()

    open_spotify.assert_not_called()
    open_tidal.assert_not_called()


def test_dry_run_cli_rejects_invalid_audit_schema_before_authentication(
    mocker,
    tmp_path,
):
    config = tmp_path / "config.yml"
    config.write_text("spotify:\n  client_id: test\n", encoding="utf-8")
    invalid = tmp_path / "not-an-audit.csv"
    invalid.write_text("track_id,title\n1,Example\n", encoding="utf-8")
    open_spotify = mocker.patch(
        "spotify_to_tidal.__main__._auth.open_spotify_session"
    )
    open_tidal = mocker.patch(
        "spotify_to_tidal.__main__._auth.open_tidal_session"
    )
    mocker.patch("sys.argv", [
        "spotify_to_tidal",
        "--config", str(config),
        "--dry-run",
        "--dry-run-audit-input", str(invalid),
    ])

    with pytest.raises(SystemExit, match="Invalid --dry-run-audit-input"):
        __main__.main()

    open_spotify.assert_not_called()
    open_tidal.assert_not_called()


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
