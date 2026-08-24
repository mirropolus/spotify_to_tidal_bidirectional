#!/usr/bin/env python3

import datetime
import os
import sys
import spotipy
from spotipy.cache_handler import MemoryCacheHandler
import tidalapi
import webbrowser
import yaml

__all__ = [
    'open_spotify_session',
    'open_tidal_session'
]

SPOTIFY_SCOPES = 'playlist-read-private, user-library-read'
SPOTIFY_WRITE_SCOPES = 'playlist-modify-public, playlist-modify-private, user-library-modify'


def _truthy_environment(name: str) -> bool:
    return os.getenv(name, "").lower() in {"1", "true", "yes", "on"}


def _setting(config: dict, key: str, environment_name: str):
    return os.getenv(environment_name) or config.get(key)


def open_spotify_session(config, sync_direction: str = "spotify_to_tidal") -> spotipy.Spotify:
    if sync_direction in ("tidal_to_spotify", "bidirectional"):
        scope = SPOTIFY_SCOPES + ', ' + SPOTIFY_WRITE_SCOPES
    else:
        scope = SPOTIFY_SCOPES

    refresh_token = os.getenv("SPOTIFY_REFRESH_TOKEN")
    oauth_arguments = dict(
        username=_setting(config, 'username', 'SPOTIFY_USERNAME'),
        scope=scope,
        client_id=_setting(config, 'client_id', 'SPOTIFY_CLIENT_ID'),
        client_secret=_setting(config, 'client_secret', 'SPOTIFY_CLIENT_SECRET'),
        redirect_uri=_setting(config, 'redirect_uri', 'SPOTIFY_REDIRECT_URI'),
        requests_timeout=2,
        open_browser=False if refresh_token else config.get('open_browser', True),
    )
    if refresh_token:
        # CI tokens remain in memory and are never written to Spotipy's disk cache.
        oauth_arguments['cache_handler'] = MemoryCacheHandler()

    credentials_manager = spotipy.SpotifyOAuth(**oauth_arguments)
    try:
        if refresh_token:
            credentials_manager.refresh_access_token(refresh_token)
        elif _truthy_environment("SPOTIFY_NONINTERACTIVE"):
            sys.exit(
                "Non-interactive Spotify authentication requires "
                "SPOTIFY_REFRESH_TOKEN"
            )
        else:
            credentials_manager.get_access_token(as_dict=False)
    except spotipy.SpotifyOauthError:
        sys.exit("Error opening Spotify session; OAuth token acquisition failed")

    return spotipy.Spotify(oauth_manager=credentials_manager)


def open_tidal_session(config=None) -> tidalapi.Session:
    environment_access_token = os.getenv("TIDAL_ACCESS_TOKEN")
    environment_refresh_token = os.getenv("TIDAL_REFRESH_TOKEN")

    if config:
        session = tidalapi.Session(config=config)
    else:
        session = tidalapi.Session()

    if environment_access_token:
        expiry_time = None
        if os.getenv("TIDAL_EXPIRY_TIME"):
            try:
                expiry_time = datetime.datetime.fromisoformat(
                    os.environ["TIDAL_EXPIRY_TIME"].replace("Z", "+00:00")
                )
            except ValueError:
                sys.exit("TIDAL_EXPIRY_TIME must be an ISO 8601 datetime")
        try:
            loaded = session.load_oauth_session(
                token_type=os.getenv("TIDAL_TOKEN_TYPE", "Bearer"),
                access_token=environment_access_token,
                refresh_token=environment_refresh_token,
                expiry_time=expiry_time,
            )
        except Exception:
            sys.exit("Error loading Tidal session from environment credentials")
        if not loaded:
            sys.exit("Tidal rejected the environment session credentials")
        return session

    try:
        with open('.session.yml', 'r') as session_file:
            previous_session = yaml.safe_load(session_file)
    except OSError:
        previous_session = None

    if previous_session:
        try:
            if session.load_oauth_session(token_type= previous_session['token_type'],
                                   access_token=previous_session['access_token'],
                                   refresh_token=previous_session['refresh_token'] ):
                return session
        except Exception:
            print("Error loading previous Tidal session; interactive login required")

    if _truthy_environment("TIDAL_NONINTERACTIVE"):
        sys.exit(
            "Non-interactive Tidal authentication requires TIDAL_ACCESS_TOKEN "
            "and should include TIDAL_REFRESH_TOKEN"
        )

    login, future = session.login_oauth()
    print('Login with the webbrowser: ' + login.verification_uri_complete)
    url = login.verification_uri_complete
    if not url.startswith('https://'):
        url = 'https://' + url
    webbrowser.open(url)
    future.result()
    with open('.session.yml', 'w') as f:
        yaml.dump( {'session_id': session.session_id,
                   'token_type': session.token_type,
                   'access_token': session.access_token,
                   'refresh_token': session.refresh_token}, f )
    return session
