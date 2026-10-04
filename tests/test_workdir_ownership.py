"""
tests/test_workdir_ownership.py — Propriété des dossiers de travail et TLS sans repli.
"""

from __future__ import annotations

import os
import ssl
import stat
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from core import tls
from core import workdir as workdir_mod
from core.workdir import (
    PROCESS_DIR_MARKER,
    WORK_DIR_MARKER,
    _urlopen_image,
    cleanable_work_dir_entries,
    clear_work_dir,
    create_process_work_dir,
    ensure_work_dir,
    is_owned_process_dir,
    is_owned_work_root,
    prepare_process_work_dir,
    sanitize_process_folder_name,
)


def _symlinks_supported(tmp_path: Path) -> bool:
    """Windows sans privilège / mode développeur : pas de liens symboliques."""
    probe = tmp_path / "_link_probe"
    try:
        probe.symlink_to(tmp_path, target_is_directory=True)
    except (OSError, NotImplementedError):
        return False
    probe.unlink()
    return True


# ---------------------------------------------------------------------------
# Racine du work_dir
# ---------------------------------------------------------------------------

def test_ensure_work_dir_marks_only_created_or_adopted_roots(tmp_path: Path) -> None:
    created = ensure_work_dir(tmp_path / "new")
    assert is_owned_work_root(created)

    user_dir = tmp_path / "videos"
    user_dir.mkdir()
    (user_dir / "film.mkv").write_bytes(b"x")
    ensure_work_dir(user_dir)
    assert not is_owned_work_root(user_dir)

    empty_user_dir = tmp_path / "empty"
    empty_user_dir.mkdir()
    ensure_work_dir(empty_user_dir)
    assert not is_owned_work_root(empty_user_dir)

    ensure_work_dir(user_dir, adopt=True)
    assert is_owned_work_root(user_dir)


def test_cleanup_of_user_folder_only_touches_muxiveo_items(tmp_path: Path) -> None:
    root = tmp_path / "videos"
    (root / "Inception").mkdir(parents=True)
    (root / "Inception" / "Inception.mkv").write_bytes(b"film")
    (root / "notes.txt").write_text("perso", encoding="utf-8")
    (root / "tmdb_covers" / "abc").mkdir(parents=True)
    (root / "tmdb_covers" / "abc" / "cover.jpg").write_bytes(b"jpg")
    job = create_process_work_dir(root, output_path=Path("/out/Inception.mkv"))

    cleanable = cleanable_work_dir_entries(root)
    assert sorted(p.name for p in cleanable) == sorted([job.path.name, "tmdb_covers"])

    clear_work_dir(root)

    assert (root / "Inception" / "Inception.mkv").read_bytes() == b"film"
    assert (root / "notes.txt").exists()
    assert not job.path.exists()
    assert not (root / "tmdb_covers").exists()


def test_cleanup_of_owned_root_removes_everything_but_marker(tmp_path: Path) -> None:
    root = ensure_work_dir(tmp_path / "work")
    (root / "legacy_job").mkdir()
    (root / "legacy_job" / "film1.hevc").write_bytes(b"x")

    assert [p.name for p in cleanable_work_dir_entries(root)] == ["legacy_job"]
    clear_work_dir(root)

    assert not (root / "legacy_job").exists()
    assert (root / WORK_DIR_MARKER).is_file()


# ---------------------------------------------------------------------------
# Dossiers process
# ---------------------------------------------------------------------------

def test_process_dir_never_reuses_or_empties_existing_folder(tmp_path: Path) -> None:
    root = tmp_path / "videos"
    existing = root / "Inception"
    existing.mkdir(parents=True)
    (existing / "keep.mkv").write_bytes(b"film")

    first = prepare_process_work_dir(root, output_path=Path("/out/Inception.mkv"))
    second = prepare_process_work_dir(root, output_path=Path("/out/Inception.mkv"))

    assert (existing / "keep.mkv").read_bytes() == b"film"
    assert first != second
    assert first.parent == root and second.parent == root
    assert first.name.startswith("Inception.") and second.name.startswith("Inception.")
    assert (first / PROCESS_DIR_MARKER).is_file()


def test_process_dir_long_name_is_truncated(tmp_path: Path) -> None:
    job = create_process_work_dir(tmp_path, process_name="x" * 400)
    assert job.path.is_dir()
    assert len(job.path.name) < 120


def test_process_dir_remove_requires_matching_token(tmp_path: Path) -> None:
    job = create_process_work_dir(tmp_path, process_name="job")
    (job.path / "film1.hevc").write_bytes(b"x")

    (job.path / PROCESS_DIR_MARKER).write_text("autre-execution\n", encoding="ascii")
    assert not job.is_owned()
    assert job.remove() is False
    assert job.path.exists()

    (job.path / PROCESS_DIR_MARKER).write_text(f"{job.token}\n", encoding="ascii")
    assert job.remove() is True
    assert not job.path.exists()


