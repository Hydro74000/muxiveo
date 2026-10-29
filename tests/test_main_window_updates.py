"""Regression tests for the update UI with real Qt signals and widgets."""

from types import SimpleNamespace
from unittest.mock import Mock, patch
import threading
import time

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMainWindow, QProgressDialog

from core.update_check import ReleaseAsset, UpdateCheckError, UpdateInfo
from core.update_install import InstallKind, UpdateInstallError
from ui.main_window import MainWindow, _Sidebar


class UpdateWindow(MainWindow):
    """Use the production handlers without constructing unrelated media panels."""

    def __init__(self):
        QMainWindow.__init__(self)
        self._config = SimpleNamespace(
            update_channel="unstable", check_updates=True, update_popup=False,
            save_last_update_check=Mock(), save_update_popup=Mock(),
        )
        self._sidebar = _Sidebar(self)
        self._running = False
        self._update_info = None
        self.log_info = Mock()
        self.log_error = Mock()
        self.log_warn = Mock()
        self._show_update_dialog = Mock()
        self._schedule_update_check()

    def closeEvent(self, event):  # fermeture en fin de session de tests : ni panneaux ni workers à arrêter
        QMainWindow.closeEvent(self, event)


@pytest.fixture
def window(qt_app, monkeypatch):
    # Keep startup checks explicit and prevent any real network access.
    monkeypatch.setattr(QTimer, "singleShot", Mock())
    monkeypatch.setattr("ui.main_window.query_latest_release", Mock(return_value=None))
    widget = UpdateWindow()
    yield widget
    widget.deleteLater()
    qt_app.processEvents()


def _finish_download(window, qt_app, result):
    jobs = []

    def deferred_thread(*, target, **kwargs):
        return SimpleNamespace(start=lambda: jobs.append(target))

    with patch("ui.main_window.threading.Thread", deferred_thread), \
         patch("ui.main_window.download_update", side_effect=result if isinstance(result, Exception) else None,
               return_value=result), \
         patch("ui.main_window.QMessageBox.warning") as warning, \
         patch("ui.main_window.apply_update") as apply, \
         patch("ui.main_window.QApplication.quit") as quit_app:
        window._start_update_install(UpdateInfo("99.0.0", "https://example/"), InstallKind.APPIMAGE)
        qt_app.processEvents()
        # Téléchargement, puis installation (thread distinct, hors du thread UI).
        while jobs:
            jobs.pop(0)()
            qt_app.processEvents()
    return warning, apply, quit_app


def test_download_failure_is_reported_not_cancelled(window, qt_app):
    warning, apply, quit_app = _finish_download(window, qt_app, UpdateInstallError("SHA-256 invalide"))
    warning.assert_called_once()
    window.log_error.assert_called_once_with("SHA-256 invalide")
    window.log_info.assert_not_called()
    apply.assert_not_called()
    quit_app.assert_not_called()
    assert window._update_download_cancel is None


def test_successful_download_installs_and_quits(window, qt_app, tmp_path):
    downloaded = tmp_path / "verified.AppImage"
    downloaded.write_bytes(b"verified")
    warning, apply, quit_app = _finish_download(window, qt_app, downloaded)
    warning.assert_not_called()
    apply.assert_called_once_with(InstallKind.APPIMAGE, downloaded)
    quit_app.assert_called_once()


@pytest.mark.parametrize("cancelled,running", [(True, False), (False, True)])
def test_cancel_or_new_operation_prevents_install(window, tmp_path, cancelled, running):
    downloaded = tmp_path / "verified.AppImage"
    downloaded.write_bytes(b"verified")
    window._running = running
    with patch("ui.main_window.apply_update") as apply, patch("ui.main_window.QApplication.quit") as quit_app:
        window._on_update_downloaded(downloaded, InstallKind.APPIMAGE, cancelled)
    apply.assert_not_called()
    quit_app.assert_not_called()
    deadline = time.monotonic() + 5
    while downloaded.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not downloaded.exists()


