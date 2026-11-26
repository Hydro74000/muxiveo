"""
tests/test_update_transactions.py — Mises à jour : transactions par installation, journal des comptes rendus sous
verrou (D3, C5 ; plan de remédiation 5.2.0, lot 7). Bibliothèque standard seule : exécuté aussi par la CI
``ci-update-process`` (trois OS, ``--noconftest``).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from core import update_apply


def _work(parent: Path, name: str, install: Path | None, result: dict | None = None, *, age: float = 0,
          legacy: bool = False) -> Path:
    work = parent / f"{update_apply.WORKDIR_PREFIX}{name}"
    work.mkdir()
    if legacy:  # format des versions antérieures : propriétaire lu dans prepared.json
        (work / update_apply.WORKDIR_TAG).touch()
        if install is not None:
            (work / update_apply.PREPARED_FILE).write_text(json.dumps({"install": str(install)}))
    else:
        assert install is not None
        update_apply.write_workdir_tag(work, install)
    if result is not None:
        (work / update_apply.RESULT_FILE).write_text(json.dumps(result))
    stamp = time.time() - age
    os.utime(work, (stamp, stamp))
    return work


def test_two_installations_under_one_parent_keep_their_own_backup_and_reports(tmp_path):
    """R4 du contre-audit : la seule sauvegarde de A était supprimable, les comptes rendus mêlés."""
    a, b = tmp_path / "A", tmp_path / "B"
    only_a = _work(tmp_path, "0", a, {"ok": True, "version": "5.2.0"}, age=100)
    _work(tmp_path, "1", b, {"ok": True, "version": "5.2.1"}, age=99)
    _work(tmp_path, "2", b, {"ok": True, "version": "5.2.2"}, age=98)
    discard: list[Path] = []
    reported = update_apply.collect_results(tmp_path, discard=discard, install=a)
    assert only_a not in discard and [entry["version"] for entry in reported] == ["5.2.0"]
    reported_b = update_apply.collect_results(tmp_path, discard=discard, install=b)
    assert sorted(entry["version"] for entry in reported_b) == ["5.2.1", "5.2.2"]
    assert [path.name for path in discard] == [f"{update_apply.WORKDIR_PREFIX}1"]  # remplacée chez B seulement


def test_legacy_and_current_workdirs_share_the_retention_of_their_install(tmp_path):
    install = tmp_path / "Muxiveo"
    old = _work(tmp_path, "old", install, {"ok": True, "version": "5.1.0"}, age=200, legacy=True)
    new = _work(tmp_path, "new", install, {"ok": True, "version": "5.2.0"}, age=100)
    discard: list[Path] = []
    update_apply.collect_results(tmp_path, discard=discard, install=install)
    assert discard == [old] and new not in discard


def test_unknown_owner_is_neither_reported_nor_removed(tmp_path):
    install = tmp_path / "Muxiveo"
    unknown = _work(tmp_path, "unknown", None, {"ok": True, "version": "5.0.0"}, age=9 * 86400, legacy=True)
    discard: list[Path] = []
    assert update_apply.collect_results(tmp_path, discard=discard, install=install) == []
    assert discard == [] and unknown.is_dir()
    abandoned = _work(tmp_path, "abandoned", None, age=7200, legacy=True)  # téléchargement abandonné : retiré
    update_apply.collect_results(tmp_path, install=install)
    assert not abandoned.exists() and unknown.is_dir()


def test_removed_unreported_workdir_keeps_its_report_in_the_journal(tmp_path):
    install = tmp_path / "Muxiveo"
    replaced = _work(tmp_path, "a", install, {"ok": True, "version": "5.1.0"}, age=200)
    _work(tmp_path, "b", install, {"ok": True, "version": "5.2.0"}, age=100)
    update_apply.collect_results(tmp_path, install=install, report=False)  # ménage sans signalement
    assert not replaced.exists()
    pending = update_apply.collect_results(tmp_path, install=install)
    assert sorted(entry["version"] for entry in pending) == ["5.1.0", "5.2.0"]
    update_apply.mark_reported(tmp_path, install, [entry["id"] for entry in pending])
    assert update_apply.collect_results(tmp_path, install=install) == []
    assert update_apply.pending_reports(tmp_path, install) == []


def test_many_unreported_reports_are_never_lost(tmp_path):
    install = tmp_path / "Muxiveo"
    for index in range(25):
        _work(tmp_path, f"{index:02d}", install, {"ok": True, "version": f"5.{index}.0"}, age=1000 - index)
    update_apply.collect_results(tmp_path, install=install, report=False)
    assert len(list(tmp_path.glob(f"{update_apply.WORKDIR_PREFIX}*"))) == 1  # dernière réussite gardée
    assert not update_apply.REPORTS_DIR.startswith(update_apply.WORKDIR_PREFIX)
    assert len(update_apply.collect_results(tmp_path, install=install)) == 25


def test_detached_cleanup_and_marking_never_lose_journal_entries(tmp_path):
    """Deux processus réels : l'auxiliaire copié lancé en ``python -I`` (ménage) et l'application (journal)."""
    install = tmp_path / "Muxiveo"
    works = [_work(tmp_path, f"{index:02d}", install, {"ok": True, "version": f"4.{index}.0"}, age=1000 - index)
             for index in range(30)]
    helper = tmp_path / f"{update_apply.CLEANUP_PREFIX}test"
    helper.mkdir()
    (helper / update_apply.CLEANUP_FILE).write_text(json.dumps({"install": str(install),
                                                                "paths": [str(work) for work in works]}))
    script = helper / "update_apply.py"
    shutil.copyfile(update_apply.__file__, script)
    process = subprocess.Popen([sys.executable, "-I", str(script), "cleanup", str(helper)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    owner = os.path.realpath(install)
    for index in range(40):  # écritures concurrentes du même journal depuis ce processus
        update_apply._keep_report(tmp_path, owner, {"id": f"synthetic-{index}", "ok": True, "version": "x"})
    _out, err = process.communicate(timeout=60)
    assert process.returncode == 0, err.decode()
    ids = {entry["id"] for entry in update_apply.pending_reports(tmp_path, install)}
    assert {f"synthetic-{index}" for index in range(40)} <= ids
    assert {work.name for work in works[:-1]} <= ids  # comptes rendus des 29 dossiers retirés conservés


def test_concurrent_marking_and_cleanup_never_resurrect_or_lose_a_report(tmp_path):
    """Signalement (application) pendant le ménage de l'auxiliaire copié lancé en ``python -I`` : un compte rendu
    signalé n'est jamais recopié ensuite, un compte rendu non signalé n'est jamais perdu."""
    install = tmp_path / "Muxiveo"
    works = [_work(tmp_path, f"{index:02d}", install, {"ok": True, "version": f"4.{index}.0", "finished": index},
                   age=1000 - index) for index in range(30)]
    helper = tmp_path / f"{update_apply.CLEANUP_PREFIX}test"
    helper.mkdir()
    (helper / update_apply.CLEANUP_FILE).write_text(json.dumps({"install": str(install),
                                                                "paths": [str(work) for work in works]}))
    script = helper / "update_apply.py"
    shutil.copyfile(update_apply.__file__, script)
    marked = [work.name for work in works[::2]]
    process = subprocess.Popen([sys.executable, "-I", str(script), "cleanup", str(helper)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    marker = threading.Thread(target=lambda: update_apply.mark_reported(tmp_path, install, marked))
    marker.start()
    marker.join(30)
    _out, err = process.communicate(timeout=60)
    assert process.returncode == 0, err.decode()
    pending = {entry["id"] for entry in update_apply.collect_results(tmp_path, install=install)}
    assert not pending & set(marked)  # signalés : jamais repris
    assert {work.name for work in works[1::2]} <= pending  # non signalés : tous gardés


def test_retention_ignores_the_reported_marker_date(tmp_path):
    """Le témoin ``reported`` change la date du dossier : la rétention suit l'heure de fin du résultat."""
    install = tmp_path / "Muxiveo"
    older = _work(tmp_path, "a", install, {"ok": True, "version": "5.1.0", "finished": 100.0}, age=50)
    newer = _work(tmp_path, "b", install, {"ok": True, "version": "5.2.0", "finished": 200.0}, age=100)
    (older / update_apply.REPORTED_FILE).touch()  # date du dossier « a » rafraîchie par un signalement tardif
    discard: list[Path] = []
    update_apply.collect_results(tmp_path, discard=discard, install=install, report=False)
    assert discard == [older] and newer not in discard


