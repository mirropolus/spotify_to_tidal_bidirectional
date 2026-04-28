#!/usr/bin/env python3

import asyncio
from .cache import failure_cache, track_match_cache, reverse_track_match_cache
import datetime
from difflib import SequenceMatcher
from functools import partial
from typing import Callable, List, Sequence, Set, Mapping
import math
import requests
import sys
import spotipy
import tidalapi
from .tidalapi_patch import add_multiple_tracks_to_playlist, clear_tidal_playlist, get_all_favorites, get_all_playlists, get_all_playlist_tracks
import time
from tqdm.asyncio import tqdm as atqdm
from tqdm import tqdm
import traceback
import unicodedata
import math

from .type import spotify as t_spotify
from .type import SyncDirectionLiteral, PlaylistConfig

_VALID_SYNC_DIRECTIONS = {"spotify_to_tidal", "tidal_to_spotify", "bidirectional"}


def resolve_sync_direction(config: dict, cli_override: str | None) -> SyncDirectionLiteral:
    """
    Returns the effective sync direction.
    Priority: CLI argument > config file > default ("spotify_to_tidal").
    Raises SystemExit with a descriptive message for invalid values.
    """
    if cli_override is not None:
        direction = cli_override
    else:
        direction = config.get("sync_direction", "spotify_to_tidal")

    if direction not in _VALID_SYNC_DIRECTIONS:
        sys.exit(
            f"Invalid sync_direction '{direction}'. "
            f"Must be one of: {', '.join(sorted(_VALID_SYNC_DIRECTIONS))}."
        )
    return direction  # type: ignore[return-value]


def resolve_playlist_sync_direction(
    playlist_config: PlaylistConfig | None,
    global_direction: SyncDirectionLiteral,
) -> SyncDirectionLiteral:
    """
    Returns the effective sync direction for a single playlist.
    Per-playlist sync_direction overrides the global direction.
    """
    if playlist_config is not None and "sync_direction" in playlist_config:
        return playlist_config["sync_direction"]
    return global_direction


def normalize(s) -> str:
    return unicodedata.normalize('NFD', s).encode('ascii', 'ignore').decode('ascii')

def simple(input_string: str) -> str:
    # only take the first part of a string before any hyphens or brackets to account for different versions
    return input_string.split('-')[0].strip().split('(')[0].strip().split('[')[0].strip()

def isrc_match(tidal_track: tidalapi.Track, spotify_track) -> bool:
    if "isrc" in spotify_track["external_ids"]:
        return tidal_track.isrc == spotify_track["external_ids"]["isrc"]
    return False

def duration_match(tidal_track: tidalapi.Track, spotify_track, tolerance=2) -> bool:
    # the duration of the two tracks must be the same to within 2 seconds
    return abs(tidal_track.duration - spotify_track['duration_ms']/1000) < tolerance

def name_match(tidal_track, spotify_track) -> bool:
    def exclusion_rule(pattern: str, tidal_track: tidalapi.Track, spotify_track: t_spotify.SpotifyTrack):
        spotify_has_pattern = pattern in spotify_track['name'].lower()
        tidal_has_pattern = pattern in tidal_track.name.lower() or (not tidal_track.version is None and (pattern in tidal_track.version.lower()))
        return spotify_has_pattern != tidal_has_pattern

    # handle some edge cases
    if exclusion_rule("instrumental", tidal_track, spotify_track): return False
    if exclusion_rule("acapella", tidal_track, spotify_track): return False
    if exclusion_rule("remix", tidal_track, spotify_track): return False

    # the simplified version of the Spotify track name must be a substring of the Tidal track name
    # Try with both un-normalized and then normalized
    simple_spotify_track = simple(spotify_track['name'].lower()).split('feat.')[0].strip()
    return simple_spotify_track in tidal_track.name.lower() or normalize(simple_spotify_track) in normalize(tidal_track.name.lower())

def artist_match(tidal: tidalapi.Track | tidalapi.Album, spotify) -> bool:
    def split_artist_name(artist: str) -> Sequence[str]:
       if '&' in artist:
           return artist.split('&')
       elif ',' in artist:
           return artist.split(',')
       else:
           return [artist]

    def get_tidal_artists(tidal: tidalapi.Track | tidalapi.Album, do_normalize=False) -> Set[str]:
        result: list[str] = []
        for artist in tidal.artists:
            if do_normalize:
                artist_name = normalize(artist.name)
            else:
                artist_name = artist.name
            result.extend(split_artist_name(artist_name))
        return set([simple(x.strip().lower()) for x in result])

    def get_spotify_artists(spotify, do_normalize=False) -> Set[str]:
        result: list[str] = []
        for artist in spotify['artists']:
            if do_normalize:
                artist_name = normalize(artist['name'])
            else:
                artist_name = artist['name']
            result.extend(split_artist_name(artist_name))
        return set([simple(x.strip().lower()) for x in result])
    # There must be at least one overlapping artist between the Tidal and Spotify track
    # Try with both un-normalized and then normalized
    if get_tidal_artists(tidal).intersection(get_spotify_artists(spotify)) != set():
        return True
    return get_tidal_artists(tidal, True).intersection(get_spotify_artists(spotify, True)) != set()

