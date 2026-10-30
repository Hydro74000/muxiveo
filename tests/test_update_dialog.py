"""Popup « Mise à jour disponible » : notes de version repliées, lues à la demande ; préférence."""

from __future__ import annotations

import time

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QColor, QImage, QTextDocument
from PySide6.QtWidgets import QLabel

from core.update_check import UpdateCheckError, UpdateInfo
from ui.dialogs import update_dialog
from core.update_install import InstallKind
from ui.dialogs.update_dialog import UpdateDialog, heading_slug, install_unavailable_reason


def _wait(qt_app, predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.001)


def test_notes_collapsed_and_loaded_once_on_expand(qt_app):
    calls: list[str] = []
    dialog = UpdateDialog(
        UpdateInfo("99.0.0", "https://example/"), current="v1.0.0",
        notes_loader=lambda version: calls.append(version) or "## Nouveautés\n* **Popup** de mise à jour",
    )
    assert dialog._notes_view.isHidden() and not calls and dialog.popup_enabled
    dialog._notes_toggle.setChecked(True)
    _wait(qt_app, lambda: "Popup" in dialog._notes_view.toPlainText())
    assert calls == ["99.0.0"] and not dialog._notes_view.isHidden()
    dialog._notes_toggle.setChecked(False)
    dialog._notes_toggle.setChecked(True)
    assert calls == ["99.0.0"]
    dialog.deleteLater()


def test_embedded_notes_skip_network_and_failure_is_explained(qt_app):
    def _never(version: str) -> str:
        raise AssertionError("réseau interrogé")

    dialog = UpdateDialog(UpdateInfo("99.0.0", "https://example/", notes="Corps"), current="v1",
                          notes_loader=_never)
    dialog._notes_toggle.setChecked(True)
    assert dialog._notes_view.toPlainText() == "Corps"
    dialog.deleteLater()

    def _offline(version: str) -> str:
        raise UpdateCheckError("URLError: offline")

    dialog = UpdateDialog(UpdateInfo("99.0.0", "https://example/"), current="v1", notes_loader=_offline)
    dialog._notes_toggle.setChecked(True)
    _wait(qt_app, lambda: "offline" in dialog._notes_view.toPlainText())
    assert "offline" in dialog._notes_view.toPlainText()
    dialog.deleteLater()


def test_preference_checkbox_and_choices(qt_app):
    dialog = UpdateDialog(UpdateInfo("99.0.0", "https://example/"), current="v1", popup_enabled=False,
                          can_install=True, notes_loader=lambda _v: "")
    assert not dialog.popup_enabled
    dialog._silence_box.setChecked(False)
    assert dialog.popup_enabled
    dialog._finish(UpdateDialog.INSTALL)
    assert dialog.choice == "install" and dialog.result() == dialog.DialogCode.Accepted
    dialog.deleteLater()


def test_table_of_contents_scrolls_to_headings(qt_app):
    """Sommaire des notes : ancres explicites vides et identifiants GitHub des titres ; tout reste mis en page."""
    sentence = "Une phrase de notes de version assez longue pour remplir la ligne. " * 3

    def part(title: str, anchor: str) -> str:
        lines = [f'<a name="{anchor}"></a>', f"# {title}", ""]
        for n in range(1, 4):
            lines += [f"## {n}. Section", ""]
            # Sans lien : leur recoloration relancerait la mise en page et masquerait le défaut.
            lines += [f"* **Point {i}.** {sentence}" for i in range(12)] + [""]
        return "\n".join(lines)

    notes = ("# Notes\n\n* [Français](#version-française)\n* [English](#english)\n\n"
             + part("Version Française", "version-française") + "\n" + part("English Version", "english"))
    dialog = UpdateDialog(UpdateInfo("99.0.0", "https://example/", notes=notes), current="v1")
    dialog.show()
    dialog._notes_toggle.setChecked(True)
    qt_app.processEvents()
    document = dialog._notes_view.document()
    layout = document.documentLayout()
    block = document.begin()
    while block.isValid():                    # titres retouchés au-delà de la zone affichée : aucun bloc invisible
        assert not block.text() or layout.blockBoundingRect(block).height() > 0, block.text()
        block = block.next()
    bar = dialog._notes_view.verticalScrollBar()
    dialog._open_link(QUrl("#version-française"))
    french = bar.value()
    dialog._open_link(QUrl("#english"))
    english = bar.value()
    assert 0 < french < english < bar.maximum()
    bar.setValue(0)
    dialog._open_link(QUrl("#english-version"))                  # identifiant GitHub du titre
    assert bar.value() == english
    dialog._open_link(QUrl("#1-section-1"))                      # titre répété : suffixe GitHub
    assert bar.value() > english
    dialog.close()
    dialog.deleteLater()


