"""Read-only favorites synchronization planning."""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
import contextlib
import csv
import datetime
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
    isrc_match,
    match,
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
    "safety_flags",
    "source_service",
    "source_id",
    "target_service",
    "target_candidate_id",
    "isrc",
    "artist",
    "title",
    "target_isrc",
    "target_artist",
    "target_title",
    "source_duration_ms",
    "target_duration_ms",
    "match_method",
    "source_date_added",
    "source_added_cluster_size",
    "audit_status",
    "audit_target_ids",
    "coalesced_source_ids",
    "reason",
]

AUDIT_MATCHED_STATUSES = {
    "matched",
    "matched_equivalent",
    "timestamp_mismatch",
}


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


def _tidal_duration_ms(track) -> int | str:
    duration = getattr(track, "duration", None)
    return round(duration * 1000) if isinstance(duration, (int, float)) else ""


def _spotify_duration_ms(track: dict) -> int | str:
    duration = track.get("duration_ms")
    return round(duration) if isinstance(duration, (int, float)) else ""


def _match_method(tidal_track, spotify_track: dict) -> str:
    try:
        if isrc_match(tidal_track, spotify_track):
            return "exact_isrc"
        if match(tidal_track, spotify_track):
            return "metadata"
    except (AttributeError, KeyError, TypeError):
        pass
    return "catalog_unverified"


def _split_ids(value: str | None) -> set[str]:
    return {
        item.strip() for item in (value or "").split(";")
        if item.strip()
    }


def load_audit_crosscheck(path: str | Path | None) -> dict:
    """Load only saved-library IDs from a prior read-only audit CSV."""
    snapshot = {
        "existing_tidal_ids": set(),
        "existing_spotify_ids": set(),
        "spotify_to_tidal": defaultdict(
            lambda: {"targets": set(), "statuses": set()}
        ),
        "tidal_to_spotify": defaultdict(
            lambda: {"targets": set(), "statuses": set()}
        ),
    }
    if path is None:
        return snapshot

    with Path(path).open(newline="", encoding="utf-8-sig") as report:
        reader = csv.DictReader(report)
        fields = set(reader.fieldnames or [])
        if (
            "status" not in fields
            or not fields.intersection({"tidal_id", "matched_tidal_ids"})
            or not fields.intersection({"spotify_id", "matched_spotify_ids"})
        ):
            raise ValueError(
                "audit CSV requires status plus Tidal and Spotify saved-ID columns"
            )
        for row in reader:
            tidal_ids = _split_ids(row.get("matched_tidal_ids"))
            spotify_ids = _split_ids(row.get("matched_spotify_ids"))
            tidal_ids.update(_split_ids(row.get("tidal_id")))
            spotify_ids.update(_split_ids(row.get("spotify_id")))
            snapshot["existing_tidal_ids"].update(tidal_ids)
            snapshot["existing_spotify_ids"].update(spotify_ids)

            status = row.get("status", "")
            if (
                status not in AUDIT_MATCHED_STATUSES
                or not tidal_ids
                or not spotify_ids
            ):
                continue
            for spotify_id in spotify_ids:
                entry = snapshot["spotify_to_tidal"][spotify_id]
                entry["targets"].update(tidal_ids)
                entry["statuses"].add(status)
            for tidal_id in tidal_ids:
                entry = snapshot["tidal_to_spotify"][tidal_id]
                entry["targets"].update(spotify_ids)
                entry["statuses"].add(status)
    return snapshot