def test_unreadable_journal_is_never_overwritten_and_keeps_the_workdir(tmp_path):
    install = tmp_path / "Muxiveo"
    path, _lock = update_apply._journal_paths(tmp_path, os.path.realpath(install))
    path.parent.mkdir(parents=True)
    path.write_text("{ tronqué", encoding="utf-8")
    replaced = _work(tmp_path, "a", install, {"ok": True, "version": "5.1.0", "finished": 1.0}, age=200)
    _work(tmp_path, "b", install, {"ok": True, "version": "5.2.0", "finished": 2.0}, age=100)
    update_apply.collect_results(tmp_path, install=install, report=False)
    assert path.read_text(encoding="utf-8") == "{ tronqué"  # jamais remplacé par une liste vide
    assert replaced.is_dir()  # compte rendu non recopiable : dossier gardé


def test_journal_addition_beyond_the_readable_size_is_refused(tmp_path, monkeypatch):
    install = tmp_path / "Muxiveo"
    monkeypatch.setattr(update_apply, "MAX_REPORTS_BYTES", 200)
    replaced = _work(tmp_path, "a", install, {"ok": True, "version": "5.1.0", "finished": 1.0,
                                              "notes": "x" * 400}, age=200)
    _work(tmp_path, "b", install, {"ok": True, "version": "5.2.0", "finished": 2.0}, age=100)
    update_apply.collect_results(tmp_path, install=install, report=False)
    assert replaced.is_dir()  # journal plein : compte rendu laissé dans son dossier
    assert update_apply.pending_reports(tmp_path, install) == []


def test_a_workdir_removed_by_another_cleanup_is_never_resurrected(tmp_path):
    install = tmp_path / "Muxiveo"
    gone = tmp_path / f"{update_apply.WORKDIR_PREFIX}gone"  # retiré entre-temps (signalé ou recopié ailleurs)
    owner = os.path.realpath(install)
    assert update_apply._keep_report(tmp_path, owner, {"id": gone.name, "ok": True}, gone)
    assert update_apply.pending_reports(tmp_path, install) == []