def match(tidal_track, spotify_track) -> bool:
    if not spotify_track['id']: return False
    return isrc_match(tidal_track, spotify_track) or (
        duration_match(tidal_track, spotify_track)
        and name_match(tidal_track, spotify_track)
        and artist_match(tidal_track, spotify_track)
    )

def test_album_similarity(spotify_album, tidal_album, threshold=0.6):
    return SequenceMatcher(None, simple(spotify_album['name']), simple(tidal_album.name)).ratio() >= threshold and artist_match(tidal_album, spotify_album)

async def tidal_search(spotify_track, rate_limiter, tidal_session: tidalapi.Session) -> tidalapi.Track | None:
    def _search_for_track_in_album():
        # search for album name and first album artist
        if 'album' in spotify_track and 'artists' in spotify_track['album'] and len(spotify_track['album']['artists']):
            query = simple(spotify_track['album']['name']) + " " + simple(spotify_track['album']['artists'][0]['name'])
            album_result = tidal_session.search(query, models=[tidalapi.album.Album])
            for album in album_result['albums']:
                if album.num_tracks >= spotify_track['track_number'] and test_album_similarity(spotify_track['album'], album):
                    album_tracks = album.tracks()
                    if len(album_tracks) < spotify_track['track_number']:
                        assert( not len(album_tracks) == album.num_tracks ) # incorrect metadata :(
                        continue
                    track = album_tracks[spotify_track['track_number'] - 1]
                    if match(track, spotify_track):
                        failure_cache.remove_match_failure(spotify_track['id'])
                        return track

    def _search_for_standalone_track():
        # if album search fails then search for track name and first artist
        query = simple(spotify_track['name']) + ' ' + simple(spotify_track['artists'][0]['name'])
        for track in tidal_session.search(query, models=[tidalapi.media.Track])['tracks']:
            if match(track, spotify_track):
                failure_cache.remove_match_failure(spotify_track['id'])
                return track
    await rate_limiter.acquire()
    album_search = await asyncio.to_thread( _search_for_track_in_album )
    if album_search:
        return album_search
    await rate_limiter.acquire()
    track_search = await asyncio.to_thread( _search_for_standalone_track )
    if track_search:
        return track_search

    # if none of the search modes succeeded then store the track id to the failure cache
    failure_cache.cache_match_failure(spotify_track['id'])

async def spotify_search(tidal_track: tidalapi.Track, rate_limiter: asyncio.Semaphore, spotify_session: spotipy.Spotify):
    """
    Searches Spotify for the equivalent of a Tidal track.
    1. Try ISRC match via spotify_session.search(q=f"isrc:{tidal_track.isrc}", type="track")
    2. Fall back to name + first artist search, then verify with match()
    Records failure in failure_cache on miss (key: "tidal:{tidal_track.id}").
    """
    cache_key = f"tidal:{tidal_track.id}"

    # Step 1: attempt ISRC match
    await rate_limiter.acquire()
    isrc_results = await asyncio.to_thread(
        spotify_session.search,
        q=f"isrc:{tidal_track.isrc}",
        type="track"
    )
    for candidate in isrc_results.get('tracks', {}).get('items', []):
        if match(tidal_track, candidate):
            failure_cache.remove_match_failure(cache_key)
            return candidate

    # Step 2: fall back to name + first artist search
    await rate_limiter.acquire()
    name_results = await asyncio.to_thread(
        spotify_session.search,
        q=f"{simple(tidal_track.name)} {simple(tidal_track.artists[0].name)}",
        type="track"
    )
    for candidate in name_results.get('tracks', {}).get('items', []):
        if match(tidal_track, candidate):
            failure_cache.remove_match_failure(cache_key)
            return candidate

    # Both steps failed — record the miss
    failure_cache.cache_match_failure(cache_key)
    return None


