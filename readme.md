# spotify_to_tidal (bidirectional fork)

A command line tool for synchronizing playlists between Spotify and Tidal. Supports one-way sync (Spotify → Tidal or Tidal → Spotify) as well as full bidirectional sync. Due to various performance optimisations, it is particularly suited for periodic synchronisation of very large collections.

> **This is a fork** of the original [spotify_to_tidal](https://github.com/spotify2tidal/spotify_to_tidal) project, extended with bidirectional sync support.

---

## Requirements

- Python 3.10 or higher
- A Spotify account
- A Tidal account
- Git (to clone the repository)

---

## Installation

### 1. Install Python 3.10+

Check your Python version first:

```bash
python3 --version
```

If it shows 3.9 or lower, install a newer version. On macOS with Homebrew:

```bash
brew install python@3.11
```

### 2. Clone the repository

```bash
git clone https://github.com/mirropolus/spotify_to_tidal_bidirectional.git
cd spotify_to_tidal_bidirectional
```

### 3. Install the tool

```bash
python3.11 -m pip install -e .
```

> **Note for macOS users:** If you get `No module named pip`, run this first:
> ```bash
> curl -sS https://bootstrap.pypa.io/get-pip.py | python3.11
> ```
> Then retry the install command above.

---

## Setup

### 1. Create your config file

Copy the example config:

```bash
cp example_config.yml config.yml
```

### 2. Create a Spotify app

1. Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) and log in
2. Click **Create app**
3. Fill in any name and description
4. In the **Redirect URIs** field, enter exactly: `http://127.0.0.1:8888/callback` and click **Add**
5. Click **Save**
6. On the app page, click **Settings** to find your **Client ID** and **Client Secret**

### 3. Edit config.yml

Open `config.yml` and fill in your details:

```yaml
spotify:
  client_id: YOUR_CLIENT_ID        # from the Spotify app settings
  client_secret: YOUR_CLIENT_SECRET
  username: YOUR_SPOTIFY_USERNAME  # your Spotify username (not email)
  redirect_uri: http://127.0.0.1:8888/callback
```

> Your Spotify username can be found in your Spotify profile settings. It may be a string of letters and numbers, not your display name.

---

## Running the tool

### Basic sync (Spotify → Tidal)

```bash
python3.11 -m spotify_to_tidal
```

The first time you run it, a browser window will open asking you to log in to Spotify and authorize the app. After that, your session is saved and you won't need to log in again.

You will also be prompted to log in to Tidal the first time.

### Sync a specific playlist

```bash
python3.11 -m spotify_to_tidal --uri 1ABCDEqsABCD6EaABCDa0a
```

Replace `1ABCDEqsABCD6EaABCDa0a` with your playlist ID (found in the Spotify playlist URL).

### Sync Liked Songs only

```bash
python3.11 -m spotify_to_tidal --sync-favorites
```

To import Tidal favorites into Spotify Liked Songs, add the direction:

```bash
python3.11 -m spotify_to_tidal --sync-favorites --sync-direction tidal_to_spotify
```

### Liked Songs chronology

New Tidal-only favorites are saved to Spotify with the original Tidal
`date_added` value. The sync sends Spotify's documented `timestamped_ids`
payload in batches of 50, with timestamps converted to UTC ISO 8601. Request
order therefore does not determine Liked Songs chronology.

The favorites sync is deliberately non-destructive:

- existing Spotify likes are not submitted again and retain their current
  `added_at` value;
- no likes or favorites are removed;
- the library is not rebuilt;
- a Tidal favorite without `date_added` is logged and skipped rather than being
  assigned the current time.

