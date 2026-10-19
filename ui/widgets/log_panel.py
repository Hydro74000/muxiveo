"""Journal commun : origine stable, filtre et suivi respectueux de la lecture."""
from __future__ import annotations

import re
from collections import deque
from datetime import datetime

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QTextBlockFormat, QTextCharFormat, QTextCursor, QTextFormat
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QMenu, QPushButton, QTextEdit, QVBoxLayout, QWidget

from core.i18n import translate_text
from core.logging import LogLevel
from ui.design_system import colors as C, font_px, scale

_SOURCE = re.compile(r"^\[(HYBRID|DOVI|HDR|MEDIA|ENCODE|REMUX|EXTRACT)\]\s*")
_Entry = tuple[str, LogLevel, str, str]
_BADGE_SOURCE = int(QTextFormat.Property.UserProperty) + 1


def _source_color(source: str) -> str:
    """Couleur du badge d'origine."""
    if source == "DOVI":
        return C.PURPLE
    if source in {"ENCODE", "REMUX", "MEDIA", "EXTRACT"}:
        return C.INFO
    return {"HYBRID": C.ACCENT, "HDR": C.WARN}.get(source, C.TEXT_SEC)


def split_log_source(message: str) -> tuple[str, str]:
    """Reconnaît les origines du journal sans dépendre du panneau actif."""
    match = _SOURCE.match(message)
    return (match[1], message[match.end():]) if match else ("APP", message)


class _LogTextEdit(QTextEdit):
    """Dessine les badges arrondis en conservant le texte copiable du journal."""

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setFont(self.font())
        block = self.cursorForPosition(QPoint(0, 0)).block()
        cursor = QTextCursor(self.document())
        while block.isValid():
            cursor.setPosition(block.position())
            if self.cursorRect(cursor).top() > self.viewport().height():
                break
            iterator = block.begin()
            while not iterator.atEnd():
                fragment = iterator.fragment()
                source = fragment.charFormat().property(_BADGE_SOURCE)
                if source:
                    cursor.setPosition(fragment.position())
                    left = self.cursorRect(cursor)
                    cursor.setPosition(fragment.position() + fragment.length())
                    right = self.cursorRect(cursor)
                    rect = QRectF(left.x(), left.y() + 1, right.x() - left.x(), left.height() - 2)
                    color = QColor(_source_color(str(source)))
                    background = QColor(color)
                    background.setAlpha(40)
                    painter.fillRect(rect, QColor(C.LOG_BG))
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(background)
                    painter.drawRoundedRect(rect, scale(3), scale(3))
                    painter.setPen(color)
                    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(source))
                iterator += 1
            block = block.next()
        painter.end()


