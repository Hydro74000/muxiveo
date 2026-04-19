"""
tests/test_encode_workflow.py — Tests unitaires pour core/workflows/encode.py

Plan de couverture :

    Helpers RAM — cross-platform :
        _total_ram_bytes :
            - Linux  : lecture MemTotal /proc/meminfo
            - macOS  : parse sysctl hw.memsize
            - Windows: MEMORYSTATUSEX.ullTotalPhys via ctypes
            - Retourne 0 sur erreur

        _available_ram_bytes :
            - Linux  : lecture MemAvailable /proc/meminfo
            - macOS  : parse vm_stat (free+inactive+speculative+purgeable)
            - Windows: MEMORYSTATUSEX.ullAvailPhys via ctypes
            - Retourne 0 sur erreur

        _macos_available_ram :
            - Parse correctement vm_stat avec différentes tailles de page
            - Retourne 0 si vm_stat échoue

        _ram_buffer_dir :
            - Linux/macOS : /dev/shm si writable
            - Linux/macOS : None si /dev/shm absent ou non writable
            - Windows     : None systématiquement

    _shm_path (méthode d'instance) — formule de seuil :
        Formule : available - file_size ≥ total × threshold_pct / 100
        - Retourne RAM si formule vérifiée
        - Retourne disque si RAM insuffisante
        - Retourne disque si ram_buffer_enabled=False (config)
        - Retourne disque si _ram_buffer_dir() = None
        - Retourne disque si total ou available = 0
        - Frontière exacte du seuil

    Gestion des fichiers dans _run_with_metadata_inject :
        Tracking ext_files :
            - Un fichier RAM est enregistré dans ext_files
            - Un fichier disque N'est PAS dans ext_files
            - _free() retire le fichier de ext_files
            - _free() silencieux si fichier déjà absent

        Pas de src.hevc :
            - aucune extraction ffmpeg vers src.hevc n'est créée

        Cleanup des intermédiaires HEVC :
            - enc.hevc supprimé dans les snapshots suivant la création de enc_hdr10p.hevc
            - enc_hdr10p.hevc supprimé dans les snapshots suivant enc_dv.hevc

        Comptage fichiers simultanés :
            - Jamais > 2 fichiers .hevc/.mkv simultanément (DV / HDR10+ / les deux)

        Cleanup ext_files sur annulation / exception :
            - Fichiers ext_files supprimés si RuntimeError
            - Fichiers ext_files supprimés si TaskCancelledError
            - Itération sur copie de ext_files (pas de mutation pendant le finally)

        Désactivation du buffer RAM :
            - ram_buffer_enabled=False → tous les HEVC vont sur disque (ext_files vide)

    Codec COPY — passthrough métadonnées (TestCopyCodecMetadataPassthrough) :
        build_command single pass :
            - codec=copy → -map_metadata 0 présent
            - codec=copy → -map_metadata:s:v:0 0:s:v:0 présent
            - codec=copy → -map_metadata avant la sortie
            - codec≠copy (libx265, libx264, nvenc, amf…) → pas de -map_metadata
        build_command two pass (SIZE) :
            - codec=copy → forcé en single-pass (pas de -pass)
            - codec≠copy → aucune passe ne contient -map_metadata
        Interaction : audio, subtitles → -map_metadata toujours présent pour copy

    run() bypass inject (TestRunCopyBypassesInject) :
        - codec=copy + copy_dv=True  → _run_with_metadata_inject NON appelé
        - codec=copy + copy_hdr10plus=True → idem
        - codec=copy + les deux → idem
        - codec=libx265 + copy_dv=True → _run_with_metadata_inject APPELÉ (régression)
        - codec=copy + copy_dv → log INFO "passthrough" émis
        - codec=copy sans flags → runner standard utilisé

    Méthodes build_command (régression) :
        - CRF → list[str] ; SIZE → list[list[str]]
        - codec copy → pas de -crf ; audio copy → pas de -b:a ; AAC → bitrate présent
        - TrueHD core → BSF truehd_core

    validate :
        - Source manquante, source==output, SIZE sans durée, master_display invalide, max_cll invalide

    ProfileManager :
        - save/load round-trip, delete, names(), overwrite

Exécution :
    cd mediarecode && pytest tests/test_encode_workflow.py -v
"""

from __future__ import annotations

import json
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call
import tempfile
import os

import pytest
from PySide6.QtCore import QCoreApplication, Qt

_app: QCoreApplication | None = None


def _get_app() -> QCoreApplication:
    global _app
    if _app is None:
        _app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    return _app


@pytest.fixture(autouse=True)
def qt_app():
    return _get_app()


from core.workflows.encode import (
    AUDIO_CODECS,
    AudioTrackSettings,
    EncodeConfig,
    EncodeError,
    EncodePreset,
    EncodeWorkflow,
    ProfileManager,
    QualityMode,
    TrackMetaEdit,
    TrackTimeOffset,
    VideoEncodeSettings,
)
from core.inspector import ChapterEntry
from core.runner import TaskSignals


# ---------------------------------------------------------------------------
# Fixtures communes
# ---------------------------------------------------------------------------

def _make_video_settings(**kw) -> VideoEncodeSettings:
    defaults = dict(
        codec="libx265", quality_mode=QualityMode.CRF, crf=18,
        bitrate_kbps=5000, target_size_mb=4000, preset="slow",
        extra_params="", inject_hdr_meta=False, master_display="",
        max_cll="", tonemap_to_sdr=False, tonemap_algorithm="hable",
    )
    defaults.update(kw)
    return VideoEncodeSettings(**defaults)


def _make_config(source: Path, output: Path, **kw) -> EncodeConfig:
    defaults = dict(
        source=source, output=output, video=_make_video_settings(),
        audio_tracks=[], copy_subtitles=False, duration_s=3600.0,
        copy_dv=False, copy_hdr10plus=False, dovi_profile="0", work_dir=None,
    )
    defaults.update(kw)
    return EncodeConfig(**defaults)


def _make_workflow(enabled=True, threshold=15, ffmpeg_threads=None) -> EncodeWorkflow:
    return EncodeWorkflow(
        ffmpeg_bin="ffmpeg", dovi_tool_bin="dovi_tool",
        hdr10plus_bin="hdr10plus_tool",
        ram_buffer_enabled=enabled,
        ram_buffer_threshold_pct=threshold,
        ffmpeg_threads=ffmpeg_threads,
    )


# ===========================================================================
# FFmpeg threads
# ===========================================================================

class TestFfmpegThreads:

    def test_default_threads_use_cpu_count_times_0_75_rounded_up(self):
        with patch("core.workflows.encode.workflow.os.cpu_count", return_value=8):
            wf = _make_workflow()
        assert wf._ffmpeg_threads == 6

    def test_negative_threads_fallback_to_default(self):
        with patch("core.workflows.encode.workflow.os.cpu_count", return_value=4):
            wf = _make_workflow(ffmpeg_threads=-3)
        assert wf._ffmpeg_threads == 3

    def test_single_pass_build_includes_threads(self, tmp_path):
        src = tmp_path / "source.mkv"
        src.touch()
        wf = _make_workflow(ffmpeg_threads=10)
        cmd = wf.build_command_single(_make_config(src, tmp_path / "out.mkv"))

        assert "-threads" in cmd
        assert cmd[cmd.index("-threads") + 1] == "10"

    def test_two_pass_build_includes_threads_on_both_passes(self, tmp_path):
        src = tmp_path / "source.mkv"
        src.touch()
        video = _make_video_settings(quality_mode=QualityMode.SIZE)
        wf = _make_workflow(ffmpeg_threads=14)
        cmds = wf.build_command(_make_config(src, tmp_path / "out.mkv", video=video))

        assert cmds[0][cmds[0].index("-threads") + 1] == "14"
        assert cmds[1][cmds[1].index("-threads") + 1] == "14"


class TestFfmpegProgressArgs:

    def test_single_pass_build_includes_machine_progress_flags(self, tmp_path):
        src = tmp_path / "source.mkv"
        src.touch()
        wf = _make_workflow()

        cmd = wf.build_command_single(_make_config(src, tmp_path / "out.mkv"))

        assert "-progress" in cmd
        assert cmd[cmd.index("-progress") + 1] == "pipe:1"
        assert "-nostats" in cmd
        assert cmd.index("-nostats") < cmd.index("-i")

    def test_two_pass_build_uses_machine_progress_and_platform_null_sink(self, tmp_path):
        src = tmp_path / "source.mkv"
        src.touch()
        wf = _make_workflow()
        video = _make_video_settings(quality_mode=QualityMode.SIZE)

        cmds = wf.build_command(_make_config(src, tmp_path / "out.mkv", video=video, duration_s=3600.0))

        for cmd in cmds:
            assert "-progress" in cmd
            assert cmd[cmd.index("-progress") + 1] == "pipe:1"
            assert "-nostats" in cmd
        assert cmds[0][-1] == os.devnull

    def test_video_only_build_includes_machine_progress_flags(self, tmp_path):
        src = tmp_path / "source.mkv"
        src.touch()
        wf = _make_workflow()
        output_hevc = tmp_path / "enc.hevc"

        cmd = wf._build_video_only_cmd(_make_config(src, tmp_path / "out.mkv"), output_hevc)

        assert "-progress" in cmd
        assert cmd[cmd.index("-progress") + 1] == "pipe:1"
        assert "-nostats" in cmd

# ===========================================================================
# _total_ram_bytes — cross-platform
# ===========================================================================

