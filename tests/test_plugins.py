"""Extension d'accélération NVIDIA (TensorRT) : gestionnaire, sonde, encodage et interface."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

from core import plugins
from core.github_release import ReleaseAsset
from core.plugins import platform_tag as real_platform_tag
from core.plugins import plugins_root as real_plugins_root
from core.plugins import trt_engine_cache_dir as real_trt_engine_cache_dir
from core.version import MVO_RIFE_TRT_VERSION
from core.workflows.encode.interpolation import (
    InterpolationSource,
    NvofCapability,
    TrtCapability,
    build_rife_stage,
    detect_gpu_acceleration as real_detect_gpu_acceleration,
)

PLATFORM = "linux-x86_64"
LIB = plugins.TRT_LIBRARIES[PLATFORM]


@pytest.fixture(autouse=True)
def _linux_platform(monkeypatch):
    monkeypatch.setattr(plugins, "platform_tag", lambda *_a, **_k: PLATFORM)


def _archive(tmp_path: Path, *, version: str = MVO_RIFE_TRT_VERSION, abi: int = plugins.TRT_PLUGIN_ABI,
             tamper: bool = False, extra: tuple[str, bytes] | None = None) -> ReleaseAsset:
    """Archive d'extension minimale (manifeste, bibliothèque factice, modèle) et son asset vérifié."""
    files = {LIB: b"bibliotheque", "models/rife-v4.6.onnx": b"modele", "LICENSES/MIT.txt": b"licence"}
    manifest = {
        "name": plugins.TRT_PLUGIN_ID, "version": version, "abi": abi, "platform": PLATFORM,
        "library": LIB, "models": ["rife-v4.6", "rife-v4.6-uhd"],
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
    }
    if tamper:
        files[LIB] = b"modifiee"
    top = f"mvo-rife-trt-{version}-{PLATFORM}"
    archive = tmp_path / f"{top}.tar.gz"
    with tarfile.open(archive, "w:gz") as t:
        for name, data in [*files.items(), ("manifest.json", json.dumps(manifest).encode())]:
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
        if extra is not None:
            info = tarfile.TarInfo(extra[0])
            info.size = len(extra[1])
            t.addfile(info, io.BytesIO(extra[1]))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    return ReleaseAsset("Hydro74000/muxiveo-plugins", "tag", archive.name, archive.as_uri(), digest)


def test_locations_stay_outside_the_application(tmp_path):
    env = {"XDG_DATA_HOME": str(tmp_path / "data"), "XDG_CACHE_HOME": str(tmp_path / "cache")}
    assert real_plugins_root(env, "linux") == tmp_path / "data" / "muxiveo" / "plugins"
    assert real_trt_engine_cache_dir(env, "linux") == tmp_path / "cache" / "muxiveo" / "trt-engines"
    win = {"LOCALAPPDATA": "C:/Users/u/AppData/Local"}
    assert real_plugins_root(win, "win32") == Path("C:/Users/u/AppData/Local/Muxiveo/plugins")
    assert real_trt_engine_cache_dir(win, "win32") == Path("C:/Users/u/AppData/Local/Muxiveo/cache/trt-engines")


def test_platform_tag_limits_to_linux_and_windows_x86_64():
    assert real_platform_tag("linux", "x86_64") == "linux-x86_64"
    assert real_platform_tag("win32", "AMD64") == "windows-x86_64"
    assert real_platform_tag("darwin", "arm64") is None
    assert real_platform_tag("linux", "aarch64") is None


def test_install_activates_verified_version_and_keeps_engine_cache_separate(tmp_path):
    root = tmp_path / "plugins"
    progress: list[tuple[int, int]] = []
    installed = plugins.install(root, asset=_archive(tmp_path), progress=lambda d, t: progress.append((d, t)))
    assert installed.version == MVO_RIFE_TRT_VERSION
    assert (installed.path / LIB).read_bytes() == b"bibliotheque"
    assert plugins.installed_plugin(root) == installed
    assert progress and progress[-1][0] > 0
    pointer = json.loads((root / plugins.TRT_PLUGIN_ID / "current.json").read_text())
    assert pointer["version"] == MVO_RIFE_TRT_VERSION
    assert not plugins.update_available(root)
    # aucun reste de téléchargement ni dossier temporaire
    leftovers = [p.name for p in (root / plugins.TRT_PLUGIN_ID).iterdir()]
    assert sorted(leftovers) == sorted(["current.json", installed.path.name])


