"""Read-only favorites synchronization planning."""

from __future__ import annotations

import asyncio
from collections import Counter
import contextlib
import csv
from pathlib import Path
from typing import Sequence

import spotipy
import tidalapi

from .audit import _catalog_matches_for_unmatched
from .sync import (
    _favorite_datetime,
    deduplicate_spotify_saved_items,
    deduplicate_tidal_favorites,
    get_spotify_saved_track_items,
    repeat_on_request_error,
    semantically_unmatched_spotify_favorites,
    semantically_unmatched_tidal_favorites,
    spotify_added_at,
    tidal_search,
)
from .tidalapi_patch import get_all_favorites
from .type import SyncDirectionLiteral


DRY_RUN_FIELDS = [
    "direction",
    "action",
    "source_service",
    "source_id",
    "target_service",
    "target_candidate_id",
    "isrc",
    "artist",
    "title",
    "source_date_added",
    "reason",
]


def _tidal_metadata(track) -> tuple[str, str, str]:
    artists = ", ".join(
        artist.name for artist in getattr(track, "artists", [])
        if getattr(artist, "name", None)
    )
    return (
        getattr(track, "isrc", "") or "",
        artists,
        getattr(track, "name", "") or "",
    )


def _spotify_metadata(track: dict) -> tuple[str, str, str]:
    artists = ", ".join(
        artist.get("name", "") for artist in track.get("artists", [])
        if artist.get("name")
    )
    return (
        track.get("external_ids", {}).get("isrc", ""),
        artists,
        track.get("name", ""),
    )


async def _tidal_catalog_matches(
    tidal_session: tidalapi.Session,
    spotify_saved_items: Sequence[dict],
    config: dict,
) -> list[object | None]:
    if not spotify_saved_items:
        return []
    max_concurrency = config.get("max_concurrency", 10)
    rate_limit = config.get("rate_limit", 10)
    semaphore = asyncio.Semaphore(max_concurrency)

    async def release_tokens():
        interval = max_concurrency / rate_limit / 4
        while True:
            await asyncio.sleep(interval)
            for _ in range(max(1, round(rate_limit * interval))):
                semaphore.release()

    limiter_task = asyncio.create_task(release_tokens())
    try:
        return list(await asyncio.gather(*[
            repeat_on_request_error(
                tidal_search,
                item["track"],
                semaphore,
                tidal_session,
                record_failure=False,
            )
            for item in spotify_saved_items
        ]))
    finally:
        limiter_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await limiter_task


async def collect_favorites_dry_run_rows(
    spotify_session: spotipy.Spotify,
    tidal_session: tidalapi.Session,
    config: dict,
    direction: SyncDirectionLiteral,
) -> list[dict]:
    """Build a favorites-only plan. Provider collection writes are forbidden."""
    print("Loading favorite tracks from Tidal (read-only dry-run)")
    tidal_tracks = await repeat_on_request_error(
        get_all_favorites,
        tidal_session.user.favorites,
        order="DATE",
        order_direction="ASC",
    )
    tidal_tracks = deduplicate_tidal_favorites(tidal_tracks)

    print("Loading Liked Songs from Spotify (read-only dry-run)")
    spotify_items = await repeat_on_request_error(
        get_spotify_saved_track_items,
        spotify_session,
    )
    spotify_items = deduplicate_spotify_saved_items(spotify_items)

    rows = []
    existing_spotify_ids = {
        str(item["track"]["id"])
        for item in spotify_items
        if item.get("track") and item["track"].get("id")
    }
    existing_tidal_ids = {str(track.id) for track in tidal_tracks}

    if direction in {"tidal_to_spotify", "bidirectional"}:
        tidal_unmatched = semantically_unmatched_tidal_favorites(
            tidal_tracks,
            spotify_items,
        )
        catalog = await _catalog_matches_for_unmatched(
            spotify_session,
            tidal_unmatched,
            config,
        )
        for track in tidal_unmatched:
            candidate = catalog.get(str(track.id))
            candidate_id = str(candidate.get("id", "")) if candidate else ""
            source_date = _favorite_datetime(track)
            if candidate is None:
                action, reason = "match_failed", "no_safe_spotify_catalog_match"
            elif candidate_id in existing_spotify_ids:
                action, reason = "skip_existing", "candidate_id_already_liked"
            elif source_date is None:
                action, reason = "blocked", "missing_tidal_date_added"
            else:
                action, reason = "would_add", "semantically_absent_from_spotify"
            isrc, artist, title = _tidal_metadata(track)
            rows.append({
                "direction": "tidal_to_spotify",
                "action": action,
                "source_service": "tidal",
                "source_id": str(track.id),
                "target_service": "spotify",
                "target_candidate_id": candidate_id,
                "isrc": isrc,
                "artist": artist,
                "title": title,
                "source_date_added": (
                    spotify_added_at(source_date) if source_date else ""
                ),
                "reason": reason,
            })

    if direction in {"spotify_to_tidal", "bidirectional"}:
        spotify_unmatched = semantically_unmatched_spotify_favorites(
            spotify_items,
            tidal_tracks,
        )
        catalog = await _tidal_catalog_matches(
            tidal_session,
            spotify_unmatched,
            config,
        )
        for item, candidate in zip(spotify_unmatched, catalog):
            spotify_track = item["track"]
            candidate_id = str(getattr(candidate, "id", "")) if candidate else ""
            if candidate is None:
                action, reason = "match_failed", "no_safe_tidal_catalog_match"
            elif candidate_id in existing_tidal_ids:
                action, reason = "skip_existing", "candidate_id_already_favorited"
            else:
                action, reason = "would_add", "semantically_absent_from_tidal"
            isrc, artist, title = _spotify_metadata(spotify_track)
            source_date = item.get("added_at") or ""
            rows.append({
                "direction": "spotify_to_tidal",
                "action": action,
                "source_service": "spotify",
                "source_id": str(spotify_track.get("id", "")),
                "target_service": "tidal",
                "target_candidate_id": candidate_id,
                "isrc": isrc,
                "artist": artist,
                "title": title,
                "source_date_added": source_date,
                "reason": reason,
            })

    return rows


async def favorites_dry_run(
    spotify_session: spotipy.Spotify,
    tidal_session: tidalapi.Session,
    config: dict,
    direction: SyncDirectionLiteral,
    output_path: str | Path,
) -> Path:
    rows = await collect_favorites_dry_run_rows(
        spotify_session,
        tidal_session,
        config,
        direction,
    )
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", newline="", encoding="utf-8") as report:
        writer = csv.DictWriter(report, fieldnames=DRY_RUN_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    counts = Counter((row["direction"], row["action"]) for row in rows)
    summary = ", ".join(
        f"{direction}:{action}={count}"
        for (direction, action), count in sorted(counts.items())
    )
    print(f"Favorites dry-run written to {destination} ({summary or 'no changes'})")
    return destination


def favorites_dry_run_wrapper(
    spotify_session: spotipy.Spotify,
    tidal_session: tidalapi.Session,
    config: dict,
    direction: SyncDirectionLiteral,
    output_path: str | Path,
) -> Path:
    return asyncio.run(favorites_dry_run(
        spotify_session,
        tidal_session,
        config,
        direction,
        output_path,
    ))