def test_process_dir_remove_refuses_without_marker(tmp_path: Path) -> None:
    job = create_process_work_dir(tmp_path, process_name="job")
    (job.path / PROCESS_DIR_MARKER).unlink()
    assert job.remove() is False
    assert job.path.exists()


def test_process_dir_remove_refuses_symlink_replacement(tmp_path: Path) -> None:
    if not _symlinks_supported(tmp_path):
        pytest.skip("liens symboliques non autorisés sur ce système")
    job = create_process_work_dir(tmp_path / "work", process_name="job")
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / PROCESS_DIR_MARKER).write_text(f"{job.token}\n", encoding="ascii")
    (victim / "precious.mkv").write_bytes(b"film")
    marker = (job.path / PROCESS_DIR_MARKER)
    marker.unlink()
    job.path.rmdir()
    job.path.symlink_to(victim, target_is_directory=True)

    assert job.remove() is False
    assert (victim / "precious.mkv").exists()


def test_is_owned_process_dir_requires_direct_child(tmp_path: Path) -> None:
    job = create_process_work_dir(tmp_path / "work", process_name="job")
    assert is_owned_process_dir(job.path, root=tmp_path / "work")
    assert not is_owned_process_dir(job.path, root=tmp_path)


# ---------------------------------------------------------------------------
# Multi-OS : noms Windows, racine derrière un lien, verrous, lecture seule
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["CON", "nul", "Com1", "LPT9.mkv", "aux.Film"])
def test_process_folder_name_avoids_windows_device_names(name: str) -> None:
    folder = sanitize_process_folder_name(name)
    assert folder.startswith("job_")
    assert folder.split(".", 1)[0].lower() not in {"con", "nul", "com1", "lpt9", "aux"}


def test_process_dir_under_linked_root_is_owned(tmp_path: Path) -> None:
    """Racine derrière un lien (/home → /var/home, /var → /private/var) : propriété reconnue."""
    if not _symlinks_supported(tmp_path):
        pytest.skip("liens symboliques non autorisés sur ce système")
    real_root = tmp_path / "var" / "home"
    real_root.mkdir(parents=True)
    linked_root = tmp_path / "home"
    linked_root.symlink_to(real_root, target_is_directory=True)

    job = create_process_work_dir(linked_root, process_name="job")
    assert job.is_owned()
    assert is_owned_process_dir(real_root / job.path.name, root=linked_root)
    assert job.remove() is True
    assert not (real_root / job.path.name).exists()


def test_partial_removal_keeps_marker_for_later_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    job = create_process_work_dir(tmp_path, process_name="job")
    (job.path / "film1.hevc").write_bytes(b"x")
    (job.path / "locked.hevc").write_bytes(b"x")
    real_remove = workdir_mod._remove_entry

    def _locked(entry: Path) -> None:
        if entry.name == "locked.hevc":
            raise PermissionError("fichier utilisé par un autre processus")
        real_remove(entry)

    monkeypatch.setattr(workdir_mod, "_remove_entry", _locked)
    with pytest.raises(PermissionError):
        job.remove()
    assert (job.path / PROCESS_DIR_MARKER).is_file()
    assert job.is_owned()
    assert job.path in cleanable_work_dir_entries(tmp_path)

    monkeypatch.setattr(workdir_mod, "_remove_entry", real_remove)
    assert job.remove() is True


def test_remove_handles_readonly_files(tmp_path: Path) -> None:
    job = create_process_work_dir(tmp_path, process_name="job")
    nested = job.path / "attachments"
    nested.mkdir()
    for path in (job.path / "ro.bin", nested / "ro_nested.bin"):
        path.write_bytes(b"x")
        os.chmod(path, stat.S_IREAD)
    assert job.remove() is True
    assert not job.path.exists()


@pytest.mark.parametrize("cleanup", [clear_work_dir, workdir_mod.remove_path])
@pytest.mark.parametrize("nested", [False, True])
def test_cleanup_retries_locked_process_dir(tmp_path, monkeypatch, cleanup, nested):
    job = create_process_work_dir(tmp_path, process_name="job")
    payload_dir = job.path / "nested" if nested else job.path
    payload_dir.mkdir(exist_ok=True)
    locked = payload_dir / "locked.hevc"
    locked.write_bytes(b"video")
    real_unlink = os.unlink

    def locked_unlink(path, *args, **kwargs):
        if Path(path).name == locked.name:
            raise PermissionError("fichier utilisé par un autre processus")
        return real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patcher:
        patcher.setattr(os, "unlink", locked_unlink)
        cleanup(tmp_path if cleanup is clear_work_dir else job.path)

    assert locked.read_bytes() == b"video"
    assert job.is_owned()
    assert cleanable_work_dir_entries(tmp_path) == [job.path]
    clear_work_dir(tmp_path)
    assert not job.path.exists()


