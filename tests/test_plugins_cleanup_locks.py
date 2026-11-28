"""
tests/test_plugins_cleanup_locks.py — Extensions : nettoyage sûr entre processus (N2 ; plan de remédiation 5.2.0,
lot 8).

Verrou d'installation ``<racine>/.locks/<id>.lock`` détenu pendant toute modification ; nettoyage non bloquant ;
baux d'usage (moteur, accélération, sondes) respectés par le nettoyage et par ``remove``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core import plugins
from tests.test_plugins import _archive

_HOLD = """
import sys, time
from pathlib import Path
sys.path.insert(0, {repo!r})
from core.file_lock import FileLock
{setup}
print("pret", flush=True)
time.sleep(30)
"""


def _hold(tmp_path: Path, setup: str) -> subprocess.Popen:
    """Second processus réel qui tient un verrou ou un bail, jusqu'à ce qu'il soit tué."""
    script = tmp_path / f"hold-{time.monotonic_ns()}.py"
    script.write_text(_HOLD.format(repo=str(Path(__file__).resolve().parents[1]), setup=setup), encoding="utf-8")
    process = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True)
    assert process.stdout is not None and process.stdout.readline().strip() == "pret"
    return process


def _stop(process: subprocess.Popen) -> None:
    process.kill()
    process.wait(10)


def _installed(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "plugins"
    current = plugins.install(plugins.TRT, root, asset=_archive(tmp_path)).path
    base = root / plugins.TRT_PLUGIN_ID
    old = base / "0.9.0-ancienne"
    old.mkdir()
    (old / "manifest.json").write_text("{}")
    (base / ".staging-autre").mkdir()
    (base / ".download-autre.part").write_bytes(b"x")
    return root, base, current


def test_cleanup_is_a_noop_while_another_process_modifies_the_extension(tmp_path):
    root, base, current = _installed(tmp_path)
    lock = root / ".locks" / f"{plugins.TRT_PLUGIN_ID}.lock"
    holder = _hold(tmp_path, f"lock = FileLock(Path({str(lock)!r})); assert lock.try_acquire()")
    try:
        plugins.cleanup_orphans(plugins.TRT, root)  # installation en cours ailleurs : rien n'est touché
        assert (base / "0.9.0-ancienne").is_dir() and (base / ".staging-autre").is_dir()
        assert (base / ".download-autre.part").is_file() and current.is_dir()
    finally:
        _stop(holder)
    plugins.cleanup_orphans(plugins.TRT, root)
    assert not (base / "0.9.0-ancienne").exists() and not (base / ".staging-autre").exists()
    assert not (base / ".download-autre.part").exists() and current.is_dir()


def test_remove_waits_then_refuses_while_another_process_installs(tmp_path, monkeypatch):
    root, base, _current = _installed(tmp_path)
    monkeypatch.setattr(plugins, "_LOCK_WAIT_S", 0.3)
    lock = root / ".locks" / f"{plugins.TRT_PLUGIN_ID}.lock"
    holder = _hold(tmp_path, f"lock = FileLock(Path({str(lock)!r})); assert lock.try_acquire()")
    try:
        with pytest.raises(plugins.PluginError):
            plugins.remove(plugins.TRT, root)
        assert plugins.installed_plugin(plugins.TRT, root) is not None
    finally:
        _stop(holder)
    assert plugins.remove(plugins.TRT, root) and not base.exists()
    assert (root / ".locks").is_dir()  # verrous hors des dossiers supprimés


def test_remove_keeps_a_leased_version_until_its_lease_ends(tmp_path):
    root, base, current = _installed(tmp_path)
    holder = _hold(tmp_path, "from core import plugins\n"
                             f"lease = plugins.PluginLease(Path({str(current)!r}))")
    try:
        assert plugins.remove(plugins.TRT, root) is False  # version utilisée par un autre processus
        assert current.is_dir() and not (base / "0.9.0-ancienne").exists()
    finally:
        _stop(holder)
    plugins.cleanup_orphans(plugins.TRT, root)
    assert not current.exists()


def test_cleanup_keeps_a_leased_old_rife_version(tmp_path):
    root, base, _current = _installed(tmp_path)
    old = base / "0.9.0-ancienne"
    lease = plugins.lease_for(old / "muxiveo-rife", root)
    assert lease is not None
    try:
        plugins.cleanup_orphans(plugins.TRT, root)
        assert old.is_dir()
    finally:
        lease.release()
    plugins.cleanup_orphans(plugins.TRT, root)
    assert not old.exists()


def test_lease_for_paths_outside_the_user_extensions(tmp_path):
    root = tmp_path / "plugins"
    root.mkdir()
    assert plugins.lease_for("/usr/bin/muxiveo-rife", root) is None
    assert plugins.lease_for(None, root) is None
    assert plugins.leases_for("/usr/bin/x", "", root=root) == []


def test_encode_leases_the_engine_version_for_the_whole_job(tmp_path, monkeypatch, qt_app):
    from core.workflows.encode.models import EncodeConfig, FrameInterpolationSettings, VideoEncodeSettings
    from core.workflows.encode.workflow import EncodeWorkflow

    root, base, _current = _installed(tmp_path)
    old = base / "0.9.0-ancienne"
    engine = old / "muxiveo-rife"
    engine.write_text("#!/bin/sh\n")
    monkeypatch.setattr(plugins, "plugins_root", lambda *_a, **_k: root)
    video = VideoEncodeSettings(codec="libx265", interpolation=FrameInterpolationSettings(enabled=True, factor=2))
    config = EncodeConfig(source=tmp_path / "s.mkv", output=tmp_path / "o.mkv", video=video)
    workflow = EncodeWorkflow(rife_bin=str(engine))
    leases = workflow._engine_leases(config)
    try:
        assert len(leases) == 1
        plugins.cleanup_orphans(plugins.TRT, root)
        assert old.is_dir()  # moteur loué : jamais retiré pendant le job
    finally:
        for lease in leases:
            lease.release()
    plain = EncodeConfig(source=tmp_path / "s.mkv", output=tmp_path / "o.mkv",
                         video=VideoEncodeSettings(codec="libx265"))
    assert workflow._engine_leases(plain) == []


def test_install_holds_the_lock_for_the_whole_download(tmp_path, monkeypatch):
    """Pendant le téléchargement d'``install`` : verrou tenu, un nettoyage d'un autre processus ne touche rien."""
    from core.file_lock import lock_is_free

    root = tmp_path / "plugins"
    asset = _archive(tmp_path)
    original = plugins._download
    seen: list[bool] = []

    def download(asset_, dest, progress, cancel):
        lock = root / ".locks" / f"{plugins.TRT_PLUGIN_ID}.lock"
        seen.append(not lock_is_free(lock))
        script = tmp_path / "cleanup.py"
        script.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
            "from pathlib import Path\nfrom core import plugins\n"
            f"plugins.cleanup_orphans(plugins.TRT, Path({str(root)!r}))\n", encoding="utf-8")
        subprocess.run([sys.executable, str(script)], check=True, timeout=30)
        assert Path(str(dest)).parent.is_dir()
        return original(asset_, dest, progress, cancel)

    monkeypatch.setattr(plugins, "_download", download)
    installed = plugins.install(plugins.TRT, root, asset=asset)
    assert seen == [True] and installed.path.is_dir()


def test_encode_releases_its_leases_only_at_the_end_of_the_job(tmp_path, monkeypatch, qt_app):
    from core.runner import TaskSignals
    from core.workflows.encode.models import EncodeConfig, FrameInterpolationSettings, VideoEncodeSettings
    from core.workflows.encode.workflow import EncodeWorkflow

    root, base, _current = _installed(tmp_path)
    old = base / "0.9.0-ancienne"
    engine = old / "muxiveo-rife"
    engine.write_text("#!/bin/sh\n")
    monkeypatch.setattr(plugins, "plugins_root", lambda *_a, **_k: root)
    signals = TaskSignals()
    workflow = EncodeWorkflow(rife_bin=str(engine))
    monkeypatch.setattr(workflow, "_run_with_preparation", lambda *a, **k: signals)
    video = VideoEncodeSettings(codec="libx265", interpolation=FrameInterpolationSettings(enabled=True, factor=2))
    workflow.run(EncodeConfig(source=tmp_path / "s.mkv", output=tmp_path / "o.mkv", video=video))
    plugins.cleanup_orphans(plugins.TRT, root)
    assert old.is_dir()  # job en cours : moteur loué
    signals.finished.emit("ok")
    qt_app.processEvents()
    plugins.cleanup_orphans(plugins.TRT, root)
    assert not old.exists()  # job terminé : bail libéré, version retirée au nettoyage suivant


def test_probe_of_a_bare_engine_name_leases_the_resolved_binary(tmp_path, monkeypatch):
    from core.workflows.encode import interpolation

    root, base, _current = _installed(tmp_path)
    old = base / "0.9.0-ancienne"
    engine = old / "muxiveo-rife"
    engine.write_text("#!/bin/sh\n")
    engine.chmod(0o755)
    monkeypatch.setattr(plugins, "plugins_root", lambda *_a, **_k: root)
    monkeypatch.setenv("PATH", str(old))
    seen: list[list[str]] = []

    def detection(cmd, **_kwargs):
        seen.append(cmd)
        plugins.cleanup_orphans(plugins.TRT, root)
        assert old.is_dir()  # sonde en cours : version louée
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(interpolation, "run_detection", detection)
    interpolation._engine_probe(["muxiveo-rife", "--version"], capture_output=True)
    assert seen[0][0] == str(engine)  # lancé sous le chemin loué, jamais le nom nu


def test_probe_through_an_empty_path_segment_runs_an_absolute_path(tmp_path, monkeypatch):
    """Segment vide de ``PATH`` (dossier courant) : ``which`` rend un nom nu, lancé sous son chemin absolu."""
    from core.workflows.encode import interpolation

    engine = tmp_path / "muxiveo-rife"
    engine.write_text("#!/bin/sh\n")
    engine.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep)  # deux segments vides
    monkeypatch.setattr(plugins, "leases_for", lambda *_a, **_k: [])
    seen: list[list[str]] = []
    monkeypatch.setattr(interpolation, "run_detection",
                        lambda cmd, **_k: seen.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    interpolation._engine_probe(["muxiveo-rife", "--version"], capture_output=True)
    assert seen[0][0] == str(engine)