class TestTotalRamBytes:

    def test_linux_reads_memtotal(self):
        """Linux : MemTotal depuis /proc/meminfo."""
        fake = "MemTotal:       16384000 kB\nMemFree: 4096000 kB\n"
        with patch("sys.platform", "linux"), \
             patch.object(Path, "read_text", return_value=fake):
            assert EncodeWorkflow._total_ram_bytes() == 16_384_000 * 1024

    def test_linux_returns_zero_if_memtotal_absent(self):
        fake = "MemFree: 4096000 kB\n"
        with patch("sys.platform", "linux"), \
             patch.object(Path, "read_text", return_value=fake):
            assert EncodeWorkflow._total_ram_bytes() == 0

    def test_linux_returns_zero_on_ioerror(self):
        with patch("sys.platform", "linux"), \
             patch.object(Path, "read_text", side_effect=OSError):
            assert EncodeWorkflow._total_ram_bytes() == 0

    def test_macos_parses_sysctl(self):
        """macOS : sysctl hw.memsize retourne RAM totale en octets."""
        with patch("sys.platform", "darwin"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="17179869184\n")
            assert EncodeWorkflow._total_ram_bytes() == 17_179_869_184

    def test_macos_returns_zero_on_sysctl_failure(self):
        with patch("sys.platform", "darwin"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert EncodeWorkflow._total_ram_bytes() == 0

    def test_windows_uses_ctypes(self):
        """Windows : lit ullTotalPhys via GlobalMemoryStatusEx."""
        fake_stat = MagicMock()
        fake_stat.ullTotalPhys = 17_179_869_184
        with patch("sys.platform", "win32"), \
             patch.object(EncodeWorkflow, "_win_mem_status", return_value=fake_stat):
            assert EncodeWorkflow._total_ram_bytes() == 17_179_869_184

    def test_windows_returns_zero_if_ctypes_unavailable(self):
        """Windows : fallback à 0 si ctypes/_ctypes est indisponible."""
        with patch("sys.platform", "win32"), \
             patch.object(EncodeWorkflow, "_win_mem_status", side_effect=ImportError):
            assert EncodeWorkflow._total_ram_bytes() == 0

    def test_unknown_platform_returns_zero(self):
        with patch("sys.platform", "freebsd"):
            assert EncodeWorkflow._total_ram_bytes() == 0


# ===========================================================================
# _available_ram_bytes — cross-platform
# ===========================================================================

class TestAvailableRamBytes:

    def test_linux_reads_memavailable(self):
        """Linux : MemAvailable depuis /proc/meminfo."""
        fake = "MemTotal: 16384000 kB\nMemAvailable: 8192000 kB\n"
        with patch("sys.platform", "linux"), \
             patch.object(Path, "read_text", return_value=fake):
            assert EncodeWorkflow._available_ram_bytes() == 8_192_000 * 1024

    def test_linux_returns_zero_when_memavailable_absent(self):
        fake = "MemTotal: 16384000 kB\nMemFree: 4096000 kB\n"
        with patch("sys.platform", "linux"), \
             patch.object(Path, "read_text", return_value=fake):
            assert EncodeWorkflow._available_ram_bytes() == 0

    def test_linux_returns_zero_on_ioerror(self):
        with patch("sys.platform", "linux"), \
             patch.object(Path, "read_text", side_effect=OSError):
            assert EncodeWorkflow._available_ram_bytes() == 0

    def test_linux_parses_large_values(self):
        fake = "MemAvailable:   67108864 kB\n"
        with patch("sys.platform", "linux"), \
             patch.object(Path, "read_text", return_value=fake):
            assert EncodeWorkflow._available_ram_bytes() == 67_108_864 * 1024

    def test_macos_delegates_to_macos_available_ram(self):
        """macOS : délègue à _macos_available_ram."""
        with patch("sys.platform", "darwin"), \
             patch.object(EncodeWorkflow, "_macos_available_ram", return_value=4_000_000_000):
            assert EncodeWorkflow._available_ram_bytes() == 4_000_000_000

    def test_windows_reads_ullavailphys(self):
        fake_stat = MagicMock()
        fake_stat.ullAvailPhys = 8_589_934_592
        with patch("sys.platform", "win32"), \
             patch.object(EncodeWorkflow, "_win_mem_status", return_value=fake_stat):
            assert EncodeWorkflow._available_ram_bytes() == 8_589_934_592

    def test_windows_available_returns_zero_if_ctypes_unavailable(self):
        with patch("sys.platform", "win32"), \
             patch.object(EncodeWorkflow, "_win_mem_status", side_effect=ImportError):
            assert EncodeWorkflow._available_ram_bytes() == 0

    def test_unknown_platform_returns_zero(self):
        with patch("sys.platform", "freebsd"):
            assert EncodeWorkflow._available_ram_bytes() == 0


# ===========================================================================
# _macos_available_ram
# ===========================================================================

class TestMacosAvailableRam:

    _VM_STAT_SAMPLE = (
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
        "Pages free:                               1234.\n"
        "Pages active:                             5678.\n"
        "Pages inactive:                           2000.\n"
        "Pages speculative:                         500.\n"
        "Pages throttled:                             0.\n"
        "Pages wired down:                         3000.\n"
        "Pages purgeable:                           300.\n"
    )

    def test_parses_free_inactive_speculative_purgeable(self):
        """Additionne free+inactive+speculative+purgeable avec la bonne taille de page."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=self._VM_STAT_SAMPLE)
            result = EncodeWorkflow._macos_available_ram()
        expected = (1234 + 2000 + 500 + 300) * 16384
        assert result == expected

    def test_returns_zero_on_subprocess_failure(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert EncodeWorkflow._macos_available_ram() == 0

    def test_uses_default_page_size_4096_when_not_parseable(self):
        """Taille de page par défaut = 4096 si non parseable."""
        vm_stat_no_pagesize = (
            "Pages free:     1000.\n"
            "Pages inactive:  500.\n"
            "Pages speculative: 200.\n"
            "Pages purgeable:   100.\n"
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=vm_stat_no_pagesize)
            result = EncodeWorkflow._macos_available_ram()
        assert result == (1000 + 500 + 200 + 100) * 4096


# ===========================================================================
# _ram_buffer_dir
# ===========================================================================

class TestRamBufferDir:

    def test_linux_returns_dev_shm_when_writable(self):
        with patch("sys.platform", "linux"), \
             patch.object(Path, "is_dir", return_value=True), \
             patch("os.access", return_value=True):
            assert EncodeWorkflow._ram_buffer_dir() == Path("/dev/shm")

    def test_linux_returns_none_when_shm_not_dir(self):
        with patch("sys.platform", "linux"), \
             patch.object(Path, "is_dir", return_value=False):
            assert EncodeWorkflow._ram_buffer_dir() is None

    def test_linux_returns_none_when_shm_not_writable(self):
        with patch("sys.platform", "linux"), \
             patch.object(Path, "is_dir", return_value=True), \
             patch("os.access", return_value=False):
            assert EncodeWorkflow._ram_buffer_dir() is None

    def test_macos_returns_dev_shm_when_available(self):
        with patch("sys.platform", "darwin"), \
             patch.object(Path, "is_dir", return_value=True), \
             patch("os.access", return_value=True):
            assert EncodeWorkflow._ram_buffer_dir() == Path("/dev/shm")

    def test_windows_always_returns_none(self):
        """Windows n'a pas de répertoire RAM standard."""
        with patch("sys.platform", "win32"):
            assert EncodeWorkflow._ram_buffer_dir() is None

    def test_unknown_platform_returns_none(self):
        with patch("sys.platform", "freebsd11"):
            assert EncodeWorkflow._ram_buffer_dir() is None


# ===========================================================================
# _shm_path — formule de seuil  (available - file_size ≥ total × pct / 100)
# ===========================================================================

class TestShmPath:
    """
    Formule : available_before - file_size >= total_ram * threshold_pct / 100
    Valeurs par défaut du workflow : enabled=True, threshold=15 %
    """

    def _wf(self, enabled=True, threshold=15) -> EncodeWorkflow:
        return _make_workflow(enabled=enabled, threshold=threshold)

    def _patch_ram(self, available: int, total: int) -> ExitStack:
        stack = ExitStack()
        stack.enter_context(patch.object(EncodeWorkflow, "_available_ram_bytes", return_value=available))
        stack.enter_context(patch.object(EncodeWorkflow, "_total_ram_bytes",     return_value=total))
        stack.enter_context(patch.object(EncodeWorkflow, "_ram_buffer_dir",      return_value=Path("/dev/shm")))
        return stack

    # ── formule correcte ─────────────────────────────────────────────────────

    def test_uses_shm_when_formula_satisfied(self, tmp_path):
        """
        total=10 Go, file=1 Go, available=3 Go
        3 - 1 = 2 Go  ≥  10 × 15% = 1.5 Go  → RAM
        """
        total     = 10 * 2**30
        file_size =  1 * 2**30
        available =  3 * 2**30
        wf = self._wf()
        with self._patch_ram(available, total):
            result = wf._shm_path(tmp_path, "test.hevc", file_size)
        assert result == Path("/dev/shm/test.hevc")

    def test_uses_disk_when_formula_not_satisfied(self, tmp_path):
        """
        total=10 Go, file=1 Go, available=2.4 Go
        2.4 - 1 = 1.4 Go  <  10 × 15% = 1.5 Go  → disque
        """
        total     = 10 * 2**30
        file_size =  1 * 2**30
        available = int(2.4 * 2**30)
        wf = self._wf()
        with self._patch_ram(available, total):
            result = wf._shm_path(tmp_path, "test.hevc", file_size)
        assert result == tmp_path / "test.hevc"

    def test_boundary_exactly_at_threshold_uses_shm(self, tmp_path):
        """
        Exactement à la limite : available - file = total × pct / 100
        → condition satisfaite (>=) → RAM
        """
        total     = 10 * 2**30
        threshold = 15
        file_size =  1 * 2**30
        available = file_size + int(total * threshold / 100)   # exactement égal
        wf = self._wf(threshold=threshold)
        with self._patch_ram(available, total):
            result = wf._shm_path(tmp_path, "test.hevc", file_size)
        assert result == Path("/dev/shm/test.hevc")

    def test_boundary_one_byte_above_threshold_uses_shm(self, tmp_path):
        """Un octet au-dessus de la limite exacte → RAM."""
        total     = 10 * 2**30
        threshold = 15
        file_size =  1 * 2**30
        available = file_size + int(total * threshold / 100) + 1
        wf = self._wf(threshold=threshold)
        with self._patch_ram(available, total):
            result = wf._shm_path(tmp_path, "test.hevc", file_size)
        assert result == Path("/dev/shm/test.hevc")

    # ── config disabled ───────────────────────────────────────────────────────

    def test_disabled_config_always_returns_disk(self, tmp_path):
        """ram_buffer_enabled=False → disque même avec RAM abondante."""
        wf = self._wf(enabled=False)
        with self._patch_ram(10 * 2**30, 10 * 2**30):
            result = wf._shm_path(tmp_path, "test.hevc", 1_000_000)
        assert result == tmp_path / "test.hevc"

    # ── ram_buffer_dir absent ─────────────────────────────────────────────────

    def test_no_ram_dir_returns_disk(self, tmp_path):
        """Pas de répertoire RAM (Windows) → disque même avec RAM suffisante."""
        wf = self._wf()
        with patch.object(EncodeWorkflow, "_ram_buffer_dir", return_value=None), \
             patch.object(EncodeWorkflow, "_available_ram_bytes", return_value=10 * 2**30), \
             patch.object(EncodeWorkflow, "_total_ram_bytes",     return_value=10 * 2**30):
            result = wf._shm_path(tmp_path, "test.hevc", 1_000_000)
        assert result == tmp_path / "test.hevc"

    # ── RAM indisponible / inconnue ───────────────────────────────────────────

    def test_zero_total_returns_disk(self, tmp_path):
        """total=0 → impossible d'évaluer le seuil → disque."""
        wf = self._wf()
        with patch.object(EncodeWorkflow, "_ram_buffer_dir", return_value=Path("/dev/shm")), \
             patch.object(EncodeWorkflow, "_available_ram_bytes", return_value=8 * 2**30), \
             patch.object(EncodeWorkflow, "_total_ram_bytes",     return_value=0):
            result = wf._shm_path(tmp_path, "test.hevc", 1_000_000)
        assert result == tmp_path / "test.hevc"

    def test_zero_available_returns_disk(self, tmp_path):
        """available=0 → disque."""
        wf = self._wf()
        with patch.object(EncodeWorkflow, "_ram_buffer_dir", return_value=Path("/dev/shm")), \
             patch.object(EncodeWorkflow, "_available_ram_bytes", return_value=0), \
             patch.object(EncodeWorkflow, "_total_ram_bytes",     return_value=10 * 2**30):
            result = wf._shm_path(tmp_path, "test.hevc", 1_000_000)
        assert result == tmp_path / "test.hevc"

    # ── seuil configurable ────────────────────────────────────────────────────

    def test_custom_threshold_5pct(self, tmp_path):
        """
        Seuil 5% : total=10 Go, file=1 Go, available=1.5 Go
        1.5 - 1 = 0.5 Go  ≥  10 × 5% = 0.5 Go  → exactement égal → RAM (>=)
        """
        total     = 10 * 2**30
        file_size =  1 * 2**30
        available = file_size + int(total * 5 / 100)
        wf = self._wf(threshold=5)
        with self._patch_ram(available, total):
            result = wf._shm_path(tmp_path, "test.hevc", file_size)
        assert result == Path("/dev/shm/test.hevc")   # exactement égal → RAM

    def test_threshold_clamped_to_90(self, tmp_path):
        """threshold > 90 est ramené à 90 par le constructeur."""
        wf = _make_workflow(threshold=200)
        assert wf._ram_buffer_threshold_pct == 90

    def test_threshold_clamped_to_0(self, tmp_path):
        """threshold < 0 est ramené à 0 (toujours utiliser RAM si disponible)."""
        wf = _make_workflow(threshold=-5)
        assert wf._ram_buffer_threshold_pct == 0


# ===========================================================================
# _run_with_metadata_inject — gestion fichiers (mock _run_cmd)
# ===========================================================================

def _collect_signals(signals, timeout=10.0):
    app = _get_app()
    done = [False]
    signals.finished.connect(lambda _: done.__setitem__(0, True),
                              Qt.ConnectionType.QueuedConnection)
    signals.failed.connect(lambda *_: done.__setitem__(0, True),
                            Qt.ConnectionType.QueuedConnection)
    signals.cancelled.connect(lambda: done.__setitem__(0, True),
                               Qt.ConnectionType.QueuedConnection)
    deadline = time.monotonic() + timeout
    while not done[0] and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


class TestMetadataInjectFileManagement:

    def _run_inject(self, tmp_path: Path, copy_dv: bool, copy_hdr10plus: bool,
                    ram_enabled: bool = False) -> list[list[Path]]:
        """
        Lance le workflow avec des mocks.
        ram_enabled=False : force les HEVC sur disque pour tester les snapshots disque.
        Retourne la liste des snapshots (fichiers présents après chaque _run_cmd).
        """
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 200_000)
        out = tmp_path / "output.mkv"
        config = _make_config(source=src, output=out, copy_dv=copy_dv,
                              copy_hdr10plus=copy_hdr10plus,
                              work_dir=tmp_path / "work")
        wf = _make_workflow(enabled=ram_enabled)
        snapshots: list[list[Path]] = []

        def _fake_run_cmd(cmd, signals=None, cwd=None, progress_cb=None):
            for arg in reversed(cmd):
                s = str(arg)
                if any(s.endswith(ext) for ext in (".hevc", ".mkv", ".bin", ".json")):
                    p = Path(s)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"\x00" * 100_000)
                    break
            work = tmp_path / "work"
            snap: list[Path] = []
            if work.exists():
                snap = [p for p in work.rglob("*")
                        if p.is_file() and p.suffix in (".hevc", ".mkv")]
            snapshots.append(snap[:])
            return ""

        # Force disque pour tests de comptage (pas de /dev/shm)
        with patch.object(EncodeWorkflow, "_shm_path",
                          side_effect=lambda tmp_dir, name, _size: tmp_dir / name):
            with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run_cmd):
                sigs = wf._run_with_metadata_inject(config)
                _collect_signals(sigs)
        return snapshots

    # ── pas de src.hevc ──────────────────────────────────────────────────────

    @pytest.mark.parametrize("copy_dv,copy_hdr10plus", [
        (True, False), (False, True), (True, True)
    ])
    def test_src_hevc_is_never_created(self, tmp_path, copy_dv, copy_hdr10plus):
        """Le workflow n'alloue jamais de src.hevc intermédiaire."""
        snapshots = self._run_inject(tmp_path, copy_dv, copy_hdr10plus)
        assert all(
            not any("src.hevc" in str(path) for path in snap)
            for snap in snapshots
        )

    # ── cleanup des intermédiaires ────────────────────────────────────────────

    def test_enc_hevc_deleted_after_hdr10plus_injection(self, tmp_path):
        """enc.hevc absent dans tous les snapshots après la création de enc_hdr10p.hevc."""
        snapshots = self._run_inject(tmp_path, copy_dv=False, copy_hdr10plus=True)
        first_hdr10p = next(
            (i for i, snap in enumerate(snapshots)
             if any("enc_hdr10p.hevc" in str(p) for p in snap)), None
        )
        assert first_hdr10p is not None, "enc_hdr10p.hevc jamais créé"
        for snap in snapshots[first_hdr10p + 1:]:
            names = [p.name for p in snap]
            assert "enc.hevc" not in names, \
                f"enc.hevc encore présent après injection HDR10+ : {names}"

    def test_enc_hdr10p_deleted_after_dv_injection(self, tmp_path):
        """enc_hdr10p.hevc absent dans tous les snapshots après création de enc_dv.hevc."""
        snapshots = self._run_inject(tmp_path, copy_dv=True, copy_hdr10plus=True)
        first_dv = next(
            (i for i, snap in enumerate(snapshots)
             if any("enc_dv.hevc" in str(p) for p in snap)), None
        )
        assert first_dv is not None, "enc_dv.hevc jamais créé"
        for snap in snapshots[first_dv + 1:]:
            names = [p.name for p in snap]
            assert "enc_hdr10p.hevc" not in names, \
                f"enc_hdr10p.hevc encore présent après injection DV : {names}"

    # ── comptage max 2 fichiers ───────────────────────────────────────────────

    @pytest.mark.parametrize("copy_dv,copy_hdr10plus", [
        (True, False), (False, True), (True, True)
    ])
    def test_max_two_large_files_at_any_time(self, tmp_path, copy_dv, copy_hdr10plus):
        """Jamais plus de 2 gros fichiers simultanés sur disque."""
        snapshots = self._run_inject(tmp_path, copy_dv, copy_hdr10plus)
        for i, snap in enumerate(snapshots):
            count = len([p for p in snap if p.exists()])
            assert count <= 2, (
                f"Snapshot {i} : {count} fichiers > 2 : {[p.name for p in snap]}"
            )

    # ── buffer RAM désactivé ──────────────────────────────────────────────────

    def test_disabled_ram_buffer_no_ext_files(self, tmp_path):
        """
        ram_buffer_enabled=False → _shm_path retourne toujours disque
        → ext_files reste vide tout au long du workflow.
        """
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 200_000)
        config = _make_config(source=src, output=tmp_path / "out.mkv",
                              copy_dv=True, copy_hdr10plus=True,
                              work_dir=tmp_path / "work")
        wf = _make_workflow(enabled=False)

        shm_calls: list[Path] = []

        def _fake_shm_path(tmp_dir, name, _size):
            # Avec enabled=False, le vrai _shm_path retourne toujours disque
            p = tmp_dir / name
            if p.parent == Path("/dev/shm"):
                shm_calls.append(p)
            return p

        def _fake_run(cmd, signals=None, cwd=None, progress_cb=None):
            for arg in reversed(cmd):
                s = str(arg)
                if any(s.endswith(ext) for ext in (".hevc", ".mkv", ".bin", ".json")):
                    p = Path(s)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"\x00" * 50_000)
                    break
            return ""

        with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run):
            sigs = wf._run_with_metadata_inject(config)
            _collect_signals(sigs)

        assert shm_calls == [], "Des fichiers /dev/shm créés malgré enabled=False"


class TestRuntimeCleanup:
    def test_run_two_pass_cleans_passlogs(self, tmp_path):
        wf = _make_workflow()

        def _fake_run(cmd, cwd=None, label=None, progress_cb=None, signals=None):
            assert cwd == tmp_path
            (tmp_path / "ffmpeg2pass-0.log").write_text("log", encoding="utf-8")
            (tmp_path / "ffmpeg2pass-0.log.mbtree").write_text("tree", encoding="utf-8")
            return ""

        with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run):
            sigs = wf._run_two_pass([["ffmpeg", "-pass", "1"], ["ffmpeg", "-pass", "2"]], cwd=tmp_path)
            _collect_signals(sigs)

        assert not (tmp_path / "ffmpeg2pass-0.log").exists()
        assert not (tmp_path / "ffmpeg2pass-0.log.mbtree").exists()

    def test_run_cleans_relocated_tmdb_covers(self, tmp_path):
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 1000)
        work_dir = tmp_path / "work"
        tmdb_root = work_dir / "tmdb_covers"
        guid_dir = tmdb_root / "deadbeef"
        guid_dir.mkdir(parents=True)
        cover = guid_dir / "cover.jpg"
        cover.write_bytes(b"jpeg")
        out = tmp_path / "output.mkv"
        cfg = _make_config(
            source=src,
            output=out,
            video=_make_video_settings(codec="copy"),
            extra_attachments=[cover],
            work_dir=work_dir,
        )
        wf = _make_workflow()

        with patch.object(wf._runner, "run") as mock_run:
            signals = TaskSignals()
            mock_run.return_value = signals
            returned = wf.run(cfg)
            process_dir = work_dir / "output"
            attachments_dir = process_dir / "attachments"
            assert (attachments_dir / "cover.jpg").exists()
            assert not guid_dir.exists()
            returned.finished.emit("done")
            _get_app().processEvents()

        assert not attachments_dir.exists()
        assert not process_dir.exists()


# ===========================================================================
# Cleanup ext_files sur annulation / exception
# ===========================================================================

class TestExtFilesCleanupOnFailure:

    def _run_and_fail(self, tmp_path: Path, fail_after: int, exc_class) -> list[Path]:
        """Simule une erreur après N commandes, retourne les fichiers /dev/shm créés."""
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 200_000)
        config = _make_config(source=src, output=tmp_path / "out.mkv",
                              copy_dv=True, copy_hdr10plus=True,
                              work_dir=tmp_path / "work")
        wf = _make_workflow(enabled=True)

        call_n = [0]
        created_ext: list[Path] = []
        shm = Path("/dev/shm")

        def _fake_shm_path(tmp_dir, name, _size):
            if name.endswith(".hevc"):
                p = shm / f"_test_cleanup_{id(wf)}_{name}"
                created_ext.append(p)
                return p
            return tmp_dir / name

        def _fake_run(cmd, signals=None, cwd=None, progress_cb=None):
            call_n[0] += 1
            for arg in reversed(cmd):
                s = str(arg)
                if any(s.endswith(ext) for ext in (".hevc", ".mkv", ".bin", ".json")):
                    p = Path(s)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"\x00" * 50_000)
                    break
            if call_n[0] >= fail_after:
                raise exc_class("Simulated")
            return ""

        with patch.object(EncodeWorkflow, "_shm_path", side_effect=_fake_shm_path), \
             patch.object(wf._runner, "_run_cmd", side_effect=_fake_run):
            sigs = wf._run_with_metadata_inject(config)
            _collect_signals(sigs)

        return created_ext

    def test_ext_files_cleaned_on_runtime_error(self, tmp_path):
        """Fichiers ext (shm) supprimés si RuntimeError."""
        created = self._run_and_fail(tmp_path, fail_after=4, exc_class=RuntimeError)
        for p in created:
            assert not p.exists(), f"Fichier ext non nettoyé : {p}"

    def test_ext_files_cleaned_on_cancel(self, tmp_path):
        """Fichiers ext (shm) supprimés si TaskCancelledError."""
        from core.runner import TaskCancelledError
        created = self._run_and_fail(tmp_path, fail_after=4, exc_class=TaskCancelledError)
        for p in created:
            assert not p.exists(), f"Fichier ext non nettoyé après cancel : {p}"

    def test_finally_iterates_copy_of_ext_files(self, tmp_path):
        """
        Le finally itère sur une copie de ext_files (list(ext_files)).
        Pas de RuntimeError si ext_files est muté pendant l'itération.
        Ce test vérifie qu'aucune exception ne remonte du finally.
        """
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 200_000)
        config = _make_config(source=src, output=tmp_path / "out.mkv",
                              copy_dv=True, work_dir=tmp_path / "work")
        wf = _make_workflow(enabled=True)

        def _fast_fail(cmd, signals=None, cwd=None, progress_cb=None):
            raise RuntimeError("Fast fail")

        result: list = []
        # Les connexions doivent être établies avant le lancement du thread
        # pour ne pas manquer un signal émis avant processEvents().
        with patch.object(EncodeWorkflow, "_shm_path",
                          side_effect=lambda t, n, _: t / n), \
             patch.object(wf._runner, "_run_cmd", side_effect=_fast_fail):
            sigs = wf._run_with_metadata_inject(config)
            sigs.failed.connect(lambda msg, _: result.append(msg),
                                Qt.ConnectionType.QueuedConnection)
            sigs.finished.connect(lambda _: result.append("ok"),
                                  Qt.ConnectionType.QueuedConnection)
            _collect_signals(sigs)
        # Le workflow se termine proprement (pas de crash dans le finally)
        assert len(result) == 1


