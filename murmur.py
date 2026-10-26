# Gnomist Praxis 1 · murmur.py · MIT
"""Murmur Miniplayer: a small Spotify miniplayer for Windows 11.

Reads whatever Spotify is playing from the Windows media session (SMTC),
so no Spotify login or API keys are needed. Only Spotify is shown; other
players are ignored. Volume controls Spotify's slider in the Windows
volume mixer (0-100%).

Install:
  python -m pip install -r requirements.txt
Run:
  python murmur.py        (or rename to .pyw to hide the console)

Hover the player for the controls (back / play / next and a volume slider)
and a small X (top right) to close it. Click the microphone icon to show or
hide the lyrics strip. Scroll the mouse wheel over the player to change
volume. Double-click the player to switch between full and nano mode.
Right-click for settings: opacity, nano mode, behavior.

Settings: ~/.murmur.json
Lyrics cache: ~/.murmur_lyrics
"""

import asyncio
import bisect
import hashlib
import importlib.util
import json
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, QEvent, QObject, Signal, QTimer, QRectF, QPoint, QRect
from PySide6.QtGui import (
    QColor,
    QContextMenuEvent,
    QEnterEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPixmap,
    QImage,
    QFont,
    QFontMetricsF,
    QLinearGradient,
    QPen,
    QBrush,
    QResizeEvent,
    QTextOption,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QLabel,
    QPushButton,
    QHBoxLayout,
    QVBoxLayout,
    QMenu,
    QSlider,
    QWidgetAction,
    QToolTip,
    QAbstractButton,
)

from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)
from winrt.windows.storage.streams import DataReader

__version__ = "0.1.0-alpha"
APP_NAME = "Murmur Miniplayer"


# ── Settings ─────────────────────────
CONFIG_PATH = Path.home() / ".murmur.json"
LYRICS_CACHE = Path.home() / ".murmur_lyrics"
LEGACY_CONFIG = Path.home() / ".spotify_miniplayer.json"
LEGACY_LYRICS = Path.home() / ".spotify_miniplayer_lyrics"
LRCLIB = "https://lrclib.net/api"
DEFAULT_BG = QColor(24, 24, 28)

BORDER_PURPLE = QColor(143, 89, 255)
BORDER_ORANGE = QColor(255, 148, 51)

FULL_W = 360
NANO_SIZE = 144
ART_FULL = 92
ART_NANO = 120

DEFAULTS = {
    "show_lyrics": True,
    "opacity": 93,  # background opacity, percent
    "nano": False,
    "always_on_top": True,
    "locked": False,
    "controls_always": False,
}

# Shown in the lyrics strip, keyed by the kind of failure.
LYRICS_ERROR_TEXT = {
    "server": "The lyrics server is having trouble right now "
    "(not your app). Retrying shortly.",
    "network": "Can't reach the lyrics server right now. Retrying shortly.",
    "other": "Couldn't load lyrics. Retrying shortly.",
}


def _migrate_legacy_files():
    """Carry settings and the lyrics cache over from the old app name.

    Runs once: only when the new files don't exist yet. The old settings
    file is copied (left as a backup); the cache folder is moved.
    """
    try:
        if not CONFIG_PATH.exists() and LEGACY_CONFIG.exists():
            shutil.copyfile(LEGACY_CONFIG, CONFIG_PATH)
        if not LYRICS_CACHE.exists() and LEGACY_LYRICS.is_dir():
            LEGACY_LYRICS.rename(LYRICS_CACHE)
    except OSError as e:
        print("migration error:", repr(e))


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


# ── Spotify volume ───────────────────
# ∴ COM stays on its own thread, away from Qt.
VOLUME_POOL = ThreadPoolExecutor(max_workers=1)
HAVE_PYCAW = importlib.util.find_spec("pycaw") is not None


def spotify_volume(op: str, value: float | None = None) -> float | None:
    """Get or set Spotify's slider in the Windows volume mixer.

    Windows tops out at 100% per app.

    Args:
        op: 'get' or 'set'.
        value: New volume 0..1 (for 'set').

    Returns:
        Spotify's volume 0..1, or None if it can't be found (Spotify is
        closed or hasn't started playing audio yet).
    """
    try:
        import comtypes
        from pycaw.pycaw import AudioUtilities
    except Exception:
        return None
    comtypes.CoInitialize()
    try:
        result = None
        for s in AudioUtilities.GetAllSessions():
            proc = s.Process
            if proc is not None and proc.name().lower() == "spotify.exe":
                vol = s.SimpleAudioVolume
                if op == "set":
                    vol.SetMasterVolume(max(0.0, min(1.0, float(value))), None)
                result = vol.GetMasterVolume()
        return result
    except Exception as e:
        print("volume error:", repr(e))
        return None
    finally:
        try:
            comtypes.CoUninitialize()
        except Exception:
            pass


