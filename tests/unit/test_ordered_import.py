import csv
import datetime
from pathlib import Path

import pytest

from spotify_to_tidal import __main__
from spotify_to_tidal.dry_run import DRY_RUN_FIELDS
from spotify_to_tidal.ordered_import import (
    CHECKPOINT_FIELDS,
    OrderedImportError,
    execute_ordered_import,
    load_ordered_import_checkpoint,
    prepare_ordered_import,
)


UTC = datetime.timezone.utc


def _reviewed_row(
    source_id: str,
    target_id: str,
    isrc: str,
    added_at: str,
    **overrides,
):
    row = {field: "" for field in DRY_RUN_FIELDS}
    row.update({
        "direction": "tidal_to_spotify",
        "action": "would_add",
        "source_service": "tidal",
        "source_id": source_id,
        "target_service": "spotify",
        "target_candidate_id": target_id,
        "isrc": isrc,
        "artist": f"Tidal Artist {isrc}",
        "title": f"Tidal Title {isrc}",
        "target_isrc": isrc,
        "target_artist": f"Spotify Artist {isrc}",
        "target_title": f"Spotify Title {isrc}",
        "match_method": "exact_isrc",
        "source_date_added": added_at,
    })
    row.update(overrides)
    return row


def _write_reviewed(path: Path, rows):
    with path.open("w", newline="", encoding="utf-8") as report:
        writer = csv.DictWriter(report, fieldnames=DRY_RUN_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _write_checkpoint(path: Path, rows):
    with path.open("w", newline="", encoding="utf-8") as report:
        writer = csv.DictWriter(report, fieldnames=CHECKPOINT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _make_checkpoint(tmp_path, rows=None):
    reviewed = tmp_path / "reviewed.csv"
    checkpoint = tmp_path / "checkpoint.csv"
    if rows is None:
        rows = [
            _reviewed_row("tidal-new", "spotify-new", "ISRC-NEW", "2024-01-01T00:00:00Z"),
            _reviewed_row("tidal-old", "spotify-old", "ISRC-OLD", "2020-01-01T00:00:00Z"),
        ]
    _write_reviewed(reviewed, rows)
    prepare_ordered_import(reviewed, checkpoint)
    return checkpoint


class FakeSpotify:
    def __init__(self, *, existing=None, target_isrcs=None, fixed_time=False):
        self.liked = list(existing or [])
        self.target_isrcs = dict(target_isrcs or {
            "spotify-old": "ISRC-OLD",
            "spotify-new": "ISRC-NEW",
            "spotify-third": "ISRC-THIRD",
        })
        self.fixed_time = fixed_time
        self.archive_uris = []
        self.playlist_id = "archive-1"
        self.put_calls = []
        self.post_calls = []
        self._like_count = len(self.liked)

    def current_user_saved_tracks(self, limit=50, offset=0):
        items = list(reversed(self.liked))[offset:offset + limit]
        return {
            "items": items,
            "next": None,
            "limit": limit,
            "total": len(self.liked),
        }

    def track(self, target_id):
        return {
            "id": target_id,
            "uri": f"spotify:track:{target_id}",
            "external_ids": {"isrc": self.target_isrcs.get(target_id, "")},
        }

    def _post(self, endpoint, payload):
        self.post_calls.append((endpoint, payload))
        if endpoint == "me/playlists":
            return {"id": self.playlist_id}
        if endpoint == f"playlists/{self.playlist_id}/items":
            self.archive_uris.extend(payload["uris"])
            return {"snapshot_id": f"snapshot-{len(self.archive_uris)}"}
        raise AssertionError(endpoint)

    def _get(self, endpoint, **kwargs):
        if endpoint != f"playlists/{self.playlist_id}/items":
            raise AssertionError(endpoint)
        return {
            "items": [{"item": {"uri": uri}} for uri in self.archive_uris],
            "next": None,
        }

    def _put(self, endpoint, **kwargs):
        assert endpoint == "me/library"
        uri = kwargs["uris"]
        self.put_calls.append((endpoint, kwargs))
        target_id = uri.rsplit(":", 1)[-1]
        if self.fixed_time:
            added_at = "2026-08-27T12:00:00Z"
        else:
            added_at = (
                datetime.datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
                + datetime.timedelta(seconds=65 * self._like_count)
            ).isoformat().replace("+00:00", "Z")
        self._like_count += 1
        self.liked.append({
            "added_at": added_at,
            "track": {
                "id": target_id,
                "external_ids": {"isrc": self.target_isrcs[target_id]},
            },
        })
        return None


def test_prepare_ordered_import_filters_and_sorts_safe_rows(tmp_path):
    reviewed = tmp_path / "reviewed.csv"
    checkpoint = tmp_path / "checkpoint.csv"
    blocked = _reviewed_row(
        "tidal-blocked",
        "spotify-blocked",
        "ISRC-BLOCKED",
        "2019-01-01T00:00:00Z",
        action="match_failed",
    )
    spotify_to_tidal = _reviewed_row(
        "spotify-source",
        "tidal-target",
        "ISRC-OTHER",
        "2018-01-01T00:00:00Z",
        direction="spotify_to_tidal",
        source_service="spotify",
        target_service="tidal",
    )
    _write_reviewed(reviewed, [
        _reviewed_row("tidal-new", "spotify-new", "isrc-new", "2024-01-01T00:00:00Z"),
        blocked,
        spotify_to_tidal,
        _reviewed_row("tidal-old", "spotify-old", "isrc-old", "2020-01-01T00:00:00+00:00"),
    ])

    prepare_ordered_import(reviewed, checkpoint)
    rows = load_ordered_import_checkpoint(checkpoint)

    assert [row["source_id"] for row in rows] == ["tidal-old", "tidal-new"]
    assert [row["sequence"] for row in rows] == ["1", "2"]
    assert [row["isrc"] for row in rows] == ["ISRC-OLD", "ISRC-NEW"]
    assert len({row["plan_sha256"] for row in rows}) == 1
    assert all(row["status"] == "planned" for row in rows)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"safety_flags": "review"}, "contains safety flags"),
        ({"match_method": "metadata"}, "must use exact_isrc"),
        ({"target_isrc": "DIFFERENT"}, "ISRCs must match exactly"),
        ({"source_date_added": "not-a-date"}, "invalid ISO 8601"),
    ],
)
def test_prepare_ordered_import_rejects_unsafe_rows(tmp_path, override, message):
    reviewed = tmp_path / "reviewed.csv"
    checkpoint = tmp_path / "checkpoint.csv"
    row = _reviewed_row(
        "tidal-one",
        "spotify-one",
        "ISRC-ONE",
        "2020-01-01T00:00:00Z",
        **override,
    )
    _write_reviewed(reviewed, [row])

    with pytest.raises(OrderedImportError, match=message):
        prepare_ordered_import(reviewed, checkpoint)

    assert not checkpoint.exists()


