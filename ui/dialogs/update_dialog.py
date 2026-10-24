"""
ui/dialogs/update_dialog.py — popup « Mise à jour disponible ».

Version proposée, avertissements (pré-version, mise à jour manuelle), notes de version GitHub dans un volet
replié (lues à sa première ouverture, hors du thread de l'interface) et case « Ne plus afficher de popup de
mise à jour ». Le choix (installer, voir la release) et la préférence sont appliqués par la fenêtre principale.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable

from PySide6.QtCore import QByteArray, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QImage, QTextCharFormat, QTextCursor, QTextDocument, QTextFormat
from PySide6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QLabel, QPushButton, QTextBrowser, QVBoxLayout, QWidget

from core.i18n import translate_text
from core.update_check import UpdateCheckError, UpdateInfo, fetch_release_notes
from ui.design_system import colors as _C, font_px, scale
from ui.desktop import open_external
from ui.styles import _checkbox_style, _primary_button, _secondary_button

# Ancre explicite vide placée juste avant un titre (`<a name="english-version"></a>` puis `# English Version`) :
# l'import Markdown de Qt l'ignore (aucun caractère porteur), elle est reportée sur le titre.
# Aucun quantificateur ne se recouvre (fin de ligne, puis lignes vides une à une) : coût linéaire, même sur des
# notes faites de milliers de lignes vides.
_EXPLICIT_ANCHOR_RE = re.compile(
    r'<a[ \t]+(?:name|id)[ \t]*=[ \t]*"([^"\n]+)"[ \t]*>[ \t]*</a>[ \t\r]*\n(?:[ \t\r]*\n)*[ \t]*#{1,6}[ \t]+([^\n]+)',
    re.IGNORECASE,
)


def heading_slug(text: str) -> str:
    """Identifiant GitHub d'un titre : minuscules, ponctuation retirée (lettres accentuées gardées), espaces → tirets."""
    return re.sub(r"[^\w\- ]", "", text.strip().lower()).replace(" ", "-")


class _NotesBrowser(QTextBrowser):
    """Notes de version distantes : aucune ressource chargée (fichier local, chemin relatif au dossier courant,
    chemin UNC Windows qui déclencherait une authentification SMB vers un serveur tiers).

    Couvre les ressources demandées au document (images de fond, feuilles de style) ; les images en ligne sont
    retirées avant affichage (`_strip_images`), le moteur d'images de Qt lisant leur fichier sans passer par ici."""

    def loadResource(self, resource_type: int, name: QUrl | str) -> object:
        # Valeur non nulle : le repli de QTextDocument (lecture directe du fichier local) n'est pas tenté.
        if resource_type == QTextDocument.ResourceType.ImageResource.value:
            image = QImage(1, 1, QImage.Format.Format_ARGB32)
            image.fill(0)
            return image
        return QByteArray()


