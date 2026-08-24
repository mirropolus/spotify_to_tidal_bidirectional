# Favorite Bridge Audit mobile web app

Favorite Bridge Audit is a responsive, installable web application for safely
comparing Spotify Liked Songs and Tidal favorites. It reuses the project's
matching and timestamp-anomaly detection code and produces the same CSV schema
as `--audit-favorites`.

The web app is deliberately audit-only:

- Spotify requests only `user-library-read` through Authorization Code with
  PKCE.
- Tidal uses Authorization Code with PKCE (S256) and requests exactly the
  third-party read-only scope `collection.read`.
- There are no sync, playlist, unlike, delete-favorite, or library-write
  routes.
- Provider tokens, OAuth state and generated CSV bytes are stored only in the
  server process. They are never written to a cache, session file or browser
  storage.
- Sessions expire after two hours by default and can be deleted immediately
  from the interface.

## 1. Register the provider applications

### Spotify

1. Open the Spotify Developer Dashboard and create an application.
2. Add the exact redirect URI used by the audit server. For the local example:

   `http://127.0.0.1:8765/auth/spotify/callback`

3. Copy the Client ID. The web app uses PKCE, so it does not need or accept a
   Spotify Client Secret.
4. While the Spotify application is in Development Mode, add every test user
   to its allowlist. Current Spotify limits and endpoint availability apply.

For a hosted installation, replace the callback with its exact HTTPS origin,
for example `https://audit.example.org/auth/spotify/callback`.

### Tidal