# ===========================================================================
# build_command (régression)
# ===========================================================================

class TestBuildCommand:

    def setup_method(self):
        self.wf = _make_workflow()

    def test_crf_mode_returns_single_command(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        config = _make_config(src, tmp_path / "out.mkv",
                              video=_make_video_settings(quality_mode=QualityMode.CRF))
        result = self.wf.build_command(config)
        assert isinstance(result, list) and isinstance(result[0], str)

    def test_size_mode_returns_two_commands(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        config = _make_config(src, tmp_path / "out.mkv",
                              video=_make_video_settings(quality_mode=QualityMode.SIZE),
                              duration_s=3600.0)
        result = self.wf.build_command(config)
        assert isinstance(result, list) and isinstance(result[0], list)
        assert len(result) == 2

    def test_single_pass_contains_source_and_output(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        out = tmp_path / "out.mkv"
        cmd = self.wf.build_command(_make_config(src, out))
        cmd_str = " ".join(cmd)
        assert str(src) in cmd_str and str(out) in cmd_str

    def test_copy_codec_no_crf_flag(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        config = _make_config(src, tmp_path / "out.mkv",
                              video=_make_video_settings(codec="copy"))
        assert "-crf" not in self.wf.build_command(config)

    def test_audio_copy_codec_no_bitrate(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        track = AudioTrackSettings(stream_index=1, codec="copy", bitrate_kbps=384)
        cmd = self.wf.build_command(_make_config(src, tmp_path / "out.mkv",
                                                 audio_tracks=[track]))
        assert "-b:a:0" not in cmd

    def test_audio_aac_has_bitrate(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        track = AudioTrackSettings(stream_index=1, codec="aac", bitrate_kbps=256)
        cmd = self.wf.build_command(_make_config(src, tmp_path / "out.mkv",
                                                 audio_tracks=[track]))
        assert "-b:a:0" in cmd and "256k" in cmd

    def test_audio_ac3_has_bitrate(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        track = AudioTrackSettings(stream_index=1, codec="ac3", bitrate_kbps=448)
        cmd = self.wf.build_command(_make_config(src, tmp_path / "out.mkv",
                                                 audio_tracks=[track]))
        assert "-c:a:0" in cmd and cmd[cmd.index("-c:a:0") + 1] == "ac3"
        assert "-b:a:0" in cmd and "448k" in cmd

    def test_audio_eac3_7_1_forces_5_1_downmix(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        track = AudioTrackSettings(
            stream_index=1,
            codec="eac3",
            bitrate_kbps=640,
            input_channels=8,
            input_channel_layout="7.1",
        )
        cmd = self.wf.build_command(_make_config(src, tmp_path / "out.mkv", audio_tracks=[track]))
        assert "-ac:a:0" in cmd and cmd[cmd.index("-ac:a:0") + 1] == "6"
        assert "-channel_layout:a:0" in cmd and cmd[cmd.index("-channel_layout:a:0") + 1] == "5.1"

    def test_audio_eac3_5_1_keeps_native_channels(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        track = AudioTrackSettings(
            stream_index=1,
            codec="eac3",
            bitrate_kbps=640,
            input_channels=6,
            input_channel_layout="5.1",
        )
        cmd = self.wf.build_command(_make_config(src, tmp_path / "out.mkv", audio_tracks=[track]))
        assert "-ac:a:0" not in cmd
        assert "-channel_layout:a:0" not in cmd

    def test_truehd_core_bsf_present(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        track = AudioTrackSettings(stream_index=2, codec="copy",
                                   bitrate_kbps=384, extract_truehd_core=True)
        cmd = self.wf.build_command(_make_config(src, tmp_path / "out.mkv",
                                                 audio_tracks=[track]))
        assert "truehd_core" in " ".join(cmd)

    def test_truehd_core_bsf_ignored_for_transcoded_audio(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        track = AudioTrackSettings(stream_index=2, codec="eac3",
                                   bitrate_kbps=640, extract_truehd_core=True)
        cmd = self.wf.build_command(_make_config(src, tmp_path / "out.mkv",
                                                 audio_tracks=[track]))
        assert "truehd_core" not in cmd
        assert "-bsf:a:0" not in cmd
        assert "-c:a:0" in cmd and cmd[cmd.index("-c:a:0") + 1] == "eac3"

    def test_audio_track_order_follows_config_across_sources(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        alt = tmp_path / "alt.mkv"; alt.touch()
        tracks = [
            AudioTrackSettings(stream_index=5, codec="aac", bitrate_kbps=192, source_path=alt),
            AudioTrackSettings(stream_index=1, codec="copy", source_path=src),
        ]
        cmd = self.wf.build_command(_make_config(src, tmp_path / "out.mkv", audio_tracks=tracks))

        map_values = [cmd[i + 1] for i, arg in enumerate(cmd[:-1]) if arg == "-map"]
        assert map_values[:3] == ["0:v:0", "1:5", "0:1"]
        assert "-c:a:0" in cmd and cmd[cmd.index("-c:a:0") + 1] == "aac"
        assert "-b:a:0" in cmd and cmd[cmd.index("-b:a:0") + 1] == "192k"
        assert "-c:a:1" in cmd and cmd[cmd.index("-c:a:1") + 1] == "copy"

    def test_subtitle_track_order_follows_config_across_sources(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        alt = tmp_path / "alt.mkv"; alt.touch()
        cmd = self.wf.build_command(_make_config(
            src,
            tmp_path / "out.mkv",
            copy_subtitles=False,
            subtitle_tracks=[(alt, 7), (src, 4)],
        ))

        map_values = [cmd[i + 1] for i, arg in enumerate(cmd[:-1]) if arg == "-map"]
        assert map_values[:3] == ["0:v:0", "1:7", "0:4"]
        assert "-c:s" in cmd and cmd[cmd.index("-c:s") + 1] == "copy"

    def test_vaapi_single_pass_adds_device_and_hwupload_only_for_vaapi_codec(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        with patch.object(EncodeWorkflow, "_vaapi_device", return_value="/dev/dri/renderD128"):
            cmd = self.wf.build_command_single(
                _make_config(src, tmp_path / "out.mkv",
                             video=_make_video_settings(codec="hevc_vaapi"))
            )

        assert "-vaapi_device" in cmd
        assert cmd[cmd.index("-vaapi_device") + 1] == "/dev/dri/renderD128"
        assert cmd.index("-vaapi_device") < cmd.index("-i")
        assert "-vf" in cmd
        assert cmd[cmd.index("-vf") + 1] == "format=nv12,hwupload"

    def test_vaapi_two_pass_adds_device_and_hwupload_on_both_passes(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        with patch.object(EncodeWorkflow, "_vaapi_device", return_value="/dev/dri/renderD128"):
            cmds = self.wf.build_command(
                _make_config(
                    src,
                    tmp_path / "out.mkv",
                    video=_make_video_settings(codec="h264_vaapi", quality_mode=QualityMode.SIZE),
                    duration_s=3600.0,
                )
            )

        for pass_cmd in cmds:
            assert "-vaapi_device" in pass_cmd
            assert pass_cmd[pass_cmd.index("-vaapi_device") + 1] == "/dev/dri/renderD128"
            assert "-vf" in pass_cmd
            assert pass_cmd[pass_cmd.index("-vf") + 1] == "format=nv12,hwupload"

    def test_non_vaapi_codec_does_not_receive_vaapi_args(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        with patch.object(EncodeWorkflow, "_vaapi_device", return_value="/dev/dri/renderD128"):
            cmd = self.wf.build_command_single(
                _make_config(
                    src,
                    tmp_path / "out.mkv",
                    video=_make_video_settings(codec="libx265", tonemap_to_sdr=True),
                )
            )

        assert "-vaapi_device" not in cmd
        assert "-vf" in cmd
        assert "hwupload" not in cmd[cmd.index("-vf") + 1]


# ===========================================================================
# validate
# ===========================================================================

class TestValidate:

    def setup_method(self):
        self.wf = _make_workflow()

    def test_missing_source(self, tmp_path):
        config = _make_config(tmp_path / "absent.mkv", tmp_path / "out.mkv")
        errors = self.wf.validate(config)
        assert any("introuvable" in e.lower() or "source" in e.lower() for e in errors)

    def test_source_equals_output(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        errors = self.wf.validate(_make_config(src, src))
        assert len(errors) >= 1

    def test_output_dir_not_writable(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        config = _make_config(src, tmp_path / "out.mkv")
        with patch(
            "core.workflows.encode.workflow.tempfile.NamedTemporaryFile",
            side_effect=OSError("blocked"),
        ):
            errors = self.wf.validate(config)
        assert any("inscriptible" in e.lower() for e in errors)

    def test_size_mode_without_duration(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        config = _make_config(src, tmp_path / "out.mkv",
                              video=_make_video_settings(quality_mode=QualityMode.SIZE),
                              duration_s=None)
        errors = self.wf.validate(config)
        assert any("durée" in e.lower() or "taille" in e.lower() for e in errors)

    def test_copy_size_mode_without_duration_is_allowed(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        config = _make_config(
            src,
            tmp_path / "out.mkv",
            video=_make_video_settings(codec="copy", quality_mode=QualityMode.SIZE),
            duration_s=None,
        )
        errors = self.wf.validate(config)
        assert not any("durée" in e.lower() or "taille" in e.lower() for e in errors)

    def test_valid_config_no_errors(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        assert self.wf.validate(_make_config(src, tmp_path / "out.mkv")) == []

    def test_invalid_master_display(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        config = _make_config(src, tmp_path / "out.mkv",
                              video=_make_video_settings(inject_hdr_meta=True,
                                                         master_display="INVALID"))
        assert any("master_display" in e.lower() or "format" in e.lower()
                   for e in self.wf.validate(config))

    def test_invalid_max_cll(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        config = _make_config(src, tmp_path / "out.mkv",
                              video=_make_video_settings(inject_hdr_meta=True,
                                                         max_cll="INVALID"))
        assert any("cll" in e.lower() for e in self.wf.validate(config))

    def test_validate_rejects_negative_video_track_offset(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        config = _make_config(
            src,
            tmp_path / "out.mkv",
            track_time_offsets=[
                TrackTimeOffset(track_type="video", source_path=src, stream_index=0, offset_ms=-50)
            ],
        )
        errors = self.wf.validate(config)
        assert any("vidéo" in e.lower() and "négatif" in e.lower() for e in errors)


# ===========================================================================
# ProfileManager
# ===========================================================================

class TestProfileManager:

    def test_save_and_load_roundtrip(self, tmp_path):
        pm = ProfileManager(tmp_path)
        pm.save(EncodePreset(name="Test CRF18", codec="libx265", crf=18))
        loaded = pm.load_all()
        assert len(loaded) == 1
        assert loaded[0].name == "Test CRF18" and loaded[0].crf == 18

    def test_delete_removes_profile(self, tmp_path):
        pm = ProfileManager(tmp_path)
        pm.save(EncodePreset(name="ToDelete"))
        pm.delete("ToDelete")
        assert pm.load_all() == []

    def test_names_returns_all_names(self, tmp_path):
        pm = ProfileManager(tmp_path)
        pm.save(EncodePreset(name="Alpha"))
        pm.save(EncodePreset(name="Beta"))
        assert set(pm.names()) == {"Alpha", "Beta"}

    def test_load_all_empty_initially(self, tmp_path):
        assert ProfileManager(tmp_path).load_all() == []

    def test_save_overwrites_existing(self, tmp_path):
        pm = ProfileManager(tmp_path)
        pm.save(EncodePreset(name="P", crf=18))
        pm.save(EncodePreset(name="P", crf=28))
        loaded = pm.load_all()
        assert len(loaded) == 1 and loaded[0].crf == 28


# ===========================================================================
# _hdr_meta_args — comportement par codec
# ===========================================================================

class TestHdrMetaArgs:
    """Vérifie que _hdr_meta_args génère les bons flags selon le codec."""

    def setup_method(self):
        self.wf = _make_workflow()

    def _vs(self, codec: str, master: str = "G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,50)",
            max_cll: str = "343,203") -> VideoEncodeSettings:
        return _make_video_settings(codec=codec, inject_hdr_meta=True,
                                    master_display=master, max_cll=max_cll)

    def test_libx265_color_flags_only_no_standalone_master_display(self):
        """libx265 : couleur seulement — master_display/max_cll via -x265-params."""
        args = self.wf._hdr_meta_args(self._vs("libx265"))
        assert "-color_primaries" in args
        assert "-master_display" not in args
        assert "-max_cll" not in args

    def test_libx265_x265_params_contains_master_display(self, tmp_path):
        """libx265 : -x265-params fusionné avec master-display et max-cll."""
        src = tmp_path / "s.mkv"; src.touch()
        vs = self._vs("libx265")
        config = _make_config(src, tmp_path / "o.mkv", video=vs)
        cmd = self.wf.build_command_single(config)
        idx = cmd.index("-x265-params")
        params = cmd[idx + 1]
        assert "master-display=" in params
        assert "max-cll=" in params

    def test_libx265_x265_params_merges_extra_params(self, tmp_path):
        """libx265 : extra_params + HDR meta fusionnés dans un seul -x265-params."""
        src = tmp_path / "s.mkv"; src.touch()
        vs = _make_video_settings(codec="libx265", inject_hdr_meta=True,
                                   master_display="G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,50)",
                                   max_cll="343,203", extra_params="hdr-opt=1:repeat-headers=1")
        config = _make_config(src, tmp_path / "o.mkv", video=vs)
        cmd = self.wf.build_command_single(config)
        # Un seul -x265-params dans la commande
        assert cmd.count("-x265-params") == 1
        params = cmd[cmd.index("-x265-params") + 1]
        assert "hdr-opt=1" in params
        assert "master-display=" in params
        assert "max-cll=" in params

    def test_hevc_nvenc_adds_master_display_and_max_cll(self):
        """hevc_nvenc : -master_display et -max_cll ajoutés comme options encoder."""
        args = self.wf._hdr_meta_args(self._vs("hevc_nvenc"))
        assert "-color_primaries" in args
        assert "-master_display" in args
        assert "-max_cll" in args
        md_idx = args.index("-master_display")
        assert "G(8500" in args[md_idx + 1]

    def test_hevc_nvenc_no_master_display_when_empty(self):
        """hevc_nvenc : pas de -master_display si master_display vide."""
        vs = _make_video_settings(codec="hevc_nvenc", inject_hdr_meta=True,
                                   master_display="", max_cll="")
        args = self.wf._hdr_meta_args(vs)
        assert "-master_display" not in args
        assert "-max_cll" not in args
        assert "-color_primaries" in args

    def test_hevc_amf_color_flags_only(self):
        """hevc_amf : couleur seulement, pas de master_display/max_cll."""
        args = self.wf._hdr_meta_args(self._vs("hevc_amf"))
        assert "-color_primaries" in args
        assert "-master_display" not in args
        assert "-max_cll" not in args

    def test_hevc_qsv_color_flags_only(self):
        """hevc_qsv : couleur seulement."""
        args = self.wf._hdr_meta_args(self._vs("hevc_qsv"))
        assert "-color_primaries" in args
        assert "-master_display" not in args

    def test_libsvtav1_color_flags_only(self):
        """libsvtav1 : couleur seulement, pas de master_display/max_cll."""
        args = self.wf._hdr_meta_args(self._vs("libsvtav1"))
        assert "-color_primaries" in args
        assert "-master_display" not in args

    def test_copy_no_color_flags(self):
        """copy : aucun flag de couleur — pas pertinent pour un stream copié."""
        args = self.wf._hdr_meta_args(self._vs("copy"))
        assert args == []

    def test_h264_codecs_no_flags(self):
        """h264_* et libx264 : aucun flag HDR — H.264 est SDR."""
        for codec in ("libx264", "h264_nvenc", "h264_amf", "h264_qsv"):
            args = self.wf._hdr_meta_args(self._vs(codec))
            assert args == [], f"{codec} devrait retourner [] mais retourne {args}"


# ===========================================================================
# _run_with_metadata_inject — optimisation codec copy
# ===========================================================================

class TestMetadataInjectCopyCodec:
    """
    Vérifie que _run_with_metadata_inject fonctionne correctement quand il est
    appelé directement avec codec=copy (cas rare — en usage normal, run() dévie
    vers le chemin standard avant d'appeler cette méthode).

    Depuis la refactorisation copy :
      - aucune extraction source.hevc n'est créée
      - enc.hevc est produit directement par l'étape vidéo
      - test_copy_inject_completes_and_produces_output reste valide
    """

    def _run_inject_copy(self, tmp_path: Path, copy_dv: bool, copy_hdr10plus: bool):
        """Lance le workflow copy+inject et retourne (cmds_run, snapshots)."""
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 200_000)
        config = _make_config(
            source=src, output=tmp_path / "output.mkv",
            video=_make_video_settings(codec="copy"),
            copy_dv=copy_dv, copy_hdr10plus=copy_hdr10plus,
            work_dir=tmp_path / "work",
        )
        wf = _make_workflow(enabled=False)
        cmds_run: list[list[str]] = []
        snapshots: list[list[str]] = []

        def _fake_run(cmd, signals=None, cwd=None, progress_cb=None):
            cmds_run.append(list(cmd))
            for arg in reversed(cmd):
                s = str(arg)
                if any(s.endswith(ext) for ext in (".hevc", ".mkv", ".bin", ".json")):
                    p = Path(s)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"\x00" * 100_000)
                    break
            work = tmp_path / "work"
            if work.exists():
                names = [p.name for p in work.rglob("*")
                         if p.is_file() and p.suffix in (".hevc", ".mkv")]
                snapshots.append(names[:])
            return ""

        with patch.object(EncodeWorkflow, "_shm_path",
                          side_effect=lambda tmp_dir, name, _size: tmp_dir / name):
            with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run):
                sigs = wf._run_with_metadata_inject(config)
                _collect_signals(sigs)

        return cmds_run, snapshots

    def test_no_src_hevc_temp_created_for_copy(self, tmp_path):
        """Aucun src.hevc temporaire n'est créé dans le chemin codec=copy."""
        _, snapshots = self._run_inject_copy(tmp_path, copy_dv=True, copy_hdr10plus=False)
        assert all("src.hevc" not in snap for snap in snapshots)

    def test_enc_hevc_created_by_direct_encode_not_extraction(self, tmp_path):
        """
        enc.hevc est créé DIRECTEMENT par la commande d'encodage vidéo (-f hevc),
        pas par extraction depuis un encoded.mkv (qui n'existe plus).
        La commande 'ffmpeg -i encoded.mkv ... enc.hevc' ne doit pas apparaître.
        """
        cmds, _ = self._run_inject_copy(tmp_path, copy_dv=True, copy_hdr10plus=False)
        # Aucune commande ffmpeg avec encoded.mkv en entrée
        extraction_from_mkv = [
            c for c in cmds
            if "encoded.mkv" in " ".join(c)
            and any("encoded.mkv" in c[i+1] for i, a in enumerate(c[:-1]) if a == "-i")
        ]
        assert len(extraction_from_mkv) == 0, \
            f"Commande d'extraction depuis encoded.mkv trouvée (ne devrait pas exister) : {extraction_from_mkv}"
        # enc.hevc est créé par une commande ffmpeg avec -f hevc en sortie
        direct_encode_cmds = [
            c for c in cmds
            if "-f" in c and "hevc" in c and "enc.hevc" in " ".join(c)
            and not any("encoded.mkv" in c[i+1] for i, a in enumerate(c[:-1]) if a == "-i")
        ]
        assert len(direct_encode_cmds) >= 1, \
            "enc.hevc non créé par encodage direct (-f hevc)"

    def test_dv_extract_reads_source_mkv_directly(self, tmp_path):
        """L'extraction RPU lit directement source.mkv, sans src.hevc."""
        cmds, _ = self._run_inject_copy(tmp_path, copy_dv=True, copy_hdr10plus=False)
        extract_cmds = [c for c in cmds if c[:2] == ["dovi_tool", "extract-rpu"]]
        assert len(extract_cmds) == 1
        cmd = extract_cmds[0]
        idx = cmd.index("-i")
        assert cmd[idx + 1].endswith("source.mkv")

    def test_hdr10plus_extract_reads_source_mkv_directly(self, tmp_path):
        """L'extraction HDR10+ lit directement source.mkv, sans src.hevc."""
        cmds, _ = self._run_inject_copy(tmp_path, copy_dv=False, copy_hdr10plus=True)
        extract_cmds = [c for c in cmds if c[:2] == ["hdr10plus_tool", "extract"]]
        assert len(extract_cmds) == 1
        cmd = extract_cmds[0]
        assert cmd[2].endswith("source.mkv")

    @pytest.mark.parametrize("copy_dv,copy_hdr10plus", [
        (True, False), (False, True), (True, True)
    ])
    def test_copy_inject_completes_and_produces_output(self, tmp_path,
                                                        copy_dv, copy_hdr10plus):
        """Le workflow se termine et la commande ffmpeg de reconstitution finale est émise."""
        cmds, _ = self._run_inject_copy(tmp_path, copy_dv, copy_hdr10plus)
        # La reconstitution finale est une commande ffmpeg avec 2 inputs et output.mkv
        recon_cmds = [
            c for c in cmds
            if c[0] == "ffmpeg" and "output.mkv" in " ".join(c)
        ]
        assert len(recon_cmds) == 1, \
            f"Commande de reconstitution ffmpeg absente ou dupliquée. Cmds : {[c[0] for c in cmds]}"
        assert "output.mkv" in " ".join(recon_cmds[0])

    def test_copy_dv_injects_rpu_into_enc_hevc(self, tmp_path):
        """
        Depuis la refactorisation, dovi_tool inject-rpu opère sur enc.hevc
        (ou enc_hdr10p.hevc si HDR10+ a déjà été injecté).
        """
        cmds, _ = self._run_inject_copy(tmp_path, copy_dv=True, copy_hdr10plus=False)
        dv_injects = [c for c in cmds if "inject-rpu" in c]
        assert len(dv_injects) == 1
        # Le fichier d'entrée de inject-rpu doit être enc.hevc (ou enc_hdr10p.hevc)
        inj_cmd = dv_injects[0]
        i_flag = inj_cmd.index("-i")
        input_file = inj_cmd[i_flag + 1]
        assert "enc.hevc" in input_file or "enc_hdr10p.hevc" in input_file, \
            f"inject-rpu n'opère pas sur enc.hevc : {input_file}"


# ===========================================================================
# Codec COPY — passthrough métadonnées dans les commandes FFmpeg
# ===========================================================================

class TestCopyCodecMetadataPassthrough:
    """
    Vérifie que -map_metadata 0 et -map_metadata:s:v:0 0:s:v:0 sont injectés
    dans les commandes FFmpeg quand et seulement quand codec=copy.

    Justification :
      -map_metadata 0            : préserve titre, chapitres, attachments container.
      -map_metadata:s:v:0 0:s:v:0 : préserve color primaries, mastering display,
                                    content light level, etc. au niveau du stream.
      Avec -c:v copy les NAL SEI (RPU DoVi, HDR10+) sont déjà dans le bitstream.
    """

    def setup_method(self):
        self.wf = _make_workflow()

    def _cmd_copy(self, tmp_path, **video_kw) -> list[str]:
        src = tmp_path / "src.mkv"; src.touch()
        vs = _make_video_settings(codec="copy", **video_kw)
        return self.wf.build_command_single(
            _make_config(src, tmp_path / "out.mkv", video=vs)
        )

    def _cmd_x265(self, tmp_path) -> list[str]:
        src = tmp_path / "src.mkv"; src.touch()
        return self.wf.build_command_single(
            _make_config(src, tmp_path / "out.mkv",
                         video=_make_video_settings(codec="libx265"))
        )

    # ── single pass ──────────────────────────────────────────────────────────

    def test_copy_single_pass_has_map_metadata_global(self, tmp_path):
        """-map_metadata 0 présent dans la commande single pass (copy)."""
        cmd = self._cmd_copy(tmp_path)
        assert "-map_metadata" in cmd
        idx = cmd.index("-map_metadata")
        assert cmd[idx + 1] == "0"

    def test_copy_single_pass_has_map_metadata_stream(self, tmp_path):
        """-map_metadata:s:v:0 0:s:v:0 présent dans la commande single pass (copy)."""
        cmd = self._cmd_copy(tmp_path)
        assert "-map_metadata:s:v:0" in cmd
        idx = cmd.index("-map_metadata:s:v:0")
        assert cmd[idx + 1] == "0:s:v:0"

    def test_copy_single_pass_metadata_before_output(self, tmp_path):
        """-map_metadata apparaît avant le chemin de sortie."""
        src = tmp_path / "src.mkv"; src.touch()
        out = tmp_path / "out.mkv"
        vs = _make_video_settings(codec="copy")
        cmd = self.wf.build_command_single(
            _make_config(src, out, video=vs)
        )
        assert cmd.index("-map_metadata") < cmd.index(str(out))

    def test_non_copy_single_pass_no_map_metadata(self, tmp_path):
        """-map_metadata absent pour les codecs de réencodage (libx265)."""
        cmd = self._cmd_x265(tmp_path)
        assert "-map_metadata" not in cmd

    @pytest.mark.parametrize("codec", ["libx264", "libsvtav1", "hevc_nvenc", "hevc_amf"])
    def test_non_copy_codecs_no_map_metadata(self, tmp_path, codec):
        """-map_metadata absent pour tout codec de réencodage."""
        src = tmp_path / "src.mkv"; src.touch()
        cmd = self.wf.build_command_single(
            _make_config(src, tmp_path / "out.mkv",
                         video=_make_video_settings(codec=codec))
        )
        assert "-map_metadata" not in cmd

    # ── mode SIZE ─────────────────────────────────────────────────────────────

    def test_copy_size_mode_forces_single_pass_with_map_metadata(self, tmp_path):
        """codec=copy en mode SIZE reste en single-pass (pas de 2-pass)."""
        src = tmp_path / "src.mkv"; src.touch()
        vs = _make_video_settings(codec="copy", quality_mode=QualityMode.SIZE)
        cmd = self.wf.build_command(
            _make_config(src, tmp_path / "out.mkv", video=vs, duration_s=3600.0)
        )
        assert isinstance(cmd[0], str), "codec=copy doit retourner list[str] (single-pass)"
        assert "-pass" not in cmd
        assert "-map_metadata" in cmd
        idx = cmd.index("-map_metadata")
        assert cmd[idx + 1] == "0"

    def test_non_copy_two_pass_no_map_metadata(self, tmp_path):
        """Passe 2 sans -map_metadata pour codec de réencodage en mode SIZE."""
        src = tmp_path / "src.mkv"; src.touch()
        vs = _make_video_settings(codec="libx265", quality_mode=QualityMode.SIZE)
        cmds = self.wf.build_command(
            _make_config(src, tmp_path / "out.mkv", video=vs, duration_s=3600.0)
        )
        for pass_cmd in cmds:
            assert "-map_metadata" not in pass_cmd

    # ── interaction avec d'autres flags ──────────────────────────────────────

    def test_copy_with_audio_tracks_metadata_still_present(self, tmp_path):
        """-map_metadata présent même avec des pistes audio configurées."""
        src = tmp_path / "src.mkv"; src.touch()
        audio = [AudioTrackSettings(stream_index=1, codec="copy")]
        vs = _make_video_settings(codec="copy")
        cmd = self.wf.build_command_single(
            _make_config(src, tmp_path / "out.mkv", video=vs, audio_tracks=audio)
        )
        assert "-map_metadata" in cmd

    def test_copy_with_subtitles_metadata_still_present(self, tmp_path):
        """-map_metadata présent même avec copy_subtitles=True."""
        src = tmp_path / "src.mkv"; src.touch()
        vs = _make_video_settings(codec="copy")
        cmd = self.wf.build_command_single(
            _make_config(src, tmp_path / "out.mkv", video=vs, copy_subtitles=True)
        )
        assert "-map_metadata" in cmd


# ===========================================================================
# run() — codec COPY dévie vers chemin standard (pas _run_with_metadata_inject)
# ===========================================================================

class TestRunCopyBypassesInject:
    """
    Vérifie que run() n'appelle PAS _run_with_metadata_inject quand codec=copy,
    même si copy_dv ou copy_hdr10plus est activé.

    Comportement attendu :
      - codec=copy + copy_dv=True  → chemin standard FFmpeg (passthrough)
      - codec=copy + copy_hdr10plus=True → idem
      - codec=libx265 + copy_dv=True → _run_with_metadata_inject (inchangé)
      - log_message("INFO", "...passthrough...") émis quand copy est court-circuité
    """

    def _make_copy_config(self, tmp_path, copy_dv=False, copy_hdr10plus=False) -> EncodeConfig:
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 1000)
        return _make_config(
            source=src, output=tmp_path / "output.mkv",
            video=_make_video_settings(codec="copy"),
            copy_dv=copy_dv, copy_hdr10plus=copy_hdr10plus,
        )

    @pytest.mark.parametrize("copy_dv,copy_hdr10plus", [
        (True, False), (False, True), (True, True)
    ])
    def test_copy_codec_does_not_call_metadata_inject(self, tmp_path, copy_dv, copy_hdr10plus):
        """run() avec codec=copy ne doit jamais appeler _run_with_metadata_inject."""
        config = self._make_copy_config(tmp_path, copy_dv=copy_dv,
                                        copy_hdr10plus=copy_hdr10plus)
        wf = _make_workflow()
        inject_called = [False]

        original = wf._run_with_metadata_inject

        def _spy(cfg):
            inject_called[0] = True
            return original(cfg)

        with patch.object(wf, "_run_with_metadata_inject", side_effect=_spy), \
             patch.object(wf._runner, "run") as mock_run:
            mock_run.return_value = MagicMock()
            wf.run(config)

        assert not inject_called[0], \
            "codec=copy ne doit pas passer par _run_with_metadata_inject"

    def test_copy_codec_skips_dynamic_hdr_detection(self, tmp_path):
        """run() avec codec=copy ne doit pas lancer la détection ffprobe/mediainfo."""
        config = self._make_copy_config(tmp_path, copy_dv=True, copy_hdr10plus=True)
        wf = _make_workflow()
        detection_called = {"value": False}

        def _fake_detect(_source):
            detection_called["value"] = True
            return (True, True)

        with patch.object(wf, "_detect_source_dynamic_hdr_presence", side_effect=_fake_detect), \
             patch.object(wf._runner, "run") as mock_run:
            mock_run.return_value = MagicMock()
            wf.run(config)

        assert detection_called["value"] is False, \
            "La détection HDR dynamique ne doit pas être appelée en codec=copy."

    def test_non_copy_with_copy_dv_calls_metadata_inject(self, tmp_path):
        """run() avec codec=libx265 + copy_dv=True DOIT appeler _run_with_metadata_inject."""
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 1000)
        config = _make_config(
            source=src, output=tmp_path / "output.mkv",
            video=_make_video_settings(codec="libx265"),
            copy_dv=True,
        )
        wf = _make_workflow()
        inject_called = [False]

        def _spy(cfg):
            inject_called[0] = True
            return MagicMock()

        with patch.object(wf, "_detect_source_dynamic_hdr_presence", return_value=(True, False)), \
             patch.object(wf, "_run_with_metadata_inject", side_effect=_spy):
            wf.run(config)

        assert inject_called[0], \
            "codec≠copy + copy_dv doit passer par _run_with_metadata_inject"

    def test_non_copy_with_copy_dv_but_no_hdr_presence_uses_standard_runner(self, tmp_path):
        """Si aucun DV/HDR10+ détecté, codec≠copy reste sur le chemin encode standard."""
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 1000)
        config = _make_config(
            source=src,
            output=tmp_path / "output.mkv",
            video=_make_video_settings(codec="libx265"),
            copy_dv=True,
        )
        wf = _make_workflow()
        inject_called = [False]

        def _spy(_cfg):
            inject_called[0] = True
            return MagicMock()

        with patch.object(wf, "_detect_source_dynamic_hdr_presence", return_value=(False, False)), \
             patch.object(wf, "_run_with_metadata_inject", side_effect=_spy), \
             patch.object(wf._runner, "run") as mock_run:
            mock_run.return_value = MagicMock()
            wf.run(config)

        assert not inject_called[0], "Sans DV/HDR10+ source, l'injection ne doit pas être lancée."
        assert mock_run.called, "Le chemin encode standard doit rester utilisé."

    @pytest.mark.parametrize("copy_dv,copy_hdr10plus", [
        (True, False), (False, True), (True, True)
    ])
    def test_copy_codec_emits_passthrough_log(self, tmp_path, copy_dv, copy_hdr10plus):
        """run() émet un log INFO 'passthrough' quand copy_dv/hdr10+ est ignoré."""
        config = self._make_copy_config(tmp_path, copy_dv=copy_dv,
                                        copy_hdr10plus=copy_hdr10plus)
        wf = _make_workflow()
        log_msgs: list[tuple[str, str]] = []
        wf.log_message.connect(lambda lvl, msg: log_msgs.append((lvl, msg)))

        with patch.object(wf._runner, "run") as mock_run:
            mock_run.return_value = MagicMock()
            wf.run(config)

        passthrough_logs = [
            (lvl, msg) for lvl, msg in log_msgs
            if lvl == "INFO" and "passthrough" in msg.lower()
        ]
        assert len(passthrough_logs) >= 1, \
            f"Aucun log INFO 'passthrough' émis. Logs reçus : {log_msgs}"

    def test_copy_no_flags_uses_standard_ffmpeg_runner(self, tmp_path):
        """run() avec codec=copy sans copy_dv/hdr10+ utilise le runner standard."""
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 1000)
        config = _make_config(
            source=src, output=tmp_path / "output.mkv",
            video=_make_video_settings(codec="copy"),
        )
        wf = _make_workflow()
        with patch.object(wf._runner, "run") as mock_run:
            mock_run.return_value = MagicMock()
            wf.run(config)


class TestRunIntegratedMetadata:
    def _base_config(self, tmp_path: Path) -> EncodeConfig:
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 1000)
        return _make_config(
            source=src,
            output=tmp_path / "output.mkv",
            video=_make_video_settings(codec="copy"),
        )

    def test_chapter_overrides_stays_on_single_pass_runner(self, tmp_path):
        cfg = self._base_config(tmp_path)
        cfg.chapter_overrides = []
        wf = _make_workflow()

        with patch.object(wf._runner, "run") as mock_run:
            mock_run.return_value = MagicMock()
            wf.run(cfg)

        assert mock_run.called

    def test_writing_application_stays_on_single_pass_runner(self, tmp_path):
        cfg = self._base_config(tmp_path)
        wf = EncodeWorkflow(
            ffmpeg_bin="ffmpeg",
            dovi_tool_bin="dovi_tool",
            hdr10plus_bin="hdr10plus_tool",
            writing_application="MediarecodeTest",
        )

        with patch.object(wf._runner, "run") as mock_run:
            mock_run.return_value = MagicMock()
            wf.run(cfg)

        assert mock_run.called


class TestInjectStorageChecks:
    def _make_inject_config(self, tmp_path: Path) -> EncodeConfig:
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 200_000)
        return _make_config(
            source=src,
            output=tmp_path / "output.mkv",
            video=_make_video_settings(codec="libx265"),
            copy_dv=True,
            work_dir=tmp_path / "work",
        )

    def test_inject_path_checks_storage_before_launch(self, tmp_path):
        cfg = self._make_inject_config(tmp_path)
        wf = _make_workflow()
        logs: list[tuple[str, str]] = []
        wf.log_message.connect(lambda lvl, msg: logs.append((lvl, msg)))

        with patch.object(wf, "_detect_source_dynamic_hdr_presence", return_value=(True, False)), \
             patch.object(wf, "_estimate_inject_storage_requirements", return_value=(100, 200)) as mock_est, \
             patch("core.workflows.encode.workflow.shutil.disk_usage") as mock_du, \
             patch.object(wf, "_run_with_metadata_inject") as mock_inject:
            mock_du.return_value = SimpleNamespace(total=10_000_000, used=1, free=9_000_000)
            mock_inject.return_value = MagicMock()
            wf.run(cfg)

        assert mock_est.called
        assert mock_inject.called
        assert any("Estimation espace injection" in msg for _lvl, msg in logs), \
            f"Log d'estimation absent. Logs: {logs}"

    def test_inject_path_raises_when_storage_is_insufficient(self, tmp_path):
        cfg = self._make_inject_config(tmp_path)
        wf = _make_workflow()

        with patch.object(wf, "_detect_source_dynamic_hdr_presence", return_value=(True, False)), \
             patch.object(wf, "_estimate_inject_storage_requirements", return_value=(10_000, 20_000)), \
             patch("core.workflows.encode.workflow.shutil.disk_usage") as mock_du, \
             patch.object(wf, "_run_with_metadata_inject") as mock_inject:
            mock_du.return_value = SimpleNamespace(total=100_000, used=99_900, free=100)
            with pytest.raises(EncodeError, match="Espace disque insuffisant"):
                wf.run(cfg)

        assert not mock_inject.called


class TestDynamicHdrDetection:
    def test_detect_source_dynamic_hdr_presence_falls_back_to_mediainfo(self, tmp_path):
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 1000)
        wf = _make_workflow()

        ffprobe_payload = json.dumps({
            "streams": [
                {
                    "codec_type": "video",
                    "side_data_list": [],
                }
            ]
        })

        def _fake_subprocess_run(cmd, *args, **kwargs):
            prog = Path(cmd[0]).name
            if prog.startswith("ffprobe"):
                return MagicMock(returncode=0, stdout=ffprobe_payload, stderr="")
            if prog == "mediainfo":
                if cmd[1] == "--Inform=Video;%HDR_Format%":
                    return MagicMock(returncode=0, stdout="Dolby Vision", stderr="")
                if cmd[1] == "--Inform=Video;%HDR_Format_Compatibility%":
                    return MagicMock(returncode=0, stdout="HDR10+", stderr="")
            raise AssertionError(f"Commande subprocess inattendue: {cmd}")

        with patch("subprocess.run", side_effect=_fake_subprocess_run):
            assert wf._detect_source_dynamic_hdr_presence(src) == (True, True)


class TestIntegratedMetadataCommand:
    def test_tag_overrides_disable_metadata_copy(self, tmp_path):
        src = tmp_path / "source.mkv"; src.write_bytes(b"\x00")
        out = tmp_path / "output.mkv"
        cfg = _make_config(
            src,
            out,
            video=_make_video_settings(codec="copy"),
            tag_overrides={"GENRE": "Drama"},
            file_title="Titre",
        )
        wf = _make_workflow()
        cmd = wf.build_command_single(cfg)

        idx = cmd.index("-map_metadata")
        # Avec tag_overrides, les tags sources sont ignorés mais les chapitres
        # restent préservés via chapter_map (fallback = input 0). On vérifie
        # que la source des tags globaux == source des chapitres.
        chap_idx = cmd.index("-map_chapters")
        assert cmd[idx + 1] == cmd[chap_idx + 1]
        assert "GENRE=Drama" in cmd
        assert "title=Titre" in cmd

    def test_chapter_overrides_with_tag_overrides_map_metadata_from_chapter_input(self, tmp_path):
        src = tmp_path / "source.mkv"; src.write_bytes(b"\x00")
        out = tmp_path / "output.mkv"
        cfg = _make_config(
            src,
            out,
            video=_make_video_settings(codec="copy"),
            tag_overrides={"GENRE": "Drama"},
            chapter_overrides=[
                ChapterEntry(timecode_s=0.0, name="Intro"),
                ChapterEntry(timecode_s=30.0, name="Outro"),
            ],
        )
        wf = _make_workflow()

        cmd = wf.build_command_single(cfg)

        idx = cmd.index("-map_metadata")
        assert cmd[idx + 1] == "1"
        chap_idx = cmd.index("-map_chapters")
        assert cmd[chap_idx + 1] == "1"

    def test_tag_sources_copy_from_last_source(self, tmp_path):
        src = tmp_path / "source.mkv"; src.write_bytes(b"\x00")
        tag1 = tmp_path / "tag1.mkv"; tag1.write_bytes(b"\x00")
        tag2 = tmp_path / "tag2.mkv"; tag2.write_bytes(b"\x00")
        out = tmp_path / "output.mkv"
        cfg = _make_config(
            src,
            out,
            video=_make_video_settings(codec="copy"),
            tag_sources=[tag1, tag2],
        )
        wf = _make_workflow()
        cmd = wf.build_command_single(cfg)

        assert str(tag2) in cmd
        idx = cmd.index("-map_metadata")
        assert cmd[idx + 1] == "1"

    def test_empty_chapter_overrides_remove_chapters(self, tmp_path):
        src = tmp_path / "source.mkv"; src.write_bytes(b"\x00")
        out = tmp_path / "output.mkv"
        cfg = _make_config(
            src,
            out,
            video=_make_video_settings(codec="copy"),
            chapter_overrides=[],
        )
        wf = _make_workflow()
        cmd = wf.build_command_single(cfg)

        idx = cmd.index("-map_chapters")
        assert cmd[idx + 1] == "-1"

    def test_main_command_does_not_force_muxing_application_tag(self, tmp_path):
        src = tmp_path / "source.mkv"; src.write_bytes(b"\x00")
        out = tmp_path / "output.mkv"
        cfg = _make_config(src, out, video=_make_video_settings(codec="copy"))
        wf = EncodeWorkflow(
            ffmpeg_bin="ffmpeg",
            dovi_tool_bin="dovi_tool",
            hdr10plus_bin="hdr10plus_tool",
            writing_application="MediarecodeMuxApp",
        )
        cmd = wf.build_command_single(cfg)
        assert not any("muxing_application=" in str(arg) for arg in cmd)

    def test_main_command_includes_threads_argument(self, tmp_path):
        src = tmp_path / "source.mkv"; src.write_bytes(b"\x00")
        out = tmp_path / "output.mkv"
        cfg = _make_config(
            src,
            out,
            video=_make_video_settings(codec="copy"),
            tag_overrides={},
        )
        wf = _make_workflow(ffmpeg_threads=11)

        cmd = wf.build_command_single(cfg)

        assert "-threads" in cmd
        assert cmd[cmd.index("-threads") + 1] == "11"


class TestSelectedAttachedPicHandling:

    def test_run_converts_selected_cover_to_attach(self, tmp_path):
        """
        Un attachment sélectionné reporté par ffprobe comme attached_pic ne doit
        plus être remappé via -map 0:stream, mais extrait puis réattaché avec
        -attach pour conserver son statut d'attachment logique.
        """
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 1000)
        out = tmp_path / "output.mkv"
        config = _make_config(
            source=src,
            output=out,
            video=_make_video_settings(codec="copy"),
            audio_tracks=[AudioTrackSettings(stream_index=1, codec="ac3", bitrate_kbps=384)],
            attachment_streams=[(src, 9)],
        )
        wf = _make_workflow()
        captured: list[list[str]] = []

        ffprobe_payload = json.dumps({
            "streams": [
                {
                    "index": 9,
                    "disposition": {"attached_pic": 1},
                    "tags": {"filename": "cover.jpg", "mimetype": "image/jpeg"},
                }
            ]
        })

        def _fake_subprocess_run(cmd, *args, **kwargs):
            prog = Path(cmd[0]).name
            if prog.startswith("ffprobe"):
                return MagicMock(returncode=0, stdout=ffprobe_payload, stderr="")
            if prog == "ffmpeg" and "-map" in cmd and "0:9" in cmd:
                Path(cmd[-1]).write_bytes(b"jpeg-data")
                return MagicMock(returncode=0, stdout="", stderr="")
            raise AssertionError(f"Commande subprocess inattendue: {cmd}")

        with patch("subprocess.run", side_effect=_fake_subprocess_run), \
             patch.object(wf._runner, "run") as mock_run:
            signals = TaskSignals()
            mock_run.side_effect = lambda cmd, **_kw: captured.append(list(cmd)) or signals
            returned = wf.run(config)
            returned.finished.emit("done")

        assert returned is signals
        assert len(captured) == 1
        cmd = captured[0]
        assert "-attach" in cmd
        attach_path = Path(cmd[cmd.index("-attach") + 1])
        assert attach_path.name == "cover.jpg"
        assert "0:9" not in cmd, f"Le stream cover ne doit plus être remappé: {cmd}"
        assert "-metadata:s:t:0" in cmd
        idx = cmd.index("-metadata:s:t:0")
        assert cmd[idx + 1] == "mimetype=image/jpeg"
        assert mock_run.called


# ===========================================================================
# _run_with_metadata_inject — pistes audio dans la reconstitution finale
# ===========================================================================

class TestMetadataInjectAudio:
    """
    Vérifie que la commande de reconstitution finale dans _run_with_metadata_inject
    contient les bons arguments audio (map, codec, bitrate, BSF).

    Contexte :
      - La reconstitution ffmpeg utilise deux inputs :
          input 0 = current_hevc (flux vidéo injecté)
          input 1 = source (audio + subs)
      - L'audio est donc mappé via "-map 1:{stream_index}".
      - Les arguments codec sont appliqués par output stream index (0-based).

    Scénarios testés :
      - Aucune piste audio → aucun -map audio ni -c:a dans la reconstitution
      - Audio copy → -map 1:N, -c:a:0 copy, pas de -b:a:0
      - Audio AAC → -map 1:N, -c:a:0 aac, -b:a:0 NNNk
      - Audio EAC3 → -map 1:N, -c:a:0 eac3, -b:a:0 NNNk
      - Audio FLAC → -map 1:N, -c:a:0 flac, pas de -b:a:0
      - Plusieurs pistes → -map 1:N1, -c:a:0, -map 1:N2, -c:a:1
      - TrueHD core → -bsf:a:0 truehd_core avant -c:a:0 copy
      - copy_subtitles=True → -map 1:s? -c:s copy dans la reconstitution
    """

    def _run_inject_with_audio(
        self,
        tmp_path: Path,
        audio_tracks: list,
        copy_subtitles: bool = False,
    ) -> list[list[str]]:
        """
        Lance _run_with_metadata_inject (libx265 + copy_dv) avec les pistes audio données.
        Retourne la liste de toutes les commandes exécutées.
        """
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 200_000)
        config = _make_config(
            source=src,
            output=tmp_path / "output.mkv",
            video=_make_video_settings(codec="libx265"),
            audio_tracks=audio_tracks,
            copy_subtitles=copy_subtitles,
            copy_dv=True,
            work_dir=tmp_path / "work",
        )
        wf = _make_workflow(enabled=False)
        cmds_run: list[list[str]] = []

        def _fake_run(cmd, signals=None, cwd=None, progress_cb=None):
            cmds_run.append(list(cmd))
            for arg in reversed(cmd):
                s = str(arg)
                if any(s.endswith(ext) for ext in (".hevc", ".mkv", ".bin", ".json")):
                    p = Path(s)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"\x00" * 100_000)
                    break
            return ""

        with patch.object(EncodeWorkflow, "_shm_path",
                          side_effect=lambda tmp_dir, name, _size: tmp_dir / name):
            with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run):
                sigs = wf._run_with_metadata_inject(config)
                _collect_signals(sigs)

        return cmds_run

    def _get_recon_cmd(self, cmds: list[list[str]]) -> list[str]:
        """Extrait la commande de reconstitution finale (ffmpeg avec output.mkv)."""
        recon = [c for c in cmds if c[0] == "ffmpeg" and "output.mkv" in " ".join(c)]
        assert len(recon) == 1, f"Commande de reconstitution introuvable. Cmds: {[c[0] for c in cmds]}"
        return recon[0]

    def test_no_audio_tracks_no_audio_args(self, tmp_path):
        """Sans pistes audio, la reconstitution ne contient ni -map audio ni -c:a."""
        cmds = self._run_inject_with_audio(tmp_path, audio_tracks=[])
        recon = self._get_recon_cmd(cmds)
        assert "-c:a:0" not in recon
        assert not any(arg.startswith("-c:a") for arg in recon)

    def test_audio_copy_maps_from_source_input(self, tmp_path):
        """Audio copy : mappé depuis l'input 1 (source), pas l'input 0 (HEVC)."""
        track = AudioTrackSettings(stream_index=2, codec="copy")
        cmds = self._run_inject_with_audio(tmp_path, [track])
        recon = self._get_recon_cmd(cmds)
        assert "-map" in recon
        assert "1:2" in recon, f"Attendu -map 1:2 (source input 1). Reconstitution: {recon}"
        # Pas de -map 0:2 (l'HEVC n'a pas d'audio)
        cmd_str = " ".join(recon)
        assert "0:2" not in cmd_str, "Audio ne doit pas être mappé depuis le HEVC (input 0)"

    def test_audio_copy_no_bitrate(self, tmp_path):
        """Audio copy : pas de -b:a dans la reconstitution."""
        track = AudioTrackSettings(stream_index=1, codec="copy", bitrate_kbps=384)
        cmds = self._run_inject_with_audio(tmp_path, [track])
        recon = self._get_recon_cmd(cmds)
        assert "-b:a:0" not in recon
        assert "-c:a:0" in recon
        assert "copy" in recon

    def test_audio_aac_has_codec_and_bitrate(self, tmp_path):
        """Audio AAC : -c:a:0 aac et -b:a:0 NNNk présents dans la reconstitution."""
        track = AudioTrackSettings(stream_index=1, codec="aac", bitrate_kbps=192)
        cmds = self._run_inject_with_audio(tmp_path, [track])
        recon = self._get_recon_cmd(cmds)
        assert "-c:a:0" in recon
        assert "aac" in recon
        assert "-b:a:0" in recon
        assert "192k" in recon

    def test_audio_eac3_has_codec_and_bitrate(self, tmp_path):
        """Audio EAC3 : -c:a:0 eac3 et -b:a:0 NNNk présents dans la reconstitution."""
        track = AudioTrackSettings(stream_index=1, codec="eac3", bitrate_kbps=640)
        cmds = self._run_inject_with_audio(tmp_path, [track])
        recon = self._get_recon_cmd(cmds)
        assert "eac3" in recon
        assert "640k" in recon

    def test_audio_eac3_7_1_is_downmixed_to_5_1_in_reconstitution(self, tmp_path):
        track = AudioTrackSettings(
            stream_index=1,
            codec="eac3",
            bitrate_kbps=640,
            input_channels=8,
            input_channel_layout="7.1(side)",
        )
        cmds = self._run_inject_with_audio(tmp_path, [track])
        recon = self._get_recon_cmd(cmds)
        assert "-ac:a:0" in recon and recon[recon.index("-ac:a:0") + 1] == "6"
        assert "-channel_layout:a:0" in recon and recon[recon.index("-channel_layout:a:0") + 1] == "5.1"

    def test_audio_flac_no_bitrate(self, tmp_path):
        """Audio FLAC : -c:a:0 flac sans -b:a (codec sans débit)."""
        track = AudioTrackSettings(stream_index=1, codec="flac")
        cmds = self._run_inject_with_audio(tmp_path, [track])
        recon = self._get_recon_cmd(cmds)
        assert "flac" in recon
        assert "-b:a:0" not in recon

    def test_multiple_audio_tracks(self, tmp_path):
        """Plusieurs pistes : chaque track mappée depuis source avec le bon index de sortie."""
        tracks = [
            AudioTrackSettings(stream_index=1, codec="copy"),
            AudioTrackSettings(stream_index=3, codec="aac", bitrate_kbps=256),
        ]
        cmds = self._run_inject_with_audio(tmp_path, tracks)
        recon = self._get_recon_cmd(cmds)
        # Piste 0 : copy depuis stream 1 de la source
        assert "1:1" in recon
        assert "-c:a:0" in recon
        # Piste 1 : aac depuis stream 3 de la source
        assert "1:3" in recon
        assert "-c:a:1" in recon
        assert "256k" in recon

    def test_reordered_audio_tracks_across_sources_preserved_in_reconstitution(self, tmp_path):
        alt = tmp_path / "alt.mkv"
        tracks = [
            AudioTrackSettings(stream_index=5, codec="aac", bitrate_kbps=192, source_path=alt),
            AudioTrackSettings(stream_index=1, codec="copy"),
        ]
        cmds = self._run_inject_with_audio(tmp_path, tracks)
        recon = self._get_recon_cmd(cmds)

        map_values = [recon[i + 1] for i, arg in enumerate(recon[:-1]) if arg == "-map"]
        assert map_values[:3] == ["0:v:0", "2:5", "1:1"]
        assert "-c:a:0" in recon and recon[recon.index("-c:a:0") + 1] == "aac"
        assert "-b:a:0" in recon and recon[recon.index("-b:a:0") + 1] == "192k"
        assert "-c:a:1" in recon and recon[recon.index("-c:a:1") + 1] == "copy"

    def test_truehd_core_bsf_in_reconstitution(self, tmp_path):
        """extract_truehd_core=True : -bsf:a:0 truehd_core dans la reconstitution."""
        track = AudioTrackSettings(stream_index=1, codec="copy", extract_truehd_core=True)
        cmds = self._run_inject_with_audio(tmp_path, [track])
        recon = self._get_recon_cmd(cmds)
        cmd_str = " ".join(recon)
        assert "truehd_core" in cmd_str, \
            f"BSF truehd_core absent de la reconstitution. Cmd: {recon}"

    def test_copy_subtitles_mapped_from_source(self, tmp_path):
        """copy_subtitles=True : -map 1:s? -c:s copy dans la reconstitution."""
        cmds = self._run_inject_with_audio(tmp_path, audio_tracks=[], copy_subtitles=True)
        recon = self._get_recon_cmd(cmds)
        cmd_str = " ".join(recon)
        assert "1:s?" in cmd_str, \
            f"Subs non mappés depuis source (attendu '1:s?'). Cmd: {recon}"
        assert "-c:s" in recon and "copy" in recon


# ===========================================================================
# file_title — balise Title du segment dans les commandes FFmpeg
# ===========================================================================

class TestEncodeFileTitleCommand:
    """
    Vérifie que -metadata title=<value> est injecté dans chaque variante
    de build_command (single-pass, two-pass pass2) avec la valeur exacte.
    """

    def setup_method(self):
        self.wf = _make_workflow()

    def _src(self, tmp_path: Path) -> Path:
        src = tmp_path / "src.mkv"
        src.touch()
        return src

    # ── single pass ──────────────────────────────────────────────────────────

    def test_single_pass_title_flag_present(self, tmp_path):
        """-metadata title=... présent dans le single pass."""
        src = self._src(tmp_path)
        cmd = self.wf.build_command_single(
            _make_config(src, tmp_path / "out.mkv", file_title="Mon Film")
        )
        assert "-metadata" in cmd
        idx = cmd.index("-metadata")
        assert cmd[idx + 1] == "title=Mon Film"

    def test_single_pass_title_empty(self, tmp_path):
        """-metadata title= (vide) présent si file_title=''."""
        src = self._src(tmp_path)
        cmd = self.wf.build_command_single(
            _make_config(src, tmp_path / "out.mkv", file_title="")
        )
        assert "-metadata" in cmd
        idx = cmd.index("-metadata")
        assert cmd[idx + 1] == "title="

    def test_single_pass_title_before_output(self, tmp_path):
        """-metadata title= précède le chemin de sortie."""
        src = self._src(tmp_path)
        out = tmp_path / "out.mkv"
        cmd = self.wf.build_command_single(
            _make_config(src, out, file_title="Film")
        )
        assert cmd.index("-metadata") < cmd.index(str(out))

    # ── two pass (mode SIZE) ─────────────────────────────────────────────────

    def test_two_pass_pass2_title_present(self, tmp_path):
        """-metadata title=... présent dans la passe 2 (mode SIZE)."""
        src = self._src(tmp_path)
        vs = _make_video_settings(quality_mode=QualityMode.SIZE)
        cmds = self.wf.build_command(
            _make_config(src, tmp_path / "out.mkv", video=vs,
                         duration_s=3600.0, file_title="Film Encode")
        )
        pass2 = cmds[1]
        assert "-metadata" in pass2
        idx = pass2.index("-metadata")
        assert pass2[idx + 1] == "title=Film Encode"

    def test_two_pass_pass1_no_title(self, tmp_path):
        """-metadata absent de la passe 1 (analyse seule)."""
        src = self._src(tmp_path)
        vs = _make_video_settings(quality_mode=QualityMode.SIZE)
        cmds = self.wf.build_command(
            _make_config(src, tmp_path / "out.mkv", video=vs,
                         duration_s=3600.0, file_title="Film Encode")
        )
        pass1 = cmds[0]
        assert "-metadata" not in pass1

    # ── reconstitution finale (_run_with_metadata_inject) ────────────────────

    def _run_inject(self, tmp_path: Path, file_title: str) -> list[str]:
        """Lance _run_with_metadata_inject et retourne la commande de reconstitution."""
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 200_000)
        config = _make_config(
            source=src,
            output=tmp_path / "output.mkv",
            video=_make_video_settings(codec="libx265"),
            copy_dv=True,
            work_dir=tmp_path / "work",
            file_title=file_title,
        )
        wf = _make_workflow(enabled=False)
        cmds_run: list[list[str]] = []

        def _fake_run(cmd, signals=None, cwd=None, progress_cb=None):
            cmds_run.append(list(cmd))
            for arg in reversed(cmd):
                s = str(arg)
                if any(s.endswith(ext) for ext in (".hevc", ".mkv", ".bin", ".json")):
                    p = Path(s)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"\x00" * 100_000)
                    break
            return ""

        with patch.object(EncodeWorkflow, "_shm_path",
                          side_effect=lambda tmp_dir, name, _size: tmp_dir / name):
            with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run):
                sigs = wf._run_with_metadata_inject(config)
                _collect_signals(sigs)

        recon = [c for c in cmds_run if c[0] == "ffmpeg" and "output.mkv" in " ".join(c)]
        assert len(recon) == 1, f"Commande de reconstitution introuvable. Cmds: {cmds_run}"
        return recon[0]

    def test_inject_path_title_present(self, tmp_path):
        """-metadata title=... présent dans la reconstitution finale (DV/HDR10+)."""
        recon = self._run_inject(tmp_path, "Mon Film DV")
        assert "-metadata" in recon
        idx = recon.index("-metadata")
        assert recon[idx + 1] == "title=Mon Film DV"

    def test_inject_path_title_empty(self, tmp_path):
        """-metadata title= (vide) présent si file_title='' dans le chemin DV/HDR10+."""
        recon = self._run_inject(tmp_path, "")
        assert "-metadata" in recon
        idx = recon.index("-metadata")
        assert recon[idx + 1] == "title="

    def test_inject_path_title_before_output(self, tmp_path):
        """-metadata title= précède le chemin de sortie dans la reconstitution finale."""
        recon = self._run_inject(tmp_path, "Film DV")
        meta_idx = recon.index("-metadata")
        # output.mkv est le dernier argument de la commande de reconstitution
        assert meta_idx < len(recon) - 1


class TestInjectPathIntegratedPostproc:
    def _run_inject(self, tmp_path: Path, **config_overrides) -> list[list[str]]:
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 200_000)
        default_cfg = dict(
            source=src,
            output=tmp_path / "output.mkv",
            video=_make_video_settings(codec="libx265"),
            copy_dv=True,
            work_dir=tmp_path / "work",
            file_title="Film DV",
        )
        default_cfg.update(config_overrides)
        config = _make_config(**default_cfg)
        wf = EncodeWorkflow(
            ffmpeg_bin="ffmpeg",
            dovi_tool_bin="dovi_tool",
            hdr10plus_bin="hdr10plus_tool",
            writing_application="MediarecodeMuxApp",
            ram_buffer_enabled=False,
        )

        cmds_run: list[list[str]] = []

        def _fake_run(cmd, signals=None, cwd=None, progress_cb=None):
            cmds_run.append(list(cmd))
            for arg in reversed(cmd):
                s = str(arg)
                if any(s.endswith(ext) for ext in (".hevc", ".mkv", ".bin", ".json", ".ffmetadata")):
                    p = Path(s)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"\x00" * 100_000)
                    break
            return ""

        with patch.object(EncodeWorkflow, "_shm_path", side_effect=lambda tmp_dir, name, _size: tmp_dir / name):
            with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run):
                sigs = wf._run_with_metadata_inject(config)
                _collect_signals(sigs)
        return cmds_run

    def _get_recon_cmd(self, cmds: list[list[str]]) -> list[str]:
        recon = [c for c in cmds if c[0] == "ffmpeg" and "output.mkv" in " ".join(c)]
        assert len(recon) == 1, f"Attendu une seule commande ffmpeg de sortie, obtenu {len(recon)}"
        return recon[0]

    def test_recon_applies_postproc_features_in_single_pass(self, tmp_path):
        cfg = dict(
            tag_overrides={"GENRE": "Drama"},
            chapter_overrides=[],
            track_meta_edits=[TrackMetaEdit(track_order=1, language="fr-FR", title="Main Video")],
        )
        cmds = self._run_inject(tmp_path, **cfg)
        recon = self._get_recon_cmd(cmds)

        assert "-map_metadata" in recon
        assert recon[recon.index("-map_metadata") + 1] == "-1"
        assert "-map_chapters" in recon
        assert recon[recon.index("-map_chapters") + 1] == "-1"
        assert "GENRE=Drama" in recon
        assert not any("muxing_application=" in str(arg) for arg in recon)
        assert "-metadata:s:v:0" in recon
        assert "language=fr-FR" in recon
        assert "language-ietf=" in recon
        assert "title=Main Video" in recon

    def test_inject_path_calls_matroska_header_patch_hook(self, tmp_path):
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 200_000)
        config = _make_config(
            source=src,
            output=tmp_path / "output.mkv",
            video=_make_video_settings(codec="libx265"),
            copy_dv=True,
            work_dir=tmp_path / "work",
        )
        wf = EncodeWorkflow(
            ffmpeg_bin="ffmpeg",
            dovi_tool_bin="dovi_tool",
            hdr10plus_bin="hdr10plus_tool",
            writing_application="MediarecodeMuxApp",
            ram_buffer_enabled=False,
        )

        def _fake_run(cmd, signals=None, cwd=None, progress_cb=None):
            for arg in reversed(cmd):
                s = str(arg)
                if any(s.endswith(ext) for ext in (".hevc", ".mkv", ".bin", ".json", ".ffmetadata")):
                    p = Path(s)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"\x00" * 64_000)
                    break
            return ""

        with patch.object(EncodeWorkflow, "_shm_path", side_effect=lambda tmp_dir, name, _size: tmp_dir / name):
            with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run):
                with patch.object(wf._muxing_post_action, "apply_if_mkv") as patch_hook:
                    sigs = wf._run_with_metadata_inject(config)
                    _collect_signals(sigs)

        assert patch_hook.called

    def test_recon_maps_metadata_from_last_tag_source(self, tmp_path):
        tag1 = tmp_path / "tag1.mkv"
        tag2 = tmp_path / "tag2.mkv"
        tag1.write_bytes(b"\x00")
        tag2.write_bytes(b"\x00")

        cmds = self._run_inject(
            tmp_path,
            tag_sources=[tag1, tag2],
            tag_overrides=None,
            chapter_overrides=None,
        )
        recon = self._get_recon_cmd(cmds)

        assert str(tag2) in recon
        assert "-map_metadata" in recon
        idx = recon.index("-map_metadata")
        assert recon[idx + 1] == "2"

    def test_recon_uses_chapter_input_metadata_when_tags_are_overridden(self, tmp_path):
        cmds = self._run_inject(
            tmp_path,
            tag_overrides={"GENRE": "Drama"},
            chapter_overrides=[
                ChapterEntry(timecode_s=0.0, name="Intro"),
                ChapterEntry(timecode_s=30.0, name="Outro"),
            ],
        )
        recon = self._get_recon_cmd(cmds)

        idx = recon.index("-map_metadata")
        assert recon[idx + 1] == "2"
        chap_idx = recon.index("-map_chapters")
        assert recon[chap_idx + 1] == "2"

    def test_recon_wraps_injected_hevc_before_final_mux(self, tmp_path):
        cmds = self._run_inject(tmp_path)
        wrap_cmds = [c for c in cmds if c[0] == "ffmpeg" and str(c[-1]).endswith("enc_wrapped.mkv")]
        assert len(wrap_cmds) == 1, f"Commande d'encapsulation introuvable. Cmds: {cmds}"
        wrap = wrap_cmds[0]
        assert "-f" in wrap
        assert wrap[wrap.index("-f") + 1] == "hevc"
        assert "-framerate" in wrap
        assert "-bsf:v" in wrap
        assert wrap[wrap.index("-bsf:v") + 1].startswith("setts=pts=N/(")

        recon = self._get_recon_cmd(cmds)
        first_input = recon[recon.index("-i") + 1]
        assert first_input.endswith("enc_wrapped.mkv")
        assert not first_input.endswith(".hevc")


# ===========================================================================
# extra_attachments — pièces jointes manuelles dans les commandes FFmpeg
# ===========================================================================

class TestEncodeExtraAttachments:
    """
    Vérifie que -attach / -metadata:s:t:N sont injectés pour chaque fichier
    dans EncodeConfig.extra_attachments, dans les trois variantes :
      - single pass (build_command_single)
      - two-pass pass2 (build_command avec mode SIZE)
      - reconstitution finale (_run_with_metadata_inject)

    Conventions ffmpeg :
      -attach <path>                       — attache le fichier
      -metadata:s:t:N mimetype=<mime>      — type MIME de l'attachement N
      -metadata:s:t:N filename=<name>      — nom dans le MKV (cover → "cover")
    """

    def setup_method(self):
        self.wf = _make_workflow()

    def _src(self, tmp_path: Path) -> Path:
        src = tmp_path / "src.mkv"
        src.touch()
        return src

    # ── helpers ─────────────────────────────────────────────────────────────

    def _single(self, tmp_path: Path, extras: list[Path], **kw) -> list[str]:
        src = self._src(tmp_path)
        return self.wf.build_command_single(
            _make_config(src, tmp_path / "out.mkv",
                         extra_attachments=extras, **kw)
        )

    def _pass2(self, tmp_path: Path, extras: list[Path]) -> list[str]:
        src = self._src(tmp_path)
        vs = _make_video_settings(quality_mode=QualityMode.SIZE)
        cmds = self.wf.build_command(
            _make_config(src, tmp_path / "out.mkv", video=vs,
                         duration_s=3600.0, extra_attachments=extras)
        )
        return cmds[1]   # pass2

    # ── single pass — présence et valeurs ────────────────────────────────────

    def test_single_attach_flag_present(self, tmp_path):
        """-attach présent pour un attachement manuel (single pass)."""
        extras = [tmp_path / "poster.jpg"]
        cmd = self._single(tmp_path, extras)
        assert "-attach" in cmd

    def test_single_attach_path_correct(self, tmp_path):
        """Le chemin suivant -attach est le chemin absolu du fichier."""
        poster = tmp_path / "poster.jpg"
        cmd = self._single(tmp_path, [poster])
        idx = cmd.index("-attach")
        assert cmd[idx + 1] == str(poster)

    def test_single_attach_mimetype_jpeg(self, tmp_path):
        """-metadata:s:t:0 mimetype=image/jpeg pour un .jpg."""
        cmd = self._single(tmp_path, [tmp_path / "poster.jpg"])
        assert "-metadata:s:t:0" in cmd
        idx = cmd.index("-metadata:s:t:0")
        assert cmd[idx + 1] == "mimetype=image/jpeg"

    def test_single_attach_mimetype_png(self, tmp_path):
        """-metadata:s:t:0 mimetype=image/png pour un .png."""
        cmd = self._single(tmp_path, [tmp_path / "cover.png"])
        assert "-metadata:s:t:0" in cmd
        idx = cmd.index("-metadata:s:t:0")
        assert cmd[idx + 1] == "mimetype=image/png"

    def test_single_attach_mimetype_unknown(self, tmp_path):
        """-metadata:s:t:0 mimetype=application/octet-stream pour extension inconnue."""
        cmd = self._single(tmp_path, [tmp_path / "data.bin"])
        assert "-metadata:s:t:0" in cmd
        idx = cmd.index("-metadata:s:t:0")
        assert cmd[idx + 1] == "mimetype=application/octet-stream"

    def test_single_attach_filename_non_cover(self, tmp_path):
        """-metadata:s:t:0 filename=<basename> pour un fichier non-cover."""
        cmd = self._single(tmp_path, [tmp_path / "poster.jpg"])
        # Il y a deux -metadata:s:t:0 : mimetype puis filename — prendre le second
        indices = [i for i, a in enumerate(cmd) if a == "-metadata:s:t:0"]
        assert len(indices) == 2
        filename_val = cmd[indices[1] + 1]
        assert filename_val == "filename=poster.jpg"

    def test_single_attach_filename_cover(self, tmp_path):
        """-metadata:s:t:0 filename=cover pour un fichier nommé cover.*."""
        cmd = self._single(tmp_path, [tmp_path / "cover.jpg"])
        indices = [i for i, a in enumerate(cmd) if a == "-metadata:s:t:0"]
        assert len(indices) == 2
        filename_val = cmd[indices[1] + 1]
        assert filename_val == "filename=cover"

    def test_single_cover_case_insensitive(self, tmp_path):
        """COVER.JPG → filename=cover (insensible à la casse)."""
        cmd = self._single(tmp_path, [tmp_path / "COVER.JPG"])
        indices = [i for i, a in enumerate(cmd) if a == "-metadata:s:t:0"]
        filename_val = cmd[indices[1] + 1]
        assert filename_val == "filename=cover"

    def test_single_no_extra_no_attach(self, tmp_path):
        """extra_attachments=[] → aucun -attach dans la commande."""
        cmd = self._single(tmp_path, [])
        assert "-attach" not in cmd

    # ── single pass — indices multiples ──────────────────────────────────────

    def test_single_two_attachments_indices(self, tmp_path):
        """Deux attachements → indices 0 et 1 dans les metadata:s:t:N."""
        extras = [tmp_path / "cover.jpg", tmp_path / "notes.txt"]
        cmd = self._single(tmp_path, extras)
        assert "-metadata:s:t:0" in cmd
        assert "-metadata:s:t:1" in cmd

    def test_single_two_attach_flags(self, tmp_path):
        """Deux attachements → deux occurrences de -attach."""
        extras = [tmp_path / "cover.jpg", tmp_path / "notes.txt"]
        cmd = self._single(tmp_path, extras)
        assert sum(1 for a in cmd if a == "-attach") == 2

    def test_single_attachment_streams_offset(self, tmp_path):
        """attachment_streams existants → extra_attachments commencent à l'index N."""
        src = self._src(tmp_path)
        att_streams = [(src, 5)]   # 1 stream existant → extra débute à l'index 1
        extras = [tmp_path / "cover.jpg"]
        with patch.object(
            self.wf,
            "_describe_attachment_stream",
            return_value={
                "is_attached_pic": False,
                "filename": "DejaVuSans.ttf",
                "mimetype": "application/x-truetype-font",
            },
        ):
            cmd = self.wf.build_command_single(
                _make_config(src, tmp_path / "out.mkv",
                             attachment_streams=att_streams,
                             extra_attachments=extras)
            )
        assert "filename=DejaVuSans.ttf" in cmd
        assert "-metadata:s:t:1" in cmd
        indices = [i for i, a in enumerate(cmd) if a == "-metadata:s:t:1"]
        assert any(cmd[i + 1] == "filename=cover" for i in indices)

    def test_single_attachment_streams_write_filename_and_mimetype(self, tmp_path):
        src = self._src(tmp_path)
        cfg = _make_config(
            src,
            tmp_path / "out.mkv",
            attachment_streams=[(src, 5)],
        )
        with patch.object(
            self.wf,
            "_describe_attachment_stream",
            return_value={
                "is_attached_pic": False,
                "filename": "DejaVuSans.ttf",
                "mimetype": "application/x-truetype-font",
            },
        ):
            cmd = self.wf.build_command_single(cfg)

        assert "mimetype=application/x-truetype-font" in cmd
        assert "filename=DejaVuSans.ttf" in cmd

    # ── two-pass pass2 ───────────────────────────────────────────────────────

    def test_pass2_attach_flag_present(self, tmp_path):
        """-attach présent dans la passe 2 (mode SIZE)."""
        cmd = self._pass2(tmp_path, [tmp_path / "poster.jpg"])
        assert "-attach" in cmd

    def test_pass2_attach_mimetype(self, tmp_path):
        """-metadata:s:t:0 mimetype=image/jpeg dans la passe 2."""
        cmd = self._pass2(tmp_path, [tmp_path / "cover.jpg"])
        assert "-metadata:s:t:0" in cmd
        idx = cmd.index("-metadata:s:t:0")
        assert cmd[idx + 1] == "mimetype=image/jpeg"

    def test_pass2_cover_filename(self, tmp_path):
        """-metadata:s:t:0 filename=cover dans la passe 2 pour cover.*."""
        cmd = self._pass2(tmp_path, [tmp_path / "cover.jpg"])
        indices = [i for i, a in enumerate(cmd) if a == "-metadata:s:t:0"]
        filename_val = cmd[indices[1] + 1]
        assert filename_val == "filename=cover"

    def test_pass1_no_attach(self, tmp_path):
        """-attach absent de la passe 1 (analyse seule, pas de sortie)."""
        src = self._src(tmp_path)
        vs = _make_video_settings(quality_mode=QualityMode.SIZE)
        cmds = self.wf.build_command(
            _make_config(src, tmp_path / "out.mkv", video=vs,
                         duration_s=3600.0, extra_attachments=[tmp_path / "cover.jpg"])
        )
        pass1 = cmds[0]
        assert "-attach" not in pass1

    # ── reconstitution finale (_run_with_metadata_inject) ────────────────────

    def _run_inject_with_extras(
        self, tmp_path: Path, extras: list[Path], ffmpeg_threads: int | None = None
    ) -> list[list[str]]:
        """Lance _run_with_metadata_inject (libx265 + copy_dv) avec extra_attachments."""
        src = tmp_path / "source.mkv"
        src.write_bytes(b"\x00" * 200_000)
        config = _make_config(
            source=src,
            output=tmp_path / "output.mkv",
            video=_make_video_settings(codec="libx265"),
            copy_dv=True,
            work_dir=tmp_path / "work",
            extra_attachments=extras,
        )
        wf = _make_workflow(enabled=False, ffmpeg_threads=ffmpeg_threads)
        cmds_run: list[list[str]] = []

        def _fake_run(cmd, signals=None, cwd=None, progress_cb=None):
            cmds_run.append(list(cmd))
            for arg in reversed(cmd):
                s = str(arg)
                if any(s.endswith(ext) for ext in (".hevc", ".mkv", ".bin", ".json")):
                    p = Path(s)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"\x00" * 100_000)
                    break
            return ""

        with patch.object(EncodeWorkflow, "_shm_path",
                          side_effect=lambda tmp_dir, name, _size: tmp_dir / name):
            with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run):
                sigs = wf._run_with_metadata_inject(config)
                _collect_signals(sigs)

        return cmds_run

    def _get_recon_cmd(self, cmds: list[list[str]]) -> list[str]:
        recon = [c for c in cmds if c[0] == "ffmpeg" and "output.mkv" in " ".join(c)]
        assert len(recon) == 1
        return recon[0]

    def test_inject_attach_flag_present(self, tmp_path):
        """-attach présent dans la reconstitution finale (chemin DV/HDR10+)."""
        extras = [tmp_path / "cover.jpg"]
        cmds = self._run_inject_with_extras(tmp_path, extras)
        recon = self._get_recon_cmd(cmds)
        assert "-attach" in recon

    def test_inject_reconstruction_includes_threads(self, tmp_path):
        cmds = self._run_inject_with_extras(tmp_path, [], ffmpeg_threads=9)
        recon = self._get_recon_cmd(cmds)
        assert recon[recon.index("-threads") + 1] == "9"

    def test_inject_cover_filename(self, tmp_path):
        """filename=cover dans la reconstitution finale pour cover.*."""
        extras = [tmp_path / "cover.jpg"]
        cmds = self._run_inject_with_extras(tmp_path, extras)
        recon = self._get_recon_cmd(cmds)
        indices = [i for i, a in enumerate(recon) if a == "-metadata:s:t:0"]
        assert any(recon[i + 1] == "filename=cover" for i in indices)

    def test_inject_no_attach_when_empty(self, tmp_path):
        """extra_attachments=[] → aucun -attach dans la reconstitution."""
        cmds = self._run_inject_with_extras(tmp_path, [])
        recon = self._get_recon_cmd(cmds)
        assert "-attach" not in recon


