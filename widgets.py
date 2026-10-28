# Gnomist Praxis 1 · widgets.py · MIT
"""Custom-drawn widgets: progress bar, icons, lyrics button, and lyrics strip."""

import bisect

from PySide6.QtCore import Qt, QEvent, Signal, QTimer, QPointF, QRectF
from PySide6.QtGui import (
    QColor,
    QEnterEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QFont,
    QFontMetricsF,
    QImage,
    QLinearGradient,
    QPen,
    QTextOption,
    QTransform,
)
from PySide6.QtWidgets import QWidget, QAbstractButton


# ── Type ─────────────────────────────
def ui_font(pt: float, semibold: bool = False) -> QFont:
    """Segoe UI Variable, or Segoe UI where it isn't installed.

    Qt lists each weight of the variable font as its own family, and
    weight 600 on the plain family snaps to Bold, so semibold is chosen
    by family name.

    Args:
        pt: Point size.
        semibold: Use the semibold weight.
    """
    name = "Segoe UI Variable Text Semibold" if semibold else "Segoe UI Variable Text"
    f = QFont()
    f.setFamilies([name, "Segoe UI"])
    f.setPointSizeF(pt)
    if semibold:
        f.setWeight(QFont.DemiBold)  # picks Segoe UI Semibold in the fallback
    return f


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


def _icon_shape(kind: str, c: QPointF, s: float) -> QPainterPath:
    """Filled outline of a playback or speaker icon.

    Args:
        kind: 'play', 'pause', 'prev', 'next', or 'speaker'.
        c: Center of the icon.
        s: Icon height in pixels.
    """
    path = QPainterPath()
    x, cy = c.x(), c.y()

    def tri(x0: float, x1: float, h: float):
        """Add a triangle with its base at x0 and its tip at x1.

        Args:
            x0: Base x.
            x1: Tip x.
            h: Height, centered on cy.
        """
        path.moveTo(x0, cy - h / 2)
        path.lineTo(x1, cy)
        path.lineTo(x0, cy + h / 2)
        path.closeSubpath()

    if kind == "play":
        tri(x - 0.35 * s, x + 0.45 * s, s)  # nudged right to look centered
    elif kind == "pause":
        path.addRect(QRectF(x - 0.3 * s, cy - s / 2, 0.2 * s, s))
        path.addRect(QRectF(x + 0.1 * s, cy - s / 2, 0.2 * s, s))
    elif kind in ("next", "prev"):
        h = 0.78 * s
        tri(x - 0.5 * s, x - 0.07 * s, h)
        tri(x - 0.07 * s, x + 0.36 * s, h)
        path.addRect(QRectF(x + 0.36 * s, cy - h / 2, 0.14 * s, h))
        if kind == "prev":
            path = QTransform().translate(2 * x, 0).scale(-1, 1).map(path)
    elif kind == "speaker":
        path.addRect(QRectF(x - 0.55 * s, cy - 0.2 * s, 0.25 * s, 0.4 * s))
        path.moveTo(x - 0.3 * s, cy - 0.2 * s)
        path.lineTo(x + 0.05 * s, cy - 0.5 * s)
        path.lineTo(x + 0.05 * s, cy + 0.5 * s)
        path.lineTo(x - 0.3 * s, cy + 0.2 * s)
        path.closeSubpath()
    return path


class IconButton(QAbstractButton):
    """Round, transparent button with its icon drawn in code.

    Font symbols differ in size and weight from glyph to glyph; drawing
    them keeps the set even. Play and pause share one box, so the button
    doesn't shift when it toggles.
    """

    def __init__(
        self,
        kind: str,
        w: int,
        h: int,
        size: float,
        color: QColor = QColor(255, 255, 255),
        hover_color: QColor | None = None,
        bg: QColor = QColor(0, 0, 0, 0),
        hover_bg: QColor = QColor(255, 255, 255, 40),
    ):
        """Create the button.

        Args:
            kind: 'prev', 'play', 'next', or 'close'.
            w: Button width.
            h: Button height.
            size: Icon height in pixels.
            color: Icon color.
            hover_color: Icon color on hover (defaults to color).
            bg: Background color.
            hover_bg: Background color on hover.
        """
        super().__init__()
        self.kind = kind
        self.playing = False
        self._icon_size = size
        self.color = color
        self.hover_color = hover_color or color
        self.bg = bg
        self.hover_bg = hover_bg
        self.setFixedSize(w, h)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover)  # repaint on enter and leave

    def set_playing(self, playing: bool):
        """Show pause while playing, play otherwise (play button only).

        Args:
            playing: Whether Spotify is playing.
        """
        if playing != self.playing:
            self.playing = playing
            self.update()

    def paintEvent(self, _: QPaintEvent):
        """Draw the round background and the icon."""
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        hover = self.underMouse()
        bg = self.hover_bg if hover else self.bg
        if bg.alpha():
            r = self.height() / 2
            p.setPen(Qt.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(QRectF(self.rect()), r, r)

        color = self.hover_color if hover else self.color
        c = QRectF(self.rect()).center()
        s = self._icon_size
        if self.kind == "close":
            d = s / 2
            pen = QPen(color, 1.2)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawLine(QPointF(c.x() - d, c.y() - d), QPointF(c.x() + d, c.y() + d))
            p.drawLine(QPointF(c.x() - d, c.y() + d), QPointF(c.x() + d, c.y() - d))
            return
        kind = "pause" if self.kind == "play" and self.playing else self.kind
        p.fillPath(_icon_shape(kind, c, s), color)


class SpeakerIcon(QWidget):
    """Speaker drawn beside the volume slider."""

    def __init__(self, size: float, color: QColor):
        """Create the icon.

        Args:
            size: Icon height in pixels.
            color: Icon color.
        """
        super().__init__()
        self._icon_size = size
        self.color = color
        self.setFixedSize(16, 16)

    def paintEvent(self, _: QPaintEvent):
        """Draw the speaker and two sound waves."""
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        s = self._icon_size
        c = QRectF(self.rect()).center() - QPointF(0.05 * s, 0)  # waves included
        p.fillPath(_icon_shape("speaker", c, s), self.color)
        pen = QPen(self.color, 1.1)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        for r in (0.32 * s, 0.6 * s):
            box = QRectF(c.x() + 0.05 * s - r, c.y() - r, 2 * r, 2 * r)
            p.drawArc(box, -45 * 16, 90 * 16)


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
        self.status = ""
        self._notice = ""
        self._notice_id = 0
        self._font = ui_font(11, semibold=True)
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
        p.setFont(ui_font(8))
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

        # draw on a layer so the edge fade can thin the text itself and
        # leave the see-through panel behind it untouched
        dpr = self.devicePixelRatioF()
        layer = QImage(
            round(W * dpr), round(H * dpr), QImage.Format_ARGB32_Premultiplied
        )
        layer.setDevicePixelRatio(dpr)
        layer.fill(Qt.transparent)
        p = QPainter(layer)
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
        p.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        edge = 22.0 / W
        mask = QLinearGradient(0, 0, W, 0)
        mask.setColorAt(0, QColor(0, 0, 0, 0))
        mask.setColorAt(edge, QColor(0, 0, 0))
        mask.setColorAt(1 - edge, QColor(0, 0, 0))
        mask.setColorAt(1, QColor(0, 0, 0, 0))
        p.fillRect(QRectF(0, 0, W, H), mask)
        p.end()

        p = QPainter(self)
        p.drawImage(0, 0, layer)
