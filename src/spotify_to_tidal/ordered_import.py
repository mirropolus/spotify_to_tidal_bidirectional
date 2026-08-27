"""Guarded relative-order import for Tidal favorites into Spotify."""

from __future__ import annotations

import csv
import datetime
import hashlib
import json
from pathlib import Path
import time
from typing import Callable, Sequence

import spotipy


DEFAULT_ARCHIVE_PLAYLIST_NAME = "Tidal Favorites — Chronological Archive"
DEFAULT_SPACING_SECONDS = 65
MINIMUM_SPACING_SECONDS = 61

CHECKPOINT_FIELDS = [
    "plan_sha256",
    "sequence",
    "source_date_added",
    "source_id",
    "target_id",
    "isrc",
    "artist",
    "title",
    "target_artist",
    "target_title",
    "status",
    "observed_spotify_added_at",
    "archive_playlist_id",
    "archive_snapshot_id",
    "archive_status",
    "error",
]

IMMUTABLE_PLAN_FIELDS = [
    "sequence",
    "source_date_added",
    "source_id",
    "target_id",
    "isrc",
    "artist",
    "title",
    "target_artist",
    "target_title",
]

_VALID_STATUSES = {"planned", "like_requested", "liked_verified"}
_VALID_ARCHIVE_STATUSES = {"planned", "playlist_created", "playlist_verified"}


class OrderedImportError(RuntimeError):
    """The relative-order import could not proceed safely."""


