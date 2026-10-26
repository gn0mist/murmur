"""
Spotify miniplayer for Windows 11 (companion app).

Reads whatever Spotify is playing from the Windows media session (SMTC),
so no Spotify login or API keys are needed.

Install:
  python -m pip install PySide6 pycaw winrt-runtime winrt-Windows.Media.Control winrt-Windows.Storage.Streams winrt-Windows.Foundation winrt-Windows.Foundation.Collections
Run:
  python spotify_miniplayer.py        (or rename to .pyw to hide the console)

Hover the player for the controls (back / play / next and a volume slider)
and a small X (top right) to close it. Click the microphone icon to show or
hide the lyrics strip. Scroll the mouse wheel over the player to change
volume. Double-click the player to switch between full and nano mode.
Right-click for settings: opacity, nano mode, behavior.

Only Spotify is shown; other players are ignored.

Volume controls Spotify's slider in the Windows volume mixer (0-100%).

Settings: ~/.spotify_miniplayer.json
Lyrics cache: ~/.spotify_miniplayer_lyrics
"""
import asyncio
import bisect
import hashlib
import importlib.util
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import Qt, QObject, Signal, QTimer, QRectF, QPoint, QRect
from PySide6.QtGui import (
    QColor, QPainter, QPainterPath, QPixmap, QImage, QFont, QFontMetricsF,
    QLinearGradient, QPen, QBrush, QTextOption,
)
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QHBoxLayout, QVBoxLayout, QMenu,
    QSlider, QWidgetAction, QToolTip, QAbstractButton,
)

from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)
from winrt.windows.storage.streams import DataReader

__version__ = "0.1.0-alpha"

CONFIG_PATH = Path.home() / ".spotify_miniplayer.json"
LYRICS_CACHE = Path.home() / ".spotify_miniplayer_lyrics"
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
    "lyrics_offset": 0.0,
    "opacity": 93,             # background opacity, percent
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


# --------------------------------------------------------------------------
# Lyrics: fetch from LRCLIB (cached on disk) and parse LRC timestamps
# --------------------------------------------------------------------------
_TS = re.compile(r"\[(\d{1,3}):(\d{1,2}(?:\.\d{1,3})?)\]")
_WORD_TAG = re.compile(r"<\d+:\d+(?:\.\d+)?>")


def parse_lrc(text):
    """'[01:23.45] some words' -> sorted [(seconds, 'some words'), ...]
    Blank lines are kept (they mark gaps / the end of the previous line)."""
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


def _clean_title(t):
    t = re.sub(r"\s*[\(\[].*?[\)\]]", "", t)  # (feat. X), [Live]
    t = re.sub(r"\s+-\s+.*$", "", t)          # " - Remastered 2011"
    return t.strip() or t


