from typing import TypedDict, Literal, List, Optional
from typing_extensions import NotRequired

SyncDirectionLiteral = Literal["spotify_to_tidal", "tidal_to_spotify", "bidirectional"]
ConflictResolutionLiteral = Literal["spotify_wins", "tidal_wins", "both_win"]


class SpotifyConfig(TypedDict):
    client_id: str
    client_secret: str
    username: str
    redirect_url: str


class TidalConfig(TypedDict):
    access_token: str
    refresh_token: str
    session_id: str
    token_type: Literal["Bearer"]


class PlaylistConfig(TypedDict):
    spotify_id: str
    tidal_id: str
    sync_direction: NotRequired[SyncDirectionLiteral]  # per-playlist override


class SyncConfig(TypedDict):
    spotify: SpotifyConfig
    sync_playlists: NotRequired[List[PlaylistConfig]]
    excluded_playlists: NotRequired[List[str]]
    sync_direction: NotRequired[SyncDirectionLiteral]        # global default
    conflict_resolution: NotRequired[ConflictResolutionLiteral]  # global default
    sync_favorites_default: NotRequired[bool]
    max_concurrency: NotRequired[int]
    rate_limit: NotRequired[int]
