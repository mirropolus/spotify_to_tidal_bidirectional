import asyncio
import csv
import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from spotify_to_tidal import __main__
from spotify_to_tidal.audit import (
    audit_integrity_warnings,
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
    assert by_tidal_id["tidal-only"]["spotify_id"] == ""
    assert by_tidal_id["tidal-only"]["spotify_catalog_candidate_id"] == "spotify-catalog"
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


def test_audit_deduplicates_tidal_ids_and_preserves_earliest_timestamp():
    later = tidal_track(
        "tidal-duplicate",
        "ISRCDUPLICATE",
        datetime.datetime(2025, 1, 30, tzinfo=UTC),
    )
    earlier = tidal_track(
        "tidal-duplicate",
        "ISRCDUPLICATE",
        datetime.datetime(2021, 5, 12, 14, 32, 10, tzinfo=UTC),
    )
    spotify = [
        saved_item(
            "spotify-duplicate",
            "ISRCDUPLICATE",
            "2026-05-26T08:28:12Z",
        )
    ]

    rows = build_favorites_audit_rows([later, earlier], spotify)

    assert len(rows) == 1
    assert rows[0]["tidal_date_added"] == "2021-05-12T14:32:10.000Z"
    assert rows[0]["tidal_source_occurrences"] == 2
    assert rows[0]["tidal_source_duplicate_conflict"] == "date_added_conflict"
    assert audit_integrity_warnings(rows) == [
        "Collapsed 1 duplicate Tidal collection records across 1 IDs; "
        "the earliest timestamp was retained.",
        "1 duplicated Tidal IDs had conflicting source data; inspect the CSV "
        "conflict column before planning changes.",
    ]


def test_audit_reports_duplicate_metadata_conflict():
    first = tidal_track(
        "tidal-duplicate",
        "ISRCONE",
        datetime.datetime(2024, 1, 1, tzinfo=UTC),
    )
    second = tidal_track(
        "tidal-duplicate",
        "ISRCTWO",
        datetime.datetime(2024, 1, 1, tzinfo=UTC),
    )

    rows = build_favorites_audit_rows([first, second], [])

    assert len(rows) == 1
    assert rows[0]["tidal_source_duplicate_conflict"] == "metadata_conflict"


def test_cluster_uses_all_spotify_likes_and_flags_shorter_delta():
    tidal = [
        tidal_track(
            "tidal-paired",
            "ISRCPAIRED",
            datetime.datetime(2026, 4, 28, 8, 28, tzinfo=UTC),
        )
    ]
    spotify = [
        saved_item("spotify-paired", "ISRCPAIRED", "2026-05-26T08:28:10Z"),
        saved_item("spotify-only-1", "ISRCOTHER1", "2026-05-26T08:28:11Z"),
        saved_item("spotify-only-2", "ISRCOTHER2", "2026-05-26T08:28:12Z"),
    ]

    rows = build_favorites_audit_rows(tidal, spotify)
    paired = next(row for row in rows if row["tidal_id"] == "tidal-paired")

    assert paired["status"] == "timestamp_mismatch"
    assert paired["spotify_added_cluster_size"] == 3
    assert paired["timestamp_suspect_reason"] == (
        "spotify_added_later_than_tidal_in_cluster;clustered_spotify_added_at"
    )
    assert {
        row["spotify_added_cluster_size"] for row in rows if row["spotify_id"]
    } == {3}


def test_same_day_timestamp_in_large_cluster_remains_matched():
    tidal = [
        tidal_track(
            "tidal-paired",
            "ISRCPAIRED",
            datetime.datetime(2026, 5, 26, 7, 30, tzinfo=UTC),
        )
    ]
    spotify = [
        saved_item("spotify-paired", "ISRCPAIRED", "2026-05-26T08:28:10Z"),
        saved_item("spotify-only-1", "ISRCOTHER1", "2026-05-26T08:28:11Z"),
        saved_item("spotify-only-2", "ISRCOTHER2", "2026-05-26T08:28:12Z"),
    ]

    rows = build_favorites_audit_rows(tidal, spotify)
    paired = next(row for row in rows if row["tidal_id"] == "tidal-paired")

    assert paired["status"] == "matched"
    assert paired["spotify_added_cluster_size"] == 3
    assert paired["timestamp_suspect_reason"] == ""


def test_audit_deduplicates_spotify_saved_ids_and_keeps_earliest_added_at():
    spotify = [
        saved_item("spotify-duplicate", "ISRCDUPLICATE", "2026-05-26T08:28:12Z"),
        saved_item("spotify-duplicate", "ISRCDUPLICATE", "2024-01-01T00:00:00Z"),
    ]

    rows = build_favorites_audit_rows([], spotify)

    assert len(rows) == 1
    assert rows[0]["spotify_added_at"] == "2024-01-01T00:00:00.000Z"
    assert rows[0]["spotify_source_occurrences"] == 2
    assert rows[0]["spotify_source_duplicate_conflict"] == "added_at_conflict"


def test_spotify_alternative_version_is_matched_equivalent_not_spotify_only():
    tidal = [
        tidal_track(
            "tidal-one",
            "ISRCONE",
            datetime.datetime(2026, 5, 26, 8, 0, tzinfo=UTC),
        )
    ]
    spotify = [
        saved_item("spotify-one", "ISRCONE", "2026-05-26T08:20:00Z"),
        saved_item("spotify-alternative", "ISRCONE", "2026-05-26T08:21:00Z"),
    ]

    rows = build_favorites_audit_rows(tidal, spotify)
    by_spotify = {row["spotify_id"]: row for row in rows if row["spotify_id"]}

    assert by_spotify["spotify-one"]["status"] == "matched"
    alternative = by_spotify["spotify-alternative"]
    assert alternative["status"] == "matched_equivalent"
    assert alternative["matched_tidal_ids"] == "tidal-one"
    assert alternative["matched_spotify_ids"] == "spotify-alternative;spotify-one"
    assert alternative["spotify_match_count"] == 2
    assert "multiple_spotify_equivalents" in alternative["match_ambiguity"]
    assert not any(row["status"] == "spotify_only" for row in rows)


def test_tidal_alternative_version_is_matched_equivalent_not_tidal_only():
    tidal = [
        tidal_track(
            "tidal-one",
            "ISRCONE",
            datetime.datetime(2026, 5, 26, 8, 0, tzinfo=UTC),
        ),
        tidal_track(
            "tidal-alternative",
            "ISRCONE",
            datetime.datetime(2026, 5, 26, 8, 5, tzinfo=UTC),
        ),
    ]
    spotify = [
        saved_item("spotify-one", "ISRCONE", "2026-05-26T08:20:00Z"),
    ]

    rows = build_favorites_audit_rows(
        tidal,
        spotify,
        {"tidal-alternative": spotify_track("catalog-wrongly-used", "ISRCONE")},
    )
    by_tidal = {row["tidal_id"]: row for row in rows if row["tidal_id"]}

    alternative = by_tidal["tidal-alternative"]
    assert alternative["status"] == "matched_equivalent"
    assert alternative["spotify_catalog_candidate_id"] == ""
    assert alternative["matched_spotify_ids"] == "spotify-one"
    assert not any(row["status"] == "tidal_only" for row in rows)


def test_alternative_spotify_version_gets_timestamp_alert_from_tidal_reference():
    tidal = [
        tidal_track(
            "tidal-one",
            "ISRCONE",
            datetime.datetime(2026, 4, 28, 8, 0, tzinfo=UTC),
        )
    ]
    spotify = [
        saved_item("spotify-one", "ISRCONE", "2026-05-26T08:28:10Z"),
        saved_item("spotify-alternative", "ISRCONE", "2026-05-26T08:28:11Z"),
        saved_item("spotify-other", "ISRCOTHER", "2026-05-26T08:28:12Z"),
    ]

    rows = build_favorites_audit_rows(tidal, spotify)
    alternative = next(
        row for row in rows if row["spotify_id"] == "spotify-alternative"
    )

    assert alternative["status"] == "timestamp_mismatch"
    assert alternative["timestamp_reference_tidal_id"] == "tidal-one"
    assert alternative["spotify_added_cluster_size"] == 3
    assert alternative["timestamp_suspect_reason"] == (
        "spotify_added_later_than_tidal_in_cluster;clustered_spotify_added_at"
    )


def test_catalog_search_runs_only_for_semantically_unmatched_tidal_tracks(mocker):
    tidal = [
        tidal_track(
            "tidal-one",
            "ISRCONE",
            datetime.datetime(2026, 5, 26, tzinfo=UTC),
        ),
        tidal_track(
            "tidal-alternative",
            "ISRCONE",
            datetime.datetime(2026, 5, 27, tzinfo=UTC),
        ),
    ]
    spotify_session = MagicMock()
    spotify_session.current_user_saved_tracks.return_value = {
        "items": [saved_item("spotify-one", "ISRCONE", "2026-05-26T01:00:00Z")],
        "next": None,
        "limit": 50,
        "total": 1,
    }
    catalog = mocker.patch(
        "spotify_to_tidal.audit._catalog_matches_for_unmatched",
        new=mocker.AsyncMock(return_value={}),
    )

    rows = asyncio.run(
        collect_favorites_audit_rows_from_tracks(spotify_session, tidal, {})
    )

    catalog.assert_awaited_once_with(spotify_session, [], {})
    assert {row["status"] for row in rows} == {"matched", "matched_equivalent"}


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
