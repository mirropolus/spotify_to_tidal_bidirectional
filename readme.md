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

## Running daily (recommended)

To keep both services in sync automatically, schedule the tool to run once a day. On macOS/Linux you can use `cron`:

```bash
crontab -e
```

Add a line like this (runs every day at 8am):

```
0 8 * * * cd /path/to/spotify_to_tidal_bidirectional && PYTHONPATH=/usr/local/lib/python3.11/site-packages:src python3.11 -m spotify_to_tidal
```

---

## All configuration options

See `example_config.yml` for a full list of options with comments, and run `python3.11 -m spotify_to_tidal --help` for all CLI flags.

---

## Original project

This tool is a fork of [spotify_to_tidal](https://github.com/spotify2tidal/spotify_to_tidal) by the spotify2tidal community. All credit for the original one-way sync implementation goes to the original contributors.