@pytest.mark.parametrize("problem", ["sha", "tamper", "abi", "version", "traversal"])
def test_invalid_archives_are_refused_and_previous_version_kept(tmp_path, problem):
    root = tmp_path / "plugins"
    ok = tmp_path / "ok"
    ok.mkdir()
    previous = plugins.install(root, asset=_archive(ok))
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    if problem == "sha":
        asset = _archive(bad_dir)
        asset = ReleaseAsset(asset.repo, asset.tag, asset.name, asset.url, "0" * 64)
    elif problem == "tamper":
        asset = _archive(bad_dir, tamper=True)
    elif problem == "abi":
        asset = _archive(bad_dir, abi=plugins.TRT_PLUGIN_ABI + 1)
    elif problem == "version":
        asset = _archive(bad_dir, version="9.9.9")
    else:
        asset = _archive(bad_dir, extra=("../evasion.txt", b"x"))
    with pytest.raises(plugins.PluginError):
        plugins.install(root, asset=asset)
    assert plugins.installed_plugin(root) == previous
    assert not (tmp_path / "evasion.txt").exists()
    names = [p.name for p in (root / plugins.TRT_PLUGIN_ID).iterdir()]
    assert not any(n.startswith((".download-", ".staging-")) for n in names)


def test_reinstall_replaces_previous_directory(tmp_path):
    root = tmp_path / "plugins"
    first = plugins.install(root, asset=_archive(tmp_path))
    second = plugins.install(root, asset=_archive(tmp_path))
    assert second.path != first.path and not first.path.exists()
    assert plugins.installed_plugin(root) == second


def test_version_change_clears_engine_cache_but_reinstall_keeps_it(tmp_path):
    root = tmp_path / "plugins"
    cache = tmp_path / "trt-engines"
    plugins.install(root, asset=_archive(tmp_path), cache_dir=cache)
    cache.mkdir()
    (cache / "moteur.engine").write_bytes(b"x")
    plugins.install(root, asset=_archive(tmp_path), cache_dir=cache)
    assert (cache / "moteur.engine").exists()
    pointer = root / plugins.TRT_PLUGIN_ID / "current.json"
    pointer.write_text(json.dumps({**json.loads(pointer.read_text()), "version": "0.9.0"}))
    plugins.install(root, asset=_archive(tmp_path), cache_dir=cache)
    assert not cache.exists()


def test_cancelled_download_installs_nothing(tmp_path):
    root = tmp_path / "plugins"
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(plugins.PluginCancelled):
        plugins.install(root, asset=_archive(tmp_path), cancel=cancel)
    assert plugins.installed_plugin(root) is None


def test_remove_deletes_plugin_and_engine_cache(tmp_path):
    root = tmp_path / "plugins"
    cache = tmp_path / "trt-engines"
    plugins.install(root, asset=_archive(tmp_path))
    cache.mkdir()
    (cache / "moteur.engine").write_bytes(b"x")
    assert plugins.remove(root, cache)
    assert plugins.installed_plugin(root) is None
    assert not (root / plugins.TRT_PLUGIN_ID).exists() and not cache.exists()


def test_broken_pointer_means_not_installed(tmp_path):
    root = tmp_path / "plugins"
    installed = plugins.install(root, asset=_archive(tmp_path))
    (root / plugins.TRT_PLUGIN_ID / "current.json").write_text('{"version": "1.0.0", "dir": "../ailleurs"}')
    assert plugins.installed_plugin(root) is None
    (root / plugins.TRT_PLUGIN_ID / "current.json").write_text(json.dumps({"version": "1", "dir": installed.path.name}))
    (installed.path / LIB).unlink()
    assert plugins.installed_plugin(root) is None


