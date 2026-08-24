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
    "spotify_catalog_candidate_id",
    "isrc",
    "artist",
    "title",
    "tidal_date_added",
    "spotify_added_at",
    "status",
    "timestamp_delta_seconds",
    "spotify_added_cluster_size",
    "timestamp_suspect_reason",
    "tidal_source_occurrences",
    "tidal_source_duplicate_conflict",
    "spotify_source_occurrences",
    "spotify_source_duplicate_conflict",
]


class AuditStageError(RuntimeError):
    """A sanitized audit-stage failure safe to display without provider data."""


def _tidal_date_added(track) -> datetime.datetime | None:
    values = [
        value for value in (
            getattr(track, "date_added", None),
            getattr(track, "user_date_added", None),
        )
        if isinstance(value, datetime.datetime)
    ]
    if not values:
        return None
    return min(_parse_spotify_datetime(value) for value in values)


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


def _normalized_text(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _tidal_track_signature(track) -> tuple:
    isrc, artists, title = _tidal_metadata(track)
    return (
        _normalized_text(isrc),
        _normalized_text(artists),
        _normalized_text(title),
        round(float(getattr(track, "duration", 0) or 0), 3),
        _normalized_text(getattr(track, "version", None)),
    )


def _spotify_track_signature(track: dict) -> tuple:
    isrc, artists, title = _spotify_metadata(track)
    return (
        _normalized_text(isrc),
        _normalized_text(artists),
        _normalized_text(title),
        int(track.get("duration_ms", 0) or 0),
    )


def _deduplicate_tidal_tracks(
    tidal_tracks: Sequence,
) -> tuple[list[object], dict[str, dict[str, object]]]:
    """Collapse repeated collection resources and retain their earliest timestamp."""
    groups: dict[str, list[object]] = {}
    order: list[str] = []
    for index, track in enumerate(tidal_tracks):
        raw_id = getattr(track, "id", None)
        key = str(raw_id) if raw_id not in {None, ""} else f"__missing_id_{index}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(track)

    unique: list[object] = []
    info: dict[str, dict[str, object]] = {}
    for key in order:
        tracks = groups[key]
        representative = min(
            tracks,
            key=lambda track: (
                _tidal_date_added(track) is None,
                _tidal_date_added(track) or datetime.datetime.max.replace(
                    tzinfo=datetime.timezone.utc
                ),
                -sum(bool(part) for part in _tidal_track_signature(track)),
            ),
        )
        dates = {
            spotify_added_at(value)
            for track in tracks
            if (value := _tidal_date_added(track)) is not None
        }
        signatures = {_tidal_track_signature(track) for track in tracks}
        conflicts = []
        if len(dates) > 1:
            conflicts.append("date_added_conflict")
        if len(signatures) > 1:
            conflicts.append("metadata_conflict")
        unique.append(representative)
        info[str(getattr(representative, "id", ""))] = {
            "occurrences": len(tracks),
            "conflict": ";".join(conflicts),
        }
    return unique, info


def _deduplicate_spotify_saved_items(
    spotify_saved_items: Sequence[dict],
) -> tuple[list[dict], dict[str, dict[str, object]]]:
    """Defensively collapse repeated saved-track resources by Spotify track ID."""
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for index, item in enumerate(spotify_saved_items):
        track = item.get("track")
        if track is None:
            continue
        raw_id = track.get("id")
        key = str(raw_id) if raw_id not in {None, ""} else f"__missing_id_{index}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(item)

    unique: list[dict] = []
    info: dict[str, dict[str, object]] = {}
    for key in order:
        items = groups[key]
        representative = min(
            items,
            key=lambda item: (
                _parse_spotify_datetime(item.get("added_at")) is None,
                _parse_spotify_datetime(item.get("added_at"))
                or datetime.datetime.max.replace(tzinfo=datetime.timezone.utc),
            ),
        )
        dates = {
            spotify_added_at(value)
            for item in items
            if (value := _parse_spotify_datetime(item.get("added_at"))) is not None
        }
        signatures = {_spotify_track_signature(item["track"]) for item in items}
        conflicts = []
        if len(dates) > 1:
            conflicts.append("added_at_conflict")
        if len(signatures) > 1:
            conflicts.append("metadata_conflict")
        unique.append(representative)
        info[str(representative["track"].get("id", ""))] = {
            "occurrences": len(items),
            "conflict": ";".join(conflicts),
        }
    return unique, info


def _pair_saved_favorites(
    tidal_tracks: Sequence,
    spotify_saved_items: Sequence[dict],
) -> tuple[list[tuple[object, dict]], list[object], list[dict]]:
    """Pair saved-library entries one-to-one using the existing match predicate."""
    tidal_tracks, _ = _deduplicate_tidal_tracks(tidal_tracks)
    usable_items, _ = _deduplicate_spotify_saved_items(spotify_saved_items)
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
    cluster_min_delta_hours: int = 24,
) -> list[dict]:
    """Build audit rows without making network calls or modifying either account."""
    catalog_matches = catalog_matches or {}
    tidal_tracks, tidal_source_info = _deduplicate_tidal_tracks(tidal_tracks)
    spotify_saved_items, spotify_source_info = _deduplicate_spotify_saved_items(
        spotify_saved_items
    )
    pairs, tidal_unmatched, spotify_unmatched = _pair_saved_favorites(
        tidal_tracks,
        spotify_saved_items,
    )
    rows: list[dict] = []
    mismatch_seconds = mismatch_days * 24 * 60 * 60
    cluster_min_delta_seconds = cluster_min_delta_hours * 60 * 60

    def source_fields(tidal_track=None, spotify_track=None) -> dict:
        tidal_info = tidal_source_info.get(
            str(getattr(tidal_track, "id", "")),
            {"occurrences": "", "conflict": ""},
        )
        spotify_info = spotify_source_info.get(
            str(spotify_track.get("id", "")) if spotify_track else "",
            {"occurrences": "", "conflict": ""},
        )
        return {
            "tidal_source_occurrences": tidal_info["occurrences"],
            "tidal_source_duplicate_conflict": tidal_info["conflict"],
            "spotify_source_occurrences": spotify_info["occurrences"],
            "spotify_source_duplicate_conflict": spotify_info["conflict"],
        }

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
            "spotify_catalog_candidate_id": "",
            "isrc": isrc or _spotify_metadata(spotify_track)[0],
            "artist": artist,
            "title": title,
            "tidal_date_added": spotify_added_at(tidal_date) if tidal_date else "",
            "spotify_added_at": spotify_added_at(spotify_date) if spotify_date else "",
            "status": status,
            "timestamp_delta_seconds": delta if delta is not None else "",
            "spotify_added_cluster_size": "",
            "timestamp_suspect_reason": reason,
            **source_fields(tidal_track, spotify_track),
        })

    for tidal_track in tidal_unmatched:
        catalog_track = catalog_matches.get(str(tidal_track.id))
        isrc, artist, title = _tidal_metadata(tidal_track)
        tidal_date = _tidal_date_added(tidal_track)
        rows.append({
            "tidal_id": str(tidal_track.id),
            "spotify_id": "",
            "spotify_catalog_candidate_id": (
                catalog_track.get("id", "") if catalog_track else ""
            ),
            "isrc": isrc,
            "artist": artist,
            "title": title,
            "tidal_date_added": spotify_added_at(tidal_date) if tidal_date else "",
            "spotify_added_at": "",
            "status": "tidal_only" if catalog_track else "match_failed",
            "timestamp_delta_seconds": "",
            "spotify_added_cluster_size": "",
            "timestamp_suspect_reason": "missing_tidal_date_added" if tidal_date is None else "",
            **source_fields(tidal_track),
        })

    for spotify_item in spotify_unmatched:
        spotify_track = spotify_item["track"]
        isrc, artist, title = _spotify_metadata(spotify_track)
        spotify_date = _parse_spotify_datetime(spotify_item.get("added_at"))
        rows.append({
            "tidal_id": "",
            "spotify_id": spotify_track.get("id", ""),
            "spotify_catalog_candidate_id": "",
            "isrc": isrc,
            "artist": artist,
            "title": title,
            "tidal_date_added": "",
            "spotify_added_at": spotify_added_at(spotify_date) if spotify_date else "",
            "status": "spotify_only",
            "timestamp_delta_seconds": "",
            "spotify_added_cluster_size": "",
            "timestamp_suspect_reason": "",
            **source_fields(spotify_track=spotify_track),
        })

    spotify_additions_by_minute = Counter(
        row["spotify_added_at"][:16]
        for row in rows
        if row["spotify_id"] and row["spotify_added_at"]
    )
    for row in rows:
        if not row["spotify_id"] or not row["spotify_added_at"]:
            continue
        count = spotify_additions_by_minute[row["spotify_added_at"][:16]]
        row["spotify_added_cluster_size"] = count
        delta = row["timestamp_delta_seconds"]
        clustered_mismatch = (
            row["tidal_id"]
            and isinstance(delta, int)
            and delta > cluster_min_delta_seconds
            and count >= cluster_size
        )
        if clustered_mismatch:
            if row["status"] == "matched":
                row["status"] = "timestamp_mismatch"
                row["timestamp_suspect_reason"] = "spotify_added_later_than_tidal_in_cluster"
            if "clustered_spotify_added_at" not in row["timestamp_suspect_reason"]:
                separator = ";" if row["timestamp_suspect_reason"] else ""
                row["timestamp_suspect_reason"] += f"{separator}clustered_spotify_added_at"

    return rows


