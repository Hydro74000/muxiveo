"""Mise à jour par échange de dossiers (core/update_apply.py) : archive source, application macOS.

Bibliothèque standard seule (fausse application, aucun Qt) : aussi exécuté sur Linux, Windows et macOS par
`.github/workflows/update-tests.yml` (`pytest --noconftest`).
"""

from __future__ import annotations

import io
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

from core import update_apply
from core.update_apply import UpdateApplyError

_FAKE_MAIN = '''import sys
from pathlib import Path
VERSION = {version!r}
FAIL_INSTALLED = {fail_installed!r}
if sys.argv[1:] == ["--cli", "version"]:
    if FAIL_INSTALLED and Path(__file__).parent.name == "Muxiveo":
        sys.exit("version installée défaillante")
    print("Muxiveo " + VERSION)
    sys.exit(0)
Path(__file__).with_name("launched.txt").write_text(VERSION)
'''


def _release_files(version: str, *, fail_installed: bool = False, requirements: str = "PySide6\n",
                   extra: dict[str, str] | None = None) -> dict[str, str]:
    files = {
        "main.py": _FAKE_MAIN.format(version=version, fail_installed=fail_installed),
        "launcher.py": "",
        "core/__init__.py": "",
        "core/version.py": f"APP_VERSION = {version!r}\n",
        "requirements.txt": requirements,
        "docs/notes.md": "notes",
        "config.ini": f"# modèle {version}\n",                     # livré comme dans le dépôt réel
        **(extra or {}),
    }
    marker = {"schema": 1, "version": version, "files": sorted(files)}
    return {**files, update_apply.MARKER_NAME: json.dumps(marker)}