async def search_new_tracks_on_spotify(spotify_session: spotipy.Spotify, tidal_tracks: Sequence[tidalapi.Track], playlist_name: str, config: dict):
    """
    Mirrors search_new_tracks_on_tidal for the reverse direction.
    Populates reverse_track_match_cache and appends unmatched tracks to
    "songs not found.txt" with the same format.
    """
    async def _run_rate_limiter(semaphore):
        ''' Leaky bucket algorithm for rate limiting. Periodically releases items from semaphore at rate_limit'''
        _sleep_time = config.get('max_concurrency', 10) / config.get('rate_limit', 10) / 4
        t0 = datetime.datetime.now()
        while True:
            await asyncio.sleep(_sleep_time)
            t = datetime.datetime.now()
            dt = (t - t0).total_seconds()
            new_items = round(config.get('rate_limit', 10) * dt)
            t0 = t
            [semaphore.release() for i in range(new_items)]

    # Filter out tracks already in the failure cache (retry interval not elapsed)
    # and tracks already present in reverse_track_match_cache
    tracks_to_search = [
        track for track in tidal_tracks
        if not failure_cache.has_match_failure(f"tidal:{track.id}")
        and not reverse_track_match_cache.get(f"tidal:{track.id}")
    ]

    if not tracks_to_search:
        return

    # Search for each of the tracks on Spotify concurrently
    task_description = "Searching Spotify for {}/{} tracks in Tidal playlist '{}'".format(
        len(tracks_to_search), len(tidal_tracks), playlist_name
    )
    semaphore = asyncio.Semaphore(config.get('max_concurrency', 10))
    rate_limiter_task = asyncio.create_task(_run_rate_limiter(semaphore))
    search_results = await atqdm.gather(
        *[repeat_on_request_error(spotify_search, t, semaphore, spotify_session) for t in tracks_to_search],
        desc=task_description
    )
    rate_limiter_task.cancel()

    # Add the search results to the reverse cache
    song404 = []
    for idx, tidal_track in enumerate(tracks_to_search):
        if search_results[idx]:
            reverse_track_match_cache.insert((f"tidal:{tidal_track.id}", search_results[idx]['id']))
        else:
            artist_names = ', '.join([a.name for a in tidal_track.artists])
            entry = f"tidal:{tidal_track.id}: {artist_names} - {tidal_track.name}"
            song404.append(entry)
            color = ('\033[91m', '\033[0m')
            print(color[0] + "Could not find the track " + entry + color[1])

    if song404:
        file_name = "songs not found.txt"
        header = f"==========================\nPlaylist: {playlist_name}\n==========================\n"
        with open(file_name, "a", encoding="utf-8") as file:
            file.write(header)
            for song in song404:
                file.write(f"{song}\n")


async def repeat_on_request_error(function, *args, remaining=5, **kwargs):
    # utility to repeat calling the function up to 5 times if an exception is thrown
    try:
        return await function(*args, **kwargs)
    except (tidalapi.exceptions.TooManyRequests, requests.exceptions.RequestException, spotipy.exceptions.SpotifyException) as e:
        if remaining:
            print(f"{str(e)} occurred, retrying {remaining} times")
        else:
            print(f"{str(e)} could not be recovered")

        if isinstance(e, requests.exceptions.RequestException) and not e.response is None:
            print(f"Response message: {e.response.text}")
            print(f"Response headers: {e.response.headers}")

        if not remaining:
            print("Aborting sync")
            print(f"The following arguments were provided:\n\n {str(args)}")
            print(traceback.format_exc())
            sys.exit(1)
        sleep_schedule = {5: 1, 4:10, 3:60, 2:5*60, 1:10*60} # sleep variable length of time depending on retry number
        time.sleep(sleep_schedule.get(remaining, 1))
        return await repeat_on_request_error(function, *args, remaining=remaining-1, **kwargs)


async def _fetch_all_from_spotify_in_chunks(fetch_function: Callable) -> List[dict]:
    output = []
    results = fetch_function(0)
    output.extend([item['track'] for item in results['items'] if item['track'] is not None])

    # Get all the remaining tracks in parallel
    if results['next']:
        offsets = [results['limit'] * n for n in range(1, math.ceil(results['total'] / results['limit']))]
        extra_results = await atqdm.gather(
            *[asyncio.to_thread(fetch_function, offset) for offset in offsets],
            desc="Fetching additional data chunks"
        )
        for extra_result in extra_results:
            output.extend([item['track'] for item in extra_result['items'] if item['track'] is not None])

    return output


async def get_tracks_from_spotify_playlist(spotify_session: spotipy.Spotify, spotify_playlist):
    def _get_tracks_from_spotify_playlist(offset: int, playlist_id: str):
        fields = "next,total,limit,items(track(name,album(name,artists),artists,track_number,duration_ms,id,external_ids(isrc))),type"
        return spotify_session.playlist_tracks(playlist_id=playlist_id, fields=fields, offset=offset)

    print(f"Loading tracks from Spotify playlist '{spotify_playlist['name']}'")
    items = await repeat_on_request_error( _fetch_all_from_spotify_in_chunks, lambda offset: _get_tracks_from_spotify_playlist(offset=offset, playlist_id=spotify_playlist["id"]))
    track_filter = lambda item: item.get('type', 'track') == 'track' # type may be 'episode' also
    sanity_filter = lambda item: ('album' in item
                                  and 'name' in item['album']
                                  and 'artists' in item['album']
                                  and len(item['album']['artists']) > 0
                                  and item['album']['artists'][0]['name'] is not None)
    return list(filter(sanity_filter, filter(track_filter, items)))

def populate_track_match_cache(spotify_tracks_: Sequence[t_spotify.SpotifyTrack], tidal_tracks_: Sequence[tidalapi.Track]):
    """ Populate the track match cache with all the existing tracks in Tidal playlist corresponding to Spotify playlist """
    def _populate_one_track_from_spotify(spotify_track: t_spotify.SpotifyTrack):
        for idx, tidal_track in list(enumerate(tidal_tracks)):
            if tidal_track.available and match(tidal_track, spotify_track):
                track_match_cache.insert((spotify_track['id'], tidal_track.id))
                tidal_tracks.pop(idx)
                return

    def _populate_one_track_from_tidal(tidal_track: tidalapi.Track):
        for idx, spotify_track in list(enumerate(spotify_tracks)):
            if tidal_track.available and match(tidal_track, spotify_track):
                track_match_cache.insert((spotify_track['id'], tidal_track.id))
                spotify_tracks.pop(idx)
                return

    # make a copy of the tracks to avoid modifying original arrays
    spotify_tracks = [t for t in spotify_tracks_]
    tidal_tracks = [t for t in tidal_tracks_]

    # first populate from the tidal tracks
    for track in tidal_tracks:
        _populate_one_track_from_tidal(track)
    # then populate from the subset of Spotify tracks that didn't match (to account for many-to-one style mappings)
    for track in spotify_tracks:
        _populate_one_track_from_spotify(track)