def test_late_user_cancel_after_worker_result_prevents_install(window, qt_app, tmp_path):
    downloaded = tmp_path / "verified.AppImage"
    downloaded.write_bytes(b"verified")
    jobs = []
    with patch("ui.main_window.threading.Thread", side_effect=lambda **kw: SimpleNamespace(
            start=lambda: jobs.append(lambda: kw["target"](*kw.get("args", ()))))), \
         patch("ui.main_window.download_update", return_value=downloaded), \
         patch("ui.main_window.apply_update") as apply:
        window._start_update_install(UpdateInfo("99.0.0", "https://example/"), InstallKind.APPIMAGE)
        jobs[0]()  # Queue the success signal before the user cancels.
        window.findChild(QProgressDialog).close()
        qt_app.processEvents()
        assert len(jobs) == 2
        jobs[1]()  # Le ménage tardif est lui aussi délégué.
    apply.assert_not_called()
    assert not downloaded.exists()


def test_cancelled_directory_cleanup_keeps_gui_responsive_and_allows_close(window, qt_app, tmp_path, monkeypatch):
    prepared = tmp_path / "prepared"
    prepared.mkdir()
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    cleanup_threads = []
    ticks = []
    timer = QTimer(window)
    timer.setInterval(0)
    timer.timeout.connect(lambda: ticks.append(True))

    def slow_remove(path):
        assert path == prepared
        cleanup_threads.append(threading.current_thread())
        entered.set()
        release.wait(5)
        finished.set()

    monkeypatch.setattr("ui.main_window.shutil.rmtree", slow_remove)
    monkeypatch.setattr(window, "_launch_update", Mock())
    timer.start()
    try:
        window._on_update_downloaded(prepared, InstallKind.SOURCE_ARCHIVE, True)
        assert entered.wait(2)
        qt_app.processEvents()
        assert ticks and not finished.is_set()
        assert cleanup_threads[0] is not threading.current_thread() and cleanup_threads[0].daemon
        assert window.close()
        window._launch_update.assert_not_called()
    finally:
        timer.stop()
        release.set()
        assert finished.wait(2)


def test_response_from_previous_channel_is_discarded(window, qt_app):
    info = UpdateInfo("99.0.0-unstable.20260924.1.abc", "https://example/", prerelease=True)
    with patch("ui.main_window.query_latest_release", return_value=info) as fetch:
        window._run_update_check(window._update_request_id, "unstable", False)
    window._config.update_channel = "stable"
    qt_app.processEvents()
    fetch.assert_called_once_with("unstable", timeout=5.0)
    window._config.save_last_update_check.assert_not_called()
    assert window._update_info is None


def test_old_response_discarded_even_after_switching_back(window):
    old_id = window._update_request_id
    window._config.update_channel = "stable"
    window._sync_update_settings()
    window._config.update_channel = "unstable"
    window._sync_update_settings()
    window._on_update_available(old_id, "unstable", UpdateInfo("99.0.0", "https://example/"), True)
    window._config.save_last_update_check.assert_not_called()
    window._show_update_dialog.assert_not_called()


def test_channel_change_clears_badge_and_loaded_assets(window):
    info = UpdateInfo("99.0.0-unstable.20260924.1.abc", "https://example/",
                      assets=(ReleaseAsset("x", "https://example/x"),), prerelease=True)
    window._on_update_available(window._update_request_id, "unstable", info, False)
    window._config.update_channel = "stable"
    window._on_update_requested()
    assert window._update_info is None
    assert window._sidebar._update_info is None
    window._show_update_dialog.assert_not_called()


def test_disabling_updates_invalidates_pending_request(window):
    old_id = window._update_request_id
    window._config.check_updates = False
    window._sync_update_settings()
    with patch("ui.main_window.threading.Thread") as worker:
        window._start_update_check(old_id, "unstable")
    worker.assert_not_called()
    window._on_update_available(old_id, "unstable", UpdateInfo("99.0.0", "https://example/"), True)
    window._show_update_dialog.assert_not_called()


def test_current_result_uses_queried_channel_in_cache(window):
    info = UpdateInfo("99.0.0", "https://example/")
    window._on_update_available(window._update_request_id, "unstable", info, False)
    assert window._config.save_last_update_check.call_args.args[1:] == ("99.0.0", "unstable")
    assert window._sidebar._update_info is info