def test_prepare_ordered_import_rejects_duplicate_recording(tmp_path):
    reviewed = tmp_path / "reviewed.csv"
    _write_reviewed(reviewed, [
        _reviewed_row("tidal-one", "spotify-one", "ISRC-SAME", "2020-01-01T00:00:00Z"),
        _reviewed_row("tidal-two", "spotify-two", "ISRC-SAME", "2021-01-01T00:00:00Z"),
    ])

    with pytest.raises(OrderedImportError, match="duplicate recording ISRC"):
        prepare_ordered_import(reviewed, tmp_path / "checkpoint.csv")


def test_checkpoint_detects_immutable_plan_tampering(tmp_path):
    checkpoint = _make_checkpoint(tmp_path)
    rows = list(csv.DictReader(checkpoint.open(encoding="utf-8")))
    rows[0]["target_id"] = "spotify-substituted"
    _write_checkpoint(checkpoint, rows)

    with pytest.raises(OrderedImportError, match="digest does not match"):
        load_ordered_import_checkpoint(checkpoint)


def test_checkpoint_rejects_inconsistent_archive_state(tmp_path):
    checkpoint = _make_checkpoint(tmp_path)
    rows = list(csv.DictReader(checkpoint.open(encoding="utf-8")))
    for row in rows:
        row["archive_status"] = "playlist_verified"
    _write_checkpoint(checkpoint, rows)

    with pytest.raises(OrderedImportError, match="requires a playlist ID"):
        load_ordered_import_checkpoint(checkpoint)


