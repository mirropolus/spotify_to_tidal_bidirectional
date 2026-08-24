import asyncio
import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from spotipy.exceptions import SpotifyException

from spotify_to_tidal.sync import (
    SPOTIFY_TIMESTAMPED_TRACK_BATCH_SIZE,
    SpotifyTimestampSaveError,
    save_spotify_tracks_with_timestamps,
    sync_favorites_tidal_to_spotify,
)


UTC = datetime.timezone.utc


def tidal_track(track_id, date_added):
    return SimpleNamespace(
        id=track_id,
        isrc=f"ISRC-{track_id}",
        name=f"Tidal track {track_id}",
        duration=200,
        artists=[SimpleNamespace(name="Artist")],
        version=None,
        date_added=date_added,
        user_date_added=date_added,
    )


def saved_item(spotify_id, added_at="2026-01-01T00:00:00Z"):
    return {
        "added_at": added_at,
        "track": {
            "id": spotify_id,
            "name": f"Tidal track {spotify_id}",
            "duration_ms": 200000,
            "artists": [{"name": "Artist"}],
            "external_ids": {"isrc": f"ISRC-{spotify_id}"},
        },
    }


def test_regression_old_tidal_favorite_is_saved_with_old_timestamp(mocker):
    historical_date = datetime.datetime(2021, 5, 12, 14, 32, 10, tzinfo=UTC)
    track = tidal_track("tidal-1", historical_date)
    spotify = MagicMock()
    tidal = SimpleNamespace(user=SimpleNamespace(favorites=MagicMock()))

    mocker.patch(
        "spotify_to_tidal.sync.get_all_favorites",
        new=mocker.AsyncMock(return_value=[track]),
    )
    mocker.patch(
        "spotify_to_tidal.sync.get_spotify_saved_track_items",
        new=mocker.AsyncMock(return_value=[]),
    )
    mocker.patch(
        "spotify_to_tidal.sync.search_new_tracks_on_spotify",
        new=mocker.AsyncMock(),
    )
    mocker.patch(
        "spotify_to_tidal.sync.reverse_track_match_cache.get",
        return_value="spotify-1",
    )

    asyncio.run(sync_favorites_tidal_to_spotify(spotify, tidal, {}))

    spotify._put.assert_called_once_with(
        "me/tracks",
        payload={
            "timestamped_ids": [{
                "id": "spotify-1",
                "added_at": "2021-05-12T14:32:10.000Z",
            }]
        },
    )
    spotify.current_user_saved_tracks_add.assert_not_called()


def test_existing_spotify_like_is_untouched(mocker):
    track = tidal_track("tidal-1", datetime.datetime(2021, 1, 1, tzinfo=UTC))
    spotify = MagicMock()
    tidal = SimpleNamespace(user=SimpleNamespace(favorites=MagicMock()))

    mocker.patch(
        "spotify_to_tidal.sync.get_all_favorites",
        new=mocker.AsyncMock(return_value=[track]),
    )
    mocker.patch(
        "spotify_to_tidal.sync.get_spotify_saved_track_items",
        new=mocker.AsyncMock(return_value=[saved_item("spotify-1")]),
    )
    mocker.patch(
        "spotify_to_tidal.sync.search_new_tracks_on_spotify",
        new=mocker.AsyncMock(),
    )
    mocker.patch(
        "spotify_to_tidal.sync.reverse_track_match_cache.get",
        return_value="spotify-1",
    )

    asyncio.run(sync_favorites_tidal_to_spotify(spotify, tidal, {}))

    spotify._put.assert_not_called()
    spotify.current_user_saved_tracks_add.assert_not_called()


def test_existing_semantic_like_with_different_id_is_untouched(mocker):
    track = tidal_track("tidal-1", datetime.datetime(2021, 1, 1, tzinfo=UTC))
    existing = saved_item("spotify-existing")
    existing["track"]["external_ids"]["isrc"] = track.isrc
    existing["track"]["name"] = track.name
    spotify = MagicMock()
    tidal = SimpleNamespace(user=SimpleNamespace(favorites=MagicMock()))

    mocker.patch(
        "spotify_to_tidal.sync.get_all_favorites",
        new=mocker.AsyncMock(return_value=[track]),
    )
    mocker.patch(
        "spotify_to_tidal.sync.get_spotify_saved_track_items",
        new=mocker.AsyncMock(return_value=[existing]),
    )
    search = mocker.patch(
        "spotify_to_tidal.sync.search_new_tracks_on_spotify",
        new=mocker.AsyncMock(),
    )
    mocker.patch(
        "spotify_to_tidal.sync.reverse_track_match_cache.get",
        return_value="spotify-alternative-catalog-id",
    )

    asyncio.run(sync_favorites_tidal_to_spotify(spotify, tidal, {}))

    search.assert_awaited_once_with(spotify, [], "Favorites", {})
    spotify._put.assert_not_called()
    spotify.current_user_saved_tracks_add.assert_not_called()


