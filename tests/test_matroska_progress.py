from pathlib import Path

import pytest

from core.matroska.progress import native_mux_progress_callback, native_mux_progress_label
from core.matroska.writer import MatroskaWriteProgress


@pytest.mark.parametrize(("line", "expected"), [
    ("Assemblage Matroska : 0% (6452 paquets, 4.4 Mio)", "0% - 4.4 Mio"),
    ("Écriture Matroska : 75% (500000 paquets, 1200.0 Mio)", "75% - 1200.0 Mio"),
    ("Assemblage Matroska (validation) : 99% 500 paquets, 2.0 Mio", "99% - 2.0 Mio"),
    ("Assemblage Matroska (commit) : 100% (500 paquets, 2.0 Mio)", "100% - 2.0 Mio"),
    ("Assemblage Matroska : (500 paquets, 2.0 Mio)", "… - 2.0 Mio"),
    ("Assemblage Matroska natif terminé (aucun post-patch conteneur).", None),
    ("Assemblage Matroska natif multi-pistes (3 pistes) → film.mkv…", None),
    ("Écriture Matroska native multi-pistes", None),
])
def test_native_mux_label_recognizes_only_progress(line, expected):
    assert native_mux_progress_label(line) == expected


def test_native_mux_progress_limits_frequency_but_keeps_stage_changes(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("core.matroska.progress.time.monotonic", lambda: now[0])
    lines = []
    callback = native_mux_progress_callback(lines.append)

    def progress(stage, percent):
        return MatroskaWriteProgress(stage, 10, 2 * 1024 * 1024, Path("candidate"), 100, percent)

    callback(progress("clusters", 10))
    now[0] = 0.1
    callback(progress("clusters", 20))
    assert len(lines) == 1
    now[0] = 0.25
    callback(progress("clusters", 30))
    assert native_mux_progress_label(lines[-1]) == "30% - 2.0 Mio"
    callback(progress("validation", 99))
    callback(progress("commit", 100))
    assert [native_mux_progress_label(line) for line in lines] == [
        "10% - 2.0 Mio", "30% - 2.0 Mio", "99% - 2.0 Mio", "100% - 2.0 Mio",
    ]