def test_execute_creates_exact_archive_and_likes_oldest_to_newest(tmp_path):
    checkpoint = _make_checkpoint(tmp_path)
    spotify = FakeSpotify()
    sleeps = []

    execute_ordered_import(
        spotify,
        checkpoint,
        spacing_seconds=61,
        sleeper=sleeps.append,
    )

    assert spotify.archive_uris == [
        "spotify:track:spotify-old",
        "spotify:track:spotify-new",
    ]
    assert [call[1]["uris"] for call in spotify.put_calls] == spotify.archive_uris
    assert sleeps == [61]
    rows = load_ordered_import_checkpoint(checkpoint)
    assert all(row["archive_status"] == "playlist_verified" for row in rows)
    assert all(row["status"] == "liked_verified" for row in rows)
    observed = [datetime.datetime.fromisoformat(
        row["observed_spotify_added_at"].replace("Z", "+00:00")
    ) for row in rows]
    assert observed == sorted(observed)


def test_execute_rejects_stale_semantic_like_before_any_write(tmp_path):
    checkpoint = _make_checkpoint(tmp_path)
    spotify = FakeSpotify(existing=[{
        "added_at": "2026-08-27T00:00:00Z",
        "track": {
            "id": "spotify-alternative",
            "external_ids": {"isrc": "ISRC-OLD"},
        },
    }])

    with pytest.raises(OrderedImportError, match="exact-ISRC equivalent"):
        execute_ordered_import(spotify, checkpoint, sleeper=lambda _: None)

    assert spotify.post_calls == []
    assert spotify.put_calls == []


def test_execute_revalidates_catalog_isrc_before_playlist_or_like(tmp_path):
    checkpoint = _make_checkpoint(tmp_path)
    spotify = FakeSpotify(target_isrcs={
        "spotify-old": "CHANGED",
        "spotify-new": "ISRC-NEW",
    })

    with pytest.raises(OrderedImportError, match="no longer has the reviewed exact ISRC"):
        execute_ordered_import(spotify, checkpoint, sleeper=lambda _: None)

    assert spotify.post_calls == []
    assert spotify.put_calls == []


def test_execute_stops_when_spotify_timestamp_does_not_increase(tmp_path):
    checkpoint = _make_checkpoint(tmp_path)
    spotify = FakeSpotify(fixed_time=True)

    with pytest.raises(OrderedImportError, match="added_at did not increase"):
        execute_ordered_import(spotify, checkpoint, sleeper=lambda _: None)

    assert len(spotify.put_calls) == 2
    rows = load_ordered_import_checkpoint(checkpoint)
    assert rows[0]["status"] == "liked_verified"
    assert rows[1]["status"] == "like_requested"
    assert "added_at did not increase" in rows[1]["error"]


def test_execute_adopts_interrupted_requested_like_without_repeating_write(tmp_path):
    checkpoint = _make_checkpoint(tmp_path, rows=[
        _reviewed_row("tidal-old", "spotify-old", "ISRC-OLD", "2020-01-01T00:00:00Z"),
    ])
    rows = load_ordered_import_checkpoint(checkpoint)
    rows[0]["status"] = "like_requested"
    _write_checkpoint(checkpoint, rows)
    spotify = FakeSpotify(existing=[{
        "added_at": "2026-08-27T12:00:00Z",
        "track": {
            "id": "spotify-old",
            "external_ids": {"isrc": "ISRC-OLD"},
        },
    }])

    execute_ordered_import(spotify, checkpoint, sleeper=lambda _: None)

    assert spotify.put_calls == []
    assert load_ordered_import_checkpoint(checkpoint)[0]["status"] == "liked_verified"