def test_warm_up_builds_each_model_with_its_rife_model(tmp_path):
    rife_dir = tmp_path / "rife"
    (rife_dir / "rife-models" / "rife-v4.6").mkdir(parents=True)
    (rife_dir / "rife-models" / "rife-v4.6" / "flownet.param").write_text("")
    rife = rife_dir / "muxiveo-rife"
    rife.write_text("")
    plugin = plugins.InstalledPlugin("1.0.0", tmp_path / "plugin", {"models": ["rife-v4.6", "rife-v4.6-uhd", "rife-v4.15"]})
    calls: list[list[str]] = []

    def fake_run(cmd, **_kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 3 if "--uhd" in cmd else 0, stdout="", stderr="error: refusé\n")

    with patch("core.plugins.subprocess.run", side_effect=fake_run):
        failures = plugins.warm_up(str(rife), plugin, tmp_path / "cache")
    assert len(calls) == 2  # rife-v4.15 sans modèle ncnn : ignoré
    assert all(c[c.index("--backend") + 1] == "tensorrt" and c[c.index("--trt-plugin") + 1] == str(plugin.path) for c in calls)
    assert "--uhd" in calls[1] and "--uhd" not in calls[0]
    assert failures == ["rife-v4.6-uhd : error: refusé"]


def test_rife_stage_passes_plugin_only_when_given():
    plain = build_rife_stage("r", quality="balanced", source=InterpolationSource())
    assert "--trt-plugin" not in plain
    trt = build_rife_stage("r", quality="balanced", source=InterpolationSource(), trt_plugin="/p", trt_cache="/c")
    assert trt[trt.index("--trt-plugin") + 1] == "/p" and trt[trt.index("--trt-cache") + 1] == "/c"


def test_workflow_passes_plugin_only_to_recent_rife(qt_app, tmp_path):
    from core.workflows.encode import EncodeWorkflow

    rife = tmp_path / "muxiveo-rife"
    rife.write_text("")
    wf = EncodeWorkflow(ffmpeg_bin="ffmpeg", rife_bin=str(rife))
    wf.set_rife_trt_plugin(tmp_path / "plugin", tmp_path / "cache")
    with patch("core.workflows.encode.workflow._rife_version", return_value=(1, 3, 0)):
        assert wf._rife_trt_args() == ("", "")
    with patch("core.workflows.encode.workflow._rife_version", return_value=(1, 4, 0)):
        assert wf._rife_trt_args() == (str(tmp_path / "plugin"), str(tmp_path / "cache"))
    wf.set_rife_trt_plugin(None)
    with patch("core.workflows.encode.workflow._rife_version", return_value=(1, 4, 0)):
        assert wf._rife_trt_args() == ("", "")


def test_acceleration_probe_reads_nvof_and_tensorrt(tmp_path):
    binary = tmp_path / "muxiveo-rife"
    binary.write_text("")
    listing = json.dumps({"default": 0, "gpus": [{
        "index": 0, "name": "RTX", "nvof": True, "nvof_status": "disponible",
        "trt_compatible": True, "trt": True, "trt_status": "prêt (1.0.0)",
    }]})
    done = subprocess.CompletedProcess([], 0, stdout=listing, stderr="")
    with patch("core.workflows.encode.interpolation.rife_version", return_value=(1, 4, 0)), \
            patch("core.workflows.encode.interpolation.subprocess.run", return_value=done) as run:
        accel = real_detect_gpu_acceleration(str(binary), trt_plugin="/p")
    assert run.call_args.args[0][1:] == ["--list-gpus", "--trt-plugin", "/p"]
    assert accel.nvof.available and accel.trt.compatible and accel.trt.ready and accel.trt.device == "RTX"
    with patch("core.workflows.encode.interpolation.rife_version", return_value=(1, 3, 0)), \
            patch("core.workflows.encode.interpolation.subprocess.run", return_value=done) as run:
        old = real_detect_gpu_acceleration(str(binary), trt_plugin="/p")
    assert "--trt-plugin" not in run.call_args.args[0]
    assert not old.trt.compatible and "1.4.0" in old.trt.reason and old.nvof.available


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------

def _state(**kw):
    from ui.plugin_controller import TrtState

    return TrtState(**kw)


def test_state_visibility_and_readiness(tmp_path):
    compatible = TrtCapability(compatible=True, ready=False, device="RTX")
    assert not _state().visible
    assert not _state(capability=TrtCapability(reason="GPU non NVIDIA")).visible
    assert _state(capability=compatible).visible
    installed = plugins.InstalledPlugin(MVO_RIFE_TRT_VERSION, tmp_path, {})
    # installée : visible même si le GPU a changé (pour pouvoir la supprimer)
    assert _state(installed=installed).visible
    assert _state(installed=installed, capability=TrtCapability(compatible=True, ready=True)).ready
    assert _state(installed=plugins.InstalledPlugin("0.9.0", tmp_path, {})).update_available