def _strip_images(document: QTextDocument) -> None:
    """Retire les images (Markdown `![…](…)`, `<img>`) d'un document pas encore mis en page : leur chargement lit
    le fichier désigné (et ses variantes @2x) quelle que soit la provenance du texte."""
    images: list[tuple[int, int]] = []
    block = document.begin()
    while block.isValid():
        fragments = block.begin()
        while not fragments.atEnd():
            fragment = fragments.fragment()
            if fragment.isValid() and fragment.charFormat().isImageFormat():
                images.append((fragment.position(), fragment.length()))
            fragments += 1
        block = block.next()
    for position, length in reversed(images):
        cursor = QTextCursor(document)
        cursor.setPosition(position)
        cursor.setPosition(position + length, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()


class UpdateDialog(QDialog):
    """Popup d'une nouvelle version : notes de version repliées, installation ou page de la release."""

    INSTALL = "install"
    RELEASE = "release"

    _notes_ready = Signal(bool, str)   # succès, notes Markdown ou raison de l'échec

    def __init__(
        self,
        info: UpdateInfo,
        *,
        current: str,
        remarks: list[str] | tuple[str, ...] = (),
        can_install: bool = False,
        popup_enabled: bool = True,
        notes_loader: Callable[[str], str] = fetch_release_notes,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._info = info
        self._notes_loader = notes_loader
        self._notes_requested = False
        self.choice = ""
        self.setWindowTitle(translate_text("Mise à jour disponible"))
        self.setModal(True)
        self.setMinimumWidth(scale(560))
        self.setStyleSheet(f"QDialog{{background:{_C.BG_DEEP};}}")
        layout = QVBoxLayout(self)
        layout.setSpacing(scale(12))

        title = QLabel(translate_text("Muxiveo v{version} est disponible", version=info.version))
        title.setTextFormat(Qt.TextFormat.PlainText)     # texte venu du réseau : jamais interprété en HTML
        title.setWordWrap(True)
        title.setStyleSheet(
            f"color:{_C.TEXT_PRI};font-size:{font_px(15)}px;font-weight:700;background:transparent;"
        )
        layout.addWidget(title)

        lines = [translate_text("Version actuelle : {current}.", current=current), *remarks]
        details = QLabel("\n\n".join(lines))
        details.setTextFormat(Qt.TextFormat.PlainText)
        details.setWordWrap(True)
        details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        details.setStyleSheet(f"color:{_C.TEXT_SEC};background:transparent;")
        layout.addWidget(details)

        self._notes_toggle = QPushButton()
        self._notes_toggle.setCheckable(True)
        self._notes_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._notes_toggle.setStyleSheet(
            f"QPushButton{{color:{_C.TEXT_SEC};background:transparent;border:none;text-align:left;"
            f"padding:{scale(4)}px 0;font-size:{font_px(11)}px;font-weight:600;}}"
            f"QPushButton:hover{{color:{_C.TEXT_PRI};}}"
        )
        self._notes_toggle.toggled.connect(self._on_notes_toggled)
        layout.addWidget(self._notes_toggle)

        self._notes_view = _NotesBrowser()
        self._notes_view.setOpenLinks(False)
        self._notes_view.anchorClicked.connect(self._open_link)
        self._notes_view.setMinimumHeight(scale(300))
        self._notes_view.setStyleSheet(
            f"QTextBrowser{{background:{_C.BG_PANEL};color:{_C.TEXT_PRI};border:1px solid {_C.BORDER};"
            f"border-radius:{scale(6)}px;padding:{scale(8)}px;}}"
        )
        self._notes_view.hide()
        layout.addWidget(self._notes_view, 1)
        self._refresh_notes_toggle(False)

        self._silence_box = QCheckBox(translate_text("Ne plus afficher de popup de mise à jour"))
        self._silence_box.setToolTip(translate_text(
            "La nouvelle version reste signalée en bas du menu latéral. Réactivation : Paramètres → Interface."
        ))
        self._silence_box.setStyleSheet(_checkbox_style())
        self._silence_box.setChecked(not popup_enabled)
        layout.addWidget(self._silence_box)

        buttons = QHBoxLayout()
        release_btn = _secondary_button(translate_text("Voir la release"))
        release_btn.clicked.connect(lambda: self._finish(self.RELEASE))
        buttons.addWidget(release_btn)
        buttons.addStretch()
        later_btn = _secondary_button(translate_text("Plus tard"))
        later_btn.clicked.connect(self.reject)
        buttons.addWidget(later_btn)
        # Popup non sollicitée : Entrée ne doit jamais lancer l'installation (clic explicite seulement).
        later_btn.setDefault(True)
        if can_install:
            install_btn = _primary_button(translate_text("Installer"))
            install_btn.setAutoDefault(False)
            install_btn.clicked.connect(lambda: self._finish(self.INSTALL))
            buttons.addWidget(install_btn)
        layout.addLayout(buttons)

        self._notes_ready.connect(self._on_notes_ready, Qt.ConnectionType.QueuedConnection)

    # ------------------------------------------------------------------

    @property
    def popup_enabled(self) -> bool:
        """Préférence choisie dans la popup (case « Ne plus afficher… » décochée)."""
        return not self._silence_box.isChecked()

    def _finish(self, choice: str) -> None:
        self.choice = choice
        self.accept()

    def _refresh_notes_toggle(self, expanded: bool) -> None:
        arrow = "▾" if expanded else "▸"
        self._notes_toggle.setText(f"{arrow}  {translate_text('Notes de version')}")

    def _on_notes_toggled(self, expanded: bool) -> None:
        self._refresh_notes_toggle(expanded)
        self._notes_view.setVisible(expanded)
        if expanded:
            self._load_notes()
            self.resize(self.width(), max(self.height(), self.sizeHint().height()))
        else:
            # Replié : la fenêtre reprend sa hauteur une fois la mise en page recalculée.
            QTimer.singleShot(0, self, lambda: self.resize(self.width(), self.minimumSizeHint().height()))

    def _load_notes(self) -> None:
        if self._notes_requested:
            return
        self._notes_requested = True
        if self._info.notes:
            self._show_notes(self._info.notes)
            return
        self._notes_view.setPlainText(translate_text("Chargement des notes de version…"))
        threading.Thread(target=self._fetch_notes, name="muxiveo-release-notes", daemon=True).start()

    def _fetch_notes(self) -> None:
        """Thread worker : lit les notes sur GitHub puis les relaie au thread UI."""
        try:
            ok, text = True, self._notes_loader(self._info.version)
        except UpdateCheckError as exc:
            ok, text = False, str(exc)
        except Exception as exc:  # noqa: BLE001 — le volet ne doit jamais rester sur « Chargement… »
            ok, text = False, f"{type(exc).__name__}: {exc}"
        try:
            self._notes_ready.emit(ok, text)
        except RuntimeError:  # popup déjà fermée
            pass

    def _on_notes_ready(self, ok: bool, text: str) -> None:
        if ok:
            self._show_notes(text)
            return
        self._notes_view.setPlainText(translate_text(
            "Notes de version indisponibles ({reason}). Elles restent consultables sur la page de la release.",
            reason=text,
        ))

    def _show_notes(self, markdown: str) -> None:
        if markdown.strip():
            # Document préparé hors affichage puis installé d'un bloc : retoucher le document déjà mis en page
            # (mise en page paresseuse de Qt) laisse des blocs de hauteur nulle, donc invisibles.
            document = QTextDocument(self._notes_view)
            document.setDefaultFont(self._notes_view.document().defaultFont())
            document.setMarkdown(markdown)
            _strip_images(document)
            self._restyle_notes(document, markdown)
            self._notes_view.setDocument(document)
        else:
            self._notes_view.setPlainText(translate_text("Aucune note de version publiée pour cette version."))

    def _restyle_notes(self, document: QTextDocument, markdown: str) -> None:
        """Markdown GitHub à l'échelle du dialogue : titres h1/h2 +1 cran, h3 et suivants à la taille du texte ;
        liens à la couleur d'accent (l'import Markdown impose le bleu de la palette par défaut) ; chaque titre
        devient une destination du sommaire (identifiant GitHub, suffixé -1, -2… s'il se répète, et ancres
        explicites qui le précèdent dans la source)."""
        explicit: dict[str, list[str]] = {}
        for name, title in _EXPLICIT_ANCHOR_RE.findall(markdown):
            explicit.setdefault(heading_slug(title.strip().rstrip("#")), []).append(name)
        seen: dict[str, int] = {}
        link = QTextCharFormat()
        link.setForeground(QColor(_C.ACCENT))
        block = document.begin()
        while block.isValid():
            level = block.blockFormat().headingLevel()
            if level:
                fmt = QTextCharFormat()
                fmt.setProperty(QTextFormat.Property.FontSizeAdjustment, 1 if level <= 2 else 0)
                base = heading_slug(block.text())
                if base:
                    count = seen.get(base, 0)
                    seen[base] = count + 1
                    names = [base if count == 0 else f"{base}-{count}"]
                    if explicit.get(base):
                        names.append(explicit[base].pop(0))
                    fmt.setAnchor(True)
                    fmt.setAnchorNames(names)
                cursor = QTextCursor(block)
                cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
                cursor.mergeCharFormat(fmt)
            anchors: list[tuple[int, int]] = []
            fragments = block.begin()
            while not fragments.atEnd():
                fragment = fragments.fragment()
                if fragment.isValid() and fragment.charFormat().anchorHref():
                    anchors.append((fragment.position(), fragment.length()))
                fragments += 1
            for position, length in anchors:
                cursor = QTextCursor(document)
                cursor.setPosition(position)
                cursor.setPosition(position + length, QTextCursor.MoveMode.KeepAnchor)
                cursor.mergeCharFormat(link)
            block = block.next()

    def _open_link(self, url: QUrl) -> None:
        if url.scheme() in {"http", "https"}:
            open_external(url)
        elif not url.scheme() and url.hasFragment():   # sommaire des notes (#version-française…)
            self._notes_view.scrollToAnchor(url.fragment())
