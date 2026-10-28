# Gnomist Praxis 1 · settings.py · MIT
"""App identity, file paths, colors, sizes, and default settings."""

import shutil
import sys
from pathlib import Path

from PySide6.QtGui import QColor

__version__ = "0.2.0-beta"
APP_NAME = "Murmur Miniplayer"


# ── Settings ─────────────────────────
CONFIG_PATH = Path.home() / ".murmur.json"
LYRICS_CACHE = Path.home() / ".murmur_lyrics"
LEGACY_CONFIG = Path.home() / ".spotify_miniplayer.json"
LEGACY_LYRICS = Path.home() / ".spotify_miniplayer_lyrics"
# The .exe unpacks bundled files to sys._MEIPASS; from source, use this folder.
APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
ICON_PATH = APP_DIR / "assets" / "murmur.ico"
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
