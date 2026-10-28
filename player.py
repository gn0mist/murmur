# Gnomist Praxis 1 · player.py · MIT
"""The player window: full mode, nano mode, and lyrics."""

import json
import time
from concurrent.futures import Future
from typing import Callable

from PySide6.QtCore import Qt, QEvent, Signal, QTimer, QRectF, QPoint, QRect
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
    QLinearGradient,
    QPen,
    QBrush,
    QResizeEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QLabel,
    QHBoxLayout,
    QVBoxLayout,
    QMenu,
    QSlider,
    QWidgetAction,
    QToolTip,
)

from backend import HAVE_PYCAW, VOLUME_POOL, MediaBackend, spotify_volume
from settings import (
    APP_NAME,
    ART_FULL,
    ART_NANO,
    BORDER_ORANGE,
    BORDER_PURPLE,
    CONFIG_PATH,
    DEFAULT_BG,
    DEFAULTS,
    FULL_W,
    LYRICS_ERROR_TEXT,
    NANO_SIZE,
)
from widgets import (
    IconButton,
    LyricsButton,
    LyricsStrip,
    ProgressBar,
    SpeakerIcon,
    ui_font,
)


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

        # Started and stopped by _update_tick, which _apply_all calls.
        self.tick = QTimer(self)
        self.tick.timeout.connect(self.on_tick)

        self._build_ui()
        self._apply_all()
        self._restore_position()

    # ── Layout ───────────────────────
    def _build_ui(self):
        """Create every child widget for both full and nano mode."""
        self.art = QLabel()
        self.art.setFixedSize(ART_FULL, ART_FULL)

        self.title = QLabel("Nothing playing")
        self.title.setStyleSheet("color: white;")
        self.title.setFont(ui_font(11, semibold=True))
        self.artist = QLabel("")
        self.artist.setStyleSheet("color: rgba(255,255,255,170);")
        self.artist.setFont(ui_font(9))

        self.bar = ProgressBar()
        self.bar.seek.connect(self.on_seek)
        self.t_left = QLabel("0:00")
        self.t_right = QLabel("0:00")
        for l in (self.t_left, self.t_right):
            l.setStyleSheet("color: rgba(255,255,255,140);")
            l.setFont(ui_font(8))
        time_row = QHBoxLayout()
        time_row.setSpacing(8)
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
        for b in self._control_buttons(34, 26, 10):
            crow.addWidget(b)
        crow.addSpacing(8)
        speaker = SpeakerIcon(9, QColor(255, 255, 255, 170))
        self.vol_slider = QSlider(Qt.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setFixedWidth(68)
        self.vol_slider.setEnabled(False)
        self.vol_slider.valueChanged.connect(self.on_volume_changed)
        crow.addWidget(speaker)
        crow.addWidget(self.vol_slider)
        crow.addStretch(1)

        right = QVBoxLayout()
        right.setSpacing(4)
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
        crow2 = QHBoxLayout(self.cbar)
        crow2.setContentsMargins(4, 4, 4, 4)
        crow2.setSpacing(4)
        crow2.addStretch(1)
        for b in self._control_buttons(26, 26, 9):
            crow2.addWidget(b)
        crow2.addStretch(1)

        # nano-mode volume: a vertical slider over the right side of the art
        self.vbox = QWidget(self.art)
        self.vbox.setObjectName("vbox")
        self.vbox.setAttribute(Qt.WA_StyledBackground, True)
        vlay = QVBoxLayout(self.vbox)
        vlay.setContentsMargins(4, 8, 4, 8)
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
        self.root.setSpacing(4)
        self.root.addLayout(top, 1)
        self.root.addWidget(self.strip)

        self.close_btn = IconButton(
            "close",
            18,
            18,
            7,
            color=QColor(255, 255, 255, 170),
            hover_color=QColor(255, 255, 255),
            bg=QColor(0, 0, 0, 70),
            hover_bg=QColor(220, 60, 60, 200),
        )
        self.close_btn.setParent(self)
        self.close_btn.clicked.connect(self.on_quit)
        self.close_btn.hide()
        self.close_btn.raise_()

    def _control_buttons(
        self, w: int, h: int, size: float
    ) -> tuple[IconButton, IconButton, IconButton]:
        """Make the back / play / next buttons.

        Args:
            w: Button width.
            h: Button height.
            size: Icon height in pixels.

        Returns:
            (prev, play, next). Play is also remembered so its icon can
            follow the playback state.
        """
        prev = self._btn("prev", w, h, size, lambda: self.backend.command("prev"))
        play = self._btn("play", w, h, size, lambda: self.backend.command("toggle"))
        nxt = self._btn("next", w, h, size, lambda: self.backend.command("next"))
        self.play_btns.append(play)
        return prev, play, nxt

    def _btn(
        self, kind: str, w: int, h: int, size: float, slot: Callable[[], None]
    ) -> IconButton:
        """Make one round, transparent icon button.

        Args:
            kind: Icon to draw ('prev', 'play', or 'next').
            w: Button width.
            h: Button height.
            size: Icon height in pixels.
            slot: Called on click.
        """
        b = IconButton(kind, w, h, size)
        b.clicked.connect(slot)
        return b

    def resizeEvent(self, _: QResizeEvent):
        """Keep the close button pinned to the top-right corner."""
        if hasattr(self, "close_btn"):
            self.close_btn.move(self.width() - 26 - self.GLOW_PAD, 8 + self.GLOW_PAD)

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
        self.root.setContentsMargins(12 + g, 12 + g, 12 + g, 12 + g)
        if nano:
            self.setFixedSize(NANO_SIZE + 2 * g, NANO_SIZE + 2 * g)
        else:
            h = self.BASE_H + (self.LYRICS_H if self.show_lyrics else 0)
            self.setFixedSize(FULL_W + 2 * g, h + 2 * g)
        self._render_art()
        self._refresh_texts()
        self._refresh_colors()
        self._update_hover(self.underMouse())
        self._update_tick()

    def _update_hover(self, hovering: bool):
        """Show or hide the controls for the current mode.

        Full mode always shows its controls; nano keeps the art clear until
        hovered.

        Args:
            hovering: Whether the mouse is over the player.
        """
        nano = self.cfg["nano"]
        self.controls.setVisible(not nano)
        self.cbar.setVisible(hovering and nano)
        self.vbox.setVisible(hovering and nano)
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
            f"QSlider::add-page:vertical {{ background: {accent.name()};"
            " border-radius: 2px; }"
            "QSlider::handle:vertical { background: white; width: 10px;"
            " height: 10px; margin: 0 -3px; border-radius: 5px; }"
            "QSlider::handle:vertical:disabled"
            " { background: rgba(255,255,255,90); }"
            "QSlider::add-page:vertical:disabled"
            " { background: rgba(255,255,255,50); }"
        )
        self.vol_slider_v.setStyleSheet(self.vol_slider.styleSheet())
        # nano overlays take the album tint, dimmed enough to read over the art
        b = self.bg
        tint = f"background: rgba({b.red()},{b.green()},{b.blue()},190);"
        for w in (self.cbar, self.vbox):
            w.setStyleSheet(
                f"QWidget#{w.objectName()} {{ {tint} border-radius: 10px; }}"
            )
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
        lay.setContentsMargins(12, 4, 12, 4)
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
            data.pop("lyrics_offset", None)  # removed settings
            data.pop("controls_always", None)
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
                b.set_playing(False)
            self.strip.set_lines([], "")
            self._update_tick()
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
            b.set_playing(s["playing"])

        if s["art_changed"]:
            self._track_key = s["key"]
            self.strip.set_lines([], "Loading lyrics…")
            self._set_art(s["art"])
        self._update_tick()

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

    def _update_tick(self):
        """Run the 30 fps timer only while full mode has something moving.

        When it's stopped, draw one frame so a seek, skip, or mode switch
        still shows up while paused.
        """
        if self._state.get("active") and self._playing and not self.cfg["nano"]:
            if not self.tick.isActive():
                self.tick.start(33)
        else:
            self.tick.stop()
            self.on_tick()

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