def test_cached_refresh_is_asynchronous_and_deduplicated(window, qt_app):
    info = UpdateInfo("99.0.0", "https://example/")
    window._update_info = info
    entered = threading.Event()
    release = threading.Event()
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(True))

    def fetch(*args, **kwargs):
        entered.set()
        assert release.wait(3), "UI did not return control while fetching assets"
        return info

    try:
        with patch("ui.main_window.query_latest_release", side_effect=fetch) as query:
            window._on_update_requested()
            assert entered.wait(1)
            window._on_update_requested()
            timer.start(0)
            qt_app.processEvents()
            assert ticks
            query.assert_called_once_with("unstable", timeout=10.0)
            window._show_update_dialog.assert_not_called()
            release.set()
            deadline = time.monotonic() + 3
            while not window._show_update_dialog.called and time.monotonic() < deadline:
                qt_app.processEvents()
                time.sleep(0.001)
            window._show_update_dialog.assert_called_once_with(info)
            assert not window._update_refresh_pending
    finally:
        release.set()
        timer.stop()


def test_refreshed_release_must_still_be_newer(window):
    window._update_info = UpdateInfo("99.0.0", "https://example/")
    window._update_refresh_pending = True
    window._on_update_available(window._update_request_id, "unstable", UpdateInfo("0.0.1", "https://example/"), True)
    window._show_update_dialog.assert_not_called()
    assert window._update_info is None
    assert not window._update_refresh_pending


def test_offline_refresh_keeps_manual_release_link(window):
    info = UpdateInfo("99.0.0", "https://example/")
    window._update_info = info
    window._on_update_available(window._update_request_id, "unstable", None, True)
    window._show_update_dialog.assert_called_once_with(info)


def test_network_failure_is_logged_without_caching(window, qt_app):
    """Un échec réseau ne doit pas être mémorisé comme « aucune mise à jour » pendant 24 h."""
    with patch("ui.main_window.query_latest_release", side_effect=UpdateCheckError("SSLError: CERTIFICATE_VERIFY_FAILED")):
        window._run_update_check(window._update_request_id, "unstable", False)
    qt_app.processEvents()
    window._config.save_last_update_check.assert_not_called()
    window.log_warn.assert_called_once()
    assert "CERTIFICATE_VERIFY_FAILED" in window.log_warn.call_args.args[0]


def test_network_failure_on_click_keeps_manual_release_link(window, qt_app):
    info = UpdateInfo("99.0.0", "https://example/")
    window._update_info = info
    window._update_refresh_pending = True
    window._on_update_check_failed(window._update_request_id, "unstable", "offline", True)
    window._show_update_dialog.assert_called_once_with(info)
    assert not window._update_refresh_pending


@pytest.mark.parametrize("channel", ["stable", "unstable"])
def test_startup_always_checks_network_despite_recent_check(qt_app, monkeypatch, channel):
    """Vérification à chaque lancement, quel que soit le canal et la date du dernier contrôle."""
    single_shot = Mock()
    monkeypatch.setattr(QTimer, "singleShot", single_shot)
    monkeypatch.setattr(UpdateWindow, "_schedule_update_check", lambda self: None)
    widget = UpdateWindow()
    widget._config.update_channel = channel
    widget._config.last_update_check = time.time()
    widget._config.last_update_channel = channel
    widget._config.last_update_version = ""
    try:
        MainWindow._schedule_update_check(widget)
        single_shot.assert_called_once()
        with patch("ui.main_window.threading.Thread") as worker:
            single_shot.call_args.args[-1]()
        assert worker.call_args.kwargs["daemon"] is True
        assert worker.call_args.kwargs["args"][1] == channel
    finally:
        widget.deleteLater()
        qt_app.processEvents()


def test_startup_result_opens_popup_once_per_version(window):
    window._config.update_popup = True
    info = UpdateInfo("99.0.0", "https://example/")
    window._show_update_dialog.side_effect = lambda shown: setattr(window, "_update_popup_version", shown.version)
    window._on_update_available(window._update_request_id, "unstable", info, False)
    window._on_update_available(window._update_request_id, "unstable", info, False)
    window._show_update_dialog.assert_called_once_with(info)
    assert window._sidebar._update_info is info