# ── Media session ────────────────────
# ∴ Spotify only; every other player is ignored.
class MediaBackend(QObject):
    """Polls the Windows media session on a background asyncio thread."""

    updated = Signal(dict)
    lyrics_ready = Signal(object, object, object)  # (track key, lines, error)

    def __init__(self):
        """Start the background event loop and begin polling."""
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self._manager = None
        self._last_key = None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        """Run the asyncio loop forever (background thread)."""
        asyncio.set_event_loop(self.loop)
        self.loop.create_task(self._poll())
        self.loop.run_forever()

    async def _get_session(self):
        """Return Spotify's media session, or None if it isn't running."""
        if self._manager is None:
            self._manager = await SessionManager.request_async()
        for s in self._manager.get_sessions():
            if "spotify" in s.source_app_user_model_id.lower():
                return s
        return None

    async def _read_thumbnail(self, ref) -> bytes | None:
        """Read the album art stream into bytes.

        Args:
            ref: The session's thumbnail stream reference (may be None).
        """
        if ref is None:
            return None
        try:
            stream = await ref.open_read_async()
            size = stream.size
            if not size:
                return None
            reader = DataReader(stream)
            await reader.load_async(size)
            buf = bytearray(size)
            reader.read_bytes(buf)
            return bytes(buf)
        except Exception:
            return None

    async def _load_lyrics(
        self, key: tuple, title: str, artist: str, album: str, duration: float
    ):
        """Fetch lyrics for a track and emit them.

        Quick retries first; if the server is still unhappy, show the
        message and keep trying quietly about once a minute.

        Args:
            key: Track key, so the UI can ignore answers for old songs.
            title: Track title.
            artist: Track artist(s).
            album: Album title.
            duration: Track length in seconds.
        """
        quick = (0, 3, 8, 20)
        slow = (60,) * 20
        loop = asyncio.get_running_loop()
        for attempt, pause in enumerate(quick + slow):
            if pause:
                await asyncio.sleep(pause)
                if self._last_key != key:  # song changed while waiting
                    return
            try:
                raw, err = await loop.run_in_executor(
                    None,
                    fetch_synced_lyrics,
                    title,
                    artist,
                    album,
                    duration,
                    attempt > 0,  # a retry must not reuse the cache
                )
            except Exception as e:
                raw, err = None, ("other", repr(e))
            if not err:
                lines = parse_lrc(raw) if raw else []
                print(
                    f"lyrics: {title!r} / {artist!r} dur={duration:.0f}s "
                    f"-> {len(lines)} lines"
                )
                self.lyrics_ready.emit(key, lines, None)
                return
            print(f"lyrics attempt {attempt + 1} failed: {err[1]}")
            if attempt == len(quick) - 1:
                self.lyrics_ready.emit(key, [], err)

    async def _poll(self):
        """Read the session once a second and emit the playback state."""
        while True:
            try:
                session = await self._get_session()
                if session is None:
                    self._last_key = None
                    self.updated.emit({"active": False})
                else:
                    props = await session.try_get_media_properties_async()
                    info = session.get_playback_info()
                    tl = session.get_timeline_properties()
                    playing = info.playback_status == PlaybackStatus.PLAYING

                    duration = 0.0
                    position = 0.0
                    try:
                        duration = (tl.end_time - tl.start_time).total_seconds()
                        position = tl.position.total_seconds()
                        if playing:
                            # SMTC reports the position as of its last update
                            lu = tl.last_updated_time
                            if lu.tzinfo is None:
                                lu = lu.replace(tzinfo=timezone.utc)
                            position += max(
                                0.0,
                                (datetime.now(timezone.utc) - lu).total_seconds(),
                            )
                        position = min(position, duration) if duration else position
                    except Exception:
                        pass

                    key = (props.title, props.artist, props.album_title)
                    art = None
                    art_changed = key != self._last_key
                    if art_changed:
                        self._last_key = key
                        art = await self._read_thumbnail(props.thumbnail)
                        asyncio.ensure_future(
                            self._load_lyrics(
                                key,
                                props.title or "",
                                props.artist or "",
                                props.album_title or "",
                                duration,
                            )
                        )

                    self.updated.emit(
                        {
                            "active": True,
                            "key": key,
                            "title": props.title or "Unknown title",
                            "artist": props.artist or "",
                            "playing": playing,
                            "position": position,
                            "duration": duration,
                            "art_changed": art_changed,
                            "art": art,
                        }
                    )
            except Exception as e:  # keep polling no matter what
                print("poll error:", e)
            await asyncio.sleep(1.0)

    def command(self, name: str, *args: float):
        """Send a playback command from the UI thread.

        Args:
            name: 'toggle', 'next', 'prev' or 'seek'.
            *args: For 'seek', the target position in seconds.
        """
        asyncio.run_coroutine_threadsafe(self._command(name, *args), self.loop)

    async def _command(self, name: str, *args: float):
        """Carry out a playback command on the session.

        Args:
            name: 'toggle', 'next', 'prev' or 'seek'.
            *args: For 'seek', the target position in seconds.
        """
        try:
            s = await self._get_session()
            if s is None:
                return
            if name == "toggle":
                await s.try_toggle_play_pause_async()
            elif name == "next":
                await s.try_skip_next_async()
            elif name == "prev":
                await s.try_skip_previous_async()
            elif name == "seek":
                # SMTC position is in 100-nanosecond ticks
                await s.try_change_playback_position_async(int(args[0] * 10_000_000))
        except Exception as e:
            print("command error:", e)


# ── Widgets ──────────────────────────
def _mix(c1: QColor, c2: QColor, m: float) -> QColor:
    """Blend two colors.

    Args:
        c1: Color at m = 0.
        c2: Color at m = 1.
        m: Mix amount, clamped to 0..1.
    """
    m = max(0.0, min(1.0, m))
    return QColor(
        int(c1.red() + (c2.red() - c1.red()) * m),
        int(c1.green() + (c2.green() - c1.green()) * m),
        int(c1.blue() + (c2.blue() - c1.blue()) * m),
        int(c1.alpha() + (c2.alpha() - c1.alpha()) * m),
    )


class ProgressBar(QWidget):
    """Thin playback bar; click anywhere on it to seek."""

    seek = Signal(float)  # fraction 0..1

    def __init__(self):
        """Create an empty bar."""
        super().__init__()
        self.fraction = 0.0
        self.color = QColor(255, 255, 255)
        self.setFixedHeight(10)
        self.setCursor(Qt.PointingHandCursor)

    def set_fraction(self, f: float):
        """Set how much of the bar is filled.

        Args:
            f: Fraction played, clamped to 0..1.
        """
        self.fraction = max(0.0, min(1.0, f))
        self.update()

    def paintEvent(self, _: QPaintEvent):
        """Draw the track and the played part."""
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        h = 4
        y = (self.height() - h) / 2
        track = QRectF(0, y, self.width(), h)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 50))
        p.drawRoundedRect(track, 2, 2)
        fill = QRectF(0, y, self.width() * self.fraction, h)
        p.setBrush(self.color)
        p.drawRoundedRect(fill, 2, 2)

    def mousePressEvent(self, e: QMouseEvent):
        """Emit a seek to the clicked spot."""
        if e.button() == Qt.LeftButton and self.width() > 0:
            self.seek.emit(e.position().x() / self.width())
            e.accept()