def test_heading_slug_matches_github():
    assert heading_slug("Version Française") == "version-française"
    assert heading_slug(" 1. Nouveautés : `CLI` ") == "1-nouveautés--cli"


def test_notes_never_load_local_files(qt_app, tmp_path):
    """Images des notes (Markdown, <img>, fond CSS) : aucun fichier local lu (UNC Windows : fuite SMB)."""
    image = QImage(64, 64, QImage.Format.Format_RGB32)
    image.fill(QColor("red"))
    path = tmp_path / "secret.png"
    image.save(str(path))
    url = path.as_uri()
    notes = (f"# T\n\navant ![a]({url}) après\n\n<img src=\"{url}\"> <img src=\"secret.png\">\n\n"
             f"<p style=\"background-image:url({url})\">fond</p>\n")
    dialog = UpdateDialog(UpdateInfo("99.0.0", "https://example/", notes=notes), current="v1")
    dialog.show()
    dialog._notes_toggle.setChecked(True)
    qt_app.processEvents()
    document = dialog._notes_view.document()
    assert "\ufffc" not in document.toPlainText() and "avant" in document.toPlainText()
    loaded = document.resource(QTextDocument.ResourceType.ImageResource, QUrl(url))
    assert loaded is None or loaded.size() != image.size()
    dialog.close()
    dialog.deleteLater()


def test_remote_text_is_plain_and_enter_never_installs(qt_app):
    dialog = UpdateDialog(UpdateInfo("99.0.0", "https://example/"), current="v1", remarks=["<b>x</b>"],
                          can_install=True, notes_loader=lambda _v: "")
    labels = dialog.findChildren(QLabel)
    assert labels and all(label.textFormat() == Qt.TextFormat.PlainText for label in labels[:2])
    default = [button for button in dialog.findChildren(update_dialog.QPushButton) if button.isDefault()]
    assert [button.text() for button in default] in (["Plus tard"], ["Later"])
    install = next(b for b in dialog.findChildren(update_dialog.QPushButton) if b.text() in ("Installer", "Install"))
    assert not install.autoDefault()
    dialog.deleteLater()


def test_explicit_anchor_regex_is_linear():
    text = '<a name="x"></a>' + "\n" * 200_000 + "fin"
    started = time.perf_counter()
    assert update_dialog._EXPLICIT_ANCHOR_RE.findall(text) == []
    assert time.perf_counter() - started < 1.0


def test_install_greyed_with_reason_when_not_self_installable(qt_app):
    reason = install_unavailable_reason(InstallKind.MACOS_APP, blocker=update_dialog.BLOCKER_TRANSLOCATED)
    dialog = UpdateDialog(UpdateInfo("99.0.0", "https://example/"), current="v1", can_install=False,
                          install_unavailable=reason, notes_loader=lambda _v: "")
    button = dialog._install_btn
    assert not button.isEnabled() and not button.isHidden()
    assert "Applications" in button.toolTip()
    dialog.deleteLater()
    dialog = UpdateDialog(UpdateInfo("99.0.0", "https://example/"), current="v1", can_install=True,
                          notes_loader=lambda _v: "")
    assert dialog._install_btn.isEnabled() and not dialog._install_btn.toolTip()
    dialog.deleteLater()


def test_install_unavailable_reasons_name_the_way_to_update():
    assert "flatpak update" in install_unavailable_reason(InstallKind.FLATPAK)
    assert "git pull" in install_unavailable_reason(InstallKind.UNSUPPORTED, "git pull")
    assert "brew upgrade muxiveo" in install_unavailable_reason(InstallKind.UNSUPPORTED, "brew upgrade muxiveo")
    for kind in (InstallKind.WINDOWS_INSTALLER, InstallKind.APPIMAGE, InstallKind.SOURCE_ARCHIVE):
        assert "paquet vérifiable" in install_unavailable_reason(kind) or "verifiable" in install_unavailable_reason(kind)
    blockers = {
        update_dialog.BLOCKER_NOT_WRITABLE: ("modifiable", "modify"),
        update_dialog.BLOCKER_PYTHON_INSIDE: ("Python",),
        update_dialog.BLOCKER_NO_HDIUTIL: ("hdiutil",),
        update_dialog.BLOCKER_NO_MARKER: ("archive source", "source archive"),
    }
    for blocker, words in blockers.items():
        text = install_unavailable_reason(InstallKind.UNSUPPORTED, blocker=blocker)
        assert any(word in text for word in words), blocker