def audit_integrity_warnings(rows: Sequence[dict]) -> list[str]:
    """Summarize source duplicates that were safely collapsed in the report."""
    warnings = []
    for provider, label in (("tidal", "Tidal"), ("spotify", "Spotify")):
        occurrences_field = f"{provider}_source_occurrences"
        conflict_field = f"{provider}_source_duplicate_conflict"
        duplicated = [
            row for row in rows
            if int(row.get(occurrences_field) or 0) > 1
        ]
        if duplicated:
            extra = sum(int(row[occurrences_field]) - 1 for row in duplicated)
            warnings.append(
                f"Collapsed {extra} duplicate {label} collection records across "
                f"{len(duplicated)} IDs; the earliest timestamp was retained."
            )
        conflicts = sum(bool(row.get(conflict_field)) for row in duplicated)
        if conflicts:
            warnings.append(
                f"{conflicts} duplicated {label} IDs had conflicting source data; "
                "inspect the CSV conflict column before planning changes."
            )
    return warnings


async def _catalog_matches_for_unmatched(
    spotify_session: spotipy.Spotify,
    tidal_tracks: Sequence,
    config: dict,
) -> dict[str, dict | None]:
    searchable_tracks = [
        track for track in tidal_tracks
        if getattr(track, "isrc", None)
        or (
            getattr(track, "name", None)
            and any(getattr(artist, "name", None) for artist in getattr(track, "artists", []))
        )
    ]
    if not searchable_tracks:
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

    async def audit_spotify_search(track):
        has_text_metadata = (
            getattr(track, "name", None)
            and any(getattr(artist, "name", None) for artist in getattr(track, "artists", []))
        )
        if has_text_metadata:
            return await spotify_search(
                track,
                semaphore,
                spotify_session,
                record_failure=False,
            )

        # Official OpenAPI can occasionally omit an included artist resource.
        # An ISRC-only lookup remains useful and avoids indexing a missing artist.
        isrc = getattr(track, "isrc", None)
        if not isrc:
            return None
        await semaphore.acquire()
        results = await asyncio.to_thread(
            spotify_session.search,
            q=f"isrc:{isrc}",
            type="track",
        )
        return next(
            (
                candidate for candidate in results.get("tracks", {}).get("items", [])
                if candidate.get("id")
                and candidate.get("external_ids", {}).get("isrc") == isrc
            ),
            None,
        )

    try:
        results = await asyncio.gather(*[
            repeat_on_request_error(
                audit_spotify_search,
                track,
            )
            for track in searchable_tracks
        ])
    finally:
        limiter_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await limiter_task

    return {
        str(track.id): result
        for track, result in zip(searchable_tracks, results)
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
    return await collect_favorites_audit_rows_from_tracks(
        spotify_session,
        tidal_tracks,
        config,
    )


async def collect_favorites_audit_rows_from_tracks(
    spotify_session: spotipy.Spotify,
    tidal_tracks: Sequence,
    config: dict,
) -> list[dict]:
    """Audit a supplied read-only Tidal collection against Spotify Liked Songs."""
    print("Loading Liked Songs from Spotify (read-only audit)")
    try:
        spotify_items = await repeat_on_request_error(
            get_spotify_saved_track_items,
            spotify_session,
        )
    except Exception as exc:
        raise AuditStageError(
            f"Spotify Liked Songs loading failed ({type(exc).__name__})"
        ) from exc

    try:
        _, tidal_unmatched, _ = _pair_saved_favorites(tidal_tracks, spotify_items)
    except Exception as exc:
        raise AuditStageError(
            f"Favorites pairing failed ({type(exc).__name__})"
        ) from exc
    try:
        catalog_matches = await _catalog_matches_for_unmatched(
            spotify_session,
            tidal_unmatched,
            config,
        )
    except Exception as exc:
        raise AuditStageError(
            f"Spotify catalog matching failed ({type(exc).__name__})"
        ) from exc
    try:
        return build_favorites_audit_rows(
            tidal_tracks,
            spotify_items,
            catalog_matches,
            mismatch_days=config.get("audit_timestamp_mismatch_days", 30),
            cluster_size=config.get("audit_timestamp_cluster_size", 3),
            cluster_min_delta_hours=config.get(
                "audit_timestamp_cluster_min_delta_hours",
                24,
            ),
        )
    except Exception as exc:
        raise AuditStageError(
            f"Audit report construction failed ({type(exc).__name__})"
        ) from exc


def audit_favorites_wrapper(
    spotify_session: spotipy.Spotify,
    tidal_session: tidalapi.Session,
    config: dict,
    output_path: str | Path,
) -> Path:
    return asyncio.run(audit_favorites(spotify_session, tidal_session, config, output_path))
