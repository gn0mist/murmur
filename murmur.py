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

Back / play / next and a volume slider sit under the track info (in nano
mode they show on hover). Hover for a small X (top right) to close it.
Click the microphone icon to show or hide the lyrics strip. Scroll the
mouse wheel over the player to change volume. Double-click the player to
switch between full and nano mode. Right-click for settings: opacity, nano
mode, behavior.

Settings: ~/.murmur.json
Lyrics cache: ~/.murmur_lyrics
"""

import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from player import MiniPlayer
from settings import APP_NAME, ICON_PATH, _migrate_legacy_files
from widgets import ui_font


# ── Entry point ──────────────────────
def main():
    """Start the app."""
    _migrate_legacy_files()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setFont(ui_font(9))  # menus and tooltips
    app.setWindowIcon(QIcon(str(ICON_PATH)))
    w = MiniPlayer()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