def get_new_spotify_tracks(spotify_tracks: Sequence[t_spotify.SpotifyTrack]) -> List[t_spotify.SpotifyTrack]:
    ''' Extracts only the tracks that have not already been seen in our Tidal caches '''
    results = []
    for spotify_track in spotify_tracks:
        if not spotify_track['id']: continue
        if not track_match_cache.get(spotify_track['id']) and not failure_cache.has_match_failure(spotify_track['id']):
            results.append(spotify_track)
    return results

def get_tracks_for_new_tidal_playlist(spotify_tracks: Sequence[t_spotify.SpotifyTrack]) -> Sequence[int]:
    ''' gets list of corresponding tidal track ids for each spotify track, ignoring duplicates '''
    output = []
    seen_tracks = set()

    for spotify_track in spotify_tracks:
        if not spotify_track['id']: continue
        tidal_id = track_match_cache.get(spotify_track['id'])
        if tidal_id:
            if tidal_id in seen_tracks:
                track_name = spotify_track['name']
                artist_names = ', '.join([artist['name'] for artist in spotify_track['artists']])
                print(f'Duplicate found: Track "{track_name}" by {artist_names} will be ignored') 
            else:
                output.append(tidal_id)
                seen_tracks.add(tidal_id)
    return output

async def search_new_tracks_on_tidal(tidal_session: tidalapi.Session, spotify_tracks: Sequence[t_spotify.SpotifyTrack], playlist_name: str, config: dict):
    """ Generic function for searching for each item in a list of Spotify tracks which have not already been seen and adding them to the cache """
    async def _run_rate_limiter(semaphore):
        ''' Leaky bucket algorithm for rate limiting. Periodically releases items from semaphore at rate_limit'''
        _sleep_time = config.get('max_concurrency', 10)/config.get('rate_limit', 10)/4 # aim to sleep approx time to drain 1/4 of 'bucket'
        t0 = datetime.datetime.now()
        while True:
            await asyncio.sleep(_sleep_time)
            t = datetime.datetime.now()
            dt = (t - t0).total_seconds()
            new_items = round(config.get('rate_limit', 10)*dt)
            t0 = t
            [semaphore.release() for i in range(new_items)] # leak new_items from the 'bucket'

    # Extract the new tracks that do not already exist in the old tidal tracklist
    tracks_to_search = get_new_spotify_tracks(spotify_tracks)
    if not tracks_to_search:
        return

    # Search for each of the tracks on Tidal concurrently
    task_description = "Searching Tidal for {}/{} tracks in Spotify playlist '{}'".format(len(tracks_to_search), len(spotify_tracks), playlist_name)
    semaphore = asyncio.Semaphore(config.get('max_concurrency', 10))
    rate_limiter_task = asyncio.create_task(_run_rate_limiter(semaphore))
    search_results = await atqdm.gather( *[ repeat_on_request_error(tidal_search, t, semaphore, tidal_session) for t in tracks_to_search ], desc=task_description )
    rate_limiter_task.cancel()

    # Add the search results to the cache
    song404 = []
    for idx, spotify_track in enumerate(tracks_to_search):
        if search_results[idx]:
            track_match_cache.insert( (spotify_track['id'], search_results[idx].id) )
        else:
            song404.append(f"{spotify_track['id']}: {','.join([a['name'] for a in spotify_track['artists']])} - {spotify_track['name']}")
            color = ('\033[91m', '\033[0m')
            print(color[0] + "Could not find the track " + song404[-1] + color[1])
    file_name = "songs not found.txt"
    header = f"==========================\nPlaylist: {playlist_name}\n==========================\n"
    with open(file_name, "a", encoding="utf-8") as file:
        file.write(header)
        for song in song404:
            file.write(f"{song}\n")

            
