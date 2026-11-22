"""The always-on-top caption window."""

import re
import threading

from PyQt5.QtCore import QPoint, QPointF, QRect, QSettings, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QTextCharFormat,
    QTextLayout,
    QTextOption,
)
from PyQt5.QtWidgets import (
    QAction,
    QActionGroup,
    QApplication,
    QHBoxLayout,
    QMenu,
    QToolButton,
    QWidget,
)

from .audio import MICROPHONE, SYSTEM
from .translation import ArabicTranslator

GRIP = 8
PADDING = 14
RADIUS = 10
MIN_WIDTH = 320
DEFAULT_LINES = 3
DEFAULT_FONT_SIZE = 26
DEFAULT_OPACITY = 85
FONT_FAMILIES = ["Ubuntu Sans", "Ubuntu", "Noto Sans", "DejaVu Sans"]
LINE_BREAK = "\u2028"
SPEAKER_COLORS = [
    QColor(255, 255, 255),
    QColor(255, 213, 79),
    QColor(129, 212, 250),
    QColor(165, 214, 167),
    QColor(244, 143, 177),
    QColor(206, 147, 216),
]

SOURCES = [(SYSTEM, "صوت النظام"), (MICROPHONE, "الميكروفون")]
FONT_SIZES = [(size, f"{size}") for size in (20, 24, 26, 30, 36, 44, 56)]
OPACITIES = [(value, f"{value}%") for value in (100, 85, 70, 50, 30)]
SENTENCE_GAP = re.compile(r"[.?!]\s+")
TRANSLATING = "جارٍ الترجمة…"

STYLE = """
QToolButton {
    color: white; background: #3c4043; border: none;
    border-radius: 6px; min-width: 28px; min-height: 28px; font-size: 15px;
}
QToolButton:hover { background: #5f6368; }
QMenu { color: white; background: #202124; border: 1px solid #3c4043; padding: 4px; }
QMenu::item { padding: 6px 28px; border-radius: 4px; }
QMenu::item:selected { background: #3c4043; }
QMenu::separator { height: 1px; background: #3c4043; margin: 4px 8px; }
"""


