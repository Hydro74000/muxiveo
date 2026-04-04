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

    Codec COPY — passthrough métadonnées (TestCopyCodecMetadataPassthrough) :
        build_command single pass :
            - codec=copy → -map_metadata 0 présent
            - codec=copy → -map_metadata:s:v:0 0:s:v:0 présent
            - codec=copy → -map_metadata avant la sortie
            - codec≠copy (libx265, libx264, nvenc, amf…) → pas de -map_metadata
        build_command two pass (SIZE) :
            - codec=copy → pass2 contient -map_metadata 0
            - codec=copy → pass1 ne contient PAS -map_metadata
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
        """src.hevc absent dans le snapshot qui crée enc.hevc (l'encodage vidéo)."""
        snapshots = self._run_inject(tmp_path, copy_dv, copy_hdr10plus)
        # enc.hevc est créé à l'étape 5a (encodage vidéo direct, sans container)
        enc_snap_idx = next(
            (i for i, snap in enumerate(snapshots)
             if any(p.name == "enc.hevc" for p in snap)), None
        )
        assert enc_snap_idx is not None, "enc.hevc jamais créé"
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
      - src.hevc est toujours supprimé avant l'encodage (plus de branche _copy_video)
      - enc.hevc est toujours extrait depuis encoded.mkv
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

    def test_src_hevc_deleted_before_encode_for_copy(self, tmp_path):
        """
        src.hevc est supprimé AVANT l'étape d'encodage vidéo (enc.hevc).
        Plus de branche spéciale _copy_video, plus de encoded.mkv.
        """
        _, snapshots = self._run_inject_copy(tmp_path, copy_dv=True, copy_hdr10plus=False)
        enc_snap = next(
            (s for s in snapshots if any("enc.hevc" in n for n in s)), None
        )
        assert enc_snap is not None, "enc.hevc jamais créé"
        assert not any("src.hevc" in n for n in enc_snap), \
            f"src.hevc encore présent lors de l'encodage vidéo : {enc_snap}"

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
        (extrait de encoded.mkv), pas sur src.hevc (qui est supprimé avant l'encodage).
        """
        cmds, _ = self._run_inject_copy(tmp_path, copy_dv=True, copy_hdr10plus=False)
        dv_injects = [c for c in cmds if "inject-rpu" in c]
        assert len(dv_injects) == 1
        # Le fichier d'entrée de inject-rpu doit être enc.hevc (plus src.hevc)
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

    # ── two pass (mode SIZE) ──────────────────────────────────────────────────

    def test_copy_two_pass_pass2_has_map_metadata(self, tmp_path):
        """La passe 2 contient -map_metadata 0 pour codec=copy en mode SIZE."""
        src = tmp_path / "src.mkv"; src.touch()
        vs = _make_video_settings(codec="copy", quality_mode=QualityMode.SIZE)
        cmds = self.wf.build_command(
            _make_config(src, tmp_path / "out.mkv", video=vs, duration_s=3600.0)
        )
        assert isinstance(cmds[0], list), "mode SIZE doit retourner list[list[str]]"
        pass2 = cmds[1]
        assert "-map_metadata" in pass2
        idx = pass2.index("-map_metadata")
        assert pass2[idx + 1] == "0"

    def test_copy_two_pass_pass1_no_map_metadata(self, tmp_path):
        """La passe 1 ne contient PAS -map_metadata (analyse seule, pas de sortie)."""
        src = tmp_path / "src.mkv"; src.touch()
        vs = _make_video_settings(codec="copy", quality_mode=QualityMode.SIZE)
        cmds = self.wf.build_command(
            _make_config(src, tmp_path / "out.mkv", video=vs, duration_s=3600.0)
        )
        pass1 = cmds[0]
        assert "-map_metadata" not in pass1

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

        with patch.object(wf, "_run_with_metadata_inject", side_effect=_spy):
            wf.run(config)

        assert inject_called[0], \
            "codec≠copy + copy_dv doit passer par _run_with_metadata_inject"

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
        cmd = self.wf.build_command_single(
            _make_config(src, tmp_path / "out.mkv",
                         attachment_streams=att_streams,
                         extra_attachments=extras)
        )
        # Le premier attachement extra doit utiliser l'index 1, pas 0
        assert "-metadata:s:t:1" in cmd
        assert "-metadata:s:t:0" not in cmd or \
            all(cmd[i + 1].startswith("mimetype=") or cmd[i + 1].startswith("filename=")
                is False
                for i in [j for j, a in enumerate(cmd) if a == "-metadata:s:t:0"])

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
        self, tmp_path: Path, extras: list[Path]
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
        recon = [c for c in cmds if c[0] == "ffmpeg" and "output.mkv" in " ".join(c)]
        assert len(recon) == 1
        return recon[0]

    def test_inject_attach_flag_present(self, tmp_path):
        """-attach présent dans la reconstitution finale (chemin DV/HDR10+)."""
        extras = [tmp_path / "cover.jpg"]
        cmds = self._run_inject_with_extras(tmp_path, extras)
        recon = self._get_recon_cmd(cmds)
        assert "-attach" in recon

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
# _apply_track_meta_edits_inplace — commande mkvpropedit
# ===========================================================================

class TestApplyTrackMetaEdits:
    """
    Vérifie que _apply_track_meta_edits_inplace construit la commande mkvpropedit
    correctement pour chaque TrackMetaEdit :
      - --edit track:@N sélectionne la piste 1-based
      - --set language=<ISO639-2> précède --set language-ietf=<IETF>
      - --set name=<title> est émis si title est fourni
      - langage régional (fr-FR) → ISO 639-2 = fra, IETF conservé tel quel
    """

    def setup_method(self):
        self.wf = _make_workflow()

    def _run_edits(self, edits: list, output: Path) -> list[str]:
        """Lance _apply_track_meta_edits_inplace et retourne la commande capturée."""
        captured: list[list[str]] = []

        def _fake_run(cmd, **_kw):
            captured.append(list(cmd))
            return MagicMock(returncode=0, stderr="")

        with patch("subprocess.run", side_effect=_fake_run):
            self.wf._apply_track_meta_edits_inplace(output, edits)

        assert len(captured) == 1, f"Une seule commande attendue, reçu : {captured}"
        return captured[0]

    # ── fr-FR : langue régionale ──────────────────────────────────────────────

    def test_fr_FR_iso_code_is_fra(self, tmp_path):
        """fr-FR → --set language=fra (ISO 639-2/T)."""
        from core.workflows.encode.models import TrackMetaEdit
        output = tmp_path / "out.mkv"
        output.touch()
        edit = TrackMetaEdit(track_order=1, language="fr-FR")

        cmd = self._run_edits([edit], output)

        assert "--set" in cmd
        assert "language=fra" in cmd

    def test_fr_FR_ietf_tag_preserved(self, tmp_path):
        """fr-FR → --set language-ietf=fr-FR (balise IETF conservée intacte)."""
        from core.workflows.encode.models import TrackMetaEdit
        output = tmp_path / "out.mkv"
        output.touch()
        edit = TrackMetaEdit(track_order=1, language="fr-FR")

        cmd = self._run_edits([edit], output)

        assert "language-ietf=fr-FR" in cmd

    def test_fr_FR_language_before_language_ietf(self, tmp_path):
        """--set language=fra doit précéder --set language-ietf=fr-FR."""
        from core.workflows.encode.models import TrackMetaEdit
        output = tmp_path / "out.mkv"
        output.touch()
        edit = TrackMetaEdit(track_order=1, language="fr-FR")

        cmd = self._run_edits([edit], output)

        idx_lang = cmd.index("language=fra")
        idx_ietf = cmd.index("language-ietf=fr-FR")
        assert idx_lang < idx_ietf, \
            f"language= ({idx_lang}) doit précéder language-ietf= ({idx_ietf})"

    def test_fr_FR_track_selector(self, tmp_path):
        """--edit track:@1 est émis pour track_order=1."""
        from core.workflows.encode.models import TrackMetaEdit
        output = tmp_path / "out.mkv"
        output.touch()
        edit = TrackMetaEdit(track_order=1, language="fr-FR")

        cmd = self._run_edits([edit], output)

        assert "track:@1" in cmd

    # ── langue simple (fr) ───────────────────────────────────────────────────

    def test_fr_iso_code_is_fra(self, tmp_path):
        """fr (sans région) → --set language=fra."""
        from core.workflows.encode.models import TrackMetaEdit
        output = tmp_path / "out.mkv"
        output.touch()
        edit = TrackMetaEdit(track_order=2, language="fr")

        cmd = self._run_edits([edit], output)

        assert "language=fra" in cmd
        assert "language-ietf=fr" in cmd

    # ── title seul (pas de langue) ───────────────────────────────────────────

    def test_title_only_no_language_set(self, tmp_path):
        """title seul → --set name=X, aucun language= dans la commande."""
        from core.workflows.encode.models import TrackMetaEdit
        output = tmp_path / "out.mkv"
        output.touch()
        edit = TrackMetaEdit(track_order=1, language="", title="Commentaires")

        cmd = self._run_edits([edit], output)

        assert "name=Commentaires" in cmd
        assert not any(a.startswith("language=") for a in cmd)

    # ── merge remux extras : fr-FR transmis tel quel dans TrackMetaEdit ───────

    def test_merge_fr_FR_in_track_meta_edit(self, tmp_path):
        """
        _merge_remux_extras avec une piste language='fr-FR' doit produire
        un TrackMetaEdit avec language='fr-FR' (conservé tel quel).
        """
        from core.workflows.remux import RemuxConfig, SourceInput, TrackEntry
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