async def sync_playlist(spotify_session: spotipy.Spotify, tidal_session: tidalapi.Session, spotify_playlist, tidal_playlist: tidalapi.Playlist | None, config: dict):
    """ sync given playlist to tidal """
    # Get the tracks from both Spotify and Tidal, creating a new Tidal playlist if necessary
    spotify_tracks = await get_tracks_from_spotify_playlist(spotify_session, spotify_playlist)
    if len(spotify_tracks) == 0:
        return # nothing to do
    if tidal_playlist:
        old_tidal_tracks = await get_all_playlist_tracks(tidal_playlist)
    else:
        print(f"No playlist found on Tidal corresponding to Spotify playlist: '{spotify_playlist['name']}', creating new playlist")
        tidal_playlist =  tidal_session.user.create_playlist(spotify_playlist['name'], spotify_playlist['description'])
        old_tidal_tracks = []

    # Extract the new tracks from the playlist that we haven't already seen before
    populate_track_match_cache(spotify_tracks, old_tidal_tracks)
    await search_new_tracks_on_tidal(tidal_session, spotify_tracks, spotify_playlist['name'], config)
    new_tidal_track_ids = get_tracks_for_new_tidal_playlist(spotify_tracks)

    # Update the Tidal playlist if there are changes
    old_tidal_track_ids = [t.id for t in old_tidal_tracks]
    if new_tidal_track_ids == old_tidal_track_ids:
        print("No changes to write to Tidal playlist")
    elif new_tidal_track_ids[:len(old_tidal_track_ids)] == old_tidal_track_ids:
        # Append new tracks to the existing playlist if possible
        add_multiple_tracks_to_playlist(tidal_playlist, new_tidal_track_ids[len(old_tidal_track_ids):])
    else:
        # Erase old playlist and add new tracks from scratch if any reordering occured
        clear_tidal_playlist(tidal_playlist)
        add_multiple_tracks_to_playlist(tidal_playlist, new_tidal_track_ids)

async def sync_playlist_tidal_to_spotify(
    spotify_session: spotipy.Spotify,
    tidal_session: tidalapi.Session,
    tidal_playlist: tidalapi.Playlist,
    spotify_playlist,
    config: dict,
) -> None:
    """
    Reads tracks from tidal_playlist, searches for Spotify equivalents,
    then creates or updates the Spotify playlist.
    Creates a new Spotify playlist if spotify_playlist is None.
    """
    # Step 1: load tracks from the Tidal playlist
    tidal_tracks = await get_all_playlist_tracks(tidal_playlist)
    if len(tidal_tracks) == 0:
        return  # nothing to do

    # Step 2/3: resolve or create the Spotify playlist
    if spotify_playlist is None:
        print(
            f"No playlist found on Spotify corresponding to Tidal playlist: "
            f"'{tidal_playlist.name}', creating new playlist"
        )
        spotify_playlist = await repeat_on_request_error(
            asyncio.to_thread,
            spotify_session.user_playlist_create,
            spotify_session.current_user()['id'],
            tidal_playlist.name,
            description=tidal_playlist.description or "",
        )
        old_spotify_tracks = []
    else:
        # Step 4: load existing tracks from the Spotify playlist
        old_spotify_tracks = await get_tracks_from_spotify_playlist(spotify_session, spotify_playlist)

    # Step 5: search for Tidal tracks on Spotify, populating reverse_track_match_cache
    await search_new_tracks_on_spotify(spotify_session, tidal_tracks, tidal_playlist.name, config)

    # Step 6: build the new Spotify track ID list, skipping duplicates
    new_spotify_track_ids = []
    seen: set = set()
    for tidal_track in tidal_tracks:
        spotify_id = reverse_track_match_cache.get(f"tidal:{tidal_track.id}")
        if spotify_id and spotify_id not in seen:
            new_spotify_track_ids.append(spotify_id)
            seen.add(spotify_id)

    # Step 7: compare new vs. existing Spotify track IDs and update if needed
    old_spotify_track_ids = [t['id'] for t in old_spotify_tracks]

    if new_spotify_track_ids == old_spotify_track_ids:
        print("No changes to write to Spotify playlist")
        return

    if new_spotify_track_ids[:len(old_spotify_track_ids)] == old_spotify_track_ids:
        # Append-only case: only add the new tracks at the end
        tracks_to_add = new_spotify_track_ids[len(old_spotify_track_ids):]
        await repeat_on_request_error(
            asyncio.to_thread,
            spotify_session.playlist_add_items,
            spotify_playlist['id'],
            tracks_to_add,
        )
    else:
        # Reorder/replace case: clear the playlist then re-add all tracks in batches of 100
        await repeat_on_request_error(
            asyncio.to_thread,
            spotify_session.playlist_replace_items,
            spotify_playlist['id'],
            [],
        )
        for i in range(0, len(new_spotify_track_ids), 100):
            batch = new_spotify_track_ids[i:i + 100]
            await repeat_on_request_error(
                asyncio.to_thread,
                spotify_session.playlist_add_items,
                spotify_playlist['id'],
                batch,
            )


def detect_conflict(
    spotify_tracks: Sequence,
    tidal_tracks: Sequence,
) -> bool:
    """
    Returns True if both playlists have tracks not present in the other,
    indicating divergence on both sides.
    Uses the match() predicate for cross-service track comparison.
    """
    # Check if any Tidal track has no match in spotify_tracks
    tidal_has_unmatched = any(
        not any(match(tidal_track, spotify_track) for spotify_track in spotify_tracks)
        for tidal_track in tidal_tracks
    )
    # Check if any Spotify track has no match in tidal_tracks
    spotify_has_unmatched = any(
        not any(match(tidal_track, spotify_track) for tidal_track in tidal_tracks)
        for spotify_track in spotify_tracks
    )
    return tidal_has_unmatched and spotify_has_unmatched


