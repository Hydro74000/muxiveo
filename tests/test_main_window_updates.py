"""Regression tests for the update UI with real Qt signals and widgets."""

from types import SimpleNamespace
from unittest.mock import Mock, patch
import threading
import time

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMainWindow, QProgressDialog

from core.update_check import ReleaseAsset, UpdateInfo
from core.update_install import InstallKind, UpdateInstallError
from ui.main_window import MainWindow, _Sidebar


class UpdateWindow(MainWindow):
    """Use the production handlers without constructing unrelated media panels."""

    def __init__(self):
        QMainWindow.__init__(self)
        self._config = SimpleNamespace(
            update_channel="unstable", check_updates=True,
            save_last_update_check=Mock(),
        )
        self._sidebar = _Sidebar(self)
        self._running = False
        self._update_info = None
        self.log_info = Mock()
        self.log_error = Mock()
        self._show_update_dialog = Mock()
        self._schedule_update_check()


@pytest.fixture
def window(qt_app, monkeypatch):
    # Keep startup checks explicit and prevent any real network access.
    monkeypatch.setattr(QTimer, "singleShot", Mock())
    monkeypatch.setattr("ui.main_window.fetch_latest_release", Mock(return_value=None))
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
        jobs[0]()
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
    assert not downloaded.exists()


def test_late_user_cancel_after_worker_result_prevents_install(window, qt_app, tmp_path):
    downloaded = tmp_path / "verified.AppImage"
    downloaded.write_bytes(b"verified")
    jobs = []
    with patch("ui.main_window.threading.Thread", side_effect=lambda **kw: SimpleNamespace(start=lambda: jobs.append(kw["target"]))), \
         patch("ui.main_window.download_update", return_value=downloaded), \
         patch("ui.main_window.apply_update") as apply:
        window._start_update_install(UpdateInfo("99.0.0", "https://example/"), InstallKind.APPIMAGE)
        jobs[0]()  # Queue the success signal before the user cancels.
        window.findChild(QProgressDialog).close()
        qt_app.processEvents()
    apply.assert_not_called()
    assert not downloaded.exists()


def test_response_from_previous_channel_is_discarded(window, qt_app):
    info = UpdateInfo("99.0.0-unstable.20260924.1.abc", "https://example/", prerelease=True)
    with patch("ui.main_window.fetch_latest_release", return_value=info) as fetch:
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
        with patch("ui.main_window.fetch_latest_release", side_effect=fetch) as query:
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