# ===========================================================================
# Sync timeline multi-source (encode runtime)
# ===========================================================================

class TestEncodeRuntimeMultiSourceSync:

    def test_runtime_single_pass_uses_sync_remap_and_strict_flags(self, tmp_path):
        src_main = tmp_path / "main.mkv"
        src_alt = tmp_path / "alt.mkv"
        out = tmp_path / "out.mkv"
        sync_audio = tmp_path / "sync_audio.mka"
        src_main.touch()
        src_alt.touch()
        sync_audio.touch()

        wf = _make_workflow()
        cfg = _make_config(
            src_main,
            out,
            video=_make_video_settings(codec="libx265"),
            audio_tracks=[
                AudioTrackSettings(stream_index=1, codec="copy", source_path=src_alt),
            ],
            subtitle_tracks=[(src_main, 3)],
        )

        with patch.object(
            wf,
            "_prepare_multisource_sync",
            return_value=(
                {(src_alt, 1, "audio"): (2, 0)},
                [sync_audio],
                None,
                True,
            ),
        ):
            cmd, live, cleanup = wf._build_runtime_single_pass_with_sync(cfg)

        assert live is None
        assert cleanup == [sync_audio]
        assert str(sync_audio) in cmd
        sync_i = cmd.index(str(sync_audio))
        assert cmd[sync_i - 1] == "-i"
        assert cmd[sync_i - 2] == "matroska"
        assert cmd[sync_i - 3] == "-f"
        assert "-max_interleave_delta" in cmd
        assert "-max_muxing_queue_size" in cmd
        map_values = [cmd[i + 1] for i, tok in enumerate(cmd[:-1]) if tok == "-map"]
        assert "2:0" in map_values

    def test_runtime_two_pass_disables_live_and_applies_sync_to_pass2_only(self, tmp_path):
        src_main = tmp_path / "main.mkv"
        src_alt = tmp_path / "alt.mkv"
        out = tmp_path / "out.mkv"
        sync_audio = tmp_path / "sync_audio.mka"
        src_main.touch()
        src_alt.touch()
        sync_audio.touch()

        wf = _make_workflow()
        cfg = _make_config(
            src_main,
            out,
            video=_make_video_settings(codec="libx265", quality_mode=QualityMode.SIZE),
            duration_s=3600.0,
            audio_tracks=[
                AudioTrackSettings(stream_index=1, codec="copy", source_path=src_alt),
            ],
            subtitle_tracks=[(src_main, 3)],
        )

        recorded_allow_live: list[bool] = []

        def _fake_prepare(**kwargs):
            recorded_allow_live.append(bool(kwargs.get("allow_live")))
            return (
                {(src_alt, 1, "audio"): (2, 0)},
                [sync_audio],
                None,
                True,
            )

        with patch.object(wf, "_prepare_multisource_sync", side_effect=_fake_prepare):
            cmds, live, cleanup = wf._build_runtime_two_pass_with_sync(cfg)

        assert live is None
        assert cleanup == [sync_audio]
        assert recorded_allow_live == [False]

        pass1, pass2 = cmds
        assert str(sync_audio) not in pass1
        assert str(sync_audio) in pass2
        assert "-max_interleave_delta" not in pass1
        assert "-max_interleave_delta" in pass2
        map_values_pass2 = [pass2[i + 1] for i, tok in enumerate(pass2[:-1]) if tok == "-map"]
        assert "2:0" in map_values_pass2

    def test_metadata_inject_reconstruction_uses_sync_inputs(self, tmp_path):
        src_main = tmp_path / "main.mkv"
        src_alt = tmp_path / "alt.mkv"
        out = tmp_path / "out.mkv"
        sync_audio = tmp_path / "sync_audio.mka"
        src_main.write_bytes(b"\x00" * 200_000)
        src_alt.write_bytes(b"\x00" * 100_000)
        sync_audio.touch()

        wf = _make_workflow(enabled=False)
        cfg = _make_config(
            source=src_main,
            output=out,
            video=_make_video_settings(codec="libx265"),
            copy_dv=True,
            work_dir=tmp_path / "work",
            audio_tracks=[
                AudioTrackSettings(stream_index=1, codec="copy", source_path=src_alt),
            ],
            subtitle_tracks=[(src_main, 3)],
        )

        ran_cmds: list[list[str]] = []

        def _fake_run(cmd, signals=None, cwd=None, progress_cb=None):
            ran_cmds.append(list(cmd))
            for arg in reversed(cmd):
                s = str(arg)
                if any(s.endswith(ext) for ext in (".hevc", ".mkv", ".bin", ".json")):
                    p = Path(s)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"\x00" * 100_000)
                    break
            return ""

        with patch.object(
            wf,
            "_prepare_multisource_sync",
            return_value=(
                {(src_alt, 1, "audio"): (3, 0)},
                [sync_audio],
                None,
                True,
            ),
        ):
            with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run):
                sigs = wf._run_with_metadata_inject(cfg)
                _collect_signals(sigs)

        recon = [c for c in ran_cmds if str(out) in [str(x) for x in c]]
        assert len(recon) == 1
        cmd = recon[0]
        assert str(sync_audio) in cmd
        sync_i = cmd.index(str(sync_audio))
        assert cmd[sync_i - 1] == "-i"
        assert cmd[sync_i - 2] == "matroska"
        assert cmd[sync_i - 3] == "-f"
        assert "-max_interleave_delta" in cmd
        assert "-max_muxing_queue_size" in cmd
        map_values = [cmd[i + 1] for i, tok in enumerate(cmd[:-1]) if tok == "-map"]
        assert "3:0" in map_values

    def test_metadata_inject_reconstruction_applies_offsets_after_sync_remap(self, tmp_path):
        src_main = tmp_path / "main.mkv"
        src_alt = tmp_path / "alt.mkv"
        out = tmp_path / "out.mkv"
        sync_audio = tmp_path / "sync_audio.mka"
        src_main.write_bytes(b"\x00" * 200_000)
        src_alt.write_bytes(b"\x00" * 100_000)
        sync_audio.touch()

        wf = _make_workflow(enabled=False)
        cfg = _make_config(
            source=src_main,
            output=out,
            video=_make_video_settings(codec="libx265"),
            copy_dv=True,
            work_dir=tmp_path / "work",
            audio_tracks=[
                AudioTrackSettings(stream_index=1, codec="copy", source_path=src_alt),
            ],
            subtitle_tracks=[(src_main, 3)],
            track_time_offsets=[
                TrackTimeOffset(track_type="audio", source_path=src_alt, stream_index=1, offset_ms=120),
            ],
        )

        ran_cmds: list[list[str]] = []

        def _fake_run(cmd, signals=None, cwd=None, progress_cb=None):
            ran_cmds.append(list(cmd))
            for arg in reversed(cmd):
                s = str(arg)
                if any(s.endswith(ext) for ext in (".hevc", ".mkv", ".bin", ".json")):
                    p = Path(s)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"\x00" * 100_000)
                    break
            return ""

        with patch.object(
            wf,
            "_prepare_multisource_sync",
            return_value=(
                {(src_alt, 1, "audio"): (3, 0)},
                [sync_audio],
                None,
                True,
            ),
        ):
            with patch.object(wf._runner, "_run_cmd", side_effect=_fake_run):
                sigs = wf._run_with_metadata_inject(cfg)
                _collect_signals(sigs)

        recon = [c for c in ran_cmds if str(out) in [str(x) for x in c]]
        assert len(recon) == 1
        cmd = recon[0]
        assert "-itsoffset" in cmd
        assert cmd[cmd.index("-itsoffset") + 1] == "0.120"
        map_values = [cmd[i + 1] for i, tok in enumerate(cmd[:-1]) if tok == "-map"]
        assert "4:0" in map_values

    def test_prepare_multisource_sync_prefers_live_on_posix(self, tmp_path, monkeypatch):
        src_main = tmp_path / "main.mkv"
        src_alt = tmp_path / "alt.mkv"
        out = tmp_path / "out.mkv"
        sync_audio = tmp_path / "sync_audio.mka"
        src_main.touch()
        src_alt.touch()
        sync_audio.touch()

        wf = _make_workflow()
        cfg = _make_config(
            src_main,
            out,
            video=_make_video_settings(codec="copy"),
            audio_tracks=[
                AudioTrackSettings(stream_index=1, codec="copy", source_path=src_alt),
            ],
            subtitle_tracks=[(src_main, 3)],
        )

        calls = {"live": 0, "file": 0}

        class _FakeSyncer:
            def __init__(self, **_kwargs):
                pass

            def start_live_demux_session(self, **_kwargs):
                calls["live"] += 1
                return SimpleNamespace(inputs=[SimpleNamespace(key=(1, 1, "audio"), path=sync_audio, input_idx=2)])

            def prepare_from_mapped_tracks(self, **_kwargs):
                calls["file"] += 1
                return [SimpleNamespace(key=(1, 1, "audio"), path=sync_audio, input_idx=2)]

            def prepare_from_mapped_tracks_mmap(self, **_kwargs):
                calls["file"] += 1
                return [SimpleNamespace(key=(1, 1, "audio"), path=sync_audio, input_idx=2)]

        monkeypatch.setattr("core.workflows.encode.workflow.MkvmergeLikeTimelineSync", _FakeSyncer)
        monkeypatch.setattr("core.workflows.encode.workflow.os.name", "posix", raising=False)

        remap, sync_inputs, live, strict = wf._prepare_multisource_sync(
            config=cfg,
            all_sources=[src_main, src_alt],
            sync_base_input_idx=2,
            work_dir=tmp_path,
            allow_live=True,
        )

        assert calls["live"] == 1
        assert calls["file"] == 0
        assert live is not None
        assert strict is True
        assert sync_inputs == [sync_audio]
        assert remap[(src_alt, 1, "audio")] == (2, 0)

    def test_prepare_multisource_sync_fallback_prefers_ram_before_disk(self, tmp_path, monkeypatch):
        src_main = tmp_path / "main.mkv"
        src_alt = tmp_path / "alt.mkv"
        out = tmp_path / "out.mkv"
        ram_dir = tmp_path / "ram"
        ram_dir.mkdir()
        sync_audio = ram_dir / "sync_audio.mka"
        src_main.touch()
        src_alt.touch()

        wf = _make_workflow()
        cfg = _make_config(
            src_main,
            out,
            video=_make_video_settings(codec="copy"),
            audio_tracks=[
                AudioTrackSettings(stream_index=1, codec="copy", source_path=src_alt),
            ],
            subtitle_tracks=[(src_main, 3)],
        )

        calls: list[Path] = []

        class _FakeSyncer:
            def __init__(self, **_kwargs):
                pass

            def start_live_demux_session(self, **_kwargs):
                raise RuntimeError("live unavailable")

            def prepare_from_mapped_tracks_mmap(self, **_kwargs):
                tmp_dir = Path(_kwargs["tmp_dir"])
                calls.append(tmp_dir)
                return [SimpleNamespace(key=(1, 1, "audio"), path=sync_audio, input_idx=2)]

            def prepare_from_mapped_tracks(self, **_kwargs):
                pytest.fail("file fallback should not be used when RAM mmap works")

        monkeypatch.setattr("core.workflows.encode.workflow.MkvmergeLikeTimelineSync", _FakeSyncer)
        monkeypatch.setattr("core.workflows.encode.workflow.os.name", "posix", raising=False)
        monkeypatch.setattr(EncodeWorkflow, "_ram_buffer_dir", staticmethod(lambda: ram_dir))

        remap, sync_inputs, live, strict = wf._prepare_multisource_sync(
            config=cfg,
            all_sources=[src_main, src_alt],
            sync_base_input_idx=2,
            work_dir=tmp_path,
            allow_live=True,
        )

        assert calls == [ram_dir]
        assert live is None
        assert strict is True
        assert sync_inputs == [sync_audio]
        assert remap[(src_alt, 1, "audio")] == (2, 0)

    def test_runtime_single_pass_copy_subtitles_uses_sync_remap_when_resolved(self, tmp_path):
        src_main = tmp_path / "main.mkv"
        src_alt = tmp_path / "alt.mkv"
        out = tmp_path / "out.mkv"
        sync_sub = tmp_path / "sync_sub.mks"
        src_main.touch()
        src_alt.touch()
        sync_sub.touch()

        wf = _make_workflow()
        cfg = _make_config(
            src_main,
            out,
            video=_make_video_settings(codec="copy"),
            copy_subtitles=True,
            subtitle_tracks=[],
        )

        with patch.object(
            wf,
            "_prepare_multisource_sync",
            return_value=(
                {(src_alt, 4, "subtitle"): (2, 0)},
                [sync_sub],
                None,
                True,
            ),
        ), patch.object(
            wf,
            "_resolved_subtitle_tracks_for_encode",
            return_value=([(src_alt, 4)], True),
        ):
            cmd, _live, _cleanup = wf._build_runtime_single_pass_with_sync(cfg)

        map_values = [cmd[i + 1] for i, tok in enumerate(cmd[:-1]) if tok == "-map"]
        assert "2:0" in map_values
        assert all(":s?" not in mv for mv in map_values)
        assert "-c:s" in cmd and cmd[cmd.index("-c:s") + 1] == "copy"

    def test_runtime_single_pass_copy_subtitles_falls_back_to_wildcard_when_unresolved(self, tmp_path):
        src_main = tmp_path / "main.mkv"
        src_alt = tmp_path / "alt.mkv"
        out = tmp_path / "out.mkv"
        src_main.touch()
        src_alt.touch()

        wf = _make_workflow()
        cfg = _make_config(
            src_main,
            out,
            video=_make_video_settings(codec="copy"),
            copy_subtitles=True,
            subtitle_tracks=[],
            audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy", source_path=src_alt)],
        )

        with patch.object(
            wf,
            "_prepare_multisource_sync",
            return_value=({}, [], None, False),
        ), patch.object(
            wf,
            "_resolved_subtitle_tracks_for_encode",
            return_value=([], False),
        ):
            cmd, _live, _cleanup = wf._build_runtime_single_pass_with_sync(cfg)

        map_values = [cmd[i + 1] for i, tok in enumerate(cmd[:-1]) if tok == "-map"]
        assert "0:s?" in map_values
        assert "1:s?" in map_values
        assert "-c:s" in cmd and cmd[cmd.index("-c:s") + 1] == "copy"

    def test_build_single_pass_applies_video_audio_subtitle_offsets(self, tmp_path):
        src = tmp_path / "src.mkv"
        out = tmp_path / "out.mkv"
        src.touch()

        wf = _make_workflow()
        cfg = _make_config(
            src,
            out,
            video=_make_video_settings(codec="libx265"),
            audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy", source_path=src)],
            subtitle_tracks=[(src, 3)],
            copy_subtitles=False,
            track_time_offsets=[
                TrackTimeOffset(track_type="video", source_path=src, stream_index=0, offset_ms=40),
                TrackTimeOffset(track_type="audio", source_path=src, stream_index=1, offset_ms=125),
                TrackTimeOffset(track_type="subtitle", source_path=src, stream_index=3, offset_ms=-80),
            ],
        )

        cmd = wf.build_command_single(cfg)

        assert "-itsoffset" in cmd
        assert "0.040" in cmd
        assert "0.125" in cmd
        assert "-ss" in cmd
        assert "0.080" in cmd
        map_values = [cmd[i + 1] for i, tok in enumerate(cmd[:-1]) if tok == "-map"]
        assert "1:0" in map_values
        assert "2:1" in map_values
        assert "3:3" in map_values

    def test_runtime_two_pass_applies_offsets_after_sync_remap(self, tmp_path):
        src_main = tmp_path / "main.mkv"
        src_alt = tmp_path / "alt.mkv"
        out = tmp_path / "out.mkv"
        sync_audio = tmp_path / "sync_audio.mka"
        src_main.touch()
        src_alt.touch()
        sync_audio.touch()

        wf = _make_workflow()
        cfg = _make_config(
            src_main,
            out,
            video=_make_video_settings(codec="libx265", quality_mode=QualityMode.SIZE),
            duration_s=3600.0,
            audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy", source_path=src_alt)],
            subtitle_tracks=[(src_main, 3)],
            track_time_offsets=[
                TrackTimeOffset(track_type="audio", source_path=src_alt, stream_index=1, offset_ms=150),
            ],
        )

        with patch.object(
            wf,
            "_prepare_multisource_sync",
            return_value=(
                {(src_alt, 1, "audio"): (2, 0)},
                [sync_audio],
                None,
                True,
            ),
        ):
            cmds, _live, _cleanup = wf._build_runtime_two_pass_with_sync(cfg)

        pass2 = cmds[1]
        assert "-itsoffset" in pass2
        assert pass2[pass2.index("-itsoffset") + 1] == "0.150"
        map_values = [pass2[i + 1] for i, tok in enumerate(pass2[:-1]) if tok == "-map"]
        assert "3:0" in map_values

    def test_prepare_multisource_sync_disables_live_when_foreign_offset(self, tmp_path, monkeypatch):
        src_main = tmp_path / "main.mkv"
        src_alt = tmp_path / "alt.mkv"
        out = tmp_path / "out.mkv"
        src_main.touch()
        src_alt.touch()

        wf = _make_workflow()
        cfg = _make_config(
            src_main,
            out,
            video=_make_video_settings(codec="copy"),
            audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy", source_path=src_alt)],
            subtitle_tracks=[(src_main, 3)],
            track_time_offsets=[
                TrackTimeOffset(track_type="audio", source_path=src_alt, stream_index=1, offset_ms=120),
            ],
        )

        recorded_allow_live: list[bool] = []

        class _FakeHelper:
            def __init__(self, **_kwargs):
                pass

            def prepare(self, **kwargs):
                recorded_allow_live.append(bool(kwargs.get("allow_live")))
                return SimpleNamespace(prepared_inputs=[], live_session=None)

        monkeypatch.setattr("core.workflows.encode.workflow.TimelineSyncFallbackHelper", _FakeHelper)
        monkeypatch.setattr(wf._postproc_helper, "_decide_strict_interleave_with_prescan", lambda _cfg: True)

        remap, sync_inputs, live, strict = wf._prepare_multisource_sync(
            config=cfg,
            all_sources=[src_main, src_alt],
            sync_base_input_idx=2,
            work_dir=tmp_path,
            allow_live=True,
        )

        assert strict is True
        assert remap == {}
        assert sync_inputs == []
        assert live is None
        assert recorded_allow_live == [False]

    def test_prepare_multisource_sync_forces_sync_when_foreign_offset_without_subtitle_risk(
        self,
        tmp_path,
        monkeypatch,
    ):
        src_main = tmp_path / "main.mkv"
        src_alt = tmp_path / "alt.mkv"
        out = tmp_path / "out.mkv"
        src_main.touch()
        src_alt.touch()

        wf = _make_workflow()
        cfg = _make_config(
            src_main,
            out,
            video=_make_video_settings(codec="copy"),
            copy_subtitles=False,
            subtitle_tracks=[],
            audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy", source_path=src_alt)],
            track_time_offsets=[
                TrackTimeOffset(track_type="audio", source_path=src_alt, stream_index=1, offset_ms=120),
            ],
        )

        recorded_allow_live: list[bool] = []

        class _FakeHelper:
            def __init__(self, **_kwargs):
                pass

            def prepare(self, **kwargs):
                recorded_allow_live.append(bool(kwargs.get("allow_live")))
                return SimpleNamespace(prepared_inputs=[], live_session=None)

        monkeypatch.setattr("core.workflows.encode.workflow.TimelineSyncFallbackHelper", _FakeHelper)
        monkeypatch.setattr(wf._postproc_helper, "_decide_strict_interleave_with_prescan", lambda _cfg: False)

        remap, sync_inputs, live, strict = wf._prepare_multisource_sync(
            config=cfg,
            all_sources=[src_main, src_alt],
            sync_base_input_idx=2,
            work_dir=tmp_path,
            allow_live=True,
        )

        assert strict is True
        assert remap == {}
        assert sync_inputs == []
        assert live is None
        assert recorded_allow_live == [False]

    def test_prepare_multisource_sync_skips_subtitle_prescan_when_foreign_offset_forces_sync(
        self,
        tmp_path,
        monkeypatch,
    ):
        src_main = tmp_path / "main.mkv"
        src_alt = tmp_path / "alt.mkv"
        out = tmp_path / "out.mkv"
        src_main.touch()
        src_alt.touch()

        wf = _make_workflow()
        cfg = _make_config(
            src_main,
            out,
            video=_make_video_settings(codec="copy"),
            copy_subtitles=False,
            subtitle_tracks=[(src_main, 3)],
            audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy", source_path=src_alt)],
            track_time_offsets=[
                TrackTimeOffset(track_type="audio", source_path=src_alt, stream_index=1, offset_ms=120),
            ],
        )

        recorded_allow_live: list[bool] = []

        class _FakeHelper:
            def __init__(self, **_kwargs):
                pass

            def prepare(self, **kwargs):
                recorded_allow_live.append(bool(kwargs.get("allow_live")))
                return SimpleNamespace(prepared_inputs=[], live_session=None)

        monkeypatch.setattr("core.workflows.encode.workflow.TimelineSyncFallbackHelper", _FakeHelper)
        monkeypatch.setattr(
            wf._postproc_helper,
            "_decide_strict_interleave_with_prescan",
            lambda _cfg: pytest.fail("subtitle prescan must be skipped when foreign offset already forces sync"),
        )

        remap, sync_inputs, live, strict = wf._prepare_multisource_sync(
            config=cfg,
            all_sources=[src_main, src_alt],
            sync_base_input_idx=2,
            work_dir=tmp_path,
            allow_live=True,
        )

        assert strict is True
        assert remap == {}
        assert sync_inputs == []
        assert live is None
        assert recorded_allow_live == [False]


