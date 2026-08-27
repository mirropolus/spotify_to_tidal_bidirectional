import yaml
import argparse
from pathlib import Path
from spotipy.exceptions import SpotifyException
import sys

from . import sync as _sync
from . import auth as _auth
from . import audit as _audit
from . import dry_run as _dry_run
from . import ordered_import as _ordered_import

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='config.yml', help='location of the config file')
    parser.add_argument('--uri', help='synchronize a specific URI instead of the one in the config')
    parser.add_argument('--sync-favorites', action=argparse.BooleanOptionalAction, help='synchronize the favorites')
    parser.add_argument(
        '--audit-favorites',
        action='store_true',
        help='compare Tidal favorites and Spotify Liked Songs without modifying either account',
    )
    parser.add_argument(
        '--audit-output',
        default='favorites_audit.csv',
        help='CSV path for --audit-favorites (default: favorites_audit.csv)',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='write a read-only favorites sync plan without modifying either account',
    )
    parser.add_argument(
        '--dry-run-output',
        default='favorites_dry_run.csv',
        help='CSV path for --dry-run (default: favorites_dry_run.csv)',
    )
    parser.add_argument(
        '--dry-run-audit-input',
        help='optional prior favorites audit CSV used to block contradictory plan rows',
    )
    parser.add_argument(
        '--approved-favorites-plan',
        help='reviewed dry-run CSV required before Spotify-to-Tidal favorite writes',
    )
    parser.add_argument(
        '--check-spotify-timestamp-support',
        action='store_true',
        help=(
            'send an empty, non-mutating request to check whether this Spotify '
            'Client ID can preserve historical Liked Songs timestamps'
        ),
    )
    parser.add_argument(
        '--prepare-ordered-import',
        metavar='REVIEWED_DRY_RUN_CSV',
        help=(
            'create a read-only, oldest-to-newest import checkpoint from safe '
            'tidal_to_spotify would_add rows'
        ),
    )
    parser.add_argument(
        '--ordered-import-output',
        default='favorites_ordered_import.csv',
        help=(
            'checkpoint path for --prepare-ordered-import '
            '(default: favorites_ordered_import.csv)'
        ),
    )
    parser.add_argument(
        '--execute-ordered-import',
        metavar='CHECKPOINT_CSV',
        help=(
            'create the exact private archive playlist and like checkpoint '
            'tracks one at a time in relative chronological order'
        ),
    )
    parser.add_argument(
        '--accept-current-spotify-dates',
        action='store_true',
        help='required acknowledgement for --execute-ordered-import',
    )
    parser.add_argument(
        '--ordered-import-playlist-name',
        default=_ordered_import.DEFAULT_ARCHIVE_PLAYLIST_NAME,
        help='private Spotify archive playlist name for ordered import',
    )
    parser.add_argument(
        '--ordered-import-spacing-seconds',
        type=int,
        default=_ordered_import.DEFAULT_SPACING_SECONDS,
        help='delay between sequential likes (minimum and default: 65 seconds)',
    )
    parser.add_argument(
        '--sync-direction',
        dest='sync_direction',
        choices=['spotify_to_tidal', 'tidal_to_spotify', 'bidirectional'],
        default=None,
        help='sync direction: spotify_to_tidal, tidal_to_spotify, or bidirectional (overrides config file)',
    )
    args = parser.parse_args()

    with open(args.config, 'r') as f:
        config = yaml.safe_load(f) or {}

    ordered_mode = bool(
        args.prepare_ordered_import or args.execute_ordered_import
    )
    if args.prepare_ordered_import and args.execute_ordered_import:
        sys.exit(
            "Choose either --prepare-ordered-import or "
            "--execute-ordered-import, not both"
        )
    if ordered_mode:
        incompatible = any([
            args.uri,
            args.sync_favorites is not None,
            args.audit_favorites,
            args.dry_run,
            args.dry_run_audit_input,
            args.approved_favorites_plan,
            args.check_spotify_timestamp_support,
            args.sync_direction,
        ])
        if incompatible:
            sys.exit(
                "Ordered import modes must be run by themselves "
                "(apart from --config and ordered-import options)"
            )
    if args.prepare_ordered_import:
        if args.accept_current_spotify_dates:
            sys.exit(
                "--accept-current-spotify-dates is only valid with "
                "--execute-ordered-import"
            )
        if not Path(args.prepare_ordered_import).is_file():
            sys.exit(
                "--prepare-ordered-import does not exist or is not a file: "
                f"{args.prepare_ordered_import}"
            )
        try:
            _ordered_import.prepare_ordered_import(
                args.prepare_ordered_import,
                args.ordered_import_output,
            )
        except (OSError, _ordered_import.OrderedImportError) as exc:
            sys.exit(f"Invalid ordered import plan: {exc}")
        return
    if args.execute_ordered_import:
        if not args.accept_current_spotify_dates:
            sys.exit(
                "--execute-ordered-import requires "
                "--accept-current-spotify-dates"
            )
        if not Path(args.execute_ordered_import).is_file():
            sys.exit(
                "--execute-ordered-import does not exist or is not a file: "
                f"{args.execute_ordered_import}"
            )
        if (
            args.ordered_import_spacing_seconds
            < _ordered_import.MINIMUM_SPACING_SECONDS
        ):
            sys.exit(
                "--ordered-import-spacing-seconds must be at least "
                f"{_ordered_import.MINIMUM_SPACING_SECONDS}"
            )
        print("Opening Spotify ordered-import session")
        spotify_session = _auth.open_spotify_session(
            config['spotify'],
            sync_direction="tidal_to_spotify",
        )
        try:
            _ordered_import.execute_ordered_import(
                spotify_session,
                args.execute_ordered_import,
                playlist_name=args.ordered_import_playlist_name,
                spacing_seconds=args.ordered_import_spacing_seconds,
            )
        except (
            OSError,
            SpotifyException,
            _ordered_import.OrderedImportError,
        ) as exc:
            sys.exit(f"Ordered import stopped safely: {exc}")
        return
    if args.accept_current_spotify_dates:
        sys.exit(
            "--accept-current-spotify-dates requires --execute-ordered-import"
        )

    if args.check_spotify_timestamp_support:
        incompatible = any([
            args.uri,
            args.sync_favorites is not None,
            args.audit_favorites,
            args.dry_run,
            args.dry_run_audit_input,
            args.approved_favorites_plan,
            args.sync_direction,
        ])
        if incompatible:
            sys.exit(
                "--check-spotify-timestamp-support must be run by itself "
                "(apart from --config)"
            )
        print("Opening Spotify timestamp-capability session")
        spotify_session = _auth.open_spotify_session(
            config['spotify'],
            sync_direction="tidal_to_spotify",
        )
        print("Checking Spotify timestamp support (empty request; no library changes)")
        try:
            _sync.check_spotify_timestamp_support(spotify_session)
        except _sync.SpotifyTimestampSaveError as exc:
            sys.exit(str(exc))
        print(
            "Supported: Spotify accepted the non-mutating timestamp probe. "
            "This Client ID can proceed to the reviewed Tidal-to-Spotify test."
        )
        return

    if args.audit_favorites and args.dry_run:
        sys.exit("Choose either --audit-favorites or --dry-run, not both")
    if args.approved_favorites_plan and (args.audit_favorites or args.dry_run):
        sys.exit(
            "--approved-favorites-plan is only valid for a real favorites sync"
        )
    if args.approved_favorites_plan and args.uri:
        sys.exit("--approved-favorites-plan cannot be combined with --uri")
    if args.approved_favorites_plan and args.sync_favorites is not True:
        sys.exit("--approved-favorites-plan requires --sync-favorites")

    approved_plan = None
    if args.approved_favorites_plan:
        if not Path(args.approved_favorites_plan).is_file():
            sys.exit(
                "--approved-favorites-plan does not exist or is not a file: "
                f"{args.approved_favorites_plan}"
            )
        try:
            approved_plan = _dry_run.load_approved_favorites_plan(
                args.approved_favorites_plan
            )
        except (OSError, ValueError) as exc:
            sys.exit(f"Invalid --approved-favorites-plan: {exc}")

    if args.audit_favorites:
        print("Opening read-only Spotify session")
        spotify_session = _auth.open_spotify_session(
            config['spotify'],
            sync_direction="spotify_to_tidal",
        )
        print("Opening Tidal session")
        tidal_session = _auth.open_tidal_session()
        if not tidal_session.check_login():
            sys.exit("Could not connect to Tidal")
        _audit.audit_favorites_wrapper(
            spotify_session,
            tidal_session,
            config,
            args.audit_output,
        )
        return

    if args.dry_run:
        if args.uri:
            sys.exit("--dry-run is favorites-only and cannot be combined with --uri")
        if (
            args.dry_run_audit_input
            and not Path(args.dry_run_audit_input).is_file()
        ):
            sys.exit(
                "--dry-run-audit-input does not exist or is not a file: "
                f"{args.dry_run_audit_input}"
            )
        if args.dry_run_audit_input:
            try:
                _dry_run.load_audit_crosscheck(args.dry_run_audit_input)
            except (OSError, ValueError) as exc:
                sys.exit(f"Invalid --dry-run-audit-input: {exc}")
        direction = _sync.resolve_sync_direction(config, args.sync_direction)
        print("Opening read-only Spotify session")
        spotify_session = _auth.open_spotify_session(
            config['spotify'],
            sync_direction="spotify_to_tidal",
        )
        print("Opening Tidal session")
        tidal_session = _auth.open_tidal_session()
        if not tidal_session.check_login():
            sys.exit("Could not connect to Tidal")
        _dry_run.favorites_dry_run_wrapper(
            spotify_session,
            tidal_session,
            config,
            direction,
            args.dry_run_output,
            args.dry_run_audit_input,
        )
        return

    # Resolve sync direction: CLI > config > default ("spotify_to_tidal")
    sync_direction = _sync.resolve_sync_direction(config, args.sync_direction)
    if args.approved_favorites_plan and sync_direction == "tidal_to_spotify":
        sys.exit(
            "--approved-favorites-plan is unnecessary for tidal_to_spotify"
        )
    if (
        args.sync_favorites is True
        and sync_direction in {"spotify_to_tidal", "bidirectional"}
        and approved_plan is None
    ):
        sys.exit(
            "Spotify-to-Tidal favorites writes require "
            "--approved-favorites-plan from a reviewed dry-run CSV"
        )

    print("Opening Spotify session")
    spotify_session = _auth.open_spotify_session(config['spotify'], sync_direction=sync_direction)
    print("Opening Tidal session")
    tidal_session = _auth.open_tidal_session()
    if not tidal_session.check_login():
        sys.exit("Could not connect to Tidal")

    if args.uri:
        # if a playlist ID is explicitly provided as a command line argument then use that
        spotify_playlist = spotify_session.playlist(args.uri)
        tidal_playlists = _sync.get_tidal_playlists_wrapper(tidal_session)
        tidal_playlist = _sync.pick_tidal_playlist_for_spotify_playlist(spotify_playlist, tidal_playlists)
        if sync_direction == "tidal_to_spotify":
            _sync.sync_playlists_tidal_to_spotify_wrapper(spotify_session, tidal_session, [(tidal_playlist[1], tidal_playlist[0])], config)
        elif sync_direction == "bidirectional":
            _sync.sync_playlists_bidirectional_wrapper(spotify_session, tidal_session, [tidal_playlist], config)
        else:
            _sync.sync_playlists_wrapper(spotify_session, tidal_session, [tidal_playlist], config)
        sync_favorites = args.sync_favorites  # only sync favorites if command line argument explicitly passed
    elif args.sync_favorites:
        sync_favorites = True  # sync only the favorites
    elif config.get('sync_playlists', None):
        # if the config contains a sync_playlists list of mappings then use that
        playlists = _sync.get_playlists_from_config(spotify_session, tidal_session, config, sync_direction=sync_direction)
        if sync_direction == "tidal_to_spotify":
            _sync.sync_playlists_tidal_to_spotify_wrapper(spotify_session, tidal_session, playlists, config)
        elif sync_direction == "bidirectional":
            _sync.sync_playlists_bidirectional_wrapper(spotify_session, tidal_session, playlists, config)
        else:
            _sync.sync_playlists_wrapper(spotify_session, tidal_session, playlists, config)
        sync_favorites = args.sync_favorites is None and config.get('sync_favorites_default', True)
    else:
        # otherwise sync all the user playlists in the Spotify account and favorites unless explicitly disabled
        playlists = _sync.get_user_playlist_mappings(spotify_session, tidal_session, config, sync_direction=sync_direction)
        if sync_direction == "tidal_to_spotify":
            _sync.sync_playlists_tidal_to_spotify_wrapper(spotify_session, tidal_session, playlists, config)
        elif sync_direction == "bidirectional":
            _sync.sync_playlists_bidirectional_wrapper(spotify_session, tidal_session, playlists, config)
        else:
            _sync.sync_playlists_wrapper(spotify_session, tidal_session, playlists, config)
        sync_favorites = args.sync_favorites is None and config.get('sync_favorites_default', True)

    if sync_favorites:
        if (
            sync_direction in {"spotify_to_tidal", "bidirectional"}
            and approved_plan is None
        ):
            sys.exit(
                "Spotify-to-Tidal favorites writes require "
                "--approved-favorites-plan from a reviewed dry-run CSV"
            )
        if sync_direction == "tidal_to_spotify":
            _sync.sync_favorites_tidal_to_spotify_wrapper(spotify_session, tidal_session, config)
        elif sync_direction == "bidirectional":
            _sync.sync_favorites_wrapper(
                spotify_session,
                tidal_session,
                config,
                approved_plan,
            )
            _sync.sync_favorites_tidal_to_spotify_wrapper(spotify_session, tidal_session, config)
        else:
            _sync.sync_favorites_wrapper(
                spotify_session,
                tidal_session,
                config,
                approved_plan,
            )

if __name__ == '__main__':
    main()
    sys.exit(0)
