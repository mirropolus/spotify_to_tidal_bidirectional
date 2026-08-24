# Favorite Bridge Audit mobile web app

Favorite Bridge Audit is a responsive, installable web application for safely
comparing Spotify Liked Songs and Tidal favorites. It reuses the project's
matching and timestamp-anomaly detection code and produces the same CSV schema
as `--audit-favorites`.

The web app is deliberately audit-only:

- Spotify requests only `user-library-read` through Authorization Code with
  PKCE.
- Tidal device authorization requests only `r_usr`.
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

1. Sign in to the Tidal Developer Portal and create an application.
2. Copy its Client ID and Client Secret.
3. Keep the secret only in the server environment. Never put it in frontend
   JavaScript, the repository, an image, or a mobile application bundle.

The current implementation uses Tidal's device authorization endpoint through
`tidalapi`, but replaces that library's bundled credentials with the
operator-owned credentials and narrows the requested scope to `r_usr`.

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
- update the Spotify redirect URI to exactly match the hosted callback;
- keep the deployment single-process unless sessions are moved to an encrypted
  shared store;
- disable query-string logging for the OAuth callback at the reverse proxy;
- restrict access to trusted users while the application is in personal beta.

The app itself starts Uvicorn with access logging disabled so one-time OAuth
callback codes do not appear in its logs.

## 5. Report contents

The CSV contains service IDs, ISRC, artist/title, both `added_at` values,
timestamp delta, cluster information, and one of these statuses:

- `matched`
- `timestamp_mismatch`
- `tidal_only`
- `spotify_only`
- `match_failed`

The report never repairs a suspicious timestamp. Repair would require account
writes and remains outside this application.

## Security and operational limitations

- This is a single-user/personal-beta design, not yet a multi-tenant hosted
  service.
- It does not transfer provider tokens into GitHub Actions Secrets.
- It does not persist refresh tokens, so users reconnect after a process restart
  or session expiry.
- Spotify's timestamp-preserving write endpoint is deprecated. The audit app
  does not call it, but a later synchronization UI must validate endpoint access
  for newly registered Spotify applications before enabling writes.
- A public multi-user release will require provider review, privacy/retention
  documentation, encrypted durable sessions, account deletion, abuse controls,
  rate limiting, and an explicit write-consent step.