def test_failed_process_rmdir_restores_marker(tmp_path, monkeypatch):
    job = create_process_work_dir(tmp_path, process_name="job")
    real_rmdir = Path.rmdir

    def locked_rmdir(path):
        if path == job.path:
            raise PermissionError("dossier utilisé par un autre processus")
        return real_rmdir(path)

    with monkeypatch.context() as patcher:
        patcher.setattr(Path, "rmdir", locked_rmdir)
        workdir_mod.remove_path(job.path)

    assert job.is_owned()
    assert cleanable_work_dir_entries(tmp_path) == [job.path]
    clear_work_dir(tmp_path)
    assert not job.path.exists()


def test_remove_does_not_follow_links_inside_process_dir(tmp_path: Path) -> None:
    if not _symlinks_supported(tmp_path):
        pytest.skip("liens symboliques non autorisés sur ce système")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.mkv").write_bytes(b"film")
    job = create_process_work_dir(tmp_path / "work", process_name="job")
    (job.path / "link").symlink_to(outside, target_is_directory=True)
    assert job.remove() is True
    assert (outside / "precious.mkv").read_bytes() == b"film"


# ---------------------------------------------------------------------------
# TLS : aucun repli automatique non vérifié
# ---------------------------------------------------------------------------

def _ssl_failure(*_args, **_kwargs):
    raise urllib.error.URLError(ssl.SSLCertVerificationError("certificate verify failed"))


def test_cover_download_has_no_insecure_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def _urlopen(req, timeout=None, context=None):
        calls.append({"context": context})
        return _ssl_failure()

    monkeypatch.delenv("MUXIVEO_TMDB_INSECURE_SSL", raising=False)
    monkeypatch.setattr(tls.urllib.request, "urlopen", _urlopen)
    req = urllib.request.Request("https://image.tmdb.org/t/p/original/x.jpg")

    with pytest.raises(tls.TlsVerificationError, match="MUXIVEO_TMDB_INSECURE_SSL"):
        _urlopen_image(req)
    assert len(calls) == 1 and calls[0]["context"] is None


def test_insecure_tls_requires_explicit_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    contexts: list[ssl.SSLContext | None] = []
    monkeypatch.setenv("MUXIVEO_TMDB_INSECURE_SSL", "1")
    def urlopen(req, timeout=None, context=None):
        contexts.append(context)
        return "ok"

    monkeypatch.setattr(tls.urllib.request, "urlopen", urlopen)
    req = urllib.request.Request("https://image.tmdb.org/t/p/original/x.jpg")

    assert _urlopen_image(req) == "ok"
    assert contexts[0] is not None and contexts[0].verify_mode == ssl.CERT_NONE


def test_tmdb_tls_failure_is_an_explicit_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.media_info_fetcher import TmdbError, TmdbFetcher

    calls: list[object] = []

    def _urlopen(req, timeout=None, context=None):
        calls.append(context)
        return _ssl_failure()

    monkeypatch.delenv("MUXIVEO_TMDB_INSECURE_SSL", raising=False)
    monkeypatch.setattr(tls.urllib.request, "urlopen", _urlopen)
    fetcher = TmdbFetcher(api_key="secret-key")

    with pytest.raises(TmdbError, match="Connexion TMDB refusée"):
        fetcher.search("Inception")
    assert calls == [None]


def test_tmdb_debug_log_redacts_api_key(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    from core import media_info_fetcher as fetcher_mod

    monkeypatch.setenv("MUXIVEO_TMDB_DEBUG", "1")
    monkeypatch.setattr(fetcher_mod._TMDB_LOGGER, "hasHandlers", lambda: False)
    fetcher = fetcher_mod.TmdbFetcher(api_key="secret-key")

    fetcher._debug_log("test", url="https://api.themoviedb.org/3/search?api_key=secret-key&query=x")

    err = capsys.readouterr().err
    assert "secret-key" not in err
    assert "api_key=***" in err


@pytest.mark.skipif(os.name != "nt", reason="jonctions NTFS : Windows uniquement")
def test_windows_junctions_are_never_followed(tmp_path: Path) -> None:
    import _winapi

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.mkv").write_bytes(b"film")
    (outside / PROCESS_DIR_MARKER).write_text("jeton\n", encoding="ascii")

    # Jonction à l'intérieur d'un dossier process : seul le lien est retiré.
    job = create_process_work_dir(tmp_path / "work", process_name="job")
    getattr(_winapi, "CreateJunction")(str(outside), str(job.path / "junction"))
    assert job.remove() is True
    assert (outside / "precious.mkv").read_bytes() == b"film"

    # Dossier process remplacé par une jonction : propriété refusée.
    job2 = create_process_work_dir(tmp_path / "work", process_name="job")
    (outside / PROCESS_DIR_MARKER).write_text(f"{job2.token}\n", encoding="ascii")
    (job2.path / PROCESS_DIR_MARKER).unlink()
    job2.path.rmdir()
    getattr(_winapi, "CreateJunction")(str(outside), str(job2.path))
    assert job2.remove() is False
    assert (outside / "precious.mkv").read_bytes() == b"film"
