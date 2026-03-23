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

    Configuration AppConfig :
        - ram_buffer_enabled lu depuis INI / env var
        - ram_buffer_threshold_pct lu depuis INI / env var
        - Valeurs par défaut : enabled=True, threshold=15

    Gestion des fichiers dans _run_with_metadata_inject :
        Tracking ext_files :
            - Un fichier RAM est enregistré dans ext_files
            - Un fichier disque N'est PAS dans ext_files
            - _free() retire le fichier de ext_files
            - _free() silencieux si fichier déjà absent

        Cleanup src.hevc :
            - src.hevc supprimé avant l'encodage (DV seul / HDR10+ seul / les deux)

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

    Méthodes build_command (régression) :
        - CRF → list[str] ; SIZE → list[list[str]]
        - codec copy → pas de -crf ; audio copy → pas de -b:a ; AAC → bitrate présent
        - TrueHD core → BSF truehd_core

    validate :
        - Source manquante, source==output, SIZE sans durée, master_display invalide, max_cll invalide

    ProfileManager :
        - save/load round-trip, delete, names(), overwrite

Exécution :
    cd mkv_toolkit && pytest tests/test_encode_workflow.py -v
"""

from __future__ import annotations

import sys
import time
from contextlib import ExitStack
from pathlib import Path
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
    VideoEncodeSettings,
)


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


def _make_workflow(enabled=True, threshold=15) -> EncodeWorkflow:
    return EncodeWorkflow(
        ffmpeg_bin="ffmpeg", dovi_tool_bin="dovi_tool",
        hdr10plus_bin="hdr10plus_tool", mkvmerge_bin="mkvmerge",
        ram_buffer_enabled=enabled,
        ram_buffer_threshold_pct=threshold,
    )


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
# AppConfig — configuration RAM
# ===========================================================================

class TestAppConfigRamBuffer:
    """Tests des clés INI ram_buffer_enabled / ram_buffer_threshold_pct."""

    def test_defaults_enabled_true_threshold_15(self, tmp_path):
        """Valeurs par défaut : enabled=True, threshold=15."""
        from core.config import AppConfig
        with patch("core.config.QSettings") as mock_qs:
            inst = MagicMock()
            inst.value.side_effect = lambda key, default=None: default
            mock_qs.return_value = inst
            with patch("core.config._app_data_dir", return_value=tmp_path), \
                 patch.dict(os.environ, {}, clear=False):
                cfg = AppConfig()
        assert cfg.ram_buffer_enabled is True
        assert cfg.ram_buffer_threshold_pct == 15

    def test_env_var_disables_ram_buffer(self, tmp_path):
        """RAM_BUFFER_ENABLED=false désactive le buffer."""
        from core.config import AppConfig
        with patch("core.config.QSettings") as mock_qs:
            inst = MagicMock()
            inst.value.side_effect = lambda key, default=None: default
            mock_qs.return_value = inst
            with patch("core.config._app_data_dir", return_value=tmp_path), \
                 patch.dict(os.environ, {"RAM_BUFFER_ENABLED": "false"}):
                cfg = AppConfig()
        assert cfg.ram_buffer_enabled is False

    def test_env_var_sets_threshold(self, tmp_path):
        """RAM_BUFFER_THRESHOLD_PCT=25 fixe le seuil à 25."""
        from core.config import AppConfig
        with patch("core.config.QSettings") as mock_qs:
            inst = MagicMock()
            inst.value.side_effect = lambda key, default=None: default
            mock_qs.return_value = inst
            with patch("core.config._app_data_dir", return_value=tmp_path), \
                 patch.dict(os.environ, {"RAM_BUFFER_THRESHOLD_PCT": "25"}):
                cfg = AppConfig()
        assert cfg.ram_buffer_threshold_pct == 25


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

    # ── cleanup src.hevc ─────────────────────────────────────────────────────

    @pytest.mark.parametrize("copy_dv,copy_hdr10plus", [
        (True, False), (False, True), (True, True)
    ])
    def test_src_hevc_deleted_before_encoding(self, tmp_path, copy_dv, copy_hdr10plus):
        """src.hevc absent dans le snapshot qui crée encoded.mkv."""
        snapshots = self._run_inject(tmp_path, copy_dv, copy_hdr10plus)
        enc_snap_idx = next(
            (i for i, snap in enumerate(snapshots)
             if any("encoded.mkv" in str(p) for p in snap)), None
        )
        assert enc_snap_idx is not None, "encoded.mkv jamais créé"
        snap = snapshots[enc_snap_idx]
        assert not any("src.hevc" in str(p) for p in snap)

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

        with patch.object(EncodeWorkflow, "_shm_path",
                          side_effect=lambda t, n, _: t / n), \
             patch.object(wf._runner, "_run_cmd", side_effect=_fast_fail):
            sigs = wf._run_with_metadata_inject(config)
            # Si le finally lève une exception, failed sera émis mais le test ne plantera pas
            result: list = []
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

    def test_truehd_core_bsf_present(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        track = AudioTrackSettings(stream_index=2, codec="copy",
                                   bitrate_kbps=384, extract_truehd_core=True)
        cmd = self.wf.build_command(_make_config(src, tmp_path / "out.mkv",
                                                 audio_tracks=[track]))
        assert "truehd_core" in " ".join(cmd)


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

    def test_size_mode_without_duration(self, tmp_path):
        src = tmp_path / "src.mkv"; src.touch()
        config = _make_config(src, tmp_path / "out.mkv",
                              video=_make_video_settings(quality_mode=QualityMode.SIZE),
                              duration_s=None)
        errors = self.wf.validate(config)
        assert any("durée" in e.lower() or "taille" in e.lower() for e in errors)

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
