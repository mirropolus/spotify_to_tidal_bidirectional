import yaml
import argparse
from pathlib import Path
import sys

from . import sync as _sync
from . import auth as _auth
from . import audit as _audit
from . import dry_run as _dry_run

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
        '--sync-direction',
        dest='sync_direction',
        choices=['spotify_to_tidal', 'tidal_to_spotify', 'bidirectional'],
        default=None,
        help='sync direction: spotify_to_tidal, tidal_to_spotify, or bidirectional (overrides config file)',
    )
    args = parser.parse_args()

    with open(args.config, 'r') as f:
        config = yaml.safe_load(f) or {}

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