class LyricsButton(QAbstractButton):
    """Microphone icon that toggles the lyrics strip.

    Glows in the accent color when on, dim when off.
    """

    GLOW_LAYERS = ((6.0, 30), (3.5, 60))  # (pen width, alpha)
    OFF = QColor(255, 255, 255, 80)

    def __init__(self):
        """Create the checkable icon button."""
        super().__init__()
        self.setCheckable(True)
        self.setFixedSize(18, 18)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Lyrics")
        self.accent = QColor(255, 255, 255)
        self._hover = False

    def enterEvent(self, _: QEnterEvent):
        """Brighten the icon on hover."""
        self._hover = True
        self.update()

    def leaveEvent(self, _: QEvent):
        """Return to the normal look."""
        self._hover = False
        self.update()

    def _icon_paths(self) -> tuple[QPainterPath, QPainterPath]:
        """Build the microphone shape.

        Returns:
            (head, cradle): the filled capsule, and the stroked cradle,
            stand and base.
        """
        W, H = self.width(), self.height()
        cx = W / 2
        head = QPainterPath()
        head.addRoundedRect(QRectF(cx - 3, H * 0.12, 6, H * 0.48), 3, 3)
        cradle = QPainterPath()
        r = QRectF(cx - 5.5, H * 0.22, 11, H * 0.52)
        cradle.arcMoveTo(r, 180)
        cradle.arcTo(r, 180, 180)
        cradle.moveTo(cx, H * 0.74)
        cradle.lineTo(cx, H * 0.88)
        cradle.moveTo(cx - 3.5, H * 0.88)
        cradle.lineTo(cx + 3.5, H * 0.88)
        return head, cradle

    def paintEvent(self, _: QPaintEvent):
        """Draw the icon, with a glow when lyrics are on."""
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        head, cradle = self._icon_paths()
        on = self.isChecked()

        if on:
            for width, a in self.GLOW_LAYERS:
                c = QColor(self.accent)
                c.setAlpha(a)
                pen = QPen(c, width)
                pen.setJoinStyle(Qt.RoundJoin)
                pen.setCapStyle(Qt.RoundCap)
                p.strokePath(head, pen)
                p.strokePath(cradle, pen)
            color = QColor(self.accent)
        else:
            color = QColor(255, 255, 255, 150) if self._hover else self.OFF

        p.fillPath(head, color)
        pen = QPen(color, 1.4)
        pen.setCapStyle(Qt.RoundCap)
        p.strokePath(cradle, pen)


class LyricsStrip(QWidget):
    """One lyric line at a time, sung word by word.

    Each word fills with the accent color and glows as it's sung. Long
    lines scroll horizontally to follow the active word. Word timing is
    estimated: the line's duration is split across its words by length,
    since LRCLIB lines have no per-word timing. Also shows status messages
    and short notices.
    """

    SUNG = QColor(245, 245, 250, 235)
    DIM = QColor(255, 255, 255, 85)
    GLOW_LAYERS = ((9.0, 14), (6.0, 24), (3.5, 40))  # (pen width, alpha)

    def __init__(self):
        """Create an empty strip."""
        super().__init__()
        self.setFixedHeight(30)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)  # parent handles drag
        self.lines = []
        self.starts = []
        self.position = 0.0
        self.accent = QColor(255, 255, 255)
        self.bg = QColor(DEFAULT_BG)
        self.bg_alpha = 238
        self.status = ""
        self._notice = ""
        self._notice_id = 0
        self._font = QFont("Segoe UI", 11, QFont.DemiBold)
        self._fm = QFontMetricsF(self._font)
        self._idx = -1
        self._words = []  # (text, x, width, t_start, t_end)
        self._total_w = 0.0
        self._sung_end = 0.0

    def set_lines(self, lines: list[tuple[float, str]], status: str = ""):
        """Replace the lyrics.

        Args:
            lines: Timed lines from parse_lrc (may be empty).
            status: Message to show when there are no lines.
        """
        self.status = status
        self.lines = lines
        self.starts = [t for t, _ in lines]
        self._idx = -1
        self._words = []
        self.update()

    def set_position(self, pos: float):
        """Move the playhead.

        Args:
            pos: Playback position in seconds.
        """
        self.position = pos
        if self.lines:
            self.update()

    def flash(self, text: str, ms: int = 3500):
        """Show a short notice over the lyrics.

        Args:
            text: Notice text.
            ms: How long to show it, in milliseconds.
        """
        self._notice = text
        self._notice_id += 1
        nid = self._notice_id
        QTimer.singleShot(ms, lambda: self._clear_notice(nid))
        self.update()

    def _clear_notice(self, nid: int):
        """Hide a notice, unless a newer one has replaced it.

        Args:
            nid: The id the notice was shown with.
        """
        if nid == self._notice_id:
            self._notice = ""
            self.update()

    def _draw_message(self, text: str, alpha: int):
        """Draw small centered text in place of the lyrics.

        Args:
            text: Message to draw.
            alpha: Text opacity, 0-255.
        """
        p = QPainter(self)
        p.setRenderHint(QPainter.TextAntialiasing)
        p.setFont(QFont("Segoe UI", 8))
        p.setPen(QColor(255, 255, 255, alpha))
        opt = QTextOption(Qt.AlignCenter)
        opt.setWrapMode(QTextOption.WordWrap)
        p.drawText(QRectF(self.rect()).adjusted(4, 0, -4, 0), text, opt)

    def _prepare(self, idx: int):
        """Lay out a line's words and estimate when each is sung.

        Args:
            idx: Index of the line in self.lines.
        """
        start, text = self.lines[idx]
        nxt = self.lines[idx + 1][0] if idx + 1 < len(self.lines) else start + 6.0
        parts = text.split()
        self._words = []
        self._idx = idx
        if not parts:
            return
        n = len(parts)
        sung_end = min(nxt - 0.08, start + max(1.6, n * 0.7))
        sung_end = max(sung_end, start + 0.6)
        dur = sung_end - start
        weights = [len(w) + 2 for w in parts]
        total = float(sum(weights))
        space = self._fm.horizontalAdvance(" ")
        x = 0.0
        acc = 0.0
        for w, wt in zip(parts, weights):
            ww = self._fm.horizontalAdvance(w)
            t0 = start + dur * acc / total
            acc += wt
            t1 = start + dur * acc / total
            self._words.append((w, x, ww, t0, t1))
            x += ww + space
        self._total_w = x - space
        self._sung_end = sung_end

    def paintEvent(self, _: QPaintEvent):
        """Draw the current line, a notice, or a status message."""
        if self._notice:
            self._draw_message(self._notice, 230)
            return
        if not self.lines:
            if self.status:
                self._draw_message(self.status, 120)
            return
        t = self.position
        idx = bisect.bisect_right(self.starts, t) - 1
        if idx < 0:
            return
        if idx != self._idx:
            self._prepare(idx)
        if not self._words:
            return

        start = self.lines[idx][0]
        a_in = min(1.0, (t - start) / 0.3)
        hold_end = self._sung_end + 1.6
        a_out = 1.0 if t < hold_end else max(0.0, 1.0 - (t - hold_end) / 0.6)
        alpha = a_in * a_out
        if alpha <= 0.01:
            return

        W, H = self.width(), self.height()
        pad = 14.0
        last = len(self._words) - 1
        space = self._fm.horizontalAdvance(" ")

        # where the "playhead" sits along the line, to keep it in view
        head = 0.0
        for i, (w, x, ww, t0, t1) in enumerate(self._words):
            seg = ww + (space if i < last else 0.0)
            frac = max(0.0, min(1.0, (t - t0) / (t1 - t0))) if t1 > t0 else 1.0
            head = x + seg * frac
            if t <= t1:
                break

        if self._total_w <= W - 2 * pad:
            ox = (W - self._total_w) / 2
        else:
            ox = W * 0.4 - head
            ox = min(pad, max(W - pad - self._total_w, ox))

        base = H / 2 + (self._fm.ascent() - self._fm.descent()) / 2

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        p.setFont(self._font)
        p.setOpacity(alpha)

        for w, x, ww, t0, t1 in self._words:
            wx = ox + x
            if wx + ww < -20 or wx > W + 20:
                continue
            prog = max(0.0, min(1.0, (t - t0) / (t1 - t0))) if t1 > t0 else 1.0
            path = QPainterPath()
            path.addText(wx, base, self._font, w)

            p.fillPath(path, self.DIM)
            if prog <= 0.0:
                continue
            done = prog >= 1.0

            # glow: full while the word is being sung, fading shortly after
            g = 1.0 if not done else max(0.0, 1.0 - (t - t1) / 0.5)
            if g > 0.0:
                for width, a in self.GLOW_LAYERS:
                    c = QColor(self.accent)
                    c.setAlpha(min(255, int(a * g)))
                    pen = QPen(c, width)
                    pen.setJoinStyle(Qt.RoundJoin)
                    pen.setCapStyle(Qt.RoundCap)
                    p.strokePath(path, pen)

            # fill: accent while singing, easing to the "sung" color afterwards
            m = 0.0 if not done else (t - t1) / 0.5
            fill = _mix(self.accent, self.SUNG, m)
            if not done:
                p.save()
                p.setClipRect(QRectF(wx - 1, 0, ww * prog + 1, H))
                p.fillPath(path, fill)
                p.restore()
            else:
                p.fillPath(path, fill)

        # soft fade at both edges so scrolling text slides in and out
        p.setOpacity(1.0)
        edge = 22.0
        c0 = QColor(self.bg)
        c0.setAlpha(self.bg_alpha)
        c1 = QColor(self.bg)
        c1.setAlpha(0)
        gl = QLinearGradient(0, 0, edge, 0)
        gl.setColorAt(0, c0)
        gl.setColorAt(1, c1)
        p.fillRect(QRectF(0, 0, edge, H), gl)
        gr = QLinearGradient(W - edge, 0, W, 0)
        gr.setColorAt(0, c1)
        gr.setColorAt(1, c0)
        p.fillRect(QRectF(W - edge, 0, edge, H), gr)