def test_duplicate_tidal_favorite_uses_earliest_date_once(mocker):
    later = tidal_track("tidal-1", datetime.datetime(2025, 1, 1, tzinfo=UTC))
    earlier = tidal_track("tidal-1", datetime.datetime(2021, 1, 1, tzinfo=UTC))
    spotify = MagicMock()
    tidal = SimpleNamespace(user=SimpleNamespace(favorites=MagicMock()))

    mocker.patch(
        "spotify_to_tidal.sync.get_all_favorites",
        new=mocker.AsyncMock(return_value=[later, earlier]),
    )
    mocker.patch(
        "spotify_to_tidal.sync.get_spotify_saved_track_items",
        new=mocker.AsyncMock(return_value=[]),
    )
    mocker.patch(
        "spotify_to_tidal.sync.search_new_tracks_on_spotify",
        new=mocker.AsyncMock(),
    )
    mocker.patch(
        "spotify_to_tidal.sync.reverse_track_match_cache.get",
        return_value="spotify-1",
    )

    asyncio.run(sync_favorites_tidal_to_spotify(spotify, tidal, {}))

    payload = spotify._put.call_args.kwargs["payload"]
    assert payload == {
        "timestamped_ids": [{
            "id": "spotify-1",
            "added_at": "2021-01-01T00:00:00.000Z",
        }]
    }


def test_duplicate_tidal_favorite_compares_naive_and_aware_dates_safely(mocker):
    later_naive = tidal_track("tidal-1", datetime.datetime(2025, 1, 1))
    earlier_aware = tidal_track(
        "tidal-1",
        datetime.datetime(2021, 1, 1, tzinfo=UTC),
    )
    spotify = MagicMock()
    tidal = SimpleNamespace(user=SimpleNamespace(favorites=MagicMock()))
    mocker.patch(
        "spotify_to_tidal.sync.get_all_favorites",
        new=mocker.AsyncMock(return_value=[later_naive, earlier_aware]),
    )
    mocker.patch(
        "spotify_to_tidal.sync.get_spotify_saved_track_items",
        new=mocker.AsyncMock(return_value=[]),
    )
    mocker.patch(
        "spotify_to_tidal.sync.search_new_tracks_on_spotify",
        new=mocker.AsyncMock(),
    )
    mocker.patch(
        "spotify_to_tidal.sync.reverse_track_match_cache.get",
        return_value="spotify-1",
    )

    asyncio.run(sync_favorites_tidal_to_spotify(spotify, tidal, {}))

    payload = spotify._put.call_args.kwargs["payload"]
    assert payload["timestamped_ids"][0]["added_at"] == "2021-01-01T00:00:00.000Z"


def test_timestamped_saves_use_spotify_batch_limit():
    spotify = MagicMock()
    count = SPOTIFY_TIMESTAMPED_TRACK_BATCH_SIZE + 7
    tracks = [
        (f"spotify-{index}", datetime.datetime(2020, 1, 1, tzinfo=UTC))
        for index in range(count)
    ]

    asyncio.run(save_spotify_tracks_with_timestamps(spotify, tracks))

    assert spotify._put.call_count == 2
    first_payload = spotify._put.call_args_list[0].kwargs["payload"]
    second_payload = spotify._put.call_args_list[1].kwargs["payload"]
    assert len(first_payload["timestamped_ids"]) == SPOTIFY_TIMESTAMPED_TRACK_BATCH_SIZE
    assert len(second_payload["timestamped_ids"]) == 7


def test_missing_date_added_is_logged_and_skipped(mocker, caplog):
    track = tidal_track("tidal-missing", None)
    spotify = MagicMock()
    tidal = SimpleNamespace(user=SimpleNamespace(favorites=MagicMock()))

    mocker.patch(
        "spotify_to_tidal.sync.get_all_favorites",
        new=mocker.AsyncMock(return_value=[track]),
    )
    mocker.patch(
        "spotify_to_tidal.sync.get_spotify_saved_track_items",
        new=mocker.AsyncMock(return_value=[]),
    )
    mocker.patch(
        "spotify_to_tidal.sync.search_new_tracks_on_spotify",
        new=mocker.AsyncMock(),
    )
    mocker.patch(
        "spotify_to_tidal.sync.reverse_track_match_cache.get",
        return_value="spotify-missing",
    )

    asyncio.run(sync_favorites_tidal_to_spotify(spotify, tidal, {}))

    spotify._put.assert_not_called()
    spotify.current_user_saved_tracks_add.assert_not_called()
    assert "date_added is missing or invalid" in caplog.text
    assert "no timestamp was invented" in caplog.text


def test_timestamp_conversion_supports_offsets():
    spotify = MagicMock()
    timestamp = datetime.datetime(
        2021, 5, 12, 16, 32, 10,
        tzinfo=datetime.timezone(datetime.timedelta(hours=2)),
    )

    asyncio.run(save_spotify_tracks_with_timestamps(spotify, [("spotify-1", timestamp)]))

    payload = spotify._put.call_args.kwargs["payload"]
    assert payload["timestamped_ids"][0]["added_at"] == "2021-05-12T14:32:10.000Z"


def test_timestamp_conversion_supports_naive_datetime(caplog):
    spotify = MagicMock()
    timestamp = datetime.datetime(2021, 5, 12, 14, 32, 10)

    asyncio.run(save_spotify_tracks_with_timestamps(spotify, [("spotify-1", timestamp)]))

    payload = spotify._put.call_args.kwargs["payload"]
    assert payload["timestamped_ids"][0]["added_at"] == "2021-05-12T14:32:10.000Z"
    assert "treated as UTC" in caplog.text


def test_spotify_payload_rejection_is_clear_and_has_no_fallback():
    spotify = MagicMock()
    spotify._put.side_effect = SpotifyException(400, -1, "invalid payload")
    timestamp = datetime.datetime(2021, 5, 12, tzinfo=UTC)

    with pytest.raises(SpotifyTimestampSaveError, match="no non-timestamped fallback"):
        asyncio.run(
            save_spotify_tracks_with_timestamps(
                spotify,
                [("spotify-1", timestamp)],
            )
        )

    spotify.current_user_saved_tracks_add.assert_not_called()
