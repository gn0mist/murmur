# Murmur Miniplayer

A small always-on-top miniplayer for Spotify on Windows 11, with synced karaoke-style lyrics.

It reads what Spotify is playing from the Windows media session, so there's no Spotify login or API key.

**Status:** alpha (v0.1.0). Expect rough edges.

## Features

- Album art, title, artist, progress bar (click to seek)
- Back / play / next and a volume slider on hover
- Synced lyrics from [LRCLIB](https://lrclib.net), highlighted word by word
- Colors that follow the album art
- Nano mode (double-click to toggle)
- Mouse wheel over the player changes Spotify's volume
- Right-click for opacity, nano mode, always on top, lock position, and more

## Requirements

- Windows 10/11
- Python 3.10+
- The Spotify desktop app

## Install

```
python -m pip install -r requirements.txt
```

## Run

```
python murmur.py
```

Rename the file to `.pyw` to run it without a console window.

## Files it creates

- `~/.murmur.json`: settings and window position
- `~/.murmur_lyrics/`: lyrics cache

Settings and the lyrics cache from the earlier `spotify_miniplayer` name are
carried over automatically on first run.

## License

MIT. See [LICENSE](LICENSE).

---

Gnomist · Praxis 1
