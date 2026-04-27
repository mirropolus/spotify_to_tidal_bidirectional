A command line tool for synchronizing playlists between Spotify and Tidal. Supports one-way sync (Spotify → Tidal or Tidal → Spotify) as well as full bidirectional sync. Due to various performance optimisations, it is particularly suited for periodic synchronisation of very large collections.

Installation
-----------
Clone this git repository and then run:

```bash
python3 -m pip install -e .
```

Setup
-----
0. Rename the file example_config.yml to config.yml
0. Go [here](https://developer.spotify.com/documentation/general/guides/authorization/app-settings/) and register a new app on developer.spotify.com.
0. Copy and paste your client ID and client secret to the Spotify part of the config file
0. Copy and paste the value in 'redirect_uri' of the config file to Redirect URIs at developer.spotify.com and press ADD
0. Enter your Spotify username to the config file

Usage
----
To synchronize all of your Spotify playlists with your Tidal account run the following from the project root directory.
Windows ignores python module paths by default, but you can run them using `python3 -m spotify_to_tidal`

```bash
spotify_to_tidal
```

You can also just synchronize a specific playlist by doing the following:

```bash
spotify_to_tidal --uri 1ABCDEqsABCD6EaABCDa0a # accepts playlist id or full playlist uri
```

or sync just your 'Liked Songs' with:

```bash
spotify_to_tidal --sync-favorites
```

#### Bidirectional sync

To sync Tidal playlists back to Spotify (useful if you add tracks on Tidal and want them reflected in Spotify):

```bash
spotify_to_tidal --sync-direction tidal_to_spotify
```

To keep both services fully in sync with each other:

```bash
spotify_to_tidal --sync-direction bidirectional
```

The `--sync-direction` flag accepts three values:

| Value | Behaviour |
|---|---|
| `spotify_to_tidal` | *(default)* Spotify → Tidal only |
| `tidal_to_spotify` | Tidal → Spotify only |
| `bidirectional` | Both directions |

You can also set the direction permanently in `config.yml`:

```yaml
sync_direction: bidirectional
```

#### Conflict resolution

When running bidirectional sync, both playlists may have diverged since the last run. You can control which service wins:

```yaml
conflict_resolution: spotify_wins  # default
# conflict_resolution: tidal_wins
```

With `spotify_wins` (the default), the Spotify version overwrites Tidal when a conflict is detected. With `tidal_wins`, the Tidal version overwrites Spotify.

#### Per-playlist sync direction

If you use `sync_playlists` in your config to sync specific playlists, you can set a different direction per playlist:

```yaml
sync_playlists:
  - spotify_id: 1ABCDEqsABCD6EaABCDa0a
    tidal_id: a0b1234-0a1b-012a-abcd-a1b234c5d6d7
    sync_direction: tidal_to_spotify  # overrides the global sync_direction for this playlist
```

See `example_config.yml` for all configuration options, and `spotify_to_tidal --help` for all CLI flags.

---

#### Join our amazing community as a code contributor
<br><br>
<a href="https://github.com/spotify2tidal/spotify_to_tidal/graphs/contributors">
  <img class="dark-light" src="https://contrib.rocks/image?repo=spotify2tidal/spotify_to_tidal&anon=0&columns=25&max=100&r=true" />
</a>