async def sync_playlist_merge(
    spotify_session: spotipy.Spotify,
    tidal_session: tidalapi.Session,
    spotify_playlist,
    tidal_playlist: tidalapi.Playlist | None,
    config: dict,
) -> None:
    """
    Merges both playlists: adds Tidal-only tracks to Spotify and Spotify-only tracks to Tidal.
    Nothing is removed or overwritten — only additive changes are made.
    """
    playlist_name = spotify_playlist['name'] if spotify_playlist is not None else tidal_playlist.name

    # Load both track lists (create missing playlist if needed)
    if spotify_playlist is None:
        print(f"No playlist found on Spotify corresponding to Tidal playlist: '{tidal_playlist.name}', creating new playlist")
        spotify_playlist = await repeat_on_request_error(
            asyncio.to_thread,
            spotify_session.user_playlist_create,
            spotify_session.current_user()['id'],
            tidal_playlist.name,
            description=tidal_playlist.description or "",
        )
        spotify_tracks = []
    else:
        spotify_tracks = await get_tracks_from_spotify_playlist(spotify_session, spotify_playlist)

    if tidal_playlist is None:
        print(f"No playlist found on Tidal corresponding to Spotify playlist: '{spotify_playlist['name']}', creating new playlist")
        tidal_playlist = tidal_session.user.create_playlist(spotify_playlist['name'], spotify_playlist['description'])
        tidal_tracks = []
    else:
        tidal_tracks = await get_all_playlist_tracks(tidal_playlist)

    # --- Push Spotify-only tracks → Tidal ---
    populate_track_match_cache(spotify_tracks, tidal_tracks)
    await search_new_tracks_on_tidal(tidal_session, spotify_tracks, playlist_name, config)
    new_tidal_ids = get_tracks_for_new_tidal_playlist(spotify_tracks)
    existing_tidal_ids = set(t.id for t in tidal_tracks)
    tidal_ids_to_add = [tid for tid in new_tidal_ids if tid not in existing_tidal_ids]
    if tidal_ids_to_add:
        add_multiple_tracks_to_playlist(tidal_playlist, tidal_ids_to_add)
    else:
        print(f"No new tracks to add to Tidal playlist '{playlist_name}'")

    # --- Push Tidal-only tracks → Spotify ---
    await search_new_tracks_on_spotify(spotify_session, tidal_tracks, playlist_name, config)
    existing_spotify_ids = set(t['id'] for t in spotify_tracks)
    spotify_ids_to_add = []
    seen: set = set()
    for tidal_track in tidal_tracks:
        spotify_id = reverse_track_match_cache.get(f"tidal:{tidal_track.id}")
        if spotify_id and spotify_id not in existing_spotify_ids and spotify_id not in seen:
            spotify_ids_to_add.append(spotify_id)
            seen.add(spotify_id)
    if spotify_ids_to_add:
        for i in range(0, len(spotify_ids_to_add), 100):
            batch = spotify_ids_to_add[i:i + 100]
            await repeat_on_request_error(
                asyncio.to_thread,
                spotify_session.playlist_add_items,
                spotify_playlist['id'],
                batch,
            )
    else:
        print(f"No new tracks to add to Spotify playlist '{playlist_name}'")


async def sync_playlist_bidirectional(
    spotify_session: spotipy.Spotify,
    tidal_session: tidalapi.Session,
    spotify_playlist,
    tidal_playlist: tidalapi.Playlist | None,
    config: dict,
) -> None:
    """
    Detects conflict between the two playlists.
    Applies conflict_resolution policy from config (default: "spotify_wins").
    Logs the playlist name and resolution action before proceeding.
    Delegates to sync_playlist (S→T) or sync_playlist_tidal_to_spotify (T→S).
    """
    # Step 1: load both track lists
    spotify_tracks = await get_tracks_from_spotify_playlist(spotify_session, spotify_playlist) if spotify_playlist is not None else []
    tidal_tracks = await get_all_playlist_tracks(tidal_playlist) if tidal_playlist is not None else []

    # Step 2: detect conflict
    conflict = detect_conflict(spotify_tracks, tidal_tracks)

    # Step 3: get conflict resolution policy
    policy = config.get("conflict_resolution", "both_win")

    # Determine a display name for logging (use whichever side exists)
    playlist_name = spotify_playlist['name'] if spotify_playlist is not None else tidal_playlist.name

    # Step 4: log if conflict detected
    if conflict:
        print(f"Conflict detected in playlist '{playlist_name}': applying policy '{policy}'")

    # Step 5 & 6: apply policy
    if policy == "both_win":
        # Merge: add each side's unique tracks to the other, no overwrites
        await sync_playlist_merge(spotify_session, tidal_session, spotify_playlist, tidal_playlist, config)
    elif policy == "tidal_wins":
        if tidal_playlist is None:
            # No Tidal playlist exists — fall back to syncing Spotify → Tidal to create it
            if spotify_playlist is not None:
                await sync_playlist(spotify_session, tidal_session, spotify_playlist, None, config)
            return
        await sync_playlist_tidal_to_spotify(
            spotify_session, tidal_session, tidal_playlist, spotify_playlist, config
        )
    else:
        # Default: "spotify_wins" — sync Spotify → Tidal
        if spotify_playlist is None:
            # No Spotify playlist exists — fall back to syncing Tidal → Spotify to create it
            if tidal_playlist is not None:
                await sync_playlist_tidal_to_spotify(spotify_session, tidal_session, tidal_playlist, None, config)
            return
        await sync_playlist(
            spotify_session, tidal_session, spotify_playlist, tidal_playlist, config
        )