def test_execute_rejects_changed_isrc_when_resuming_requested_like(tmp_path):
    checkpoint = _make_checkpoint(tmp_path, rows=[
        _reviewed_row("tidal-old", "spotify-old", "ISRC-OLD", "2020-01-01T00:00:00Z"),
    ])
    rows = load_ordered_import_checkpoint(checkpoint)
    rows[0]["status"] = "like_requested"
    _write_checkpoint(checkpoint, rows)
    spotify = FakeSpotify(existing=[{
        "added_at": "2026-08-27T12:00:00Z",
        "track": {
            "id": "spotify-old",
            "external_ids": {"isrc": "ISRC-CHANGED"},
        },
    }])

    with pytest.raises(OrderedImportError, match="changed ISRC"):
        execute_ordered_import(spotify, checkpoint, sleeper=lambda _: None)

    assert spotify.put_calls == []


def _write_config(tmp_path):
    config = tmp_path / "config.yml"
    config.write_text("spotify:\n  client_id: test\n", encoding="utf-8")
    return config


def test_prepare_cli_is_read_only_and_does_not_authenticate(mocker, tmp_path):
    config = _write_config(tmp_path)
    reviewed = tmp_path / "reviewed.csv"
    output = tmp_path / "checkpoint.csv"
    _write_reviewed(reviewed, [
        _reviewed_row("tidal-old", "spotify-old", "ISRC-OLD", "2020-01-01T00:00:00Z"),
    ])
    open_spotify = mocker.patch(
        "spotify_to_tidal.__main__._auth.open_spotify_session"
    )
    open_tidal = mocker.patch(
        "spotify_to_tidal.__main__._auth.open_tidal_session"
    )
    mocker.patch("sys.argv", [
        "spotify_to_tidal",
        "--config", str(config),
        "--prepare-ordered-import", str(reviewed),
        "--ordered-import-output", str(output),
    ])

    __main__.main()

    assert output.exists()
    open_spotify.assert_not_called()
    open_tidal.assert_not_called()


def test_execute_cli_requires_explicit_current_date_ack_before_auth(mocker, tmp_path):
    config = _write_config(tmp_path)
    checkpoint = _make_checkpoint(tmp_path)
    open_spotify = mocker.patch(
        "spotify_to_tidal.__main__._auth.open_spotify_session"
    )
    mocker.patch("sys.argv", [
        "spotify_to_tidal",
        "--config", str(config),
        "--execute-ordered-import", str(checkpoint),
    ])

    with pytest.raises(SystemExit, match="requires --accept-current-spotify-dates"):
        __main__.main()

    open_spotify.assert_not_called()


def test_execute_cli_rejects_unsafe_spacing_before_auth(mocker, tmp_path):
    config = _write_config(tmp_path)
    checkpoint = _make_checkpoint(tmp_path)
    open_spotify = mocker.patch(
        "spotify_to_tidal.__main__._auth.open_spotify_session"
    )
    mocker.patch("sys.argv", [
        "spotify_to_tidal",
        "--config", str(config),
        "--execute-ordered-import", str(checkpoint),
        "--accept-current-spotify-dates",
        "--ordered-import-spacing-seconds", "60",
    ])

    with pytest.raises(SystemExit, match="must be at least 61"):
        __main__.main()

    open_spotify.assert_not_called()


def test_execute_cli_opens_only_spotify_and_dispatches_checkpoint(mocker, tmp_path):
    config = _write_config(tmp_path)
    checkpoint = _make_checkpoint(tmp_path)
    spotify = FakeSpotify()
    open_spotify = mocker.patch(
        "spotify_to_tidal.__main__._auth.open_spotify_session",
        return_value=spotify,
    )
    open_tidal = mocker.patch(
        "spotify_to_tidal.__main__._auth.open_tidal_session"
    )
    execute = mocker.patch(
        "spotify_to_tidal.__main__._ordered_import.execute_ordered_import"
    )
    mocker.patch("sys.argv", [
        "spotify_to_tidal",
        "--config", str(config),
        "--execute-ordered-import", str(checkpoint),
        "--accept-current-spotify-dates",
    ])

    __main__.main()

    open_spotify.assert_called_once_with(
        {"client_id": "test"},
        sync_direction="tidal_to_spotify",
    )
    open_tidal.assert_not_called()
    execute.assert_called_once_with(
        spotify,
        str(checkpoint),
        playlist_name="Tidal Favorites — Chronological Archive",
        spacing_seconds=65,
    )