1. Sign in to the [Tidal Developer Portal](https://developer.tidal.com/) and
   create an application. Tidal's official
   [authorization guide](https://developer.tidal.com/documentation/api-sdk/api-sdk-authorization)
   describes the Authorization Code + PKCE flow used here.
2. Add this exact redirect URI for the local example:

   `http://127.0.0.1:8765/auth/tidal/callback`

3. Copy its Client ID and Client Secret.
4. Keep the secret only in the server environment. Never put it in frontend
   JavaScript, the repository, an image, or a mobile application bundle.
5. If the Developer Dashboard asks which permissions the application uses,
   enable only My Collection read access (`collection.read`). Do not enable
   `collection.write`, playlist-write, or any other write permission.

For a hosted installation, register the exact HTTPS callback for that origin,
for example `https://audit.example.org/auth/tidal/callback`. It must exactly
match `AUDIT_WEB_BASE_URL` plus `/auth/tidal/callback`.

The audit uses Tidal's officially supported Authorization Code flow with PKCE
S256. It reads historical favorite timestamps from the official OpenAPI
relationship:

`GET /v2/userCollectionTracks/me/relationships/items`

Track and artist metadata is included directly in the paginated collection
relationship response with `include=items.artists`. This avoids a separate
catalog lookup and reduces the number of provider requests. The web path does
not use `tidalapi`, legacy `api.tidal.com/v1` favorites, the internal-only
Device Login flow, or the internal `r_usr` scope.

## 2. Run locally

Python 3.10 or newer is required.

```bash
git clone https://github.com/mirropolus/spotify_to_tidal_bidirectional.git
cd spotify_to_tidal_bidirectional
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[web]"
```

Set the environment values without committing them:

```bash
export AUDIT_WEB_BASE_URL=http://127.0.0.1:8765
export SPOTIFY_CLIENT_ID=your_spotify_client_id
export TIDAL_CLIENT_ID=your_tidal_client_id
export TIDAL_CLIENT_SECRET=your_tidal_client_secret
```

Start one application process:

```bash
spotify_to_tidal_web
```

Open `http://127.0.0.1:8765` in the browser. Select **Connect Spotify**, then
**Connect Tidal**, finish both provider consent screens, and select **Run
read-only audit**. The result summary appears on screen and **Download CSV**
returns `favorites_audit.csv`.

Because sessions are stored in process memory, do not start multiple Uvicorn
workers. Restarting the process intentionally disconnects every session and
removes every generated report.

Tidal may temporarily return HTTP `429` while a large collection is being
read. The audit retries read-only `GET` requests up to three times with
exponential backoff and jitter, and honors a reasonable `Retry-After` response
header. If Tidal asks for a wait longer than one minute, the request stops
instead of keeping the browser connection open and the page shows how many
seconds to wait before running the audit again. No partial report is retained.

## 3. Run with Docker

Build the image:

```bash
docker build -f Dockerfile.audit -t favorite-bridge-audit .
```

Create a local `.env` based on `.env.audit.example`, then run:

```bash
docker run --rm \
  --env-file .env \
  -p 127.0.0.1:8765:8765 \
  favorite-bridge-audit
```

The repository ignores `.env`. Do not add token values to the Docker image or
command line.

## 4. Use it from a phone

The user interface is responsive and can be installed from a compatible
mobile browser as a PWA. The safest options are:

- run it directly on a device capable of running Python and use the loopback
  URL; or
- host this container behind a private HTTPS reverse proxy and set
  `AUDIT_WEB_BASE_URL` to the exact public origin.

For a hosted origin:

- HTTPS is mandatory;
- update both Spotify and Tidal redirect URIs to exactly match their hosted
  callbacks;
- keep the deployment single-process unless sessions are moved to an encrypted
  shared store;
- disable query-string logging for the OAuth callback at the reverse proxy;
- restrict access to trusted users while the application is in personal beta.

The app itself starts Uvicorn with access logging disabled so one-time OAuth
callback codes do not appear in its logs.

## 5. Report contents

The CSV contains actual saved-library IDs, a separate
`spotify_catalog_candidate_id`, ISRC, artist/title, both `added_at` values,
timestamp delta, minute-level cluster information, source occurrence/conflict
fields, and one of these statuses:

- `matched`
- `timestamp_mismatch`
- `tidal_only`
- `spotify_only`
- `match_failed`

The report never repairs a suspicious timestamp. Repair would require account
writes and remains outside this application.

Before matching, repeated provider collection resources with the same track ID
are collapsed to one entry and the earliest available historical timestamp is
retained. `tidal_source_occurrences` and `spotify_source_occurrences` record how
many source entries were seen. The corresponding `*_duplicate_conflict`
columns report inconsistent timestamps or metadata, and the web summary warns
when duplicates or conflicts were found.

`spotify_id` means the track is actually present in Liked Songs. A catalog
search hit for a Tidal-only favorite appears only in
`spotify_catalog_candidate_id`; it must not be counted as an existing like.

The default timestamp heuristic flags Spotify dates more than 30 days after
Tidal. It also examines all Spotify likes at UTC-minute precision: a paired
track more than 24 hours later than Tidal is flagged when at least three likes
share that Spotify minute. This catches bulk-import bursts without treating a
same-day pair as damaged. Configure the web version with
`AUDIT_TIMESTAMP_MISMATCH_DAYS`, `AUDIT_TIMESTAMP_CLUSTER_SIZE`, and
`AUDIT_TIMESTAMP_CLUSTER_MIN_DELTA_HOURS` if different thresholds are needed.

## Security and operational limitations

- This is a single-user/personal-beta design, not yet a multi-tenant hosted
  service.
- It does not transfer provider tokens into GitHub Actions Secrets.
- It does not persist refresh tokens, so users reconnect after a process restart
  or session expiry.
- OAuth state and the PKCE verifier are single-use and are discarded after the
  callback, whether authorization succeeds or fails.
- Spotify's timestamp-preserving write endpoint is deprecated. The audit app
  does not call it, but a later synchronization UI must validate endpoint access
  for newly registered Spotify applications before enabling writes.
- A public multi-user release will require provider review, privacy/retention
  documentation, encrypted durable sessions, account deletion, abuse controls,
  rate limiting, and an explicit write-consent step.