The timestamped format is currently available on Spotify's
[Save Tracks for Current User endpoint](https://developer.spotify.com/documentation/web-api/reference/save-tracks-user).
Spotify marks that endpoint deprecated in favor of the generic library endpoint,
but the generic endpoint does not currently expose historical timestamps.

> Versions of this fork before this fix may have imported old Tidal favorites
> with a new Spotify `added_at`. Normal syncs do not repair those historical
> entries because doing so would require destructive remove/re-add operations.

### Audit favorites safely

Generate a read-only CSV comparison without changing either account:

```bash
python3.11 -m spotify_to_tidal --audit-favorites
```

The default report is `favorites_audit.csv` in the current directory. Choose a
different location with:

```bash
python3.11 -m spotify_to_tidal \
  --audit-favorites \
  --audit-output reports/favorites_audit.csv
```

The CSV contains actual saved-library IDs, a separate Spotify catalog-candidate
ID, ISRC, artist, title, both service timestamps, signed timestamp delta,
minute-level Spotify cluster size, source occurrence/conflict fields, and a
suspicion reason. `spotify_id` is populated only when the item is actually in
Liked Songs; `spotify_catalog_candidate_id` is only a search result for a
Tidal-only favorite.

Presence is evaluated many-to-many with the same semantic matcher used by sync.
Alternative provider IDs for the same recording are listed in
`matched_tidal_ids` and `matched_spotify_ids`; they are not incorrectly counted
as service-only entries. Match counts, `timestamp_reference_tidal_id`, and
`match_ambiguity` make version and historical-date ambiguity explicit.

Repeated collection resources with the same provider track ID are collapsed
before matching, so the report contains no duplicate saved-library IDs. The
earliest historical timestamp is retained. The source occurrence and conflict
columns make that normalization explicit, and the web app also shows an
integrity warning when it occurs. Statuses are:

| Status | Meaning |
|---|---|
| `matched` | Present in both saved libraries; no suspicious later Spotify timestamp detected |
| `matched_equivalent` | An additional provider ID maps to a recording already present on the other service |
| `timestamp_mismatch` | Present in both, but Spotify was added much later than Tidal |
| `tidal_only` | Saved only in Tidal and a Spotify catalog equivalent was found |
| `spotify_only` | Saved only in Spotify |
| `match_failed` | Saved in Tidal but no safe Spotify catalog match was found |

By default, a timestamp is suspicious when Spotify is more than 30 days later
than Tidal. The audit also detects bursts across all Spotify likes: when at
least three likes share the same UTC minute, a paired item added more than 24
hours after its Tidal timestamp is flagged even if it falls short of 30 days.
Same-day pairs remain matched. These heuristics can be adjusted in `config.yml`:

```yaml
audit_timestamp_mismatch_days: 30
audit_timestamp_cluster_size: 3
audit_timestamp_cluster_min_delta_hours: 24
```

The report is diagnostic only. It does not remove/re-add likes or repair dates.
Rows with `match_ambiguity` must not be used as automatic repair instructions.

### Preview a favorites sync

Generate a read-only synchronization plan before allowing any account writes:

```bash
python3.11 -m spotify_to_tidal \
  --dry-run \
  --sync-favorites \
  --sync-direction bidirectional \
  --dry-run-audit-input favorites_audit.csv
```

The default plan is `favorites_dry_run.csv`; choose another path with
`--dry-run-output`. The command is favorites-only, requests only Spotify read
scopes, performs catalog searches without modifying match/failure caches, and
never calls favorite, like, playlist, or delete endpoints.

The optional `--dry-run-audit-input` cross-checks current candidates against a
previous read-only audit. Primary saved IDs and many-to-many matched-ID columns
are accepted; catalog-only candidate IDs are deliberately not treated as saved
library entries. A contradictory prior match blocks the proposed action as
`audit_conflict`.

Plan actions are:

| Action | Meaning |
|---|---|
| `would_add` | Semantically absent from the destination and a safe catalog candidate was found |
| `skip_existing` | Defensive exact-ID check found the candidate already saved |
| `blocked` | A required historical timestamp is missing |
| `match_failed` | No safe destination catalog match was found |
| `duplicate_target` | Another source row resolves to the same destination ID; only the primary row can remain `would_add` |
| `origin_suspect` | A Spotify source timestamp belongs to a dense addition cluster and may have been imported by the historical bug |
| `audit_conflict` | A prior audit indicates that the recording or exact destination ID was already present |
| `metadata_review` | The candidate matched by title, artist and duration rather than exact ISRC and requires manual review |

The CSV includes source and destination ISRC, artist, title and duration,
`match_method` (`exact_isrc`, `metadata`, or `catalog_unverified`), cluster size,
safety flags, audit references and coalesced source IDs. Only `would_add` rows
represent unblocked candidate writes. `dry_run_origin_cluster_size` controls the
minimum same-UTC-minute Spotify cluster size and defaults to the audit cluster
threshold (3). Cluster and audit checks are intentionally conservative: review
their rows manually rather than treating them as confirmed errors.

Normal Tidal → Spotify favorites sync uses the same safety boundary: it
deduplicates Tidal provider IDs, keeps the earliest historical date, and does
not search or add a different Spotify version when any existing Liked Songs
track already matches the recording. The dry-run plan remains advisory and
does not repair historical timestamps.

---

## Bidirectional sync

This fork adds the ability to sync in both directions — so changes you make on Tidal also appear on Spotify, and vice versa.

### Sync Tidal → Spotify only

```bash
python3.11 -m spotify_to_tidal --sync-direction tidal_to_spotify
```

### Sync both ways

```bash
python3.11 -m spotify_to_tidal --sync-direction bidirectional
```

Or set it permanently in `config.yml`:

```yaml
sync_direction: bidirectional
```

The `--sync-direction` flag accepts three values:

| Value | Behaviour |
|---|---|
| `spotify_to_tidal` | *(default)* Spotify → Tidal only |
| `tidal_to_spotify` | Tidal → Spotify only |
| `bidirectional` | Both directions |

---

## Conflict resolution

When running bidirectional sync, both playlists may have different tracks since the last run. The `conflict_resolution` setting controls what happens:

| Value | Behaviour |
|---|---|
| `both_win` | *(default)* Merge both playlists — unique tracks from each side are added to the other, nothing is removed |
| `spotify_wins` | Spotify version overwrites Tidal |
| `tidal_wins` | Tidal version overwrites Spotify |

Set it in `config.yml`:

```yaml
conflict_resolution: both_win  # default — recommended for daily sync
```

`both_win` is the safest option: your playlists only ever grow, nothing gets deleted.

---

## Per-playlist sync direction

If you want different sync behaviour for specific playlists, use the `sync_playlists` block in `config.yml`:

```yaml
sync_playlists:
  - spotify_id: 1ABCDEqsABCD6EaABCDa0a
    tidal_id: a0b1234-0a1b-012a-abcd-a1b234c5d6d7
    sync_direction: tidal_to_spotify  # overrides the global sync_direction for this playlist
```

---

## Manual favorites sync with GitHub Actions

`.github/workflows/sync.yml` currently provides only a manual
`workflow_dispatch` trigger. Its command includes `--sync-favorites
--sync-direction bidirectional`, so it synchronizes only Tidal favorites and
Spotify Liked Songs. It does not synchronize playlists and does not run an
automatic historical repair.

Create these repository secrets under **Settings → Secrets and variables →
Actions** before enabling the workflow:

| Secret | Purpose |
|---|---|
| `SPOTIFY_CLIENT_ID` | Spotify application client ID |
| `SPOTIFY_CLIENT_SECRET` | Spotify application client secret |
| `SPOTIFY_REFRESH_TOKEN` | Refresh token from a prior local Spotify authorization with the required scopes |
| `TIDAL_ACCESS_TOKEN` | Access token from a prior local Tidal OAuth session |
| `TIDAL_REFRESH_TOKEN` | Refresh token from that Tidal OAuth session |

Authentication remains backward-compatible locally: Spotipy uses its existing
cache and Tidal uses `.session.yml`. In CI, the environment variables above are
used explicitly, browser login is disabled, and refreshed Spotify tokens are
held in memory rather than written to `.cache-*`. Tidal environment credentials
are likewise not written to `.session.yml`.

Do not commit token caches or use them as workflow artifacts. `config.yml`,
`config.yaml`, `.cache*`, `.session.yml`, `.env`, and generated favorites audit
CSVs are ignored by Git. The workflow grants only read access to repository
contents and does not upload session files or caches.

After merging, add the secrets and use **Run workflow** for the first real
production execution—but only after a local bidirectional `--dry-run` CSV has
been reviewed and approved. Review its logs and the favorites audit before
enabling any recurring execution. The daily `schedule` trigger is intentionally
omitted for now and should be added in a later change only after that manual run
has been validated. Playlist synchronization should remain out of this workflow
until it is reviewed separately.

---

## Mobile read-only audit app

The optional **Favorite Bridge Audit** PWA provides a phone-friendly OAuth and
audit flow without asking users to copy access or refresh tokens. It connects
to Spotify with PKCE and `user-library-read`, and to Tidal with Authorization
Code + PKCE limited to the public read-only `collection.read` scope. Tidal
favorites and their historical timestamps are loaded through the official
OpenAPI `userCollectionTracks` relationship; the PWA does not use Tidal Device
Login or legacy `tidalapi` favorites endpoints. It then runs the existing
matching audit and lets the user download `favorites_audit.csv`.

It contains no synchronization or library-write routes. Tokens and generated
reports remain only in process memory and expire with the browser session.

Install and start it locally with:

```bash
python -m pip install -e ".[web]"
export AUDIT_WEB_BASE_URL=http://127.0.0.1:8765
export SPOTIFY_CLIENT_ID=your_spotify_client_id
export TIDAL_CLIENT_ID=your_tidal_client_id
export TIDAL_CLIENT_SECRET=your_tidal_client_secret
spotify_to_tidal_web
```

Register these exact redirect URIs in the respective developer dashboards:

- Spotify: `http://127.0.0.1:8765/auth/spotify/callback`
- Tidal: `http://127.0.0.1:8765/auth/tidal/callback`

Open `http://127.0.0.1:8765`, connect both accounts and run the audit. Hosted
deployments must register the same callback paths under their exact HTTPS
`AUDIT_WEB_BASE_URL` origin.

See [the mobile audit application guide](docs/mobile-audit.md) for provider
registration, Docker usage, mobile/HTTPS deployment, CSV fields and security
limitations.

---

## All configuration options

See `example_config.yml` for a full list of options with comments, and run `python3.11 -m spotify_to_tidal --help` for all CLI flags.

---

## Original project

This tool is a fork of [spotify_to_tidal](https://github.com/spotify2tidal/spotify_to_tidal) by the spotify2tidal community. All credit for the original one-way sync implementation goes to the original contributors.
