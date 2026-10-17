"""Tableau dont la hauteur suit son contenu, dans une page déjà défilante."""
from PySide6.QtCore import QEvent, QSize, QTimer, Qt
from PySide6.QtWidgets import QSizePolicy, QTableWidget


class ContentTableWidget(QTableWidget):
    """Adapte la hauteur aux lignes, avec un plafond de lignes visible optionnel."""

    def __init__(self, *args, max_visible_rows: int | None = None, **kwargs):
        self._max_visible_rows = max_visible_rows
        super().__init__(*args, **kwargs)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded if max_visible_rows else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._height_timer = QTimer(self)
        self._height_timer.setSingleShot(True)
        self._height_timer.timeout.connect(self._fit_height)
        model = self.model()
        for signal in (model.rowsInserted, model.rowsRemoved, model.modelReset,
                       model.layoutChanged, self.verticalHeader().sectionResized,
                       self.horizontalScrollBar().rangeChanged):
            signal.connect(self._schedule_height)
        self._schedule_height()

    def _schedule_height(self, *_args):
        self._height_timer.start(0)

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        rows = self.verticalHeader().length() or self.verticalHeader().defaultSectionSize()
        if self._max_visible_rows and self.rowCount():
            rows = sum(self.rowHeight(row) for row in range(min(self.rowCount(), self._max_visible_rows)))
        header = 0 if self.horizontalHeader().isHidden() else self.horizontalHeader().height()
        horizontal = self.horizontalScrollBar()
        scrollbar = horizontal.sizeHint().height() if horizontal.maximum() > horizontal.minimum() else 0
        return QSize(hint.width(), rows + header + scrollbar + 2 * self.frameWidth())

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def _fit_height(self):
        self.setFixedHeight(self.sizeHint().height())
        self.updateGeometry()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange) and hasattr(self, "_height_timer"):
            self._schedule_height()