def _write_tree(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _archive(path: Path, files: dict[str, str], prefix: str) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for name, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(f"{prefix}/{name}")
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return path


def _source_setup(tmp_path: Path, new_files: dict[str, str]) -> tuple[Path, Path, Path]:
    """Installation 1.0.0 avec éléments de l'utilisateur, archive 2.0.0, dossier de travail voisin."""
    parent = tmp_path / "apps"
    install = parent / "Muxiveo"
    install.mkdir(parents=True)
    _write_tree(install, _release_files("1.0.0", extra={"core/old_module.py": "OLD = 1\n"}))
    (install / "config.ini").write_text("[ui]\ntheme = light\n", encoding="utf-8")       # Windows : écrit par le setup
    (install / ".venv" / "bin").mkdir(parents=True)
    (install / ".venv" / "bin" / "python").write_text("venv", encoding="utf-8")
    (install / "core" / "mine.py").write_text("MINE = 1\n", encoding="utf-8")
    (install / "core" / "__pycache__").mkdir()
    (install / "core" / "__pycache__" / "version.cpython-312.pyc").write_bytes(b"\0")
    (install / "docs" / "notes.md").write_text("notes modifiées", encoding="utf-8")       # livré : remplacé
    work = parent / f"{update_apply.WORKDIR_PREFIX}test"
    work.mkdir()
    (work / update_apply.WORKDIR_TAG).touch()
    archive = _archive(work / "Muxiveo-2.0.0-source.tar.gz", new_files, "Muxiveo-2.0.0")
    return install, work, archive


def _prepare_source(install: Path, work: Path, archive: Path) -> dict:
    root = update_apply.ROOT_TOKEN
    return update_apply.prepare(
        update_apply.KIND_SOURCE, archive, work, install, "2.0.0",
        [sys.executable, os.path.join(root, "main.py")], [sys.executable, os.path.join(root, "main.py")],
    )


def _exited_pid() -> int:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def _wait_file(path: Path, timeout: float = 15.0) -> str:
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, f"{path} absent"
        time.sleep(0.05)
    time.sleep(0.05)
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- extraction


@pytest.mark.parametrize("name", ["../x", "/etc/passwd", "a/../../x", "a\\b", "C:x", "a/con.txt", "a/b.", "a/b ", "a/\tb"])
def test_safe_relative_rejects_escapes_and_ambiguous_names(name):
    with pytest.raises(UpdateApplyError):
        update_apply.safe_relative(name)


def test_extract_refuses_links_traversal_and_duplicates(tmp_path):
    def build(path: Path, members: list[tarfile.TarInfo]) -> Path:
        with tarfile.open(path, "w:gz") as tar:
            for member in members:
                tar.addfile(member, io.BytesIO(b"x" * member.size) if member.isfile() else None)
        return path

    link = tarfile.TarInfo("M/lien")
    link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
    escape = tarfile.TarInfo("M/../../evil")
    escape.size = 1
    first, second = tarfile.TarInfo("M/Fichier"), tarfile.TarInfo("M/fichier")
    first.size = second.size = 1
    for index, members in enumerate(([link], [escape], [first, second])):
        with pytest.raises(UpdateApplyError):
            update_apply.extract_tar(build(tmp_path / f"a{index}.tar.gz", members), tmp_path / f"out{index}")
        assert not (tmp_path.parent / "evil").exists()


def test_carry_over_plan_keeps_everything_not_shipped(tmp_path):
    install, _work, _archive_path = _source_setup(tmp_path, _release_files("2.0.0"))
    shipped = set(json.loads((install / update_apply.MARKER_NAME).read_text())["files"])
    assert update_apply.carry_over_plan(install, shipped) == [".venv", "config.ini", "core/mine.py"]


# --------------------------------------------------------------------------- échange


@pytest.mark.parametrize("atomic", [True, False])
def test_source_update_swaps_carries_user_files_and_relaunches(tmp_path, monkeypatch, atomic):
    if not atomic:
        monkeypatch.setattr(update_apply, "_atomic_exchange", lambda left, right: False)
    install, work, archive = _source_setup(tmp_path, _release_files("2.0.0", requirements="PySide6>=6.7\n"))
    doc = _prepare_source(install, work, archive)
    assert doc["requirements_changed"] and not archive.exists()

    assert update_apply.commit(work, _exited_pid()) == 0
    assert _wait_file(install / "launched.txt") == "2.0.0"                 # nouvelle version relancée
    assert "2.0.0" in (install / "core" / "version.py").read_text()
    assert (install / "config.ini").read_text(encoding="utf-8").endswith("light\n")
    assert (install / ".venv" / "bin" / "python").is_file() and (install / "core" / "mine.py").is_file()
    assert not (install / "core" / "old_module.py").exists()                # supprimé par la nouvelle version
    assert (install / "docs" / "notes.md").read_text(encoding="utf-8") == "notes"
    result = json.loads((work / update_apply.RESULT_FILE).read_text(encoding="utf-8"))
    backup = Path(result["backup"])
    assert result["ok"] and result["requirements_changed"] and result["kept"] == [] and backup == work / "previous"
    assert (backup / "core" / "old_module.py").is_file()                    # ancienne version conservée
    assert (backup / "docs" / "notes.md").read_text(encoding="utf-8") == "notes modifiées"


def test_failed_installed_probe_rolls_back_and_relaunches_previous(tmp_path):
    install, work, archive = _source_setup(tmp_path, _release_files("2.0.0", fail_installed=True))
    _prepare_source(install, work, archive)                                 # le candidat, lui, démarre

    assert update_apply.commit(work, _exited_pid()) == 1
    assert _wait_file(install / "launched.txt") == "1.0.0"                 # ancienne version relancée
    assert (install / "core" / "old_module.py").is_file() and (install / "config.ini").is_file()
    assert (install / ".venv" / "bin" / "python").is_file()
    result = json.loads((work / update_apply.RESULT_FILE).read_text(encoding="utf-8"))
    assert not result["ok"] and "défaillante" in result["error"] and not result["rollback_incomplete"]


def _fail_renames(monkeypatch, failing: set[tuple[str, str]]) -> None:
    """Échange sans renommage atomique ; chaque couple (source, cible) de `failing` échoue une fois."""
    monkeypatch.setattr(update_apply, "_atomic_exchange", lambda left, right: False)
    real_rename = os.rename

    def rename(source, target):
        key = (Path(source).name, Path(target).name)
        if key in failing:
            failing.discard(key)
            raise OSError(5, "refusé", str(source))
        real_rename(source, target)

    monkeypatch.setattr(update_apply.os, "rename", rename)


def test_third_rename_failure_restores_previous_install(tmp_path, monkeypatch):
    install, work, archive = _source_setup(tmp_path, _release_files("2.0.0"))
    _prepare_source(install, work, archive)
    _fail_renames(monkeypatch, {("swap", "Muxiveo-2.0.0")})                # 3ᵉ renommage de l'échange

    assert update_apply.commit(work, _exited_pid()) == 1
    assert _wait_file(install / "launched.txt") == "1.0.0"                 # ancienne version en place, relancée
    assert (install / "core" / "old_module.py").is_file() and (install / "config.ini").is_file()
    assert (install / ".venv" / "bin" / "python").is_file() and not (work / "swap").exists()
    result = json.loads((work / update_apply.RESULT_FILE).read_text(encoding="utf-8"))
    assert not result["ok"] and not result["rollback_incomplete"]


def test_unrecoverable_exchange_is_flagged_and_kept(tmp_path, monkeypatch):
    install, work, archive = _source_setup(tmp_path, _release_files("2.0.0"))
    _prepare_source(install, work, archive)
    _fail_renames(monkeypatch, {("swap", "Muxiveo-2.0.0"), ("Muxiveo", "Muxiveo-2.0.0")})   # échange puis retour

    assert update_apply.commit(work, _exited_pid()) == 1
    result = json.loads((work / update_apply.RESULT_FILE).read_text(encoding="utf-8"))
    assert result["rollback_incomplete"] and str(work) in result["error"]
    assert (work / "swap" / "core" / "old_module.py").is_file()             # ancienne version intacte
    update_apply.collect_results(work.parent, now=time.time() + 10 * 86400)
    assert (work / "swap").is_dir()                                          # jamais supprimée par le ménage


def test_application_still_running_changes_nothing(tmp_path):
    install, work, archive = _source_setup(tmp_path, _release_files("2.0.0"))
    _prepare_source(install, work, archive)
    running = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert update_apply.commit(work, running.pid, wait_timeout=0.5) == 1
    finally:
        running.kill()
        running.wait()
    assert (install / "core" / "old_module.py").is_file() and not (install / "launched.txt").exists()
    assert "pas fermée" in json.loads((work / update_apply.RESULT_FILE).read_text(encoding="utf-8"))["error"]


def test_prepare_refuses_unmarked_install_and_archive(tmp_path):
    install, work, archive = _source_setup(tmp_path, _release_files("2.0.0"))
    (install / update_apply.MARKER_NAME).unlink()
    with pytest.raises(UpdateApplyError, match="sans marqueur"):
        _prepare_source(install, work, archive)
    install, work, archive = _source_setup(tmp_path / "b", {
        name: content for name, content in _release_files("2.0.0").items() if name != update_apply.MARKER_NAME
    })
    with pytest.raises(UpdateApplyError, match="marqueur"):
        _prepare_source(install, work, archive)


def test_prepare_refuses_wrong_version(tmp_path):
    install, work, archive = _source_setup(tmp_path, _release_files("2.0.0"))
    with pytest.raises(UpdateApplyError, match="ne démarre pas"):
        root = update_apply.ROOT_TOKEN
        update_apply.prepare(update_apply.KIND_SOURCE, archive, work, install, "3.0.0",
                             [sys.executable, os.path.join(root, "main.py")], [])


def test_announced_version_compared_exactly(tmp_path):
    install, work, archive = _source_setup(tmp_path, _release_files("12.0.0"))
    with pytest.raises(UpdateApplyError, match="12.0.0 au lieu de 2.0.0"):
        _prepare_source(install, work, archive)


def test_helper_script_runs_isolated_from_a_copy(tmp_path):
    """Chemin réel : script copié dans le dossier de travail, `python -I`, détaché ; prêt avant la sortie."""
    install, work, archive = _source_setup(tmp_path, _release_files("2.0.0"))
    _prepare_source(install, work, archive)
    script = work / "update_apply.py"
    shutil.copyfile(update_apply.__file__, script)
    waiter = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1)"])
    helper = subprocess.Popen([sys.executable, "-I", str(script), "commit", str(work), "--pid", str(waiter.pid)],
                              cwd=work)
    _wait_file(work / update_apply.READY_FILE)
    waiter.wait()
    assert helper.wait(timeout=60) == 0
    assert _wait_file(install / "launched.txt") == "2.0.0"


