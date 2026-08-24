"""Read-only favorites auditing for Tidal and Spotify libraries."""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
import contextlib
import csv
import datetime
from pathlib import Path
from typing import Mapping, Sequence

import spotipy
import tidalapi

from .sync import (
    get_spotify_saved_track_items,
    match,
    repeat_on_request_error,
    spotify_added_at,
    spotify_search,
)
from .tidalapi_patch import get_all_favorites


AUDIT_FIELDS = [
    "tidal_id",
    "spotify_id",
    "isrc",
    "artist",
    "title",
    "tidal_date_added",
    "spotify_added_at",
    "status",
    "timestamp_delta_seconds",
    "spotify_added_cluster_size",
    "timestamp_suspect_reason",
]


def _tidal_date_added(track) -> datetime.datetime | None:
    value = (
        getattr(track, "date_added", None)
        or getattr(track, "user_date_added", None)
    )
    return value if isinstance(value, datetime.datetime) else None


def _parse_spotify_datetime(value) -> datetime.datetime | None:
    if isinstance(value, datetime.datetime):
        parsed = value
    elif isinstance(value, str) and value:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def _tidal_metadata(track) -> tuple[str, str, str]:
    artists = ", ".join(
        artist.name for artist in getattr(track, "artists", [])
        if getattr(artist, "name", None)
    )
    return getattr(track, "isrc", "") or "", artists, getattr(track, "name", "") or ""


def _spotify_metadata(track: dict) -> tuple[str, str, str]:
    isrc = track.get("external_ids", {}).get("isrc", "")
    artists = ", ".join(
        artist.get("name", "") for artist in track.get("artists", [])
        if artist.get("name")
    )
    return isrc, artists, track.get("name", "")


def _pair_saved_favorites(
    tidal_tracks: Sequence,
    spotify_saved_items: Sequence[dict],
) -> tuple[list[tuple[object, dict]], list[object], list[dict]]:
    """Pair saved-library entries one-to-one using the existing match predicate."""
    usable_items = [
        item for item in spotify_saved_items
        if item.get("track") is not None
    ]
    available = set(range(len(usable_items)))
    spotify_by_isrc: dict[str, list[int]] = defaultdict(list)
    for index, item in enumerate(usable_items):
        isrc = item["track"].get("external_ids", {}).get("isrc")
        if isrc:
            spotify_by_isrc[isrc].append(index)

    pairs: list[tuple[object, dict]] = []
    tidal_unmatched: list[object] = []
    for tidal_track in tidal_tracks:
        candidate_index = None
        tidal_isrc = getattr(tidal_track, "isrc", None)
        for index in spotify_by_isrc.get(tidal_isrc, []):
            if index in available and match(tidal_track, usable_items[index]["track"]):
                candidate_index = index
                break
        if candidate_index is None:
            for index in sorted(available):
                if match(tidal_track, usable_items[index]["track"]):
                    candidate_index = index
                    break

        if candidate_index is None:
            tidal_unmatched.append(tidal_track)
        else:
            available.remove(candidate_index)
            pairs.append((tidal_track, usable_items[candidate_index]))

    spotify_unmatched = [usable_items[index] for index in sorted(available)]
    return pairs, tidal_unmatched, spotify_unmatched