# ===========================================================================
# _build_track_meta_args — options metadata FFmpeg
# ===========================================================================

class TestTrackMetaArgs:
    def setup_method(self):
        self.wf = _make_workflow()

    def _build_args(self, tmp_path, edits: list, *, audio_count: int = 1) -> list[str]:
        source = tmp_path / "src.mkv"
        output = tmp_path / "out.mkv"
        source.touch()
        output.touch()
        audio_tracks = [AudioTrackSettings(stream_index=i) for i in range(audio_count)]
        config = _make_config(
            source,
            output,
            audio_tracks=audio_tracks,
            track_meta_edits=edits,
        )
        return self.wf._build_track_meta_args(config)

    def test_fr_FR_language_tags(self, tmp_path):
        from core.workflows.encode.models import TrackMetaEdit
        args = self._build_args(tmp_path, [TrackMetaEdit(track_order=1, language="fr-FR")], audio_count=0)
        assert "language=fr-FR" in args
        assert "language-ietf=" in args
        assert args.index("language=fr-FR") < args.index("language-ietf=")

    def test_track_order_maps_video_audio_subtitle(self, tmp_path):
        from core.workflows.encode.models import TrackMetaEdit
        edits = [
            TrackMetaEdit(track_order=1, language="fr"),
            TrackMetaEdit(track_order=2, language="en"),
            TrackMetaEdit(track_order=3, language="es"),
        ]
        args = self._build_args(tmp_path, edits, audio_count=1)
        assert "-metadata:s:v:0" in args
        assert "-metadata:s:a:0" in args
        assert "-metadata:s:s:0" in args

    def test_iso639_2_and_und_inputs_are_kept(self, tmp_path):
        from core.workflows.encode.models import TrackMetaEdit
        args = self._build_args(
            tmp_path,
            [
                TrackMetaEdit(track_order=2, language="eng"),
                TrackMetaEdit(track_order=3, language="und"),
            ],
            audio_count=2,
        )
        assert "language=en-US" in args
        assert "language=und" in args
        assert args.count("language-ietf=") == 2

    def test_title_only_sets_stream_title(self, tmp_path):
        from core.workflows.encode.models import TrackMetaEdit
        args = self._build_args(tmp_path, [TrackMetaEdit(track_order=1, title="Commentaires")], audio_count=0)
        assert "title=Commentaires" in args
        assert not any(str(a).startswith("language=") for a in args)

    def test_disposition_flags_are_emitted_when_provided(self, tmp_path):
        from core.workflows.encode.models import TrackMetaEdit
        args = self._build_args(
            tmp_path,
            [
                TrackMetaEdit(
                    track_order=2,
                    flag_default=False,
                    flag_forced=True,
                    flag_hearing_impaired=True,
                    flag_visual_impaired=False,
                    flag_original=False,
                    flag_commentary=True,
                )
            ],
            audio_count=2,
        )
        assert "-disposition:a:0" in args
        assert "forced+hearing_impaired+comment" in args

    # ── merge remux extras : fr-FR transmis tel quel dans TrackMetaEdit ───────

    def test_merge_fr_FR_in_track_meta_edit(self, tmp_path):
        """
        _merge_remux_extras avec une piste language='fr-FR' doit produire
        un TrackMetaEdit avec language='fr-FR' (conservé tel quel).
        """
        from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry
        from core.workflows.encode.models import EncodeConfig, VideoEncodeSettings
        from ui.main_window import MainWindow

        src = tmp_path / "src.mkv"
        src.touch()
        out = tmp_path / "out.mkv"

        track = TrackEntry(
            mkv_tid=0, track_type="video", codec="H264",
            display_info="", language="fr-FR", title="",
            orig_language="fr-FR", orig_title="",
        )
        source = SourceInput(
            path=src, file_index=0, tracks=[track],
            selected_attachments=[], attachment_count=0, copy_tags=True,
        )
        rmx = RemuxConfig(
            sources=[source], output=out,
            track_order=[(0, 0)], keep_chapters=True,
        )
        enc = EncodeConfig(
            source=src, output=out,
            video=VideoEncodeSettings(),
            audio_tracks=[],
        )

        result = MainWindow._merge_remux_extras(None, enc, rmx)

        assert result.track_meta_edits, "Aucun TrackMetaEdit généré"
        edit = result.track_meta_edits[0]
        assert edit.language == "fr-FR", \
            f"Langue dans TrackMetaEdit : attendu 'fr-FR', obtenu {edit.language!r}"