async def sync_favorites(spotify_session: spotipy.Spotify, tidal_session: tidalapi.Session, config: dict):
    """ sync user favorites to tidal """
    async def get_tracks_from_spotify_favorites() -> List[dict]:
        _get_favorite_tracks = lambda offset: spotify_session.current_user_saved_tracks(offset=offset)    
        tracks = await repeat_on_request_error( _fetch_all_from_spotify_in_chunks, _get_favorite_tracks)
        tracks.reverse()
        return tracks

    def get_new_tidal_favorites() -> List[int]:
        existing_favorite_ids = set([track.id for track in old_tidal_tracks])
        new_ids = []
        for spotify_track in spotify_tracks:
            match_id = track_match_cache.get(spotify_track['id'])
            if match_id and not match_id in existing_favorite_ids:
                new_ids.append(match_id)
        return new_ids

    print("Loading favorite tracks from Spotify")
    spotify_tracks = await get_tracks_from_spotify_favorites()
    print("Loading existing favorite tracks from Tidal")
    old_tidal_tracks = await get_all_favorites(tidal_session.user.favorites, order='DATE')
    populate_track_match_cache(spotify_tracks, old_tidal_tracks)
    await search_new_tracks_on_tidal(tidal_session, spotify_tracks, "Favorites", config)
    new_tidal_favorite_ids = get_new_tidal_favorites()
    if new_tidal_favorite_ids:
        for tidal_id in tqdm(new_tidal_favorite_ids, desc="Adding new tracks to Tidal favorites"):
            tidal_session.user.favorites.add_track(tidal_id)
    else:
        print("No new tracks to add to Tidal favorites")

async def sync_favorites_tidal_to_spotify(spotify_session: spotipy.Spotify, tidal_session: tidalapi.Session, config: dict) -> None:
    """
    Reads Tidal favorites, searches for Spotify equivalents,
    and adds matched tracks to Spotify Liked Songs (skipping duplicates).
    """
    print("Loading favorite tracks from Tidal")
    tidal_tracks = await get_all_favorites(tidal_session.user.favorites, order='DATE')

    print("Loading existing Liked Songs from Spotify")
    _get_liked_tracks = lambda offset: spotify_session.current_user_saved_tracks(offset=offset)
    existing_spotify_liked = await repeat_on_request_error(_fetch_all_from_spotify_in_chunks, _get_liked_tracks)

    existing_liked_ids = {t['id'] for t in existing_spotify_liked if t}

    await search_new_tracks_on_spotify(spotify_session, tidal_tracks, "Favorites", config)

    new_liked_ids = []
    for tidal_track in tidal_tracks:
        spotify_id = reverse_track_match_cache.get(f"tidal:{tidal_track.id}")
        if spotify_id and spotify_id not in existing_liked_ids:
            new_liked_ids.append(spotify_id)

    if not new_liked_ids:
        print("No new tracks to add to Spotify Liked Songs")
        return

    for i in tqdm(range(0, len(new_liked_ids), 20), desc="Adding new tracks to Spotify Liked Songs"):
        batch = new_liked_ids[i:i + 20]
        await repeat_on_request_error(asyncio.to_thread, spotify_session.current_user_saved_tracks_add, batch)


def sync_playlists_wrapper(spotify_session: spotipy.Spotify, tidal_session: tidalapi.Session, playlists, config: dict):
  for spotify_playlist, tidal_playlist in playlists:
    # sync the spotify playlist to tidal
    asyncio.run(sync_playlist(spotify_session, tidal_session, spotify_playlist, tidal_playlist, config) )

def sync_favorites_wrapper(spotify_session: spotipy.Spotify, tidal_session: tidalapi.Session, config):
    asyncio.run(main=sync_favorites(spotify_session=spotify_session, tidal_session=tidal_session, config=config))

def sync_playlists_tidal_to_spotify_wrapper(spotify_session: spotipy.Spotify, tidal_session: tidalapi.Session, playlists, config: dict):
    """Wrapper for Tidal→Spotify playlist sync. playlists is a list of (tidal_playlist, spotify_playlist) tuples."""
    for tidal_playlist, spotify_playlist in playlists:
        asyncio.run(sync_playlist_tidal_to_spotify(spotify_session, tidal_session, tidal_playlist, spotify_playlist, config))

def sync_playlists_bidirectional_wrapper(spotify_session: spotipy.Spotify, tidal_session: tidalapi.Session, playlists, config: dict):
    """Wrapper for bidirectional playlist sync. playlists is a list of (spotify_playlist, tidal_playlist) tuples."""
    for spotify_playlist, tidal_playlist in playlists:
        asyncio.run(sync_playlist_bidirectional(spotify_session, tidal_session, spotify_playlist, tidal_playlist, config))