# ── Mini player ──────────────────────
class MiniPlayer(QWidget):
    """The frameless player window: full mode, nano mode, and lyrics."""

    BASE_H = 116
    LYRICS_H = 34
    GLOW_PAD = 1  # transparent margin around the panel for the border glow
    BORDER_GLOW = ((2.0, 90),)  # (pen width, alpha)
    volume_read = Signal(object)  # Spotify's volume (0..1) or None

    def __init__(self):
        """Load settings, build the window, and start the backend."""
        super().__init__()
        self.cfg = {**DEFAULTS, **self._load_cfg()}

        self.setWindowTitle(APP_NAME)
        flags = Qt.FramelessWindowHint | Qt.Tool
        if self.cfg["always_on_top"]:
            flags |= Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WA_TranslucentBackground)

        self.bg = QColor(DEFAULT_BG)
        self._art_hs = None  # (hue, saturation) of the current album art
        self._art_img = None
        self._vol_cur = None  # last known Spotify volume, 0-100
        self._drag_offset = None
        self._state = {"active": False}
        self._track_key = None
        self._raw_title = "Nothing playing"
        self._raw_artist = ""
        self._pos0 = 0.0
        self._mono0 = time.monotonic()
        self._playing = False
        self._last_sec = (-1, -1)
        self._vol_value = None
        # Coalesce slider drags so the mixer isn't flooded with writes.
        self._vol_timer = QTimer(self)
        self._vol_timer.setSingleShot(True)
        self._vol_timer.timeout.connect(self.on_volume_timer)

        self.backend = MediaBackend()
        self.backend.updated.connect(self.on_update)
        self.backend.lyrics_ready.connect(self.on_lyrics)
        self.volume_read.connect(self.on_volume_read)

        self._build_ui()
        self._apply_all()
        self._restore_position()

        self.tick = QTimer(self)
        self.tick.timeout.connect(self.on_tick)
        self.tick.start(33)

    # ── Layout ───────────────────────
    def _build_ui(self):
        """Create every child widget for both full and nano mode."""
        self.art = QLabel()
        self.art.setFixedSize(ART_FULL, ART_FULL)

        self.title = QLabel("Nothing playing")
        self.title.setStyleSheet("color: white;")
        self.title.setFont(QFont("Segoe UI", 11, QFont.Bold))
        self.artist = QLabel("")
        self.artist.setStyleSheet("color: rgba(255,255,255,170);")
        self.artist.setFont(QFont("Segoe UI", 9))

        self.bar = ProgressBar()
        self.bar.seek.connect(self.on_seek)
        self.t_left = QLabel("0:00")
        self.t_right = QLabel("0:00")
        for l in (self.t_left, self.t_right):
            l.setStyleSheet("color: rgba(255,255,255,140);")
            l.setFont(QFont("Segoe UI", 8))
        time_row = QHBoxLayout()
        time_row.setSpacing(6)
        time_row.addWidget(self.t_left)
        time_row.addWidget(self.bar, 1)
        time_row.addWidget(self.t_right)
        self.lyrics_btn = LyricsButton()
        self.lyrics_btn.toggled.connect(self.on_lyrics_toggled)
        time_row.addWidget(self.lyrics_btn)

        # full-mode controls row; keeps its space when hidden so the
        # layout doesn't jump on hover
        self.play_btns = []
        self.controls = QWidget()
        sp = self.controls.sizePolicy()
        sp.setRetainSizeWhenHidden(True)
        self.controls.setSizePolicy(sp)
        crow = QHBoxLayout(self.controls)
        crow.setContentsMargins(0, 0, 0, 0)
        crow.setSpacing(4)
        crow.addStretch(1)
        for b in self._control_buttons(34, 26, 11):
            crow.addWidget(b)
        crow.addSpacing(8)
        speaker = QLabel("🔊")
        speaker.setFont(QFont("Segoe UI Symbol", 10))
        speaker.setStyleSheet("color: rgba(255,255,255,170);")
        self.vol_slider = QSlider(Qt.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setFixedWidth(68)
        self.vol_slider.setEnabled(False)
        self.vol_slider.valueChanged.connect(self.on_volume_changed)
        crow.addWidget(speaker)
        crow.addWidget(self.vol_slider)
        crow.addStretch(1)

        right = QVBoxLayout()
        right.setSpacing(1)
        right.addWidget(self.title)
        right.addWidget(self.artist)
        right.addStretch(1)
        right.addLayout(time_row)
        right.addWidget(self.controls)
        self.right_w = QWidget()
        right.setContentsMargins(0, 0, 0, 0)
        self.right_w.setLayout(right)

        # nano-mode controls: a small bar overlaid on the album art
        self.cbar = QWidget(self.art)
        self.cbar.setObjectName("cbar")
        self.cbar.setAttribute(Qt.WA_StyledBackground, True)
        self.cbar.setStyleSheet(
            "QWidget#cbar { background: rgba(0,0,0,165); border-radius: 10px; }"
        )
        crow2 = QHBoxLayout(self.cbar)
        crow2.setContentsMargins(4, 3, 4, 3)
        crow2.setSpacing(2)
        crow2.addStretch(1)
        for b in self._control_buttons(26, 26, 10):
            crow2.addWidget(b)
        crow2.addStretch(1)

        # nano-mode volume: a vertical slider over the right side of the art
        self.vbox = QWidget(self.art)
        self.vbox.setObjectName("vbox")
        self.vbox.setAttribute(Qt.WA_StyledBackground, True)
        self.vbox.setStyleSheet(
            "QWidget#vbox { background: rgba(0,0,0,165); border-radius: 10px; }"
        )
        vlay = QVBoxLayout(self.vbox)
        vlay.setContentsMargins(3, 8, 3, 8)
        self.vol_slider_v = QSlider(Qt.Vertical)
        self.vol_slider_v.setRange(0, 100)
        self.vol_slider_v.setEnabled(False)
        self.vol_slider_v.valueChanged.connect(self.on_volume_changed)
        vlay.addWidget(self.vol_slider_v, 0, Qt.AlignHCenter)

        top = QHBoxLayout()
        top.setSpacing(12)
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.art, 0, Qt.AlignTop)
        top.addWidget(self.right_w, 1)

        self.strip = LyricsStrip()

        self.root = QVBoxLayout(self)
        self.root.setSpacing(2)
        self.root.addLayout(top, 1)
        self.root.addWidget(self.strip)

        self.close_btn = QPushButton("✕", self)
        self.close_btn.setFixedSize(18, 18)
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.setFont(QFont("Segoe UI Symbol", 8))
        self.close_btn.setStyleSheet(
            "QPushButton { color: rgba(255,255,255,170); background: rgba(0,0,0,70);"
            " border: none; border-radius: 9px; }"
            "QPushButton:hover { color: white; background: rgba(220,60,60,200); }"
        )
        self.close_btn.clicked.connect(self.on_quit)
        self.close_btn.hide()
        self.close_btn.raise_()

    def _control_buttons(
        self, w: int, h: int, pt: int
    ) -> tuple[QPushButton, QPushButton, QPushButton]:
        """Make the back / play / next buttons.

        Args:
            w: Button width.
            h: Button height.
            pt: Symbol font size.

        Returns:
            (prev, play, next). Play is also remembered so its symbol can
            follow the playback state.
        """
        prev = self._btn("⏮", w, h, pt, lambda: self.backend.command("prev"))
        play = self._btn("▶", w, h, pt, lambda: self.backend.command("toggle"))
        nxt = self._btn("⏭", w, h, pt, lambda: self.backend.command("next"))
        self.play_btns.append(play)
        return prev, play, nxt

    def _btn(
        self, text: str, w: int, h: int, pt: int, slot: Callable[[], None]
    ) -> QPushButton:
        """Make one round, transparent symbol button.

        Args:
            text: Symbol to show.
            w: Button width.
            h: Button height.
            pt: Symbol font size.
            slot: Called on click.
        """
        b = QPushButton(text)
        b.setFixedSize(w, h)
        b.setCursor(Qt.PointingHandCursor)
        b.setFont(QFont("Segoe UI Symbol", pt))
        b.setStyleSheet(
            "QPushButton { color: white; background: transparent; border: none;"
            f" border-radius: {h // 2}px; }}"
            "QPushButton:hover { background: rgba(255,255,255,40); }"
        )
        b.clicked.connect(slot)
        return b

    def resizeEvent(self, _: QResizeEvent):
        """Keep the close button pinned to the top-right corner."""
        if hasattr(self, "close_btn"):
            self.close_btn.move(self.width() - 24 - self.GLOW_PAD, 6 + self.GLOW_PAD)

    # ── Applying settings ────────────
    def _apply_all(self):
        """Apply the current settings to size, layout, and colors."""
        c = self.cfg
        nano = bool(c["nano"])
        self.show_lyrics = bool(c["show_lyrics"])
        self.lyrics_btn.blockSignals(True)
        self.lyrics_btn.setChecked(self.show_lyrics)
        self.lyrics_btn.blockSignals(False)

        art = ART_NANO if nano else ART_FULL
        self.art.setFixedSize(art, art)
        self.cbar.setGeometry(0, art - 34, art, 34)
        self.vbox.setGeometry(art - 26, 18, 22, max(20, art - 34 - 18 - 4))
        self.right_w.setVisible(not nano)
        self.strip.setVisible(self.show_lyrics and not nano)
        g = self.GLOW_PAD
        if nano:
            self.root.setContentsMargins(12 + g, 12 + g, 12 + g, 12 + g)
            self.setFixedSize(NANO_SIZE + 2 * g, NANO_SIZE + 2 * g)
        else:
            self.root.setContentsMargins(12 + g, 12 + g, 14 + g, 8 + g)
            h = self.BASE_H + (self.LYRICS_H if self.show_lyrics else 0)
            self.setFixedSize(FULL_W + 2 * g, h + 2 * g)
        self._render_art()
        self._refresh_texts()
        self._refresh_colors()
        self._update_hover(self.underMouse())

    def _update_hover(self, hovering: bool):
        """Show or hide the hover-only controls.

        Args:
            hovering: Whether the mouse is over the player.
        """
        show = hovering or self.cfg["controls_always"]
        nano = self.cfg["nano"]
        self.controls.setVisible(show and not nano)
        self.cbar.setVisible(show and nano)
        self.vbox.setVisible(show and nano)
        self.close_btn.setVisible(hovering)
        self.close_btn.raise_()

    def _bg_alpha(self) -> int:
        """Background alpha (0-255) from the opacity setting."""
        return int(255 * max(0, min(100, self.cfg["opacity"])) / 100)

    def _refresh_colors(self):
        """Recolor everything from the album art (or the defaults)."""
        hs = self._art_hs
        if hs:
            h, s = hs
            self.bg = QColor.fromHsv(h, min(s, 190), 58)
            accent = QColor.fromHsv(h, min(s, 150), 255)
        else:
            self.bg = QColor(DEFAULT_BG)
            accent = QColor(255, 255, 255)
        self.bar.color = accent
        self.bar.update()
        self.vol_slider.setStyleSheet(
            "QSlider::groove:horizontal { height: 4px; border-radius: 2px;"
            " background: rgba(255,255,255,50); }"
            f"QSlider::sub-page:horizontal {{ background: {accent.name()};"
            " border-radius: 2px; }"
            "QSlider::handle:horizontal { background: white; width: 10px;"
            " height: 10px; margin: -3px 0; border-radius: 5px; }"
            "QSlider::handle:horizontal:disabled"
            " { background: rgba(255,255,255,90); }"
            "QSlider::sub-page:horizontal:disabled"
            " { background: rgba(255,255,255,50); }"
            "QSlider::groove:vertical { width: 4px; border-radius: 2px;"
            " background: rgba(255,255,255,50); }"
            f"QSlider::sub-page:vertical {{ background: {accent.name()};"
            " border-radius: 2px; }"
            "QSlider::handle:vertical { background: white; width: 10px;"
            " height: 10px; margin: 0 -3px; border-radius: 5px; }"
            "QSlider::handle:vertical:disabled"
            " { background: rgba(255,255,255,90); }"
            "QSlider::sub-page:vertical:disabled"
            " { background: rgba(255,255,255,50); }"
        )
        self.vol_slider_v.setStyleSheet(self.vol_slider.styleSheet())
        self.strip.accent = accent
        self.lyrics_btn.accent = accent
        self.lyrics_btn.update()
        self.strip.bg = QColor(self.bg)
        self.strip.bg_alpha = self._bg_alpha()
        self.strip.update()
        self.update()

    def _refresh_texts(self):
        """Update the title and artist labels, elided to fit."""
        self.title.setText(self._elide(self.title, self._raw_title))
        self.artist.setText(self._elide(self.artist, self._raw_artist))

    def _elide(self, label: QLabel, text: str) -> str:
        """Shorten text with '…' so it fits beside the album art.

        Args:
            label: Label whose font is used for measuring.
            text: Full text.
        """
        # leave room for the album art, margins, and the close button
        avail = max(60, FULL_W - 152)
        return label.fontMetrics().elidedText(text, Qt.ElideRight, avail)

    # ── Painting ─────────────────────
    def paintEvent(self, _: QPaintEvent):
        """Draw the rounded panel, its gradient border, and the glow."""
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        g = self.GLOW_PAD
        panel = QRectF(self.rect()).adjusted(g, g, -g, -g)
        path.addRoundedRect(panel, 16, 16)
        c = QColor(self.bg)
        c.setAlpha(self._bg_alpha())
        p.fillPath(path, c)

        # the border fades along with the opacity setting
        alpha = self._bg_alpha()

        def border_grad(a: int) -> QBrush:
            """Purple-to-orange diagonal gradient.

            Args:
                a: Alpha for both ends, 0-255.
            """
            c_a = QColor(BORDER_PURPLE)
            c_a.setAlpha(a)
            c_b = QColor(BORDER_ORANGE)
            c_b.setAlpha(a)
            grad = QLinearGradient(panel.topLeft(), panel.bottomRight())
            grad.setColorAt(0.0, c_a)
            grad.setColorAt(1.0, c_b)
            return QBrush(grad)

        # clip the glow to outside the panel so it doesn't tint the content
        outside = QPainterPath()
        outside.addRect(QRectF(self.rect()))
        outside = outside.subtracted(path)
        p.save()
        p.setClipPath(outside)
        for width, a in self.BORDER_GLOW:
            p.setPen(QPen(border_grad(int(a * alpha / 255)), width))
            p.drawPath(path)
        p.restore()

        p.setPen(QPen(border_grad(alpha), 0.8))
        p.drawPath(path)

    # ── Mouse ────────────────────────
    def enterEvent(self, _: QEnterEvent):
        """Show the controls and refresh the volume slider."""
        self._update_hover(True)
        self._sync_volume()

    def leaveEvent(self, _: QEvent):
        """Hide the hover-only controls."""
        self._update_hover(False)

    def mousePressEvent(self, e: QMouseEvent):
        """Start dragging the window, unless its position is locked."""
        if e.button() == Qt.LeftButton and not self.cfg["locked"]:
            self._drag_offset = (
                e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )

    def mouseMoveEvent(self, e: QMouseEvent):
        """Move the window with the mouse while dragging."""
        if self._drag_offset is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, _: QMouseEvent):
        """Finish a drag and remember the new position."""
        if self._drag_offset is not None:
            self._drag_offset = None
            self._save_cfg()

    def mouseDoubleClickEvent(self, e: QMouseEvent):
        """Switch between full and nano mode."""
        if e.button() == Qt.LeftButton:
            self._set("nano", not self.cfg["nano"])
            self._save_cfg()

    def _notify(self, text: str, ms: int = 3500):
        """Show a short notice in the lyrics strip, or as a tooltip.

        Args:
            text: Notice text.
            ms: How long to show it, in milliseconds.
        """
        if self.strip.isVisible():
            self.strip.flash(text, ms)
        else:
            QToolTip.showText(
                self.mapToGlobal(
                    QPoint(8 + self.GLOW_PAD, self.height() + 4 - self.GLOW_PAD)
                ),
                text,
                self,
                QRect(),
                ms,
            )

    # ── Volume ───────────────────────
    # ∴ Spotify's mixer slider is the truth; ours follows it.
    def on_volume_changed(self, value: int):
        """Keep both sliders in step and queue the change for Spotify.

        Args:
            value: Slider position (0–100).
        """
        self._vol_cur = value
        for sl in (self.vol_slider, self.vol_slider_v):
            if sl.value() != value:
                sl.blockSignals(True)
                sl.setValue(value)
                sl.blockSignals(False)
            sl.setToolTip(f"Volume {value}%")
        self._vol_value = value / 100.0
        self._vol_timer.start(60)

    def on_volume_timer(self):
        """Send the latest slider value to Spotify."""
        if self._vol_value is not None:
            VOLUME_POOL.submit(spotify_volume, "set", self._vol_value)

    def _sync_volume(self):
        """Read Spotify's current volume (off the UI thread) into the slider."""
        if not HAVE_PYCAW:
            self.volume_read.emit(None)
            return

        def done(f: Future):
            """Hand the result back to the UI thread via a signal.

            Args:
                f: The finished volume read.
            """
            try:
                v = f.result()
            except Exception:
                v = None
            self.volume_read.emit(v)

        VOLUME_POOL.submit(spotify_volume, "get").add_done_callback(done)

    def on_volume_read(self, v: float | None):
        """Update the sliders with Spotify's volume, or disable them.

        Args:
            v: Spotify's volume 0..1, or None if it couldn't be read.
        """
        sliders = (self.vol_slider, self.vol_slider_v)
        if v is None:
            self._vol_cur = None
            tip = (
                "Start playing in Spotify to use the volume slider"
                if HAVE_PYCAW
                else "Run  pip install pycaw  to enable volume"
            )
            for sl in sliders:
                sl.setEnabled(False)
                sl.setToolTip(tip)
            return
        self._vol_cur = int(round(v * 100))
        for sl in sliders:
            sl.setEnabled(True)
            sl.blockSignals(True)
            sl.setValue(self._vol_cur)
            sl.blockSignals(False)
            sl.setToolTip(f"Volume {self._vol_cur}%")

    def wheelEvent(self, e: QWheelEvent):
        """Scroll over the player to change volume (works in nano mode too)."""
        step = 5 if e.angleDelta().y() > 0 else -5
        if self._vol_cur is None and HAVE_PYCAW:
            try:
                got = VOLUME_POOL.submit(spotify_volume, "get").result(timeout=1.5)
            except Exception:
                got = None
            if got is not None:
                self.on_volume_read(got)
        if self._vol_cur is None:
            self._notify(
                (
                    "Volume: start playing in Spotify first"
                    if HAVE_PYCAW
                    else "Volume: run  pip install pycaw"
                ),
                2500,
            )
            return
        v = max(0, min(100, self._vol_cur + step))
        self.vol_slider.setValue(v)  # fires on_volume_changed
        self._notify(f"Volume {v}%", 1200)
        e.accept()

    # ── Right-click menu ─────────────
    def _set(self, key: str, value: object):
        """Change one setting and re-apply.

        Args:
            key: Setting name.
            value: New value.
        """
        self.cfg[key] = value
        self._apply_all()

    def _toggle_item(self, menu: QMenu, label: str, key: str):
        """Add a checkable menu item bound to a setting.

        Args:
            menu: Menu to add to.
            label: Item text.
            key: Setting it toggles.
        """
        act = menu.addAction(label)
        act.setCheckable(True)
        act.setChecked(bool(self.cfg[key]))
        act.triggered.connect(lambda checked=False: self._set(key, bool(checked)))

    def _slider_item(
        self,
        menu: QMenu,
        label: str,
        lo: int,
        hi: int,
        value: int,
        on_change: Callable[[int], None],
    ):
        """Add a labeled percentage slider to a menu.

        Args:
            menu: Menu to add to.
            label: Text left of the slider.
            lo: Minimum value.
            hi: Maximum value.
            value: Starting value.
            on_change: Called with each new value.
        """
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(14, 4, 14, 4)
        name = QLabel(label)
        name.setFixedWidth(52)
        slider = QSlider(Qt.Horizontal)
        slider.setRange(lo, hi)
        slider.setValue(value)
        slider.setFixedWidth(130)
        shown = QLabel(f"{value}%")
        shown.setFixedWidth(38)

        def on_slider_changed(v: int):
            """Update the readout and pass the value on.

            Args:
                v: New slider value.
            """
            shown.setText(f"{v}%")
            on_change(v)

        slider.valueChanged.connect(on_slider_changed)
        lay.addWidget(name)
        lay.addWidget(slider)
        lay.addWidget(shown)
        act = QWidgetAction(menu)
        act.setDefaultWidget(w)
        menu.addAction(act)

    def on_lyrics_toggled(self, on: bool):
        """Show or hide the lyrics strip from the microphone button.

        Args:
            on: Whether lyrics are now on.
        """
        self._set("show_lyrics", bool(on))
        self._save_cfg()

    def on_always_on_top(self, on: bool):
        """Pin the window above others, or not.

        Args:
            on: Whether to stay on top.
        """
        self.cfg["always_on_top"] = bool(on)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, bool(on))
        self.show()  # changing window flags hides the window

    def contextMenuEvent(self, e: QContextMenuEvent):
        """Open the settings menu, then save whatever changed."""
        m = QMenu(self)

        self._slider_item(
            m,
            "Opacity",
            20,
            100,
            int(self.cfg["opacity"]),
            lambda v: self._set("opacity", v),
        )
        m.addSeparator()
        self._toggle_item(m, "Nano mode", "nano")
        bm = m.addMenu("Behavior")
        top = bm.addAction("Always on top")
        top.setCheckable(True)
        top.setChecked(bool(self.cfg["always_on_top"]))
        top.triggered.connect(lambda checked=False: self.on_always_on_top(checked))
        self._toggle_item(bm, "Lock position", "locked")
        self._toggle_item(bm, "Always show controls", "controls_always")

        m.addSeparator()
        m.addAction("Quit").triggered.connect(self.on_quit)

        m.exec(e.globalPos())
        self._save_cfg()

    def on_quit(self):
        """Save settings and exit."""
        self._save_cfg()
        QApplication.quit()

    # ── Settings file ────────────────
    # ∴ Remember what the user chose.
    @staticmethod
    def _load_cfg() -> dict:
        """Read saved settings, upgrading old ones. Empty if none."""
        try:
            data = json.loads(CONFIG_PATH.read_text())
            if not isinstance(data, dict):
                return {}
            if "nano" not in data and "compact" in data:  # renamed setting
                data["nano"] = data.pop("compact")
            data.pop("lyrics_offset", None)  # removed setting
            return data
        except Exception:
            return {}

    def _save_cfg(self):
        """Write settings and window position to disk."""
        # saved position is the panel's corner, not the glow margin's
        self.cfg.update({"x": self.x() + self.GLOW_PAD, "y": self.y() + self.GLOW_PAD})
        try:
            CONFIG_PATH.write_text(json.dumps(self.cfg, indent=2))
        except Exception:
            pass

    def _restore_position(self):
        """Move to the saved position, or the bottom-right of the screen."""
        try:
            self.move(
                int(self.cfg["x"]) - self.GLOW_PAD, int(self.cfg["y"]) - self.GLOW_PAD
            )
        except Exception:
            screen = QApplication.primaryScreen().availableGeometry()
            self.move(
                screen.right() - self.width() - 24 + self.GLOW_PAD,
                screen.bottom() - self.height() - 24 + self.GLOW_PAD,
            )

    # ── Playback state ───────────────
    # ∴ Trust the clock between polls; correct gently.
    def on_update(self, s: dict):
        """Take in a new playback state from the backend.

        Args:
            s: State dict from MediaBackend; 'active' is False when
                Spotify isn't running.
        """
        self._state = s

        if not s.get("active"):
            self._track_key = None
            self._playing = False
            self._raw_title, self._raw_artist = "Nothing playing", ""
            self._refresh_texts()
            self._art_img = None
            self._art_hs = None
            self._render_art()
            self._refresh_colors()
            self.bar.set_fraction(0)
            for b in self.play_btns:
                b.setText("▶")
            self.strip.set_lines([], "")
            return

        # Only nudge toward the reported position unless the jump is big
        # (seek, new track, pause/resume), so progress stays smooth.
        now = time.monotonic()
        predicted = self._pos0 + ((now - self._mono0) if self._playing else 0.0)
        err = s["position"] - predicted
        if s["art_changed"] or s["playing"] != self._playing or abs(err) > 0.35:
            self._pos0 = s["position"]
        else:
            self._pos0 = predicted + err * 0.25
        self._mono0 = now
        self._playing = s["playing"]

        self._raw_title, self._raw_artist = s["title"], s["artist"]
        self._refresh_texts()
        for b in self.play_btns:
            b.setText("⏸" if s["playing"] else "▶")

        if s["art_changed"]:
            self._track_key = s["key"]
            self.strip.set_lines([], "Loading lyrics…")
            self._set_art(s["art"])

    def on_lyrics(
        self,
        key: tuple,
        lines: list[tuple[float, str]],
        err: tuple[str, str] | None,
    ):
        """Show fetched lyrics, or the right fallback message.

        Args:
            key: Track the lyrics belong to.
            lines: Timed lines (empty if none were found).
            err: (kind, message) if the fetch failed, else None.
        """
        if key != self._track_key:  # a late answer for a previous song
            return
        if lines:
            self.strip.set_lines(lines, "")
        elif err:
            text = LYRICS_ERROR_TEXT.get(err[0], LYRICS_ERROR_TEXT["other"])
            self.strip.set_lines([], text)
        else:
            self.strip.set_lines([], "No synced lyrics found for this song")

    def _set_art(self, data: bytes | None):
        """Load new album art and take its average hue for the colors.

        Args:
            data: Encoded image bytes, or None.
        """
        img = QImage.fromData(data) if data else QImage()
        if img.isNull():
            self._art_img = None
            self._art_hs = None
        else:
            self._art_img = img
            avg = img.scaled(
                1, 1, Qt.IgnoreAspectRatio, Qt.SmoothTransformation
            ).pixelColor(0, 0)
            h, s, v, _ = avg.getHsv()
            self._art_hs = (max(h, 0), s)
        self._render_art()
        self._refresh_colors()

    def _render_art(self):
        """Draw the album art with rounded corners, or a placeholder."""
        size = self.art.width()
        img = self._art_img
        if img is None or img.isNull():
            self.art.clear()
            self.art.setStyleSheet(
                "background: rgba(255,255,255,20); border-radius: 10px;"
            )
            return
        self.art.setStyleSheet("")
        pm = QPixmap.fromImage(img).scaled(
            size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
        )
        rounded = QPixmap(size, size)
        rounded.fill(Qt.transparent)
        painter = QPainter(rounded)
        painter.setRenderHint(QPainter.Antialiasing)
        clip = QPainterPath()
        radius = 12 if size > 100 else 10
        clip.addRoundedRect(QRectF(0, 0, size, size), radius, radius)
        painter.setClipPath(clip)
        painter.drawPixmap(0, 0, pm)
        painter.end()
        self.art.setPixmap(rounded)

    def _current_pos(self) -> float:
        """Estimate the playback position now, between 1-second polls."""
        pos = self._pos0
        if self._playing:
            pos += time.monotonic() - self._mono0
        dur = self._state.get("duration", 0)
        return min(pos, dur) if dur > 0 else pos

    def on_tick(self):
        """Advance the progress bar, times, and lyrics (about 30 fps)."""
        s = self._state
        if not s.get("active") or self.cfg["nano"]:
            return
        pos = self._current_pos()
        dur = s["duration"]
        self.bar.set_fraction(pos / dur if dur > 0 else 0)

        secs = (int(pos), int(dur))
        if secs != self._last_sec:  # labels only change once a second
            self._last_sec = secs
            self.t_left.setText(self._fmt(pos))
            self.t_right.setText(self._fmt(dur))

        if self.show_lyrics:
            self.strip.set_position(pos)

    @staticmethod
    def _fmt(sec: float) -> str:
        """Format seconds as m:ss.

        Args:
            sec: Seconds (negative counts as 0).
        """
        sec = int(max(0, sec))
        return f"{sec // 60}:{sec % 60:02d}"

    def on_seek(self, fraction: float):
        """Seek Spotify to a clicked spot on the progress bar.

        Args:
            fraction: Position along the bar, 0..1.
        """
        dur = self._state.get("duration", 0)
        if dur > 0:
            self.backend.command("seek", fraction * dur)


# ── Entry point ──────────────────────
def main():
    """Start the app."""
    _migrate_legacy_files()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    w = MiniPlayer()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