class CaptionWindow(QWidget):
    source_changed = pyqtSignal(str)
    paused_changed = pyqtSignal(bool)
    clear_requested = pyqtSignal()
    track_speakers_changed = pyqtSignal(bool)
    _translated = pyqtSignal(int, object)

    def __init__(self, source=None):
        # GNOME on Wayland ignores "keep above" requests from native clients, so
        # this runs under XWayland as an override-redirect window. Those stack
        # above normal and fullscreen windows and never take keyboard focus.
        super().__init__(
            None,
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.X11BypassWindowManagerHint
            | Qt.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.setStyleSheet(STYLE)

        self._settings = QSettings("live-captions", "live-captions")
        self._source = source or self._settings.value("source", SYSTEM, str)
        self._opacity = self._settings.value("opacity", DEFAULT_OPACITY, int)
        self._track_speakers = self._settings.value("track_speakers", True, bool)
        self._font = QFont()
        self._font.setFamilies(FONT_FAMILIES)
        self._font.setWeight(QFont.Medium)
        self._font.setPixelSize(self._settings.value("font_size", DEFAULT_FONT_SIZE, int))

        self._turns = []
        self._status = ""
        self._paused = False
        self._press_position = None
        self._press_geometry = None
        self._press_edges = Qt.Edges()
        self._first_visible = 0

        # Translation shows the Arabic for the sentences on screen, until the
        # captions move on or the window is clicked.
        self._translator = ArabicTranslator()
        self._translating = False
        self._translation = None
        self._translation_id = 0
        self._translated_words = 0
        self._geometry_before_translation = None
        self._translated.connect(self._show_translation)

        self._build_toolbar()
        self.setMinimumSize(MIN_WIDTH, self._height_for_lines(1))
        self._restore_geometry()

        # Mutter keeps this above managed windows by itself, but another
        # override-redirect window would stay above it until it is raised again.
        self._raise_timer = QTimer(self)
        self._raise_timer.timeout.connect(self._raise_above_others)
        self._raise_timer.start(1000)

    @property
    def source(self):
        return self._source

    @property
    def track_speakers(self):
        return self._track_speakers

    def set_caption(self, turns):
        self._turns = turns
        if self._translating and (not turns or _word_count(turns) > self._translated_words):
            self._end_translation()  # The film is playing again.
        self.update()

    def set_status(self, text):
        self._status = text
        self.update()

    def contextMenuEvent(self, event):
        self._show_menu(event.globalPos())

    def enterEvent(self, event):
        self._toolbar.show()

    def leaveEvent(self, event):
        self._toolbar.hide()

    def resizeEvent(self, event):
        self._toolbar.adjustSize()
        self._toolbar.move(self.width() - self._toolbar.width() - GRIP, GRIP)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            if self._translating:
                self._end_translation()
            self._press_position = event.globalPos()
            self._press_geometry = self.geometry()
            self._press_edges = self._edges_at(event.pos())

    def mouseMoveEvent(self, event):
        if self._press_position is None:
            self._update_cursor(self._edges_at(event.pos()))
            return
        delta = event.globalPos() - self._press_position
        if self._press_edges:
            self.setGeometry(self._resized_geometry(delta))
        else:
            self.move(self._press_geometry.topLeft() + delta)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._press_position = None
            self._settings.setValue("geometry", self.geometry())

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, round(self._opacity * 2.55)))
        painter.drawRoundedRect(self.rect(), RADIUS, RADIUS)

        area = self.rect().adjusted(PADDING, PADDING, -PADDING, -PADDING)
        painter.setPen(SPEAKER_COLORS[0])
        if self._translating and self._translation:
            self._draw_lines(painter, area, self._translation, Qt.RightToLeft, newest=False)
        elif self._translating:
            self._draw_note(painter, area, TRANSLATING)
        elif self._turns:
            self._first_visible = self._draw_lines(
                painter, area, self._turns, Qt.LeftToRight, newest=True
            )
        elif self._status:
            self._draw_note(painter, area, self._status)

    def _draw_note(self, painter, area, text):
        painter.setPen(QColor(255, 255, 255, 170))
        painter.setFont(self._font)
        painter.setLayoutDirection(Qt.RightToLeft)
        alignment = Qt.AlignRight | Qt.AlignAbsolute | Qt.AlignTop | Qt.TextWordWrap
        painter.drawText(area, alignment, text)

    def _draw_lines(self, painter, area, turns, direction, newest):
        """Draw the newest or the first lines that fit; return where the first one starts."""
        layout, lines = self._lay_out(turns, area.width(), direction)
        capacity = max(1, area.height() // self._line_height())
        shown = lines[-capacity:] if newest else lines[:capacity]
        for row, line in enumerate(shown):
            top = area.top() + row * self._line_height()
            line.draw(painter, QPointF(area.left(), top - line.y()))
        return shown[0].textStart() if shown else 0

    def _lay_out(self, turns, width, direction):
        """Word-wrap the turns, one colour each. Keep the layout while using its lines."""
        option = QTextOption()
        option.setWrapMode(QTextOption.WordWrap)
        option.setTextDirection(direction)
        if direction == Qt.RightToLeft:
            option.setAlignment(Qt.AlignRight | Qt.AlignAbsolute)
        layout = QTextLayout(LINE_BREAK.join(text for _, text in turns), self._font)
        layout.setTextOption(option)
        layout.setFormats(self._turn_formats(turns))
        layout.beginLayout()
        lines = []
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(width)
            lines.append(line)
        layout.endLayout()
        return layout, lines

    def _line_height(self):
        return QFontMetrics(self._font).lineSpacing()

    def _turn_formats(self, turns):
        formats = []
        start = 0
        for speaker, text in turns:
            character_format = QTextCharFormat()
            character_format.setForeground(SPEAKER_COLORS[speaker % len(SPEAKER_COLORS)])
            turn_format = QTextLayout.FormatRange()
            turn_format.start = start
            turn_format.length = len(text)
            turn_format.format = character_format
            formats.append(turn_format)
            start += len(text) + len(LINE_BREAK)
        return formats

    def _build_toolbar(self):
        self._toolbar = QWidget(self)
        layout = QHBoxLayout(self._toolbar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self._translate_button = self._tool_button(
            "ع", "ترجمة النص الظاهر إلى العربية", self._toggle_translation
        )
        self._pause_button = self._tool_button("❚❚", "إيقاف مؤقت", self._toggle_paused)
        menu_button = self._tool_button("≡", "الإعدادات", self._show_menu_below_toolbar)
        close_button = self._tool_button("✕", "إغلاق", QApplication.quit)
        for button in (self._translate_button, self._pause_button, menu_button, close_button):
            layout.addWidget(button)
        self._toolbar.hide()

    def _tool_button(self, text, tooltip, handler):
        button = QToolButton(self._toolbar)
        button.setText(text)
        button.setToolTip(tooltip)
        button.setCursor(Qt.ArrowCursor)
        button.clicked.connect(handler)
        return button

    def _show_menu_below_toolbar(self):
        self._show_menu(self._toolbar.mapToGlobal(QPoint(0, self._toolbar.height() + 4)))

    def _show_menu(self, position):
        menu = QMenu(self)
        menu.setLayoutDirection(Qt.RightToLeft)
        self._add_choices(menu, "مصدر الصوت", SOURCES, self._source, self._set_source)
        self._add_choices(
            menu, "حجم الخط", FONT_SIZES, self._font.pixelSize(), self._set_font_size
        )
        self._add_choices(menu, "الشفافية", OPACITIES, self._opacity, self._set_opacity)
        track_speakers = menu.addAction("تمييز المتحدثين", self._set_track_speakers)
        track_speakers.setCheckable(True)
        track_speakers.setChecked(self._track_speakers)
        menu.addSeparator()
        menu.addAction("نقل إلى أعلى الشاشة", lambda: self._dock(top=True))
        menu.addAction("نقل إلى أسفل الشاشة", lambda: self._dock(top=False))
        menu.addSeparator()
        menu.addAction(
            "إخفاء الترجمة" if self._translating else "ترجمة النص الظاهر إلى العربية",
            self._toggle_translation,
        )
        menu.addAction("مسح النص", self.clear_requested.emit)
        menu.addAction("استئناف" if self._paused else "إيقاف مؤقت", self._toggle_paused)
        menu.addAction("إغلاق", QApplication.quit)
        menu.exec_(position)

    def _add_choices(self, menu, title, choices, current, handler):
        submenu = menu.addMenu(title)
        group = QActionGroup(submenu)
        for value, label in choices:
            action = QAction(label, group, checkable=True, checked=value == current)
            action.triggered.connect(lambda _checked, chosen=value: handler(chosen))
            submenu.addAction(action)

    def _set_source(self, source):
        if source != self._source:
            self._source = source
            self._settings.setValue("source", source)
            self.source_changed.emit(source)

    def _toggle_translation(self):
        if self._translating:
            self._end_translation()
            return
        turns = self._turns_on_screen()
        if not turns:
            return
        self._translating = True
        self._translation = None
        self._translation_id += 1
        self._translated_words = _word_count(self._turns)
        self._translate_button.setText("EN")
        self._translate_button.setToolTip("العودة إلى النص الإنكليزي")
        threading.Thread(
            target=self._translate, args=(self._translation_id, turns), daemon=True
        ).start()
        self.update()

    def _translate(self, translation_id, turns):
        """Runs on its own thread; the result comes back through a queued signal."""
        try:
            arabic = self._translator.translate([text for _, text in turns])
            result = [(speaker, text) for (speaker, _), text in zip(turns, arabic)]
        except Exception as error:  # Show any failure instead of losing it in a thread.
            result = [(0, f"تعذرت الترجمة: {error}")]
        self._translated.emit(translation_id, result)

    def _show_translation(self, translation_id, turns):
        if not self._translating or translation_id != self._translation_id:
            return
        self._translation = turns
        width = self.width() - 2 * PADDING
        _, lines = self._lay_out(turns, width, Qt.RightToLeft)
        self._grow_to(len(lines) * self._line_height() + 2 * PADDING)
        self.update()

    def _end_translation(self):
        self._translating = False
        self._translation = None
        self._translation_id += 1
        self._translate_button.setText("ع")
        self._translate_button.setToolTip("ترجمة النص الظاهر إلى العربية")
        if self._geometry_before_translation is not None:
            self.setGeometry(self._geometry_before_translation)
            self._geometry_before_translation = None
        self.update()

    def _grow_to(self, height):
        """Grow to show a long translation, away from the nearer screen edge."""
        available = self.screen().availableGeometry()
        height = min(height, available.height())
        if height <= self.height():
            return
        self._geometry_before_translation = self.geometry()
        geometry = QRect(self.geometry())
        if geometry.center().y() > available.center().y():
            geometry.setTop(geometry.bottom() - height + 1)
        else:
            geometry.setHeight(height)
        geometry.moveTop(max(available.top(), min(geometry.top(), available.bottom() - height + 1)))
        self.setGeometry(geometry)

    def _turns_on_screen(self):
        """Return the turns that show, each from the start of its first visible sentence."""
        on_screen = []
        start = 0
        for speaker, text in self._turns:
            end = start + len(text)
            if end > self._first_visible:
                cut = max(0, self._first_visible - start)
                ends = [gap.end() for gap in SENTENCE_GAP.finditer(text) if gap.end() <= cut]
                on_screen.append((speaker, text[ends[-1] if ends else 0 :]))
            start = end + len(LINE_BREAK)
        return on_screen

    def _set_font_size(self, size):
        if self._translating:
            self._end_translation()
        self._font.setPixelSize(size)
        self._settings.setValue("font_size", size)
        self.setMinimumHeight(self._height_for_lines(1))
        self.update()

    def _set_opacity(self, value):
        self._opacity = value
        self._settings.setValue("opacity", value)
        self.update()

    def _set_track_speakers(self, enabled):
        self._track_speakers = enabled
        self._settings.setValue("track_speakers", enabled)
        self.track_speakers_changed.emit(enabled)

    def _toggle_paused(self):
        self._paused = not self._paused
        self._pause_button.setText("▶" if self._paused else "❚❚")
        self._pause_button.setToolTip("استئناف" if self._paused else "إيقاف مؤقت")
        self.paused_changed.emit(self._paused)

    def _dock(self, top):
        if self._translating:
            self._end_translation()
        available = self.screen().availableGeometry()
        x = available.left() + (available.width() - self.width()) // 2
        y = available.top() + GRIP if top else available.bottom() - self.height() - 24
        self.move(x, y)
        self._settings.setValue("geometry", self.geometry())

    def _raise_above_others(self):
        # Raising while a menu is open would hide it behind this window.
        if self.isVisible() and QApplication.activePopupWidget() is None:
            self.raise_()

    def _height_for_lines(self, lines):
        return lines * self._line_height() + 2 * PADDING

    def _restore_geometry(self):
        available = QApplication.primaryScreen().availableGeometry()
        saved = self._settings.value("geometry", None, QRect)
        on_screen = saved is not None and any(
            screen.availableGeometry().intersects(saved) for screen in QApplication.screens()
        )
        if on_screen:
            self.setGeometry(saved)
            return
        width = min(1100, available.width() * 4 // 5)
        self.resize(width, self._height_for_lines(DEFAULT_LINES))
        self.move(
            available.left() + (available.width() - width) // 2,
            available.bottom() - self.height() - 24,
        )

    def _edges_at(self, position):
        edges = Qt.Edges()
        if position.x() < GRIP:
            edges |= Qt.LeftEdge
        if position.x() >= self.width() - GRIP:
            edges |= Qt.RightEdge
        if position.y() < GRIP:
            edges |= Qt.TopEdge
        if position.y() >= self.height() - GRIP:
            edges |= Qt.BottomEdge
        return edges

    def _resized_geometry(self, delta):
        geometry = QRect(self._press_geometry)
        width, height = self.minimumWidth(), self.minimumHeight()
        if self._press_edges & Qt.LeftEdge:
            geometry.setLeft(min(geometry.left() + delta.x(), geometry.right() - width + 1))
        if self._press_edges & Qt.RightEdge:
            geometry.setRight(max(geometry.right() + delta.x(), geometry.left() + width - 1))
        if self._press_edges & Qt.TopEdge:
            geometry.setTop(min(geometry.top() + delta.y(), geometry.bottom() - height + 1))
        if self._press_edges & Qt.BottomEdge:
            geometry.setBottom(max(geometry.bottom() + delta.y(), geometry.top() + height - 1))
        return geometry

    def _update_cursor(self, edges):
        horizontal = bool(edges & (Qt.LeftEdge | Qt.RightEdge))
        vertical = bool(edges & (Qt.TopEdge | Qt.BottomEdge))
        if horizontal and vertical:
            falling = edges == (Qt.TopEdge | Qt.LeftEdge) or edges == (
                Qt.BottomEdge | Qt.RightEdge
            )
            self.setCursor(Qt.SizeFDiagCursor if falling else Qt.SizeBDiagCursor)
        elif horizontal:
            self.setCursor(Qt.SizeHorCursor)
        elif vertical:
            self.setCursor(Qt.SizeVerCursor)
        else:
            self.setCursor(Qt.ArrowCursor)


def _word_count(turns):
    return sum(len(text.split()) for _, text in turns)