# --------------------------------------------------------------------------- suivi


def test_collect_results_reports_once_and_cleans_up(tmp_path):
    parent = tmp_path
    now = time.time()

    def workdir(name: str, age: float, **files: object) -> Path:
        path = parent / f"{update_apply.WORKDIR_PREFIX}{name}"
        path.mkdir()
        (path / update_apply.WORKDIR_TAG).touch()
        for filename, content in files.items():
            (path / filename.replace("_", ".")).write_text(json.dumps(content) if content is not None else "")
        os.utime(path, (now - age, now - age))
        return path

    old_ok = workdir("a", 300, result_json={"ok": True, "version": "1.5.0"})
    failed = workdir("b", 200, result_json={"ok": False, "version": "1.9.0", "error": "x", "cleanup_safe": True})
    broken = workdir("c", 150, result_json={"ok": False, "version": "1.9.1", "rollback_incomplete": True})
    new_ok = workdir("d", 100, result_json={"ok": True, "version": "2.0.0"})
    abandoned = workdir("e", 7200)                                           # téléchargement abandonné
    running = workdir("f", 60, ready=None)                                   # échange en cours
    (parent / f"{update_apply.WORKDIR_PREFIX}foreign").mkdir()              # sans témoin : jamais touché

    # Suppression confiée à l'appelant : rien n'est retiré pendant la lecture.
    discard: list[Path] = []
    results = update_apply.collect_results(parent, now=now, discard=discard)
    assert sorted(discard) == sorted([old_ok, failed, abandoned]) and old_ok.exists()
    for path in discard:
        shutil.rmtree(path)
    assert [r["version"] for r in results] == ["1.5.0", "1.9.0", "1.9.1", "2.0.0"]
    assert update_apply.collect_results(parent, now=now) == []               # signalés une seule fois
    assert new_ok.exists() and broken.exists() and running.exists()
    assert not old_ok.exists() and not failed.exists() and not abandoned.exists()
    assert (parent / f"{update_apply.WORKDIR_PREFIX}foreign").exists()