def _parse_datetime(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def _spotify_added_clusters(items: Sequence[dict]) -> Counter:
    minutes = []
    for item in items:
        added_at = _parse_datetime(item.get("added_at"))
        if added_at is not None:
            minutes.append(added_at.strftime("%Y-%m-%dT%H:%MZ"))
    return Counter(minutes)


def _audit_fields(
    snapshot: dict,
    direction: str,
    source_id: str,
    candidate_id: str,
) -> tuple[list[str], str, str]:
    reference = snapshot[direction].get(source_id)
    statuses = sorted(reference["statuses"]) if reference else []
    target_ids = sorted(reference["targets"]) if reference else []
    flags = []
    if target_ids:
        flags.append("prior_audit_cross_service_match")
    existing_key = (
        "existing_tidal_ids"
        if direction == "spotify_to_tidal"
        else "existing_spotify_ids"
    )
    if candidate_id and candidate_id in snapshot[existing_key]:
        flags.append("prior_audit_target_present")
    return flags, ";".join(statuses), ";".join(target_ids)


def _coalesce_destination_targets(rows: list[dict]) -> None:
    groups = defaultdict(list)
    for row in rows:
        candidate_id = row["target_candidate_id"]
        if candidate_id:
            groups[(row["direction"], candidate_id)].append(row)

    precedence = {
        "audit_conflict": 0,
        "skip_existing": 1,
        "blocked": 2,
        "origin_suspect": 3,
        "metadata_review": 4,
        "would_add": 5,
    }
    for group in groups.values():
        if len(group) < 2:
            continue
        source_ids = ";".join(sorted(row["source_id"] for row in group))
        primary = min(
            group,
            key=lambda row: (
                precedence.get(row["action"], 99),
                row["source_date_added"] or "9999",
                row["source_id"],
            ),
        )
        inherited_flags = {
            flag
            for row in group
            for flag in _split_ids(row["safety_flags"])
        }
        inherited_flags.add("duplicate_destination_target")
        for row in group:
            row["coalesced_source_ids"] = source_ids
            row["safety_flags"] = ";".join(sorted(inherited_flags))
            if row is primary:
                continue
            row["action"] = "duplicate_target"
            row["reason"] = "destination_target_coalesced"


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
    audit_input: str | Path | None = None,
) -> list[dict]:
    """Build a favorites-only plan. Provider collection writes are forbidden."""
    audit_snapshot = load_audit_crosscheck(audit_input)
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
    spotify_clusters = _spotify_added_clusters(spotify_items)
    origin_cluster_size = max(
        2,
        int(config.get(
            "dry_run_origin_cluster_size",
            config.get("audit_timestamp_cluster_size", 3),
        )),
    )

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
            match_method = _match_method(track, candidate) if candidate else ""
            source_date = _favorite_datetime(track)
            flags, audit_status, audit_targets = _audit_fields(
                audit_snapshot,
                "tidal_to_spotify",
                str(track.id),
                candidate_id,
            )
            if match_method == "metadata":
                flags.append("metadata_match_requires_review")
            elif match_method == "catalog_unverified":
                flags.append("catalog_match_cannot_be_reverified")
            if any(flag.startswith("prior_audit_") for flag in flags):
                action = "audit_conflict"
                reason = "prior_audit_indicates_destination_presence"
            elif candidate is None:
                action, reason = "match_failed", "no_safe_spotify_catalog_match"
            elif candidate_id in existing_spotify_ids:
                action, reason = "skip_existing", "candidate_id_already_liked"
            elif source_date is None:
                action, reason = "blocked", "missing_tidal_date_added"
            elif "catalog_match_cannot_be_reverified" in flags:
                action, reason = "blocked", "catalog_match_cannot_be_reverified"
            elif "metadata_match_requires_review" in flags:
                action, reason = "metadata_review", "metadata_match_requires_review"
            else:
                action, reason = "would_add", "semantically_absent_from_spotify"
            isrc, artist, title = _tidal_metadata(track)
            target_isrc, target_artist, target_title = (
                _spotify_metadata(candidate) if candidate else ("", "", "")
            )
            rows.append({
                "direction": "tidal_to_spotify",
                "action": action,
                "safety_flags": ";".join(sorted(flags)),
                "source_service": "tidal",
                "source_id": str(track.id),
                "target_service": "spotify",
                "target_candidate_id": candidate_id,
                "isrc": isrc,
                "artist": artist,
                "title": title,
                "target_isrc": target_isrc,
                "target_artist": target_artist,
                "target_title": target_title,
                "source_duration_ms": _tidal_duration_ms(track),
                "target_duration_ms": (
                    _spotify_duration_ms(candidate) if candidate else ""
                ),
                "match_method": match_method,
                "source_date_added": (
                    spotify_added_at(source_date) if source_date else ""
                ),
                "source_added_cluster_size": "",
                "audit_status": audit_status,
                "audit_target_ids": audit_targets,
                "coalesced_source_ids": "",
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
            source_id = str(spotify_track.get("id", ""))
            candidate_id = (
                str(getattr(candidate, "id", "")) if candidate else ""
            )
            match_method = (
                _match_method(candidate, spotify_track) if candidate else ""
            )
            source_date = item.get("added_at") or ""
            source_datetime = _parse_datetime(source_date)
            minute = (
                source_datetime.strftime("%Y-%m-%dT%H:%MZ")
                if source_datetime else ""
            )
            cluster_size = spotify_clusters.get(minute, 0) if minute else 0
            flags, audit_status, audit_targets = _audit_fields(
                audit_snapshot,
                "spotify_to_tidal",
                source_id,
                candidate_id,
            )
            if cluster_size >= origin_cluster_size:
                flags.append("clustered_spotify_added_at")
            if match_method == "metadata":
                flags.append("metadata_match_requires_review")
            elif match_method == "catalog_unverified":
                flags.append("catalog_match_cannot_be_reverified")
            if any(flag.startswith("prior_audit_") for flag in flags):
                action = "audit_conflict"
                reason = "prior_audit_indicates_destination_presence"
            elif candidate is None:
                action, reason = "match_failed", "no_safe_tidal_catalog_match"
            elif candidate_id in existing_tidal_ids:
                action, reason = "skip_existing", "candidate_id_already_favorited"
            elif "catalog_match_cannot_be_reverified" in flags:
                action, reason = "blocked", "catalog_match_cannot_be_reverified"
            elif "clustered_spotify_added_at" in flags:
                action, reason = "origin_suspect", "spotify_added_at_is_clustered"
            elif "metadata_match_requires_review" in flags:
                action, reason = "metadata_review", "metadata_match_requires_review"
            else:
                action, reason = "would_add", "semantically_absent_from_tidal"
            isrc, artist, title = _spotify_metadata(spotify_track)
            target_isrc, target_artist, target_title = (
                _tidal_metadata(candidate) if candidate else ("", "", "")
            )
            rows.append({
                "direction": "spotify_to_tidal",
                "action": action,
                "safety_flags": ";".join(sorted(set(flags))),
                "source_service": "spotify",
                "source_id": source_id,
                "target_service": "tidal",
                "target_candidate_id": candidate_id,
                "isrc": isrc,
                "artist": artist,
                "title": title,
                "target_isrc": target_isrc,
                "target_artist": target_artist,
                "target_title": target_title,
                "source_duration_ms": _spotify_duration_ms(spotify_track),
                "target_duration_ms": (
                    _tidal_duration_ms(candidate) if candidate else ""
                ),
                "match_method": match_method,
                "source_date_added": source_date,
                "source_added_cluster_size": cluster_size or "",
                "audit_status": audit_status,
                "audit_target_ids": audit_targets,
                "coalesced_source_ids": "",
                "reason": reason,
            })

    _coalesce_destination_targets(rows)
    return rows


async def favorites_dry_run(
    spotify_session: spotipy.Spotify,
    tidal_session: tidalapi.Session,
    config: dict,
    direction: SyncDirectionLiteral,
    output_path: str | Path,
    audit_input: str | Path | None = None,
) -> Path:
    rows = await collect_favorites_dry_run_rows(
        spotify_session,
        tidal_session,
        config,
        direction,
        audit_input,
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
    audit_input: str | Path | None = None,
) -> Path:
    return asyncio.run(favorites_dry_run(
        spotify_session,
        tidal_session,
        config,
        direction,
        output_path,
        audit_input,
    ))