def sync_favorites_tidal_to_spotify_wrapper(spotify_session: spotipy.Spotify, tidal_session: tidalapi.Session, config):
    asyncio.run(sync_favorites_tidal_to_spotify(spotify_session=spotify_session, tidal_session=tidal_session, config=config))

def get_tidal_playlists_wrapper(tidal_session: tidalapi.Session) -> Mapping[str, tidalapi.Playlist]:
    tidal_playlists = asyncio.run(get_all_playlists(tidal_session.user))
    return {playlist.name: playlist for playlist in tidal_playlists}

def pick_tidal_playlist_for_spotify_playlist(spotify_playlist, tidal_playlists: Mapping[str, tidalapi.Playlist]):
    if spotify_playlist['name'] in tidal_playlists:
      # if there's an existing tidal playlist with the name of the current playlist then use that
      tidal_playlist = tidal_playlists[spotify_playlist['name']]
      return (spotify_playlist, tidal_playlist)
    else:
      return (spotify_playlist, None)

def get_user_playlist_mappings(spotify_session: spotipy.Spotify, tidal_session: tidalapi.Session, config, sync_direction: str = "spotify_to_tidal"):
    results = []
    tidal_playlists = get_tidal_playlists_wrapper(tidal_session)

    if sync_direction == "tidal_to_spotify":
        # Enumerate Tidal playlists and pair each with a matching Spotify playlist by name (or None)
        spotify_playlists = asyncio.run(get_playlists_from_spotify(spotify_session, config))
        spotify_by_name = {p['name']: p for p in spotify_playlists}
        for tidal_name, tidal_playlist in tidal_playlists.items():
            spotify_playlist = spotify_by_name.get(tidal_name, None)
            results.append((tidal_playlist, spotify_playlist))
    elif sync_direction == "bidirectional":
        # Enumerate BOTH Spotify and Tidal playlists, merge by name (union)
        # Return (spotify_playlist, tidal_playlist) tuples where either may be None
        spotify_playlists = asyncio.run(get_playlists_from_spotify(spotify_session, config))
        spotify_by_name = {p['name']: p for p in spotify_playlists}
        tidal_by_name = dict(tidal_playlists)  # already a name->playlist mapping

        all_names = set(spotify_by_name.keys()) | set(tidal_by_name.keys())
        for name in all_names:
            spotify_playlist = spotify_by_name.get(name, None)
            tidal_playlist = tidal_by_name.get(name, None)
            results.append((spotify_playlist, tidal_playlist))
    else:
        # Default: spotify_to_tidal — existing behavior unchanged
        spotify_playlists = asyncio.run(get_playlists_from_spotify(spotify_session, config))
        for spotify_playlist in spotify_playlists:
            results.append(pick_tidal_playlist_for_spotify_playlist(spotify_playlist, tidal_playlists))
    return results

async def get_playlists_from_spotify(spotify_session: spotipy.Spotify, config):
    # get all the playlists from the Spotify account
    playlists = []
    print("Loading Spotify playlists")
    first_results = spotify_session.current_user_playlists()
    exclude_list = set([x.split(':')[-1] for x in config.get('excluded_playlists', [])])
    playlists.extend([p for p in first_results['items']])
    user_id = spotify_session.current_user()['id']

    # get all the remaining playlists in parallel
    if first_results['next']:
        offsets = [ first_results['limit'] * n for n in range(1, math.ceil(first_results['total']/first_results['limit'])) ]
        extra_results = await atqdm.gather( *[asyncio.to_thread(spotify_session.current_user_playlists, offset=offset) for offset in offsets ] )
        for extra_result in extra_results:
            playlists.extend([p for p in extra_result['items']])

    # filter out playlists that don't belong to us or are on the exclude list
    my_playlist_filter = lambda p: p and p['owner']['id'] == user_id
    exclude_filter = lambda p: not p['id'] in exclude_list
    return list(filter( exclude_filter, filter( my_playlist_filter, playlists )))

def get_playlists_from_config(spotify_session: spotipy.Spotify, tidal_session: tidalapi.Session, config, sync_direction: str = "spotify_to_tidal"):
    # get the list of playlist sync mappings from the configuration file
    def get_playlist_ids(config):
        return [(item['spotify_id'], item['tidal_id']) for item in config['sync_playlists']]
    output = []
    for spotify_id, tidal_id in get_playlist_ids(config=config):
        try:
            spotify_playlist = spotify_session.playlist(playlist_id=spotify_id)
        except spotipy.SpotifyException as e:
            print(f"Error getting Spotify playlist {spotify_id}")
            raise e
        try:
            tidal_playlist = tidal_session.playlist(playlist_id=tidal_id)
        except Exception as e:
            print(f"Error getting Tidal playlist {tidal_id}")
            raise e
        if sync_direction == "tidal_to_spotify":
            output.append((tidal_playlist, spotify_playlist))
        else:
            # "spotify_to_tidal" and "bidirectional" both use (spotify_playlist, tidal_playlist)
            output.append((spotify_playlist, tidal_playlist))
    return output

