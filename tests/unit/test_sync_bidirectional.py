"""
Tests for bidirectional sync feature.
Feature: bidirectional-sync
"""
import pytest
from unittest.mock import MagicMock, patch, call
from hypothesis import given, settings
from hypothesis import strategies as st

from spotify_to_tidal.sync import (
    resolve_sync_direction,
    resolve_playlist_sync_direction,
    detect_conflict,
)
from spotify_to_tidal.cache import TrackMatchCache

VALID_DIRECTIONS = ["spotify_to_tidal", "tidal_to_spotify", "bidirectional"]


# ---------------------------------------------------------------------------
# Property 6: Sync direction resolution priority chain
# Feature: bidirectional-sync, Property 6: Sync direction resolution priority chain
# Validates: Requirements 3.2, 3.3, 3.4
# ---------------------------------------------------------------------------

@given(
    config_direction=st.one_of(st.none(), st.sampled_from(VALID_DIRECTIONS)),
    cli_direction=st.one_of(st.none(), st.sampled_from(VALID_DIRECTIONS)),
)
@settings(max_examples=100)
def test_resolve_sync_direction_priority_chain(config_direction, cli_direction):
    """CLI arg wins over config, config wins over default."""
    config = {}
    if config_direction is not None:
        config["sync_direction"] = config_direction

    result = resolve_sync_direction(config, cli_direction)

    if cli_direction is not None:
        assert result == cli_direction
    elif config_direction is not None:
        assert result == config_direction
    else:
        assert result == "spotify_to_tidal"


# ---------------------------------------------------------------------------
# Property 7: Invalid sync direction causes exit
# Feature: bidirectional-sync, Property 7: Invalid sync direction causes exit
# Validates: Requirements 3.5
# ---------------------------------------------------------------------------

@given(st.text().filter(lambda s: s not in VALID_DIRECTIONS))
@settings(max_examples=100)
def test_resolve_sync_direction_invalid_exits(invalid_direction):
    """Any string not in VALID_DIRECTIONS causes SystemExit."""
    with pytest.raises(SystemExit):
        resolve_sync_direction({}, invalid_direction)


# ---------------------------------------------------------------------------
# Property 8: Per-playlist sync direction override
# Feature: bidirectional-sync, Property 8: Per-playlist sync direction override
# Validates: Requirements 4.2, 4.3
# ---------------------------------------------------------------------------

@given(
    global_direction=st.sampled_from(VALID_DIRECTIONS),
    playlist_direction=st.one_of(st.none(), st.sampled_from(VALID_DIRECTIONS)),
)
@settings(max_examples=100)
def test_resolve_playlist_sync_direction_override(global_direction, playlist_direction):
    """Per-playlist field wins when present; global direction used when absent."""
    if playlist_direction is not None:
        playlist_config = {"spotify_id": "abc", "tidal_id": "def", "sync_direction": playlist_direction}
        result = resolve_playlist_sync_direction(playlist_config, global_direction)
        assert result == playlist_direction
    else:
        playlist_config = {"spotify_id": "abc", "tidal_id": "def"}
        result = resolve_playlist_sync_direction(playlist_config, global_direction)
        assert result == global_direction


# ---------------------------------------------------------------------------
# Unit test: default direction is spotify_to_tidal
# Validates: Requirements 3.3
# ---------------------------------------------------------------------------

def test_resolve_sync_direction_default():
    """When no direction is specified anywhere, default is spotify_to_tidal."""
    result = resolve_sync_direction({}, None)
    assert result == "spotify_to_tidal"


# ---------------------------------------------------------------------------
# Unit test: valid direction values are accepted
# Validates: Requirements 3.1
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("direction", VALID_DIRECTIONS)
def test_resolve_sync_direction_valid_values(direction):
    """All three valid direction values are accepted without error."""
    result = resolve_sync_direction({"sync_direction": direction}, None)
    assert result == direction


# ---------------------------------------------------------------------------
# Unit test: conflict detection
# Validates: Requirements 5.4, 5.5
# ---------------------------------------------------------------------------

def test_detect_conflict_no_conflict_identical():
    """No conflict when both playlists are identical (empty)."""
    assert detect_conflict([], []) is False


def test_detect_conflict_no_conflict_one_side_empty():
    """No conflict when one side is empty (pure addition, not divergence)."""
    spotify_track = {
        'id': 'sp1', 'name': 'Song A', 'duration_ms': 200000,
        'artists': [{'name': 'Artist A'}],
        'album': {'name': 'Album A', 'artists': [{'name': 'Artist A'}]},
        'track_number': 1, 'external_ids': {},
    }
    # Tidal side is empty — no conflict (Spotify has additions, Tidal has none)
    assert detect_conflict([spotify_track], []) is False


# ---------------------------------------------------------------------------
# Unit test: resolve_playlist_sync_direction with None config
# Validates: Requirements 4.3
# ---------------------------------------------------------------------------

def test_resolve_playlist_sync_direction_none_config():
    """When playlist_config is None, global direction is returned."""
    result = resolve_playlist_sync_direction(None, "bidirectional")
    assert result == "bidirectional"