def _parse_utc(value: str) -> datetime.datetime:
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OrderedImportError(f"invalid ISO 8601 timestamp: {value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OrderedImportError(f"timestamp must include a timezone: {value}")
    return parsed.astimezone(datetime.timezone.utc)


def _canonical_timestamp(value: str) -> str:
    return _parse_utc(value).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _plan_digest(rows: Sequence[dict]) -> str:
    canonical = [
        {field: str(row.get(field, "")) for field in IMMUTABLE_PLAN_FIELDS}
        for row in rows
    ]
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _write_checkpoint(path: str | Path, rows: Sequence[dict]) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as report:
        writer = csv.DictWriter(report, fieldnames=CHECKPOINT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(destination)
    return destination


def prepare_ordered_import(
    reviewed_dry_run_path: str | Path,
    output_path: str | Path,
) -> Path:
    """Create a tamper-evident oldest-to-newest checkpoint from reviewed rows."""
    accepted: list[dict] = []
    seen_sources: set[str] = set()
    seen_targets: set[str] = set()
    seen_isrcs: set[str] = set()
    with Path(reviewed_dry_run_path).open(
        newline="", encoding="utf-8-sig"
    ) as report:
        reader = csv.DictReader(report)
        required = {
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
            "match_method",
            "source_date_added",
        }
        missing = required.difference(reader.fieldnames or [])
        if missing:
            raise OrderedImportError(
                "reviewed dry-run is missing required columns: "
                + ", ".join(sorted(missing))
            )

        for line_number, row in enumerate(reader, start=2):
            if (
                row.get("direction") != "tidal_to_spotify"
                or row.get("action") != "would_add"
            ):
                continue
            source_id = (row.get("source_id") or "").strip()
            target_id = (row.get("target_candidate_id") or "").strip()
            source_isrc = (row.get("isrc") or "").strip().upper()
            target_isrc = (row.get("target_isrc") or "").strip().upper()
            timestamp = (row.get("source_date_added") or "").strip()
            if row.get("source_service") != "tidal":
                raise OrderedImportError(
                    f"line {line_number}: source service must be tidal"
                )
            if row.get("target_service") != "spotify":
                raise OrderedImportError(
                    f"line {line_number}: target service must be spotify"
                )
            if (row.get("safety_flags") or "").strip():
                raise OrderedImportError(
                    f"line {line_number}: would_add row contains safety flags"
                )
            if row.get("match_method") != "exact_isrc":
                raise OrderedImportError(
                    f"line {line_number}: match must use exact_isrc"
                )
            if not source_id or not target_id:
                raise OrderedImportError(
                    f"line {line_number}: source and target IDs are required"
                )
            if not source_isrc or source_isrc != target_isrc:
                raise OrderedImportError(
                    f"line {line_number}: source and target ISRCs must match exactly"
                )
            if source_id in seen_sources:
                raise OrderedImportError(
                    f"line {line_number}: duplicate Tidal source ID"
                )
            if target_id in seen_targets:
                raise OrderedImportError(
                    f"line {line_number}: duplicate Spotify target ID"
                )
            if source_isrc in seen_isrcs:
                raise OrderedImportError(
                    f"line {line_number}: duplicate recording ISRC"
                )
            canonical_timestamp = _canonical_timestamp(timestamp)
            accepted.append({
                "source_date_added": canonical_timestamp,
                "source_id": source_id,
                "target_id": target_id,
                "isrc": source_isrc,
                "artist": (row.get("artist") or "").strip(),
                "title": (row.get("title") or "").strip(),
                "target_artist": (row.get("target_artist") or "").strip(),
                "target_title": (row.get("target_title") or "").strip(),
            })
            seen_sources.add(source_id)
            seen_targets.add(target_id)
            seen_isrcs.add(source_isrc)

    if not accepted:
        raise OrderedImportError(
            "reviewed dry-run contains no safe tidal_to_spotify would_add rows"
        )
    accepted.sort(
        key=lambda row: (_parse_utc(row["source_date_added"]), row["source_id"])
    )
    for sequence, row in enumerate(accepted, start=1):
        row["sequence"] = str(sequence)
    digest = _plan_digest(accepted)
    checkpoint_rows = [{
        **row,
        "plan_sha256": digest,
        "status": "planned",
        "observed_spotify_added_at": "",
        "archive_playlist_id": "",
        "archive_snapshot_id": "",
        "archive_status": "planned",
        "error": "",
    } for row in accepted]
    destination = _write_checkpoint(output_path, checkpoint_rows)
    print(
        f"Ordered import checkpoint written to {destination} "
        f"({len(checkpoint_rows)} tracks, oldest to newest, plan {digest[:12]})"
    )
    return destination


def load_ordered_import_checkpoint(path: str | Path) -> list[dict]:
    with Path(path).open(newline="", encoding="utf-8-sig") as report:
        reader = csv.DictReader(report)
        missing = set(CHECKPOINT_FIELDS).difference(reader.fieldnames or [])
        if missing:
            raise OrderedImportError(
                "ordered import checkpoint is missing required columns: "
                + ", ".join(sorted(missing))
            )
        rows = list(reader)
    if not rows:
        raise OrderedImportError("ordered import checkpoint is empty")
    expected_sequences = [str(index) for index in range(1, len(rows) + 1)]
    if [row.get("sequence", "") for row in rows] != expected_sequences:
        raise OrderedImportError("checkpoint sequence must be contiguous and ordered")
    digests = {row.get("plan_sha256", "") for row in rows}
    if len(digests) != 1 or not next(iter(digests)):
        raise OrderedImportError("checkpoint must contain one non-empty plan digest")
    if _plan_digest(rows) != next(iter(digests)):
        raise OrderedImportError("checkpoint plan digest does not match its contents")
    for row in rows:
        if row.get("status") not in _VALID_STATUSES:
            raise OrderedImportError(
                f"invalid checkpoint status for sequence {row['sequence']}"
            )
        if row.get("archive_status") not in _VALID_ARCHIVE_STATUSES:
            raise OrderedImportError(
                f"invalid archive status for sequence {row['sequence']}"
            )
        _parse_utc(row["source_date_added"])
        if row.get("status") == "liked_verified":
            _parse_utc(row.get("observed_spotify_added_at", ""))
    source_ids = [row.get("source_id", "") for row in rows]
    target_ids = [row.get("target_id", "") for row in rows]
    isrcs = [row.get("isrc", "").upper() for row in rows]
    if any(not value for value in source_ids + target_ids + isrcs):
        raise OrderedImportError("checkpoint source IDs, target IDs, and ISRCs are required")
    if len(source_ids) != len(set(source_ids)):
        raise OrderedImportError("checkpoint contains duplicate Tidal source IDs")
    if len(target_ids) != len(set(target_ids)):
        raise OrderedImportError("checkpoint contains duplicate Spotify target IDs")
    if len(isrcs) != len(set(isrcs)):
        raise OrderedImportError("checkpoint contains duplicate recording ISRCs")
    chronological = sorted(
        rows,
        key=lambda row: (_parse_utc(row["source_date_added"]), row["source_id"]),
    )
    if [row["sequence"] for row in rows] != [
        row["sequence"] for row in chronological
    ]:
        raise OrderedImportError("checkpoint rows are not oldest-to-newest")
    playlist_ids = {row.get("archive_playlist_id", "") for row in rows}
    snapshot_ids = {row.get("archive_snapshot_id", "") for row in rows}
    archive_statuses = {row.get("archive_status", "") for row in rows}
    if len(playlist_ids) != 1 or len(snapshot_ids) != 1 or len(archive_statuses) != 1:
        raise OrderedImportError("archive checkpoint fields must agree on every row")
    playlist_id = next(iter(playlist_ids))
    archive_status = next(iter(archive_statuses))
    if archive_status == "planned" and playlist_id:
        raise OrderedImportError(
            "planned archive checkpoint must not contain a playlist ID"
        )
    if archive_status != "planned" and not playlist_id:
        raise OrderedImportError(
            "created or verified archive checkpoint requires a playlist ID"
        )
    return rows


def _track_from_playlist_entry(entry: dict) -> dict:
    candidate = entry.get("item") or entry.get("track") or entry
    return candidate if isinstance(candidate, dict) else {}


def _fetch_playlist_uris(
    spotify_session: spotipy.Spotify,
    playlist_id: str,
) -> list[str]:
    uris: list[str] = []
    offset = 0
    while True:
        page = spotify_session._get(
            f"playlists/{playlist_id}/items",
            limit=100,
            offset=offset,
        )
        items = page.get("items", [])
        uris.extend(
            track["uri"]
            for track in (_track_from_playlist_entry(entry) for entry in items)
            if track.get("uri")
        )
        if not page.get("next"):
            return uris
        offset += len(items)


def _checkpoint_all(rows: list[dict], field: str, value: str) -> None:
    for row in rows:
        row[field] = value


def _ensure_archive_playlist(
    spotify_session: spotipy.Spotify,
    rows: list[dict],
    checkpoint_path: str | Path,
    playlist_name: str,
) -> None:
    planned_uris = [f"spotify:track:{row['target_id']}" for row in rows]
    playlist_id = rows[0]["archive_playlist_id"]
    if not playlist_id:
        response = spotify_session._post(
            "me/playlists",
            payload={
                "name": playlist_name,
                "public": False,
                "collaborative": False,
                "description": (
                    "Exact oldest-to-newest archive for ordered Tidal favorites "
                    f"import plan {rows[0]['plan_sha256'][:12]}."
                ),
            },
        )
        playlist_id = str((response or {}).get("id", ""))
        if not playlist_id:
            raise OrderedImportError("Spotify did not return an archive playlist ID")
        _checkpoint_all(rows, "archive_playlist_id", playlist_id)
        _checkpoint_all(rows, "archive_status", "playlist_created")
        _write_checkpoint(checkpoint_path, rows)

    existing_uris = _fetch_playlist_uris(spotify_session, playlist_id)
    if existing_uris != planned_uris[:len(existing_uris)]:
        raise OrderedImportError(
            "archive playlist contents are not an exact prefix of the plan; "
            "no playlist replacement or removal was attempted"
        )
    snapshot_id = rows[0].get("archive_snapshot_id", "")
    for offset in range(len(existing_uris), len(planned_uris), 100):
        response = spotify_session._post(
            f"playlists/{playlist_id}/items",
            payload={"uris": planned_uris[offset:offset + 100]},
        )
        snapshot_id = str((response or {}).get("snapshot_id", ""))
        _checkpoint_all(rows, "archive_snapshot_id", snapshot_id)
        _write_checkpoint(checkpoint_path, rows)
    if _fetch_playlist_uris(spotify_session, playlist_id) != planned_uris:
        raise OrderedImportError("archive playlist order verification failed")
    _checkpoint_all(rows, "archive_status", "playlist_verified")
    _checkpoint_all(rows, "archive_snapshot_id", snapshot_id)
    _write_checkpoint(checkpoint_path, rows)


def _spotify_saved_map(items: Sequence[dict]) -> dict[str, dict]:
    return {
        str(item["track"]["id"]): item
        for item in items
        if item.get("track") and item["track"].get("id")
    }


def _verify_new_like(
    spotify_session: spotipy.Spotify,
    target_id: str,
    *,
    sleeper: Callable[[float], None],
) -> str:
    for attempt in range(5):
        page = spotify_session.current_user_saved_tracks(limit=50, offset=0)
        for item in page.get("items", []):
            track = item.get("track") or {}
            if str(track.get("id", "")) == target_id:
                observed = item.get("added_at") or ""
                _parse_utc(observed)
                return observed
        if attempt < 4:
            sleeper(2)
    raise OrderedImportError(
        f"Spotify did not return newly liked track {target_id} on verification"
    )


def execute_ordered_import(
    spotify_session: spotipy.Spotify,
    checkpoint_path: str | Path,
    *,
    playlist_name: str = DEFAULT_ARCHIVE_PLAYLIST_NAME,
    spacing_seconds: int = DEFAULT_SPACING_SECONDS,
    sleeper: Callable[[float], None] = time.sleep,
) -> Path:
    """Create the exact archive and like tracks sequentially with checkpoints."""
    if spacing_seconds < MINIMUM_SPACING_SECONDS:
        raise OrderedImportError(
            f"ordered import spacing must be at least {MINIMUM_SPACING_SECONDS} seconds"
        )
    rows = load_ordered_import_checkpoint(checkpoint_path)
    statuses = [row["status"] for row in rows]
    verified_count = 0
    while verified_count < len(statuses) and statuses[verified_count] == "liked_verified":
        verified_count += 1
    remaining = statuses[verified_count:]
    if any(status == "liked_verified" for status in remaining):
        raise OrderedImportError("liked_verified checkpoint rows must form a prefix")
    if sum(status == "like_requested" for status in remaining) > 1:
        raise OrderedImportError("at most one resumable like_requested row is allowed")
    if "like_requested" in remaining and remaining[0] != "like_requested":
        raise OrderedImportError("like_requested must be the next unverified row")

    saved_items = get_spotify_saved_track_items_sync(spotify_session)
    saved_by_id = _spotify_saved_map(saved_items)
    saved_isrcs = {
        (item.get("track") or {}).get("external_ids", {}).get("isrc", "").upper()
        for item in saved_items
    }
    last_observed: datetime.datetime | None = None
    for index, row in enumerate(rows):
        existing = saved_by_id.get(row["target_id"])
        if index < verified_count:
            if existing is None:
                raise OrderedImportError(
                    f"verified Spotify target {row['target_id']} is no longer liked"
                )
            observed = _parse_utc(existing.get("added_at") or "")
            checkpoint_observed = _parse_utc(row["observed_spotify_added_at"])
            if observed != checkpoint_observed:
                raise OrderedImportError(
                    f"verified timestamp changed for Spotify target {row['target_id']}"
                )
            existing_isrc = (
                (existing.get("track") or {})
                .get("external_ids", {})
                .get("isrc", "")
                .upper()
            )
            if existing_isrc != row["isrc"].upper():
                raise OrderedImportError(
                    f"verified Spotify target {row['target_id']} changed ISRC"
                )
            if last_observed is not None and observed <= last_observed:
                raise OrderedImportError("verified Spotify timestamps are not increasing")
            last_observed = observed
            continue
        if row["status"] == "like_requested" and existing is not None:
            existing_isrc = (
                (existing.get("track") or {})
                .get("external_ids", {})
                .get("isrc", "")
                .upper()
            )
            if existing_isrc != row["isrc"].upper():
                raise OrderedImportError(
                    f"resumed Spotify target {row['target_id']} changed ISRC"
                )
            observed_text = existing.get("added_at") or ""
            observed = _parse_utc(observed_text)
            if last_observed is not None and observed <= last_observed:
                raise OrderedImportError(
                    "resumed Spotify timestamp is not later than the prior like"
                )
            row["status"] = "liked_verified"
            row["observed_spotify_added_at"] = observed_text
            row["error"] = ""
            _write_checkpoint(checkpoint_path, rows)
            verified_count += 1
            last_observed = observed
            continue
        if existing is not None or row["isrc"].upper() in saved_isrcs:
            raise OrderedImportError(
                f"planned Spotify target {row['target_id']} or an exact-ISRC "
                "equivalent is already liked; no writes were attempted"
            )

    for row in rows[verified_count:]:
        target = spotify_session.track(row["target_id"])
        target_id = str((target or {}).get("id", ""))
        target_isrc = (
            (target or {}).get("external_ids", {}).get("isrc", "").upper()
        )
        if target_id != row["target_id"] or target_isrc != row["isrc"].upper():
            raise OrderedImportError(
                f"Spotify target {row['target_id']} no longer has the reviewed exact ISRC"
            )

    _ensure_archive_playlist(
        spotify_session,
        rows,
        checkpoint_path,
        playlist_name,
    )

    last_observed = None
    for row in rows:
        if row["status"] == "liked_verified":
            last_observed = _parse_utc(row["observed_spotify_added_at"])
            continue
        if last_observed is not None:
            sleeper(spacing_seconds)
        row["status"] = "like_requested"
        row["error"] = ""
        _write_checkpoint(checkpoint_path, rows)
        try:
            spotify_session._put(
                "me/library",
                uris=f"spotify:track:{row['target_id']}",
            )
            observed_text = _verify_new_like(
                spotify_session,
                row["target_id"],
                sleeper=sleeper,
            )
            observed = _parse_utc(observed_text)
            if last_observed is not None and observed <= last_observed:
                raise OrderedImportError(
                    "Spotify added_at did not increase; the importer stopped "
                    "before liking another track"
                )
        except Exception as exc:
            row["error"] = str(exc)
            _write_checkpoint(checkpoint_path, rows)
            raise
        row["status"] = "liked_verified"
        row["observed_spotify_added_at"] = observed_text
        row["error"] = ""
        _write_checkpoint(checkpoint_path, rows)
        last_observed = observed
        print(
            f"Verified ordered like {row['sequence']}/{len(rows)}: "
            f"{row['target_artist']} — {row['target_title']} ({observed_text})"
        )
    print(
        f"Ordered import completed: {len(rows)} verified likes; "
        f"checkpoint retained at {checkpoint_path}"
    )
    return Path(checkpoint_path)


def get_spotify_saved_track_items_sync(
    spotify_session: spotipy.Spotify,
) -> list[dict]:
    """Synchronous saved-track loader for the guarded sequential importer."""
    items: list[dict] = []
    offset = 0
    while True:
        page = spotify_session.current_user_saved_tracks(limit=50, offset=offset)
        page_items = [
            item for item in page.get("items", []) if item.get("track") is not None
        ]
        items.extend(page_items)
        if not page.get("next"):
            return items
        offset += int(page.get("limit") or len(page_items) or 50)
