# Gnomist Praxis 1 · backend.py · MIT
"""Spotify's playback state (Windows media session) and mixer volume."""

import asyncio
import importlib.util
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from PySide6.QtCore import QObject, Signal
from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)
from winrt.windows.storage.streams import DataReader

from lyrics import fetch_synced_lyrics, parse_lrc

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