def build_favorites_audit_rows(
    tidal_tracks: Sequence,
    spotify_saved_items: Sequence[dict],
    catalog_matches: Mapping[str, dict | None] | None = None,
    *,
    mismatch_days: int = 30,
    cluster_size: int = 3,
) -> list[dict]:
    """Build audit rows without making network calls or modifying either account."""
    catalog_matches = catalog_matches or {}
    pairs, tidal_unmatched, spotify_unmatched = _pair_saved_favorites(
        tidal_tracks,
        spotify_saved_items,
    )
    rows: list[dict] = []
    mismatch_seconds = mismatch_days * 24 * 60 * 60

    for tidal_track, spotify_item in pairs:
        spotify_track = spotify_item["track"]
        isrc, artist, title = _tidal_metadata(tidal_track)
        tidal_date = _tidal_date_added(tidal_track)
        spotify_date = _parse_spotify_datetime(spotify_item.get("added_at"))
        delta = None
        status = "matched"
        reason = ""
        if tidal_date is not None and spotify_date is not None:
            tidal_date_utc = _parse_spotify_datetime(tidal_date)
            delta = int((spotify_date - tidal_date_utc).total_seconds())
            if delta > mismatch_seconds:
                status = "timestamp_mismatch"
                reason = "spotify_added_much_later_than_tidal"
        elif tidal_date is None:
            reason = "missing_tidal_date_added"
        elif spotify_date is None:
            reason = "missing_spotify_added_at"

        rows.append({
            "tidal_id": str(tidal_track.id),
            "spotify_id": spotify_track.get("id", ""),
            "isrc": isrc or _spotify_metadata(spotify_track)[0],
            "artist": artist,
            "title": title,
            "tidal_date_added": spotify_added_at(tidal_date) if tidal_date else "",
            "spotify_added_at": spotify_added_at(spotify_date) if spotify_date else "",
            "status": status,
            "timestamp_delta_seconds": delta if delta is not None else "",
            "spotify_added_cluster_size": "",
            "timestamp_suspect_reason": reason,
        })

    for tidal_track in tidal_unmatched:
        catalog_track = catalog_matches.get(str(tidal_track.id))
        isrc, artist, title = _tidal_metadata(tidal_track)
        tidal_date = _tidal_date_added(tidal_track)
        rows.append({
            "tidal_id": str(tidal_track.id),
            "spotify_id": catalog_track.get("id", "") if catalog_track else "",
            "isrc": isrc,
            "artist": artist,
            "title": title,
            "tidal_date_added": spotify_added_at(tidal_date) if tidal_date else "",
            "spotify_added_at": "",
            "status": "tidal_only" if catalog_track else "match_failed",
            "timestamp_delta_seconds": "",
            "spotify_added_cluster_size": "",
            "timestamp_suspect_reason": "missing_tidal_date_added" if tidal_date is None else "",
        })

    for spotify_item in spotify_unmatched:
        spotify_track = spotify_item["track"]
        isrc, artist, title = _spotify_metadata(spotify_track)
        spotify_date = _parse_spotify_datetime(spotify_item.get("added_at"))
        rows.append({
            "tidal_id": "",
            "spotify_id": spotify_track.get("id", ""),
            "isrc": isrc,
            "artist": artist,
            "title": title,
            "tidal_date_added": "",
            "spotify_added_at": spotify_added_at(spotify_date) if spotify_date else "",
            "status": "spotify_only",
            "timestamp_delta_seconds": "",
            "spotify_added_cluster_size": "",
            "timestamp_suspect_reason": "",
        })

    mismatches_by_day = Counter(
        row["spotify_added_at"][:10]
        for row in rows
        if row["status"] == "timestamp_mismatch" and row["spotify_added_at"]
    )
    for row in rows:
        if row["status"] != "timestamp_mismatch":
            continue
        count = mismatches_by_day[row["spotify_added_at"][:10]]
        row["spotify_added_cluster_size"] = count
        if count >= cluster_size:
            row["timestamp_suspect_reason"] += ";clustered_spotify_added_at"

    return rows


async def _catalog_matches_for_unmatched(
    spotify_session: spotipy.Spotify,
    tidal_tracks: Sequence,
    config: dict,
) -> dict[str, dict | None]:
    if not tidal_tracks:
        return {}

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
        results = await asyncio.gather(*[
            repeat_on_request_error(
                spotify_search,
                track,
                semaphore,
                spotify_session,
                record_failure=False,
            )
            for track in tidal_tracks
        ])
    finally:
        limiter_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await limiter_task

    return {
        str(track.id): result
        for track, result in zip(tidal_tracks, results)
    }


async def audit_favorites(
    spotify_session: spotipy.Spotify,
    tidal_session: tidalapi.Session,
    config: dict,
    output_path: str | Path,
) -> Path:
    """Audit both favorites libraries and write a local CSV; account writes are forbidden."""
    rows = await collect_favorites_audit_rows(
        spotify_session,
        tidal_session,
        config,
    )

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", newline="", encoding="utf-8") as report:
        writer = csv.DictWriter(report, fieldnames=AUDIT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    counts = Counter(row["status"] for row in rows)
    summary = ", ".join(f"{status}={count}" for status, count in sorted(counts.items()))
    print(f"Favorites audit written to {destination} ({summary})")
    return destination


async def collect_favorites_audit_rows(
    spotify_session: spotipy.Spotify,
    tidal_session: tidalapi.Session,
    config: dict,
) -> list[dict]:
    """Collect a read-only favorites audit without persisting the resulting report."""
    print("Loading favorite tracks from Tidal (read-only audit)")
    tidal_tracks = await repeat_on_request_error(
        get_all_favorites,
        tidal_session.user.favorites,
        order="DATE",
        order_direction="ASC",
    )
    print("Loading Liked Songs from Spotify (read-only audit)")
    spotify_items = await repeat_on_request_error(
        get_spotify_saved_track_items,
        spotify_session,
    )

    _, tidal_unmatched, _ = _pair_saved_favorites(tidal_tracks, spotify_items)
    catalog_matches = await _catalog_matches_for_unmatched(
        spotify_session,
        tidal_unmatched,
        config,
    )
    return build_favorites_audit_rows(
        tidal_tracks,
        spotify_items,
        catalog_matches,
        mismatch_days=config.get("audit_timestamp_mismatch_days", 30),
        cluster_size=config.get("audit_timestamp_cluster_size", 3),
    )


def audit_favorites_wrapper(
    spotify_session: spotipy.Spotify,
    tidal_session: tidalapi.Session,
    config: dict,
    output_path: str | Path,
) -> Path:
    return asyncio.run(audit_favorites(spotify_session, tidal_session, config, output_path))