class LogPanel(QWidget):
    collapse_toggled = Signal(bool)
    startup_expanded_changed = Signal(bool)

    def __init__(self, max_lines: int = 2000, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._max_lines = max_lines
        self._collapsed = False
        self._following = True
        self._updating = False
        self._unread = 0
        self._entries: deque[_Entry] = deque()
        self._pending: list[_Entry] = []
        self._startup_expanded = False
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        header = QWidget()
        self._header = header
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(self._show_context_menu)
        header.setFixedHeight(scale(38))
        header.setStyleSheet(f"background:{C.BG_PANEL};border-top:1px solid {C.BORDER};")
        row = QHBoxLayout(header)
        row.setContentsMargins(scale(16), 0, scale(12), 0)
        title = QLabel(translate_text("JOURNAL D’EXÉCUTION"))
        title.setStyleSheet(f"color:{C.TEXT_SEC};font-size:{font_px(9)}px;font-weight:700;letter-spacing:1.5px;background:transparent;border:none;")
        row.addWidget(title)
        self._filter = QComboBox()
        for text, key in (("Toutes les opérations", ""), ("Remux / Encodage", "MEDIA"), ("Hybrid Studio", "HYBRID"),
                          ("Merge DoVi", "DOVI"), ("HDR Studio", "HDR"), ("Application", "APP")):
            self._filter.addItem(translate_text(text), key)
        self._filter.setAccessibleName(translate_text("Filtrer le journal"))
        self._filter.setStyleSheet(f"QComboBox{{background:transparent;color:{C.TEXT_SEC};border:none;font-size:{font_px(10)}px;padding:0 6px;}}QComboBox QAbstractItemView{{background:{C.BG_PANEL};color:{C.TEXT_PRI};selection-background-color:{C.BG_ACTIVE};}}")
        self._filter.currentIndexChanged.connect(self._refilter)
        row.addWidget(self._filter)
        row.addStretch()
        self._follow_btn = QPushButton()
        self._follow_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._follow_btn.clicked.connect(self._resume_following)
        row.addWidget(self._follow_btn)
        for label, callback in (("Effacer", self.clear), ("⌄", self._toggle)):
            button = QPushButton(translate_text(label))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(f"QPushButton{{color:{C.TEXT_SEC};background:transparent;border:1px solid {C.BORDER};border-radius:4px;padding:3px 7px;font-size:{font_px(9)}px;}}")
            button.clicked.connect(callback)
            row.addWidget(button)
            if label == "⌄":
                self._collapse_btn = button
                button.setAccessibleName(translate_text("Replier le journal"))
        root.addWidget(header)
        self._text = _LogTextEdit()
        self._text.setReadOnly(True)
        self._text.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        font = QFont("JetBrains Mono", font_px(9))
        font.setStyleHint(QFont.StyleHint.Monospace)
        self._text.setFont(font)
        self._text.setStyleSheet(f"QTextEdit{{background:{C.LOG_BG};color:{C.TEXT_PRI};border:none;padding:8px 16px;}}QScrollBar:vertical{{background:{C.LOG_BG};width:8px;}}QScrollBar::handle:vertical{{background:{C.BORDER_LT};border-radius:4px;min-height:20px;}}QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;}}")
        self._text.verticalScrollBar().valueChanged.connect(self._scroll_changed)
        self._text.setMinimumHeight(0)
        root.addWidget(self._text)
        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.setInterval(50)
        self._flush_timer.timeout.connect(self._flush_pending)
        self._update_follow_button()

    @staticmethod
    def _matches_filter(source: str, selected: str) -> bool:
        return not selected or source == selected or (selected == "MEDIA" and source in {"ENCODE", "REMUX", "EXTRACT"})

    def log(self, message: str, level: LogLevel = LogLevel.INFO) -> None:
        source, message = split_log_source(message)
        # Un événement multi-lignes garde son origine sur chacune des lignes.
        for line in translate_text(message).splitlines() or [""]:
            self._pending.append((datetime.now().strftime("%H:%M:%S"), level, source, line))
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def _append(self, entries: list[_Entry]) -> None:
        selected = self._filter.currentData()
        cursor = QTextCursor(self._text.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        block_format = QTextBlockFormat()
        block_format.setLineHeight(scale(22), QTextBlockFormat.LineHeightTypes.FixedHeight.value)
        cursor.setBlockFormat(block_format)
        fmt = QTextCharFormat()
        for timestamp, level, source, message in entries:
            if not self._matches_filter(source, selected):
                continue
            color = getattr(C, "LOG_" + level.value)
            fmt.setFontWeight(QFont.Weight.Normal)
            fmt.setForeground(QColor(C.TEXT_SEC))
            cursor.insertText(timestamp + "  ", fmt)
            fmt.setForeground(QColor(color))
            fmt.setFontWeight(QFont.Weight.Bold)
            cursor.insertText(f"{level.value:<5} ", fmt)
            fmt.setFontWeight(QFont.Weight.Normal)
            fmt.setProperty(_BADGE_SOURCE, source)
            fmt.setForeground(QColor(_source_color(source)))
            cursor.insertText(f" {source:^7} ", fmt)
            fmt.clearProperty(_BADGE_SOURCE)
            fmt.setFontWeight(QFont.Weight.Normal)
            fmt.setForeground(QColor(C.TEXT_PRI if level in {LogLevel.INFO, LogLevel.OK} else color))
            cursor.insertText("  " + message + "\n", fmt)

    def _flush_pending(self) -> None:
        if not self._pending:
            return
        batch, self._pending = self._pending, []
        self._entries.extend(batch)
        limit = max(1, self._max_lines)
        trimmed = len(self._entries) > limit
        while len(self._entries) > limit:
            self._entries.popleft()
        self._updating = True
        scroll = self._text.verticalScrollBar()
        position = scroll.value()
        try:
            self._text.document().setMaximumBlockCount(limit + 1)
            if trimmed and self._filter.currentData():
                # Des lignes d'une autre origine peuvent expirer l'historique
                # affiché sans ajouter de bloc au document filtré.
                self._text.clear()
                self._append(list(self._entries))
            else:
                self._append(batch[-limit:])
            if self._following:
                scroll.setValue(scroll.maximum())
            else:
                scroll.setValue(position)
                selected = self._filter.currentData()
                self._unread += sum(1 for entry in batch if self._matches_filter(entry[2], selected))
        finally:
            self._updating = False
        self._update_follow_button()

    def _refilter(self) -> None:
        self._flush_pending()
        self._updating = True
        try:
            self._text.clear()
            self._append(list(self._entries))
        finally:
            self._updating = False
        self._resume_following()

    def _scroll_changed(self, value: int) -> None:
        if not self._updating:
            self._following = value >= self._text.verticalScrollBar().maximum() - 2
            if self._following:
                self._unread = 0
            self._update_follow_button()

    def _resume_following(self) -> None:
        self._following, self._unread = True, 0
        self._text.verticalScrollBar().setValue(self._text.verticalScrollBar().maximum())
        self._update_follow_button()

    def _update_follow_button(self) -> None:
        label = translate_text("● Suivi actif") if self._following else translate_text("↓ Reprendre le défilement")
        if self._unread:
            label += f" · {self._unread}"
        self._follow_btn.setText(label)
        self._follow_btn.setStyleSheet(f"QPushButton{{background:transparent;border:none;color:{C.OK if self._following else C.WARN};font-size:{font_px(10)}px;}}")

    def info(self, message: str) -> None: self.log(message, LogLevel.INFO)
    def ok(self, message: str) -> None: self.log(message, LogLevel.OK)
    def warn(self, message: str) -> None: self.log(message, LogLevel.WARN)
    def error(self, message: str) -> None: self.log(message, LogLevel.ERROR)

    def clear(self) -> None:
        self._pending.clear()
        self._entries.clear()
        self._flush_timer.stop()
        self._text.clear()
        self._resume_following()

    def is_collapsed(self) -> bool:
        return self._collapsed

    def set_startup_expanded(self, expanded: bool) -> None:
        self._startup_expanded = bool(expanded)

    def _context_menu(self) -> QMenu:
        menu = QMenu(self)
        startup = menu.addAction(translate_text("Ouvrir le journal au démarrage"))
        startup.setCheckable(True)
        startup.setChecked(self._startup_expanded)
        startup.triggered.connect(lambda checked: self.startup_expanded_changed.emit(checked))
        return menu

    def _show_context_menu(self, position: QPoint) -> None:
        menu = self._context_menu()
        menu.exec(self._header.mapToGlobal(position))
        menu.deleteLater()

    def set_collapsed(self, collapsed: bool) -> None:
        self._collapsed = bool(collapsed)
        self._text.setVisible(not self._collapsed)
        self._collapse_btn.setText("⌃" if self._collapsed else "⌄")

    def _toggle(self) -> None:
        self.set_collapsed(not self._collapsed)
        self.collapse_toggled.emit(self._collapsed)