def _http_json(url, timeout=8):
    req = urllib.request.Request(
        url, headers={"User-Agent": f"spotify-miniplayer/{__version__}"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _error_kind(e):
    if isinstance(e, urllib.error.HTTPError):
        return "server" if (e.code >= 500 or e.code == 429) else "other"
    if isinstance(e, (urllib.error.URLError, TimeoutError, OSError)):
        return "network"
    return "other"


def fetch_synced_lyrics(title, artist, album, duration, force=False):
    """Blocking (worker thread). Returns (raw LRC text or None, error or None)
    where error is (kind, message) and kind is server / network / other."""
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
            return json.loads(cache_file.read_text(encoding="utf-8")).get("synced"), None
        except Exception:
            pass

    synced = None
    try:
        if duration > 0:
            q = urllib.parse.urlencode({
                "track_name": title, "artist_name": artist,
                "album_name": album, "duration": int(round(duration)),
            })
            try:
                synced = _http_json(f"{LRCLIB}/get?{q}").get("syncedLyrics")
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
        if not synced:
            first_artist = re.split(r"[,;&]", artist)[0].strip()
            q = urllib.parse.urlencode({
                "track_name": _clean_title(title), "artist_name": first_artist,
            })
            for r in _http_json(f"{LRCLIB}/search?{q}"):
                if not r.get("syncedLyrics"):
                    continue
                if duration > 0 and r.get("duration") and abs(r["duration"] - duration) > 8:
                    continue
                synced = r["syncedLyrics"]
                break
    except Exception as e:
        print("lyrics fetch error:", repr(e))
        return None, (_error_kind(e), f"{type(e).__name__}: {e}")  # not cached

    try:
        cache_file.write_text(json.dumps({"synced": synced}), encoding="utf-8")
    except Exception:
        pass
    return synced, None


# --------------------------------------------------------------------------
# Spotify volume (Windows per-app volume mixer, via pycaw). Runs on a worker
# thread so COM doesn't fight with Qt. Windows tops out at 100% per app.
# --------------------------------------------------------------------------
VOLUME_POOL = ThreadPoolExecutor(max_workers=1)
HAVE_PYCAW = importlib.util.find_spec("pycaw") is not None


def spotify_volume(op, value=None):
    """op 'get' or 'set'. Returns Spotify's volume 0..1, or None if it can't
    be found (Spotify closed or hasn't started playing audio yet)."""
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


# --------------------------------------------------------------------------
# Backend: polls the Windows media session on a background asyncio thread
# --------------------------------------------------------------------------
class MediaBackend(QObject):
    updated = Signal(dict)
    lyrics_ready = Signal(object, object, object)  # (track key, lines, error)

    def __init__(self):
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self._manager = None
        self._last_key = None
        self._last_track = None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.create_task(self._poll())
        self.loop.run_forever()

    async def _get_session(self):
        if self._manager is None:
            self._manager = await SessionManager.request_async()
        for s in self._manager.get_sessions():
            if "spotify" in s.source_app_user_model_id.lower():
                return s
        return None  # Spotify only: ignore every other player

    async def _read_thumbnail(self, ref):
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

    # ---- lyrics ----
    async def _load_lyrics(self, key, title, artist, album, duration, force=False):
        """Quick retries first; if the server is still unhappy, show the
        message and keep trying quietly about once a minute."""
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
                    None, fetch_synced_lyrics, title, artist, album, duration,
                    force or attempt > 0,
                )
            except Exception as e:
                raw, err = None, ("other", repr(e))
            if not err:
                lines = parse_lrc(raw) if raw else []
                print(f"lyrics: {title!r} / {artist!r} dur={duration:.0f}s "
                      f"-> {len(lines)} lines")
                self.lyrics_ready.emit(key, lines, None)
                return
            print(f"lyrics attempt {attempt + 1} failed: {err[1]}")
            if attempt == len(quick) - 1:
                self.lyrics_ready.emit(key, [], err)  # show the message now

    def refetch(self):
        """Look the current song's lyrics up again, ignoring the cache."""
        if self._last_track:
            asyncio.run_coroutine_threadsafe(
                self._load_lyrics(*self._last_track, force=True), self.loop
            )

    # ---- media session polling ----
    async def _poll(self):
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
                            lu = tl.last_updated_time
                            if lu.tzinfo is None:
                                lu = lu.replace(tzinfo=timezone.utc)
                            position += max(
                                0.0, (datetime.now(timezone.utc) - lu).total_seconds()
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
                        self._last_track = (
                            key, props.title or "", props.artist or "",
                            props.album_title or "", duration,
                        )
                        asyncio.ensure_future(self._load_lyrics(*self._last_track))

                    self.updated.emit({
                        "active": True,
                        "key": key,
                        "title": props.title or "Unknown title",
                        "artist": props.artist or "",
                        "playing": playing,
                        "position": position,
                        "duration": duration,
                        "art_changed": art_changed,
                        "art": art,
                    })
            except Exception as e:  # keep polling no matter what
                print("poll error:", e)
            await asyncio.sleep(1.0)

    # ---- playback commands (called from the UI thread) ----
    def command(self, name, *args):
        asyncio.run_coroutine_threadsafe(self._command(name, *args), self.loop)

    async def _command(self, name, *args):
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


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
def _mix(c1, c2, m):
    m = max(0.0, min(1.0, m))
    return QColor(
        int(c1.red() + (c2.red() - c1.red()) * m),
        int(c1.green() + (c2.green() - c1.green()) * m),
        int(c1.blue() + (c2.blue() - c1.blue()) * m),
        int(c1.alpha() + (c2.alpha() - c1.alpha()) * m),
    )


class ProgressBar(QWidget):
    seek = Signal(float)  # fraction 0..1

    def __init__(self):
        super().__init__()
        self.fraction = 0.0
        self.color = QColor(255, 255, 255)
        self.setFixedHeight(10)
        self.setCursor(Qt.PointingHandCursor)

    def set_fraction(self, f):
        self.fraction = max(0.0, min(1.0, f))
        self.update()

    def paintEvent(self, _):
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

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.width() > 0:
            self.seek.emit(e.position().x() / self.width())
            e.accept()


class LyricsButton(QAbstractButton):
    """Microphone icon that toggles the lyrics strip. Glows in the accent
    color when on, dim when off."""

    GLOW_LAYERS = ((6.0, 30), (3.5, 60))  # (pen width, alpha)
    OFF = QColor(255, 255, 255, 80)

    def __init__(self):
        super().__init__()
        self.setCheckable(True)
        self.setFixedSize(18, 18)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Lyrics")
        self.accent = QColor(255, 255, 255)
        self._hover = False

    def enterEvent(self, e):
        self._hover = True
        self.update()

    def leaveEvent(self, e):
        self._hover = False
        self.update()

    def _icon_path(self):
        W, H = self.width(), self.height()
        cx = W / 2
        path = QPainterPath()
        # capsule head
        path.addRoundedRect(QRectF(cx - 3, H * 0.12, 6, H * 0.48), 3, 3)
        # cradle around the head
        cradle = QPainterPath()
        r = QRectF(cx - 5.5, H * 0.22, 11, H * 0.52)
        cradle.arcMoveTo(r, 180)
        cradle.arcTo(r, 180, 180)
        # stand and base
        cradle.moveTo(cx, H * 0.74)
        cradle.lineTo(cx, H * 0.88)
        cradle.moveTo(cx - 3.5, H * 0.88)
        cradle.lineTo(cx + 3.5, H * 0.88)
        return path, cradle

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        head, cradle = self._icon_path()
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
    """One lyric line at a time. Each word fills with the accent color and
    glows as it's sung. Long lines scroll horizontally to follow the active
    word. Word timing is estimated: the line's duration is split across its
    words by length. Also shows status messages and short notices."""

    SUNG = QColor(245, 245, 250, 235)
    DIM = QColor(255, 255, 255, 85)
    GLOW_LAYERS = ((9.0, 14), (6.0, 24), (3.5, 40))  # (pen width, alpha)

    def __init__(self):
        super().__init__()
        self.setFixedHeight(30)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)  # parent handles drag
        self.lines = []
        self.starts = []
        self.offset = 0.0
        self.pos = 0.0
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

    def set_lines(self, lines, status=""):
        self.status = status
        self.lines = lines
        self.starts = [t for t, _ in lines]
        self._idx = -1
        self._words = []
        self.update()

    def set_position(self, pos):
        self.pos = pos
        if self.lines:
            self.update()

    def flash(self, text, ms=3500):
        self._notice = text
        self._notice_id += 1
        nid = self._notice_id
        QTimer.singleShot(ms, lambda: self._clear_notice(nid))
        self.update()

    def _clear_notice(self, nid):
        if nid == self._notice_id:
            self._notice = ""
            self.update()

    def _draw_message(self, text, alpha):
        p = QPainter(self)
        p.setRenderHint(QPainter.TextAntialiasing)
        p.setFont(QFont("Segoe UI", 8))
        p.setPen(QColor(255, 255, 255, alpha))
        opt = QTextOption(Qt.AlignCenter)
        opt.setWrapMode(QTextOption.WordWrap)
        p.drawText(QRectF(self.rect()).adjusted(4, 0, -4, 0), text, opt)

    def _prepare(self, idx):
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

    def paintEvent(self, _):
        if self._notice:
            self._draw_message(self._notice, 230)
            return
        if not self.lines:
            if self.status:
                self._draw_message(self.status, 120)
            return
        t = self.pos + self.offset
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

        # where is the "playhead" along the line?
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

        for (w, x, ww, t0, t1) in self._words:
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


class MiniPlayer(QWidget):
    BASE_H = 116
    LYRICS_H = 34
    GLOW_PAD = 1  # transparent margin around the panel for the border glow
    BORDER_GLOW = ((2.0, 90),)  # (pen width, alpha)
    volume_read = Signal(object)  # Spotify's volume (0..1) or None

    def __init__(self):
        super().__init__()
        self.cfg = {**DEFAULTS, **self._load_cfg()}

        flags = Qt.FramelessWindowHint | Qt.Tool
        if self.cfg["always_on_top"]:
            flags |= Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WA_TranslucentBackground)

        self.bg = QColor(DEFAULT_BG)
        self._art_hs = None    # (hue, saturation) of the current album art
        self._art_img = None
        self._vol_cur = None   # last known Spotify volume, 0-100
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
        self._vol_timer = QTimer(self)
        self._vol_timer.setSingleShot(True)
        self._vol_timer.timeout.connect(self._push_volume)

        self.backend = MediaBackend()
        self.backend.updated.connect(self.on_update)
        self.backend.lyrics_ready.connect(self.on_lyrics)
        self.volume_read.connect(self._on_volume_read)

        self._build_ui()
        self._apply_all()
        self._restore_position()

        self.tick = QTimer(self)
        self.tick.timeout.connect(self._tick)
        self.tick.start(33)

    # ---- layout ----
    def _build_ui(self):
        self.art = QLabel()
        self.art.setFixedSize(ART_FULL, ART_FULL)

        self.title = QLabel("Nothing playing")
        self.title.setStyleSheet("color: white;")
        self.title.setFont(QFont("Segoe UI", 11, QFont.Bold))
        self.artist = QLabel("")
        self.artist.setStyleSheet("color: rgba(255,255,255,170);")
        self.artist.setFont(QFont("Segoe UI", 9))

        self.bar = ProgressBar()
        self.bar.seek.connect(self._on_seek)
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
        self.lyrics_btn.toggled.connect(self._lyrics_toggled)
        time_row.addWidget(self.lyrics_btn)

        # full-mode controls row
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
        self.vol_slider.valueChanged.connect(self._volume_changed)
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
            "QWidget#cbar { background: rgba(0,0,0,165); border-radius: 10px; }")
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
            "QWidget#vbox { background: rgba(0,0,0,165); border-radius: 10px; }")
        vlay = QVBoxLayout(self.vbox)
        vlay.setContentsMargins(3, 8, 3, 8)
        self.vol_slider_v = QSlider(Qt.Vertical)
        self.vol_slider_v.setRange(0, 100)
        self.vol_slider_v.setEnabled(False)
        self.vol_slider_v.valueChanged.connect(self._volume_changed)
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

        # small close button, top right (shown on hover)
        self.close_btn = QPushButton("✕", self)
        self.close_btn.setFixedSize(18, 18)
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.setFont(QFont("Segoe UI Symbol", 8))
        self.close_btn.setStyleSheet(
            "QPushButton { color: rgba(255,255,255,170); background: rgba(0,0,0,70);"
            " border: none; border-radius: 9px; }"
            "QPushButton:hover { color: white; background: rgba(220,60,60,200); }")
        self.close_btn.clicked.connect(self._quit)
        self.close_btn.hide()
        self.close_btn.raise_()

    def _control_buttons(self, w, h, pt):
        """Make the back / play / next buttons (play's text changes)."""
        prev = self._btn("⏮", w, h, pt, lambda: self.backend.command("prev"))
        play = self._btn("▶", w, h, pt, lambda: self.backend.command("toggle"))
        nxt = self._btn("⏭", w, h, pt, lambda: self.backend.command("next"))
        self.play_btns.append(play)
        return prev, play, nxt

    def _btn(self, text, w, h, pt, slot):
        b = QPushButton(text)
        b.setFixedSize(w, h)
        b.setCursor(Qt.PointingHandCursor)
        b.setFont(QFont("Segoe UI Symbol", pt))
        b.setStyleSheet(
            "QPushButton { color: white; background: transparent; border: none;"
            f" border-radius: {h // 2}px; }}"
            "QPushButton:hover { background: rgba(255,255,255,40); }")
        b.clicked.connect(slot)
        return b

    def resizeEvent(self, _):
        if hasattr(self, "close_btn"):
            self.close_btn.move(self.width() - 24 - self.GLOW_PAD, 6 + self.GLOW_PAD)

    # ---- applying settings ----
    def _apply_all(self):
        c = self.cfg
        nano = bool(c["nano"])
        self.strip.offset = float(c["lyrics_offset"])
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

    def _update_hover(self, hovering):
        show = hovering or self.cfg["controls_always"]
        nano = self.cfg["nano"]
        self.controls.setVisible(show and not nano)
        self.cbar.setVisible(show and nano)
        self.vbox.setVisible(show and nano)
        self.close_btn.setVisible(hovering)
        self.close_btn.raise_()

    def _bg_alpha(self):
        return int(255 * max(0, min(100, self.cfg["opacity"])) / 100)

    def _refresh_colors(self):
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
            "QSlider::handle:horizontal:disabled { background: rgba(255,255,255,90); }"
            "QSlider::sub-page:horizontal:disabled { background: rgba(255,255,255,50); }"
            "QSlider::groove:vertical { width: 4px; border-radius: 2px;"
            " background: rgba(255,255,255,50); }"
            f"QSlider::sub-page:vertical {{ background: {accent.name()};"
            " border-radius: 2px; }"
            "QSlider::handle:vertical { background: white; width: 10px;"
            " height: 10px; margin: 0 -3px; border-radius: 5px; }"
            "QSlider::handle:vertical:disabled { background: rgba(255,255,255,90); }"
            "QSlider::sub-page:vertical:disabled { background: rgba(255,255,255,50); }")
        self.vol_slider_v.setStyleSheet(self.vol_slider.styleSheet())
        self.strip.accent = accent
        self.lyrics_btn.accent = accent
        self.lyrics_btn.update()
        self.strip.bg = QColor(self.bg)
        self.strip.bg_alpha = self._bg_alpha()
        self.strip.update()
        self.update()

    def _refresh_texts(self):
        self.title.setText(self._elide(self.title, self._raw_title))
        self.artist.setText(self._elide(self.artist, self._raw_artist))

    def _elide(self, label, text):
        # leave room for the album art, margins, and the close button
        avail = max(60, FULL_W - 152)
        return label.fontMetrics().elidedText(text, Qt.ElideRight, avail)

    # ---- painting ----
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        g = self.GLOW_PAD
        panel = QRectF(self.rect()).adjusted(g, g, -g, -g)
        path.addRoundedRect(panel, 16, 16)
        c = QColor(self.bg)
        c.setAlpha(self._bg_alpha())
        p.fillPath(path, c)

        # purple -> orange gradient outline; thin, and it fades with opacity
        alpha = self._bg_alpha()

        def border_grad(a):
            c_a = QColor(BORDER_PURPLE)
            c_a.setAlpha(a)
            c_b = QColor(BORDER_ORANGE)
            c_b.setAlpha(a)
            grad = QLinearGradient(panel.topLeft(), panel.bottomRight())
            grad.setColorAt(0.0, c_a)
            grad.setColorAt(1.0, c_b)
            return QBrush(grad)

        # soft outer glow: wide, faint strokes kept outside the panel
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

    # ---- hover ----
    def enterEvent(self, _):
        self._update_hover(True)
        self._sync_volume()

    def leaveEvent(self, _):
        self._update_hover(False)

    # ---- dragging / double-click ----
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and not self.cfg["locked"]:
            self._drag_offset = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag_offset is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, e):
        if self._drag_offset is not None:
            self._drag_offset = None
            self._save_cfg()

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._set("nano", not self.cfg["nano"])
            self._save_cfg()

    # ---- notices ----
    def _notify(self, text, ms=3500):
        if self.strip.isVisible():
            self.strip.flash(text, ms)
        else:
            QToolTip.showText(
                self.mapToGlobal(QPoint(8 + self.GLOW_PAD, self.height() + 4 - self.GLOW_PAD)),
                text, self,
                QRect(), ms)

    # ---- volume (Spotify's slider in the Windows mixer) ----
    def _volume_changed(self, v):
        self._vol_cur = v
        for sl in (self.vol_slider, self.vol_slider_v):
            if sl.value() != v:  # keep the two sliders in step
                sl.blockSignals(True)
                sl.setValue(v)
                sl.blockSignals(False)
            sl.setToolTip(f"Volume {v}%")
        self._vol_value = v / 100.0
        self._vol_timer.start(60)

    def _push_volume(self):
        if self._vol_value is not None:
            VOLUME_POOL.submit(spotify_volume, "set", self._vol_value)

    def _sync_volume(self):
        """Read Spotify's current volume (off the UI thread) into the slider."""
        if not HAVE_PYCAW:
            self.volume_read.emit(None)
            return

        def done(f):
            try:
                v = f.result()
            except Exception:
                v = None
            self.volume_read.emit(v)

        VOLUME_POOL.submit(spotify_volume, "get").add_done_callback(done)

    def _on_volume_read(self, v):
        sliders = (self.vol_slider, self.vol_slider_v)
        if v is None:
            self._vol_cur = None
            tip = ("Start playing in Spotify to use the volume slider" if HAVE_PYCAW
                   else "Run  pip install pycaw  to enable volume")
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

    def wheelEvent(self, e):
        """Scroll over the player to change volume (works in nano mode too)."""
        step = 5 if e.angleDelta().y() > 0 else -5
        if self._vol_cur is None and HAVE_PYCAW:
            try:
                got = VOLUME_POOL.submit(spotify_volume, "get").result(timeout=1.5)
            except Exception:
                got = None
            if got is not None:
                self._on_volume_read(got)
        if self._vol_cur is None:
            self._notify("Volume: start playing in Spotify first" if HAVE_PYCAW
                         else "Volume: run  pip install pycaw", 2500)
            return
        v = max(0, min(100, self._vol_cur + step))
        self.vol_slider.setValue(v)  # fires _volume_changed
        self._notify(f"Volume {v}%", 1200)
        e.accept()

    # ---- right-click menu ----
    def _set(self, key, value):
        self.cfg[key] = value
        self._apply_all()

    def _toggle_item(self, menu, label, key):
        act = menu.addAction(label)
        act.setCheckable(True)
        act.setChecked(bool(self.cfg[key]))
        act.triggered.connect(lambda checked=False: self._set(key, bool(checked)))
        return act

    def _slider_item(self, menu, label, lo, hi, value, on_change):
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

        def changed(v):
            shown.setText(f"{v}%")
            on_change(v)

        slider.valueChanged.connect(changed)
        lay.addWidget(name)
        lay.addWidget(slider)
        lay.addWidget(shown)
        act = QWidgetAction(menu)
        act.setDefaultWidget(w)
        menu.addAction(act)

    def _lyrics_toggled(self, on):
        self._set("show_lyrics", bool(on))
        self._save_cfg()

    def _set_on_top(self, on):
        self.cfg["always_on_top"] = bool(on)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, bool(on))
        self.show()

    def contextMenuEvent(self, e):
        m = QMenu(self)

        self._slider_item(m, "Opacity", 20, 100, int(self.cfg["opacity"]),
                          lambda v: self._set("opacity", v))
        # --- Mode and behavior
        m.addSeparator()
        self._toggle_item(m, "Nano mode", "nano")
        bm = m.addMenu("Behavior")
        top = bm.addAction("Always on top")
        top.setCheckable(True)
        top.setChecked(bool(self.cfg["always_on_top"]))
        top.triggered.connect(lambda checked=False: self._set_on_top(checked))
        self._toggle_item(bm, "Lock position", "locked")
        self._toggle_item(bm, "Always show controls", "controls_always")

        m.addSeparator()
        m.addAction("Quit").triggered.connect(self._quit)

        m.exec(e.globalPos())
        self._save_cfg()

    def _quit(self):
        self._save_cfg()
        QApplication.quit()

    # ---- settings file ----
    @staticmethod
    def _load_cfg():
        try:
            data = json.loads(CONFIG_PATH.read_text())
            if not isinstance(data, dict):
                return {}
            if "nano" not in data and "compact" in data:  # renamed setting
                data["nano"] = data.pop("compact")
            return data
        except Exception:
            return {}

    def _save_cfg(self):
        # saved position is the panel's corner, not the glow margin's
        self.cfg.update({"x": self.x() + self.GLOW_PAD, "y": self.y() + self.GLOW_PAD})
        try:
            CONFIG_PATH.write_text(json.dumps(self.cfg, indent=2))
        except Exception:
            pass

    def _restore_position(self):
        try:
            self.move(int(self.cfg["x"]) - self.GLOW_PAD, int(self.cfg["y"]) - self.GLOW_PAD)
        except Exception:
            screen = QApplication.primaryScreen().availableGeometry()
            self.move(screen.right() - self.width() - 24 + self.GLOW_PAD,
                      screen.bottom() - self.height() - 24 + self.GLOW_PAD)

    # ---- state updates ----
    def on_update(self, s):
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

        # Keep playback position smooth: only nudge toward the reported
        # position unless the jump is big (seek, new track, pause/resume).
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

    def on_lyrics(self, key, lines, err):
        if key != self._track_key:  # ignore late answers for a previous song
            return
        if lines:
            self.strip.set_lines(lines, "")
        elif err:
            self.strip.set_lines([], LYRICS_ERROR_TEXT.get(err[0], LYRICS_ERROR_TEXT["other"]))
        else:
            self.strip.set_lines([], "No synced lyrics found for this song")

    def _set_art(self, data):
        img = QImage.fromData(data) if data else QImage()
        if img.isNull():
            self._art_img = None
            self._art_hs = None
        else:
            self._art_img = img
            avg = img.scaled(1, 1, Qt.IgnoreAspectRatio,
                             Qt.SmoothTransformation).pixelColor(0, 0)
            h, s, v, _ = avg.getHsv()
            self._art_hs = (max(h, 0), s)
        self._render_art()
        self._refresh_colors()

    def _render_art(self):
        size = self.art.width()
        img = self._art_img
        if img is None or img.isNull():
            self.art.clear()
            self.art.setStyleSheet(
                "background: rgba(255,255,255,20); border-radius: 10px;")
            return
        self.art.setStyleSheet("")
        pm = QPixmap.fromImage(img).scaled(
            size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        rounded = QPixmap(size, size)
        rounded.fill(Qt.transparent)
        painter = QPainter(rounded)
        painter.setRenderHint(QPainter.Antialiasing)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(0, 0, size, size), 12 if size > 100 else 10,
                            12 if size > 100 else 10)
        painter.setClipPath(clip)
        painter.drawPixmap(0, 0, pm)
        painter.end()
        self.art.setPixmap(rounded)

    # ---- smooth progress + lyrics between 1s polls ----
    def _current_pos(self):
        pos = self._pos0
        if self._playing:
            pos += time.monotonic() - self._mono0
        dur = self._state.get("duration", 0)
        return min(pos, dur) if dur > 0 else pos

    def _tick(self):
        s = self._state
        if not s.get("active") or self.cfg["nano"]:
            return
        pos = self._current_pos()
        dur = s["duration"]
        self.bar.set_fraction(pos / dur if dur > 0 else 0)

        secs = (int(pos), int(dur))
        if secs != self._last_sec:
            self._last_sec = secs
            self.t_left.setText(self._fmt(pos))
            self.t_right.setText(self._fmt(dur))

        if self.show_lyrics:
            self.strip.set_position(pos)

    @staticmethod
    def _fmt(sec):
        sec = int(max(0, sec))
        return f"{sec // 60}:{sec % 60:02d}"

    def _on_seek(self, fraction):
        dur = self._state.get("duration", 0)
        if dur > 0:
            self.backend.command("seek", fraction * dur)


def main():
    app = QApplication(sys.argv)
    w = MiniPlayer()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