def test_controller_plugin_dir_follows_setting(qt_app, tmp_path):
    from core.config import AppConfig
    from ui.plugin_controller import TrtPluginController

    config = AppConfig()
    ctrl = TrtPluginController(config)
    installed = plugins.InstalledPlugin(MVO_RIFE_TRT_VERSION, tmp_path, {})
    ctrl._state = _state(installed=installed)
    config.trt_enabled = True
    assert ctrl.plugin_dir() == str(tmp_path)
    config.trt_enabled = False
    assert ctrl.plugin_dir() == ""
    ctrl.shutdown()


def test_dashboard_tensorrt_badge(qt_app, tmp_path):
    from ui.main_window import DashboardPage

    badge = SimpleNamespace(text="", tip="", visible=False)
    badge.hide = lambda: setattr(badge, "visible", False)  # type: ignore[attr-defined]
    badge.show = lambda: setattr(badge, "visible", True)  # type: ignore[attr-defined]
    badge.setToolTip = lambda tip: setattr(badge, "tip", tip)  # type: ignore[attr-defined]
    page = SimpleNamespace(
        _trt_badge=badge,
        _apply_encoder_badge_state=lambda b, label, state: setattr(b, "text", f"{label}:{state}"),
    )
    DashboardPage.set_trt_state(cast(Any, page), _state(capability=TrtCapability(reason="GPU non NVIDIA")))
    assert not badge.visible
    DashboardPage.set_trt_state(cast(Any, page), _state(capability=TrtCapability(compatible=True, device="RTX")))
    assert badge.visible and badge.text == "TensorRT +:pending" and "RTX" in badge.tip
    installed = plugins.InstalledPlugin(MVO_RIFE_TRT_VERSION, tmp_path, {})
    DashboardPage.set_trt_state(cast(Any, page), _state(
        installed=installed, capability=TrtCapability(compatible=True, ready=True, device="RTX")))
    assert badge.text == "TensorRT:available"
    DashboardPage.set_trt_state(cast(Any, page), _state(
        installed=installed, capability=TrtCapability(compatible=True, reason="pilote trop ancien")))
    assert badge.text == "TensorRT:unavailable" and "pilote trop ancien" in badge.tip
    DashboardPage.set_trt_state(cast(Any, page), _state(
        capability=TrtCapability(compatible=True), busy="install", progress=42))
    assert badge.text == "TensorRT 42 %:pending"


def test_settings_extensions_section(qt_app, tmp_path):
    from core.config import AppConfig
    from ui.panels.settings_panel import SettingsPanel

    panel = SettingsPanel(AppConfig())
    assert panel._extensions_card is not None
    panel.set_trt_state(_state(capability=TrtCapability(reason="GPU non NVIDIA")))
    assert panel._extensions_card.isHidden()
    panel.set_trt_state(_state(capability=TrtCapability(compatible=True, device="RTX")))
    assert not panel._extensions_card.isHidden()
    assert not panel._trt_install_btn.isHidden() and panel._trt_remove_btn.isHidden()
    installed = plugins.InstalledPlugin(MVO_RIFE_TRT_VERSION, tmp_path, {})
    panel.set_trt_state(_state(installed=installed, capability=TrtCapability(compatible=True, ready=True, device="RTX")))
    assert panel._trt_install_btn.isHidden() and not panel._trt_remove_btn.isHidden()
    assert "RTX" in panel._trt_status.text()
    panel.close()


def test_encode_hint_shown_once_on_compatible_machine(qt_app, tmp_path):
    from core.config import AppConfig
    from ui.panels.encode_panel.panel import EncodePanel

    config = AppConfig()
    config.trt_hint_shown = False
    panel = EncodePanel(config)
    panel._interp_cb.setEnabled(True)
    panel._interp_cb.setChecked(True)
    panel.set_trt_state(_state(capability=TrtCapability(compatible=True, device="RTX")))
    assert not panel._trt_hint.isHidden() and config.trt_hint_shown
    panel.set_trt_state(_state(capability=TrtCapability(reason="GPU non NVIDIA")))
    assert panel._trt_hint.isHidden()
    panel.set_trt_state(_state(capability=TrtCapability(compatible=True, device="RTX")))
    assert panel._trt_hint.isHidden()  # déjà montrée une fois
    panel.close()


def test_nvof_capability_still_reported():
    assert NvofCapability(reason="x").reason == "x"