def test_popup_disabled_keeps_sidebar_badge_only(window):
    info = UpdateInfo("99.0.0", "https://example/")
    window._on_update_available(window._update_request_id, "unstable", info, False)
    window._show_update_dialog.assert_not_called()
    assert window._sidebar._update_info is info


def test_popup_deferred_while_operation_runs(window):
    window._config.update_popup = True
    window._running = True
    with patch("ui.main_window.QTimer.singleShot") as single_shot:
        window._on_update_available(window._update_request_id, "unstable", UpdateInfo("99.0.0", "https://example/"), False)
    window._show_update_dialog.assert_not_called()
    assert single_shot.call_args.args[2] == window._maybe_popup_update
    window._running = False
    window._maybe_popup_update()
    window._show_update_dialog.assert_called_once()


@pytest.mark.parametrize("choice", ["", "release", "install"])
def test_popup_checkbox_saves_preference_and_runs_choice(window, choice):
    del window._show_update_dialog                      # gestionnaire réel, dialogue simulé
    window._config.update_popup = True
    window._settings_panel = Mock()
    dialog = Mock(choice=choice, popup_enabled=False)
    info = UpdateInfo("99.0.0", "https://example/")
    with patch("ui.main_window.UpdateDialog", return_value=dialog) as dialog_cls, \
         patch("ui.main_window.detect_install_kind", return_value=InstallKind.APPIMAGE), \
         patch("ui.main_window.can_self_update", return_value=True), \
         patch("ui.main_window.open_external") as open_url, \
         patch.object(window, "_start_update_install") as install:
        dialog_cls.INSTALL, dialog_cls.RELEASE = "install", "release"
        window._show_update_dialog(info)
    assert dialog_cls.call_args.kwargs["popup_enabled"] is True and dialog_cls.call_args.kwargs["can_install"]
    window._config.save_update_popup.assert_called_once_with(False)
    window._settings_panel.refresh_field.assert_called_once_with("ui", "update_popup")
    assert open_url.called == (choice == "release")
    assert install.called == (choice == "install")
    assert window._update_popup_version == "99.0.0"


def test_sidebar_extension_update_dot(qt_app):
    sidebar = _Sidebar(compact=True)
    button = next(b for b in sidebar._buttons if b.page_index == 6)
    plain = button._compact_icon_lbl.pixmap().toImage()
    sidebar.set_page_update_available(6, True)
    assert button._has_update and not button._ready_dot.isHidden()
    assert button._compact_icon_lbl.pixmap().toImage() != plain     # pastille dessinée sur l'icône
    sidebar.set_ready_operations(set())                             # le point reste : mise à jour en attente
    assert not button._ready_dot.isHidden()
    sidebar.set_page_update_available(6, False)
    assert button._ready_dot.isHidden() and button._compact_icon_lbl.pixmap().toImage() == plain
    sidebar.deleteLater()


def test_deferred_popup_dropped_once_closing(window):
    window._config.update_popup = True
    window._update_info = UpdateInfo("99.0.0", "https://example/")
    window._shutdown = Mock()                                    # closeEvent() commencé
    with patch("ui.main_window.QTimer.singleShot") as single_shot:
        window._maybe_popup_update()
    single_shot.assert_not_called()
    window._show_update_dialog.assert_not_called()


def test_sidebar_version_link_escapes_release_url(qt_app):
    sidebar = _Sidebar()
    sidebar.set_update_available(UpdateInfo("99.0.0", 'https://example/"><img src="x">'))
    text = sidebar._version_lbl.text()
    assert "<img" not in text and "&quot;&gt;&lt;img" in text
    sidebar.deleteLater()


