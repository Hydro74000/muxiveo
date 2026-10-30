"""
ui/styles.py — Helpers UI partagés (boutons, cartes, séparateurs, styles d'input).

Module neutre, importable depuis n'importe quel panneau ou dialog sans risque
de cycle d'imports. Utilise ``ui.design_system`` pour les couleurs et le scale.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QFrame, QLabel, QPushButton, QWidget

from ui.design_system import colors as _C, font_px as _font_px, scale as _scale


def workflow_icon(name: str, color: str | None = None, size: int = 20) -> QIcon:
    """Icônes vectorielles cohérentes, indépendantes des polices emoji."""
    paths = {
        "brand": '<path d="M4 18 10 6M10 21 16 9M16 16 22 4" stroke-width="2.8"/>',
        "media": '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M3 9h18M8 4v5m8-5v5"/>',
        "hybrid": '<path d="M3 5h6l6 14h6M3 19h6L15 5h6m-3-3 3 3-3 3m0 8 3 3-3 3"/>',
        "merge_dovi": '<path d="m12 3 9 5-9 5-9-5Zm-9 9 9 5 9-5m-18 5 9 5 9-5"/>',
        "play": f'<path d="m7 4 14 8-14 8Z" fill="{color or _C.TEXT_SEC}"/>',
        "down": '<path d="m6 9 6 6 6-6"/>',
        "home": '<path d="m3 10 9-7 9 7v10H3Zm6 10v-7h6v7"/>',
        "heart": '<path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8L12 21l8.8-8.6a5.5 5.5 0 0 0 0-7.8Z"/>',
        "encode": '<path d="m13 2-9 12h7l-1 8 10-13h-7Z"/>',
        "settings": '<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3"/><circle cx="15" cy="17" r="3"/>',
        "extensions": '<path d="M8 3v5m8-5v5M6 8h12v3a6 6 0 0 1-12 0ZM12 17v5"/>',
        "hdr": f'<circle cx="12" cy="12" r="8"/><path d="M12 4a8 8 0 0 1 0 16Z" fill="{color or _C.TEXT_SEC}"/>',
    }
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color or _C.TEXT_SEC}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">{paths.get(name, paths["media"])}</svg>'
    pix = QPixmap(_scale(size), _scale(size))
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    QSvgRenderer(svg.encode()).render(painter)
    painter.end()
    return QIcon(pix)


def _section_label(text: str, color: str | None = None) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color:{color or _C.TEXT_SEC};font-size:{_font_px(9)}px;font-weight:700;"
                      f"letter-spacing:{_scale(2)}px;background:transparent;")
    return lbl


def _card(parent: QWidget | None = None) -> QWidget:
    w = QWidget(parent)
    w.setProperty("surfaceCard", True)
    w.setStyleSheet(f'QWidget[surfaceCard="true"]{{background:{_C.BG_PANEL};border:1px solid {_C.BORDER};border-radius:{_scale(8)}px;}}')
    return w


def _bolt_icon(color: str | None = None, size: int = 14) -> QIcon:
    """Icône éclair (encodeur matériel / action rapide).

    Dessinée en SVG plutôt que via le glyphe emoji ⚡, qui n'est pas rendu
    quand aucune police emoji compatible n'est disponible (AppImage, etc.).
    """
    color = color or _C.WARN
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"'
        f' fill="{color}" stroke="{color}" stroke-width="1.5"'
        ' stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/>'
        '</svg>'
    )
    renderer = QSvgRenderer(svg.encode())
    icon_size = _scale(size)
    pix = QPixmap(icon_size, icon_size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    renderer.render(painter)
    painter.end()
    return QIcon(pix)


def _primary_button(text: str, *, height: int = 36, font_size: int = 12,
                    padding_h: int = 20, radius: int = 6) -> QPushButton:
    btn = QPushButton(text)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFixedHeight(_scale(height))
    btn.setStyleSheet(f"""
        QPushButton{{background:{_C.ACCENT};color:{_C.ON_ACCENT};border:none;border-radius:{_scale(radius)}px;
                     font-size:{_font_px(font_size)}px;font-weight:700;padding:0 {_scale(padding_h)}px;
                     text-align: center;}}
        QPushButton:hover{{background:{_C.ACCENT_HOVER};}}
        QPushButton:pressed{{background:{_C.ACCENT_PRESSED};}}
        QPushButton:disabled{{background:{_C.BG_ACTIVE};color:{_C.TEXT_DIM};}}
    """)
    return btn


def _secondary_button(
    text: str,
    fixed_width: int | None = None,
    *,
    min_width: int | None = None,
    padding_h: int | None = None,
    raised: bool = False,
) -> QPushButton:
    """Bouton secondaire ; ``raised`` : variante surélevée (fond actif, survol accentué)."""
    btn = QPushButton(text)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFixedHeight(_scale(30 if raised else 28))

    is_symbol = len(text.strip()) <= 2
    if padding_h is not None:
        pad_px = _scale(padding_h)
    elif is_symbol:
        pad_px = _scale(2)
    elif fixed_width is not None and fixed_width <= 50:
        pad_px = _scale(4)
    else:
        pad_px = _scale(8)

    if fixed_width is not None:
        btn.setFixedWidth(_scale(fixed_width))
    if min_width is not None:
        btn.setMinimumWidth(_scale(min_width))

    if raised:
        btn.setStyleSheet(f"""
            QPushButton{{background:{_C.BG_ACTIVE};color:{_C.TEXT_SEC};
                         border:1px solid {_C.BORDER_LT};border-radius:{_scale(5)}px;
                         font-size:{_font_px(11)}px;padding:0 {pad_px}px;text-align: center;}}
            QPushButton:hover{{background:{_C.BG_HOVER};color:{_C.TEXT_PRI};
                               border-color:{_C.ACCENT};}}
            QPushButton:disabled{{color:{_C.TEXT_DIM};border-color:{_C.BORDER};}}
        """)
        return btn
    btn.setStyleSheet(f"""
        QPushButton{{background:{_C.BG_CARD};color:{_C.TEXT_SEC};
                     border:1px solid {_C.BORDER};border-radius:5px;
                     font-size:{_font_px(11)}px;font-weight:500;padding:0 {pad_px}px;
                     text-align: center;}}
        QPushButton:hover{{background:{_C.BG_HOVER};color:{_C.TEXT_PRI};
                           border-color:{_C.BORDER_LT};}}
        QPushButton:pressed{{background:{_C.BG_ACTIVE};}}
        QPushButton:disabled{{background:{_C.BG_DEEP};color:{_C.TEXT_DIM};
                              border-color:{_C.BORDER};}}
    """)
    return btn


def _raised_button(text: str, fixed_width: int | None = None, **kwargs: Any) -> QPushButton:
    return _secondary_button(text, fixed_width, raised=True, **kwargs)


def _separator() -> QFrame:
    sep = QFrame()
    sep.setFrameShape(QFrame.Shape.HLine)
    sep.setFixedHeight(_scale(1))
    sep.setStyleSheet(f"background:{_C.BORDER};border:none;")
    return sep


def _input_style() -> str:
    return (f"QLineEdit{{background:{_C.BG_CARD};color:{_C.TEXT_PRI};"
            f"border:1px solid {_C.BORDER};border-radius:5px;"
            f"padding:{_scale(4)}px {_scale(10)}px;font-size:{_font_px(11)}px;}}"
            f"QLineEdit:focus{{border-color:{_C.ACCENT};}}"
            f"QLineEdit:disabled{{background:{_C.BG_DEEP};color:{_C.TEXT_DIM};"
            f"border-color:{_C.BORDER};}}")


def _slider_style() -> str:
    handle = _scale(16)
    groove_h = _scale(6)
    radius = _scale(3)
    margin = _scale(6)
    return (
        f"QSlider::groove:horizontal{{background:{_C.BG_CARD};height:{groove_h}px;"
        f"border:1px solid {_C.BORDER};border-radius:{radius}px;}}"
        f"QSlider::sub-page:horizontal{{background:{_C.ACCENT};border-radius:{radius}px;}}"
        f"QSlider::add-page:horizontal{{background:{_C.BG_ACTIVE};border-radius:{radius}px;}}"
        f"QSlider::handle:horizontal{{background:{_C.TEXT_PRI};width:{handle}px;height:{handle}px;"
        f"margin:-{margin}px 0;border-radius:{_scale(8)}px;border:1px solid {_C.BORDER_LT};}}"
        f"QSlider::handle:horizontal:hover{{border-color:{_C.ACCENT};}}"
    )


def _combo_style() -> str:
    return (f"QComboBox{{background:{_C.BG_CARD};color:{_C.TEXT_PRI};"
            f"border:1px solid {_C.BORDER};border-radius:5px;"
            f"padding:{_scale(3)}px {_scale(8)}px;font-size:{_font_px(11)}px;}}"
            f"QComboBox:focus{{border-color:{_C.ACCENT};}}"
            f"QComboBox QAbstractItemView{{background:{_C.BG_CARD};"
            f"color:{_C.TEXT_PRI};selection-background-color:{_C.ACCENT_DIM}; selection-color: {_C.TEXT_PRI};}}")


def _checkbox_style() -> str:
    from ui.design_system import CHECK_ICON_PATH
    return (
        f"QCheckBox {{ color: {_C.TEXT_PRI}; font-size: {_font_px(11)}px; spacing: {_scale(8)}px; background: transparent; }}"
        f"QCheckBox::indicator {{ width: {_scale(14)}px; height: {_scale(14)}px; border-radius: {_scale(3)}px; "
        f"border: 1px solid {_C.CHECKBOX_BORDER}; background: {_C.CHECKBOX_BG}; }}"
        f"QCheckBox::indicator:hover {{ border-color: {_C.ACCENT}; }}"
        f"QCheckBox::indicator:checked {{ background: {_C.ACCENT}; border-color: {_C.ACCENT}; image: url('{CHECK_ICON_PATH}'); }}"
        f"QCheckBox::indicator:disabled {{ border-color: {_C.BORDER}; background: {_C.BG_DEEP}; }}"
        f"QCheckBox::indicator:checked:disabled {{ background: {_C.TEXT_DIM}; border-color: {_C.TEXT_DIM}; "
        f"image: url('{CHECK_ICON_PATH}'); }}"
        f"QCheckBox:disabled {{ color: {_C.TEXT_DIM}; }}"
    )


def _groupbox_checkable_style() -> str:
    from ui.design_system import CHECK_ICON_PATH
    return (
        f"QGroupBox {{ color: {_C.TEXT_PRI}; font-size: {_font_px(11)}px; font-weight: 700; "
        f"background: {_C.BG_CARD}; border: 1px solid {_C.BORDER}; border-radius: {_scale(6)}px; "
        f"margin-top: {_scale(8)}px; padding-top: {_scale(14)}px; }}"
        f"QGroupBox::title {{ subcontrol-origin: border; subcontrol-position: top left; "
        f"left: {_scale(12)}px; padding: {_scale(2)}px {_scale(6)}px; background: {_C.BG_CARD}; }}"
        f"QGroupBox::indicator {{ width: {_scale(14)}px; height: {_scale(14)}px; border-radius: {_scale(3)}px; "
        f"border: 1px solid {_C.CHECKBOX_BORDER}; background: {_C.CHECKBOX_BG}; margin-right: {_scale(6)}px; }}"
        f"QGroupBox::indicator:hover {{ border-color: {_C.ACCENT}; }}"
        f"QGroupBox::indicator:checked {{ background: {_C.ACCENT}; border-color: {_C.ACCENT}; image: url('{CHECK_ICON_PATH}'); }}"
    )
