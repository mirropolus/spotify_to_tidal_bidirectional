import asyncio
import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from spotify_to_tidal.sync import sync_favorites


UTC = datetime.timezone.utc


def spotify_track(track_id: str, isrc: str) -> dict:
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


def tidal_track(track_id, isrc: str, *, available: bool = True):
    return SimpleNamespace(
        id=track_id,
        isrc=isrc,
        name=f"Title {isrc}",
        duration=200,
        artists=[SimpleNamespace(name=f"Artist {isrc}")],
        version=None,
        available=available,
        date_added=datetime.datetime(2024, 1, 1, tzinfo=UTC),
        user_date_added=datetime.datetime(2024, 1, 1, tzinfo=UTC),
    )


def sessions(candidate=None):
    spotify = MagicMock()
    favorites = MagicMock()
    tidal = MagicMock()
    tidal.user.favorites = favorites
    tidal.track.return_value = candidate
    return spotify, tidal, favorites


def patch_saved_libraries(mocker, spotify_tracks, tidal_tracks):
    fetch_spotify = mocker.patch(
        "spotify_to_tidal.sync._fetch_all_from_spotify_in_chunks",
        new=mocker.AsyncMock(return_value=spotify_tracks),
    )
    fetch_tidal = mocker.patch(
        "spotify_to_tidal.sync.get_all_favorites",
        new=mocker.AsyncMock(return_value=tidal_tracks),
    )
    catalog_search = mocker.patch(
        "spotify_to_tidal.sync.search_new_tracks_on_tidal",
        new=mocker.AsyncMock(),
    )
    return fetch_spotify, fetch_tidal, catalog_search


def test_approved_plan_writes_only_reviewed_exact_pair(mocker):
    approved = spotify_track("spotify-approved", "ISRC-APPROVED")
    unapproved = spotify_track("spotify-unapproved", "ISRC-UNAPPROVED")
    candidate = tidal_track(123, "ISRC-APPROVED")
    spotify, tidal, favorites = sessions(candidate)
    _, _, catalog_search = patch_saved_libraries(
        mocker,
        [approved, unapproved],
        [],
    )

    asyncio.run(sync_favorites(
        spotify,
        tidal,
        {},
        {"spotify-approved": "123"},
    ))

    tidal.track.assert_called_once_with(123)
    favorites.add_track.assert_called_once_with(123)
    catalog_search.assert_not_awaited()


@pytest.mark.parametrize(
    "candidate",
    [
        tidal_track(123, "DIFFERENT-ISRC"),
        tidal_track(999, "ISRC-APPROVED"),
        tidal_track(123, "ISRC-APPROVED", available=False),
        SimpleNamespace(id=123, available=True),
    ],
)
def test_approved_plan_skips_candidate_that_no_longer_matches_review(mocker, candidate):
    approved = spotify_track("spotify-approved", "ISRC-APPROVED")
    spotify, tidal, favorites = sessions(candidate)
    _, _, catalog_search = patch_saved_libraries(mocker, [approved], [])

    asyncio.run(sync_favorites(
        spotify,
        tidal,
        {},
        {"spotify-approved": "123"},
    ))

    favorites.add_track.assert_not_called()
    catalog_search.assert_not_awaited()


def test_approved_plan_skips_source_no_longer_liked(mocker):
    spotify, tidal, favorites = sessions(tidal_track(123, "ISRC-APPROVED"))
    _, _, catalog_search = patch_saved_libraries(mocker, [], [])

    asyncio.run(sync_favorites(
        spotify,
        tidal,
        {},
        {"spotify-approved": "123"},
    ))

    tidal.track.assert_not_called()
    favorites.add_track.assert_not_called()
    catalog_search.assert_not_awaited()


def test_approved_plan_does_not_touch_existing_tidal_favorite(mocker):
    approved = spotify_track("spotify-approved", "ISRC-APPROVED")
    existing = tidal_track(123, "ISRC-APPROVED")
    spotify, tidal, favorites = sessions(existing)
    _, _, catalog_search = patch_saved_libraries(mocker, [approved], [existing])

    asyncio.run(sync_favorites(
        spotify,
        tidal,
        {},
        {"spotify-approved": "123"},
    ))

    tidal.track.assert_not_called()
    favorites.add_track.assert_not_called()
    catalog_search.assert_not_awaited()


def test_empty_approved_plan_performs_no_catalog_search_or_write(mocker):
    spotify, tidal, favorites = sessions()
    _, _, catalog_search = patch_saved_libraries(
        mocker,
        [spotify_track("spotify-one", "ISRC-ONE")],
        [],
    )

    asyncio.run(sync_favorites(spotify, tidal, {}, {}))

    tidal.track.assert_not_called()
    favorites.add_track.assert_not_called()
    catalog_search.assert_not_awaited()