def test_detached_cleanup_rechecks_backups_and_active_workdirs(tmp_path):
    """Vrai auxiliaire isolé : aucun import applicatif et aucun résultat marqué comme déjà signalé."""
    def workdir(name, age, result=None, ready=False, marked=True):
        work = tmp_path / f"{update_apply.WORKDIR_PREFIX}{name}"
        work.mkdir()
        if marked:
            (work / update_apply.WORKDIR_TAG).touch()
        if result is not None:
            (work / update_apply.RESULT_FILE).write_text(json.dumps(result), encoding="utf-8")
        if ready:
            (work / update_apply.READY_FILE).touch()
        os.utime(work, (time.time() - age, time.time() - age))
        return work

    old = workdir("old", 300, {"ok": True})
    latest = workdir("latest", 100, {"ok": True})
    active = workdir("active", 10, ready=True)
    broken = workdir("broken", 50, {"ok": False, "rollback_incomplete": True})
    ambiguous = workdir("ambiguous", 40, {"ok": False})
    corrupted = workdir("corrupted", 30, {"ok": False})
    (corrupted / update_apply.RESULT_FILE).write_text('{"ok": tr')
    personal = workdir("personal", 200, {"ok": True, "kept": ["notes.txt"]})
    for directory in (ambiguous, corrupted, personal):
        (directory / "previous").mkdir()
        (directory / "previous/notes.txt").write_text("personnel")
        os.utime(directory, (time.time() - 200, time.time() - 200))
    foreign = workdir("foreign", 1000, marked=False)
    helper = tmp_path / f"{update_apply.CLEANUP_PREFIX}test"
    helper.mkdir()
    paths = (old, latest, active, broken, ambiguous, corrupted, personal, foreign)
    (helper / update_apply.CLEANUP_FILE).write_text(json.dumps([str(path) for path in paths]))
    script = helper / "update_apply.py"
    shutil.copyfile(update_apply.__file__, script)
    result = subprocess.run([sys.executable, "-I", str(script), "cleanup", str(helper)], capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
    assert not old.exists() and not helper.exists()
    assert all(path.is_dir() for path in (latest, active, broken, foreign))
    assert all((path / "previous/notes.txt").read_text() == "personnel" for path in (ambiguous, corrupted, personal))
    assert not (latest / update_apply.REPORTED_FILE).exists()


def test_cleanup_interruption_keeps_workdir_marker(tmp_path, monkeypatch):
    work = tmp_path / f"{update_apply.WORKDIR_PREFIX}old"
    work.mkdir()
    (work / update_apply.WORKDIR_TAG).touch()
    (work / "previous").mkdir()
    monkeypatch.setattr(shutil, "rmtree", lambda *_a, **_kw: (_ for _ in ()).throw(OSError("Dossier utilisé")))
    update_apply._remove_workdir(work)
    assert (work / update_apply.WORKDIR_TAG).is_file()


@pytest.mark.parametrize("failure", ["once", "always", "relaunch", "none"])
def test_post_swap_failures_preserve_personal_collisions(tmp_path, monkeypatch, failure):
    install, work, archive = _source_setup(tmp_path, _release_files("2.0.0", extra={"notes.txt": "livré"}))
    (install / "notes.txt").write_text("personnel", encoding="utf-8")
    _prepare_source(install, work, archive)
    real_write = update_apply._write_result
    attempts = []
    launches = []

    def write(path, **fields):
        attempts.append(fields)
        if failure == "always" or (failure == "once" and len(attempts) == 1):
            raise OSError("disque indisponible")
        real_write(path, **fields)

    def launch(*_args):
        launches.append(True)
        if failure == "relaunch":
            raise OSError("relance impossible")

    monkeypatch.setattr(update_apply, "_write_result", write)
    monkeypatch.setattr(update_apply, "launch", launch)
    assert update_apply.commit(work, _exited_pid()) == (1 if failure in ("once", "always") else 0)
    assert launches == [True]
    assert "2.0.0" in (install / "core/version.py").read_text()
    assert (install / "notes.txt").read_text(encoding="utf-8") == "livré"
    assert json.loads((work / update_apply.TRANSACTION_FILE).read_text())["phase"] == "swapped"
    newer = tmp_path / "apps" / (update_apply.WORKDIR_PREFIX + "newer")
    newer.mkdir()
    (newer / update_apply.WORKDIR_TAG).touch()
    real_write(newer, ok=True)
    discard = []
    update_apply.collect_results(work.parent, now=time.time() + 10 * 86400, discard=discard)
    assert work not in discard
    update_apply.cleanup_stale([work])
    assert (work / "previous/notes.txt").read_text(encoding="utf-8") == "personnel"


@pytest.mark.parametrize("content", ['{"ok": tr', '[]', 'null', '{"ok": "true"}', '{"ok": false}'])
def test_ambiguous_result_never_discards_backup(tmp_path, content):
    work = tmp_path / (update_apply.WORKDIR_PREFIX + "broken")
    backup = work / "previous/notes.txt"
    backup.parent.mkdir(parents=True)
    backup.write_text("personnel")
    (work / update_apply.WORKDIR_TAG).touch()
    (work / update_apply.READY_FILE).touch()
    (work / update_apply.RESULT_FILE).write_text(content)
    assert update_apply.collect_results(tmp_path)[0]["ok"] is False
    update_apply.cleanup_stale([work])
    assert backup.read_text() == "personnel"


def test_result_publication_failure_leaves_previous_json_intact(tmp_path, monkeypatch):
    update_apply._write_result(tmp_path, ok=True, backup="previous")
    original = (tmp_path / update_apply.RESULT_FILE).read_bytes()

    def refuse(*_args):
        raise OSError("remplacement refusé")

    monkeypatch.setattr(update_apply.os, "replace", refuse)
    with pytest.raises(OSError):
        update_apply._write_result(tmp_path, ok=True, relaunch_error="échec")
    assert (tmp_path / update_apply.RESULT_FILE).read_bytes() == original
    assert list(tmp_path.iterdir()) == [tmp_path / update_apply.RESULT_FILE]


def test_cancel_during_tar_extraction_prevents_probe(tmp_path, monkeypatch):
    install, work, archive = _source_setup(tmp_path, _release_files("2.0.0"))
    calls = []

    def cancelled():
        calls.append(True)
        return len(calls) >= 4

    monkeypatch.setattr(update_apply, "probe", lambda *_a, **_kw: pytest.fail("Contrôle après annulation"))
    with pytest.raises(UpdateApplyError, match="annulée"):
        update_apply.prepare(update_apply.KIND_SOURCE, archive, work, install, "2.0.0", [], [], cancelled=cancelled)
    assert not (work / update_apply.PREPARED_FILE).exists()
    assert (install / "core/old_module.py").exists()


def test_cancel_during_candidate_probe_kills_and_reaps_process(tmp_path, monkeypatch):
    import threading

    install, work, archive = _source_setup(tmp_path, _release_files("2.0.0"))
    event = threading.Event()
    started = tmp_path / "probe-started"
    processes = []
    real_popen = subprocess.Popen

    def popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", popen)
    command = [sys.executable, "-c", "import pathlib,time; pathlib.Path(" + repr(str(started)) +
               ").touch(); time.sleep(30)"]

    def cancelled():
        if started.exists():
            event.set()
        return event.is_set()

    with pytest.raises(UpdateApplyError, match="annulée"):
        update_apply.prepare(update_apply.KIND_SOURCE, archive, work, install, "2.0.0", command, [], cancelled=cancelled)
    assert event.is_set() and len(processes) == 1 and processes[0].poll() is not None
    assert not (work / update_apply.PREPARED_FILE).exists()
    assert (install / "core/old_module.py").exists()


# --------------------------------------------------------------------------- macOS


@pytest.mark.skipif(sys.platform != "darwin", reason="hdiutil / ditto : macOS seulement")
def test_macos_app_update_from_dmg(tmp_path):
    def bundle(root: Path, version: str) -> Path:
        app = root / update_apply.MACOS_APP_NAME
        executable = app / "Contents" / "MacOS" / "Muxiveo"
        executable.parent.mkdir(parents=True)
        executable.write_text(
            "#!/bin/sh\n"
            f'if [ "$1 $2" = "--cli version" ]; then echo "Muxiveo {version}"; exit 0; fi\n'
            f'echo {version} > "$(dirname "$0")/../../../launched.txt"\n',
            encoding="utf-8",
        )
        executable.chmod(0o755)
        with (app / "Contents" / "Info.plist").open("wb") as out:
            plistlib.dump({"CFBundleIdentifier": "com.example.muxiveo", "CFBundleExecutable": "Muxiveo"}, out)
        return app

    parent = tmp_path / "Applications"
    parent.mkdir()
    install = bundle(parent, "1.0.0")
    staging = tmp_path / "staging"
    bundle(staging, "2.0.0")
    dmg = tmp_path / "Muxiveo-2.0.0.dmg"
    subprocess.run(["hdiutil", "create", "-quiet", "-volname", "Muxiveo", "-srcfolder", str(staging),
                    "-format", "UDZO", str(dmg)], check=True)
    work = parent / f"{update_apply.WORKDIR_PREFIX}mac"
    work.mkdir()
    (work / update_apply.WORKDIR_TAG).touch()
    shutil.copyfile(dmg, work / dmg.name)
    executable = os.path.join(update_apply.ROOT_TOKEN, "Contents", "MacOS", "Muxiveo")
    update_apply.prepare(update_apply.KIND_MACOS, work / dmg.name, work, install, "2.0.0",
                         [executable], [executable], bundle_id="com.example.muxiveo")
    assert update_apply.commit(work, _exited_pid()) == 0
    assert _wait_file(parent / "launched.txt").strip() == "2.0.0"
    assert "2.0.0" in (install / "Contents" / "MacOS" / "Muxiveo").read_text(encoding="utf-8")


@pytest.mark.parametrize("last_error, closed", [(87, True), (5, False)])
def test_windows_wait_needs_proof_that_the_application_exited(tmp_path, monkeypatch, last_error, closed):
    """OpenProcess refusé : seul ERROR_INVALID_PARAMETER (PID disparu) prouve la fermeture ;
    un accès refusé abandonne la mise à jour au lieu d'échanger sous l'application ouverte."""
    from types import SimpleNamespace

    kernel = SimpleNamespace(OpenProcess=lambda *_args: None)       # handle refusé
    fake_ctypes = SimpleNamespace(WinDLL=lambda *_a, **_k: kernel, get_last_error=lambda: last_error,
                                  c_void_p=object)
    monkeypatch.setattr(update_apply, "ctypes", fake_ctypes)
    monkeypatch.setattr(update_apply.os, "name", "nt")
    assert update_apply._wait_for_exit(1234, tmp_path, timeout=0.1) is closed
    assert (tmp_path / update_apply.READY_FILE).exists()