def test_appimage_popup_install_follows_blocker(window):
    del window._show_update_dialog                      # gestionnaire réel, dialogue simulé
    info = UpdateInfo("99.0.0", "https://example/", assets=(
        ReleaseAsset("Muxiveo-x86_64-99.0.0.AppImage", "https://example/a"), ReleaseAsset("SHA256SUMS", "https://example/s"),
    ))
    for blocker, expected in (("", True), ("not-writable", False)):
        with patch("ui.main_window.UpdateDialog", return_value=Mock(choice="", popup_enabled=False)) as dialog_cls, \
             patch("ui.main_window.detect_install_kind", return_value=InstallKind.APPIMAGE), \
             patch("core.update_install.platform.machine", return_value="x86_64"), \
             patch("core.update_install.self_install_blocker", return_value=blocker), \
             patch("ui.main_window.self_install_blocker", return_value=blocker):
            window._show_update_dialog(info)
        kwargs = dialog_cls.call_args.kwargs
        assert kwargs["can_install"] is expected
        assert bool(kwargs["install_unavailable"]) is not expected


def test_previous_update_results_reported_at_startup(window):
    results = [
        {"ok": True, "version": "2.0.0", "backup": "/apps/.muxiveo-update-x/old", "kept": ["docs/a.md"],
         "requirements_changed": True},
        {"ok": False, "version": "2.0.1", "error": "Dossier utilisé"},
    ]
    window.log_ok = Mock()
    with patch("ui.main_window.collect_update_results", return_value=results), \
         patch("ui.main_window.QTimer.singleShot") as single_shot:
        window._report_previous_updates()
    assert "2.0.0" in window.log_ok.call_args.args[0]
    warnings = " ".join(call.args[0] for call in window.log_warn.call_args_list)
    assert "docs/a.md" in warnings and ("setup" in warnings.lower())
    assert "Dossier utilisé" in window.log_error.call_args.args[0]
    single_shot.assert_called_once()                    # avertissement visible pour l'échec


def test_install_failure_is_reported_without_quitting(window, qt_app, tmp_path):
    downloaded = tmp_path / "verified.AppImage"
    downloaded.write_bytes(b"verified")
    jobs = []
    with patch("ui.main_window.threading.Thread", side_effect=lambda **kw: SimpleNamespace(start=lambda: jobs.append(kw["target"]))), \
         patch("ui.main_window.apply_update", side_effect=UpdateInstallError("Lancement impossible")), \
         patch("ui.main_window.QMessageBox.warning") as warning, \
         patch("ui.main_window.QApplication.quit") as quit_app:
        window._on_update_downloaded(downloaded, InstallKind.APPIMAGE, False)
        assert len(jobs) == 1                       # installation confiée à un thread
        jobs.pop()()
        qt_app.processEvents()
    warning.assert_called_once()
    window.log_error.assert_called_once_with("Lancement impossible")
    quit_app.assert_not_called()


@pytest.mark.parametrize("error", [None, UpdateInstallError("Élévation refusée")])
def test_windows_installer_is_launched_on_gui_thread_with_its_owner(window, tmp_path, error):
    setup = tmp_path / "setup.exe"
    thread_ids = []

    def apply(*_args, **_kwargs):
        thread_ids.append(threading.get_ident())
        if error is not None:
            raise error

    with patch("ui.main_window.apply_update", side_effect=apply) as install, \
         patch("ui.main_window.threading.Thread") as worker, \
         patch("ui.main_window.QApplication.quit") as quit_app, \
         patch("ui.main_window.QMessageBox.warning") as warning:
        window._on_update_downloaded(setup, InstallKind.WINDOWS_INSTALLER, False)
    assert thread_ids == [threading.get_ident()]
    install.assert_called_once_with(InstallKind.WINDOWS_INSTALLER, setup, owner_hwnd=int(window.winId()))
    worker.assert_not_called()
    assert quit_app.called is (error is None) and warning.called is (error is not None)


def test_startup_cleanup_is_detached_from_gui_process(window, tmp_path):
    old = tmp_path / "old"

    def collect(*, discard):
        discard.append(old)
        return []

    with patch("ui.main_window.collect_update_results", collect), \
         patch("ui.main_window.start_update_cleanup") as cleanup, \
         patch("ui.main_window.threading.Thread") as thread:
        window._report_previous_updates()
    cleanup.assert_called_once_with([old])
    thread.assert_not_called()
