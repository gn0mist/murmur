# Gnomist Praxis 1 · lyrics.py · MIT
"""Synced lyrics from LRCLIB: fetching, caching, and LRC parsing."""

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from settings import LRCLIB, LYRICS_CACHE, __version__

# ── Lyrics ───────────────────────────
# ∴ Cache answers, never failures.
_TS = re.compile(r"\[(\d{1,3}):(\d{1,2}(?:\.\d{1,3})?)\]")
_WORD_TAG = re.compile(r"<\d+:\d+(?:\.\d+)?>")


def parse_lrc(text: str) -> list[tuple[float, str]]:
    """Parse LRC text into timed lines.

    Blank lines are kept: they mark gaps and the end of the previous line.

    Args:
        text: Raw LRC, e.g. '[01:23.45] some words'.

    Returns:
        Lines as (seconds, text), sorted by time.
    """
    out = []
    for raw in text.splitlines():
        stamps = _TS.findall(raw)
        if not stamps:
            continue
        body = _WORD_TAG.sub("", _TS.sub("", raw)).strip()
        for m, s in stamps:
            out.append((int(m) * 60 + float(s), body))
    out.sort(key=lambda x: x[0])
    return out


def _clean_title(t: str) -> str:
    """Strip extras like '(feat. X)' or ' - Remastered' for a looser search.

    Args:
        t: Track title as Spotify reports it.
    """
    t = re.sub(r"\s*[\(\[].*?[\)\]]", "", t)  # (feat. X), [Live]
    t = re.sub(r"\s+-\s+.*$", "", t)  # " - Remastered 2011"
    return t.strip() or t


def _http_json(url: str, timeout: float = 8) -> object:
    """GET a URL and decode the JSON response.

    Args:
        url: Full request URL.
        timeout: Seconds before giving up.
    """
    req = urllib.request.Request(
        url, headers={"User-Agent": f"murmur-miniplayer/{__version__}"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _error_kind(e: Exception) -> str:
    """Sort a fetch error into server / network / other.

    Args:
        e: The exception raised while fetching.
    """
    if isinstance(e, urllib.error.HTTPError):
        return "server" if (e.code >= 500 or e.code == 429) else "other"
    if isinstance(e, (urllib.error.URLError, TimeoutError, OSError)):
        return "network"
    return "other"


def fetch_synced_lyrics(
    title: str, artist: str, album: str, duration: float, force: bool = False
) -> tuple[str | None, tuple[str, str] | None]:
    """Look up synced lyrics on LRCLIB. Blocking; run it on a worker thread.

    Args:
        title: Track title.
        artist: Track artist(s).
        album: Album title.
        duration: Track length in seconds (0 if unknown).
        force: Skip the disk cache and ask the server.

    Returns:
        (raw LRC text or None, error or None), where error is
        (kind, message) and kind is server / network / other.
    """
    try:
        LYRICS_CACHE.mkdir(exist_ok=True)
    except Exception:
        pass
    key = hashlib.sha1(
        f"{artist}|{title}|{album}|{int(duration)}".encode("utf-8")
    ).hexdigest()
    cache_file = LYRICS_CACHE / f"{key}.json"
    if cache_file.exists() and not force:
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            return cached.get("synced"), None
        except Exception:
            pass

    synced = None
    try:
        if duration > 0:
            q = urllib.parse.urlencode(
                {
                    "track_name": title,
                    "artist_name": artist,
                    "album_name": album,
                    "duration": int(round(duration)),
                }
            )
            try:
                synced = _http_json(f"{LRCLIB}/get?{q}").get("syncedLyrics")
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
        if not synced:
            first_artist = re.split(r"[,;&]", artist)[0].strip()
            q = urllib.parse.urlencode(
                {"track_name": _clean_title(title), "artist_name": first_artist}
            )
            for r in _http_json(f"{LRCLIB}/search?{q}"):
                if not r.get("syncedLyrics"):
                    continue
                if (
                    duration > 0
                    and r.get("duration")
                    and abs(r["duration"] - duration) > 8
                ):
                    continue
                synced = r["syncedLyrics"]
                break
    except Exception as e:
        print("lyrics fetch error:", repr(e))
        return None, (_error_kind(e), f"{type(e).__name__}: {e}")

    try:
        cache_file.write_text(json.dumps({"synced": synced}), encoding="utf-8")
    except Exception:
        pass
    return synced, None
