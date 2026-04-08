"""
tests/test_encode_hardware.py - Tests unitaires pour la detection hardware.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from core.workflows.encode.hardware import HardwareEncoderDetector


def _completed(*, returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


# Désactive le fallback système dans tous les tests (pas d'AppImage dans les tests).
_no_system_ff = patch.object(HardwareEncoderDetector, "_find_system_ffmpeg", return_value=None)


class TestHardwareEncoderDetector:

    def test_detect_windows_nvenc_and_amf_with_real_ffmpeg_probes(self):
        """
        Windows : NVENC et AMF ne doivent pas dependre de nvidia-smi.
        La detection repose sur la presence dans `ffmpeg -encoders` puis
        sur un probe FFmpeg reel pour chaque codec.
        """
        encoders = """
        V....D hevc_nvenc           NVIDIA NVENC hevc encoder
        V....D h264_amf             AMD AMF H.264 Encoder
        """
        calls: list[list[str]] = []

        def fake_run(cmd, **_kwargs):
            calls.append(list(cmd))
            if cmd == ["ffmpeg.exe", "-hide_banner", "-encoders"]:
                return _completed(stderr=encoders)
            if "hevc_nvenc" in cmd or "h264_amf" in cmd:
                return _completed(returncode=0)
            raise AssertionError(f"Commande inattendue: {cmd}")

        with patch("core.workflows.encode.hardware.subprocess.run", side_effect=fake_run), \
             _no_system_ff:
            detected, used_ff = HardwareEncoderDetector().detect("ffmpeg.exe")

        assert detected == {"hevc_nvenc", "h264_amf"}
        assert used_ff == "ffmpeg.exe"
        assert all(cmd[0] != "nvidia-smi" for cmd in calls)

    def test_detect_linux_nvenc_keeps_nvidia_shortcut_for_distrobox(self):
        """
        Linux : si le signal NVIDIA bas niveau est present, on garde la logique
        historique sans exiger un probe FFmpeg reussi.
        """
        encoders = "V....D hevc_nvenc           NVIDIA NVENC hevc encoder\n"
        calls: list[list[str]] = []

        def fake_run(cmd, **_kwargs):
            calls.append(list(cmd))
            if cmd == ["ffmpeg", "-hide_banner", "-encoders"]:
                return _completed(stdout=encoders)
            raise AssertionError(f"Commande inattendue: {cmd}")

        with patch("core.workflows.encode.hardware.subprocess.run", side_effect=fake_run), \
             patch("core.workflows.encode.hardware.sys.platform", "linux"), \
             patch.object(HardwareEncoderDetector, "_nvidia_ok", return_value=True), \
             _no_system_ff:
            detected, used_ff = HardwareEncoderDetector().detect("ffmpeg")

        assert detected == {"hevc_nvenc"}
        assert used_ff == "ffmpeg"
        assert calls == [["ffmpeg", "-hide_banner", "-encoders"]]

    def test_detect_linux_nvenc_falls_back_to_ffmpeg_probe_when_shortcut_is_unavailable(self):
        """Linux/macOS : sans nvidia-smi exploitable, le probe FFmpeg reste un fallback."""
        encoders = "V....D h264_nvenc           NVIDIA NVENC H.264 encoder\n"
        calls: list[list[str]] = []

        def fake_run(cmd, **_kwargs):
            calls.append(list(cmd))
            if cmd == ["ffmpeg", "-hide_banner", "-encoders"]:
                return _completed(stdout=encoders)
            if "h264_nvenc" in cmd:
                return _completed(returncode=0)
            raise AssertionError(f"Commande inattendue: {cmd}")

        with patch("core.workflows.encode.hardware.subprocess.run", side_effect=fake_run), \
             patch("core.workflows.encode.hardware.sys.platform", "linux"), \
             patch.object(HardwareEncoderDetector, "_nvidia_ok", return_value=False), \
             _no_system_ff:
            detected, used_ff = HardwareEncoderDetector().detect("ffmpeg")

        assert detected == {"h264_nvenc"}
        assert used_ff == "ffmpeg"
        assert any("h264_nvenc" in cmd for cmd in calls[1:])

    def test_detect_keeps_vaapi_specific_probe_on_linux(self):
        """
        Linux : VAAPI doit conserver sa logique dediee avec device explicite
        et upload des frames vers le GPU.
        """
        encoders = "V....D hevc_vaapi           H.265/HEVC (VAAPI)\n"
        probe_cmds: list[list[str]] = []

        def fake_run(cmd, **_kwargs):
            if cmd == ["ffmpeg", "-hide_banner", "-encoders"]:
                return _completed(stdout=encoders)
            probe_cmds.append(list(cmd))
            return _completed(returncode=0)

        with patch("core.workflows.encode.hardware.subprocess.run", side_effect=fake_run), \
             patch.object(HardwareEncoderDetector, "_vaapi_device", return_value="/dev/dri/renderD128"), \
             _no_system_ff:
            detected, used_ff = HardwareEncoderDetector().detect("ffmpeg")

        assert detected == {"hevc_vaapi"}
        assert used_ff == "ffmpeg"
        assert len(probe_cmds) == 1
        cmd = probe_cmds[0]
        assert "-vaapi_device" in cmd
        assert cmd[cmd.index("-vaapi_device") + 1] == "/dev/dri/renderD128"
        assert "-vf" in cmd
        assert cmd[cmd.index("-vf") + 1] == "format=nv12,hwupload"

    def test_detect_skips_vaapi_when_device_missing_but_keeps_other_codecs(self):
        """
        Linux/macOS : si VAAPI n'est pas disponible, il ne doit pas empecher
        la detection d'autres encodeurs materiels.
        """
        encoders = """
        V....D hevc_vaapi           H.265/HEVC (VAAPI)
        V....D h264_qsv             H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10 (Intel Quick Sync Video acceleration)
        """
        probe_cmds: list[list[str]] = []

        def fake_run(cmd, **_kwargs):
            if cmd == ["ffmpeg", "-hide_banner", "-encoders"]:
                return _completed(stdout=encoders)
            probe_cmds.append(list(cmd))
            return _completed(returncode=0)

        with patch("core.workflows.encode.hardware.subprocess.run", side_effect=fake_run), \
             patch.object(HardwareEncoderDetector, "_vaapi_device", return_value=None), \
             _no_system_ff:
            detected, used_ff = HardwareEncoderDetector().detect("ffmpeg")

        assert detected == {"h264_qsv"}
        assert all("hevc_vaapi" not in cmd for cmd in probe_cmds)
        assert any("h264_qsv" in cmd for cmd in probe_cmds)

    def test_failed_probe_does_not_mark_codec_as_available(self):
        """Un codec compile mais non utilisable ne doit pas etre expose a l'UI."""
        encoders = "V....D h264_nvenc           NVIDIA NVENC H.264 encoder\n"

        def fake_run(cmd, **_kwargs):
            if cmd == ["ffmpeg", "-hide_banner", "-encoders"]:
                return _completed(stdout=encoders)
            return _completed(returncode=1)

        with patch("core.workflows.encode.hardware.subprocess.run", side_effect=fake_run), \
             _no_system_ff:
            detected, _ff = HardwareEncoderDetector().detect("ffmpeg")

        assert detected == set()

    def test_detect_returns_empty_set_when_no_hardware_encoder_is_compiled(self):
        """macOS ou build software-only : aucun codec hardware ne doit etre annonce."""
        with patch(
            "core.workflows.encode.hardware.subprocess.run",
            return_value=_completed(stdout="V....D libx265           libx265 H.265 / HEVC\n"),
        ), _no_system_ff:
            detected, _ff = HardwareEncoderDetector().detect("ffmpeg")

        assert detected == set()

    def test_detect_falls_back_to_system_ffmpeg_when_bundled_has_no_hw_codecs(self):
        """
        AppImage : si le ffmpeg embarqué n'a pas de codec HW, le ffmpeg système
        doit être utilisé pour la détection et le chemin retourné doit être celui
        du système.
        """
        bundled_output = "V....D libx265           libx265 H.265 / HEVC\n"
        system_output  = "V....D h264_nvenc        NVIDIA NVENC H.264 encoder\n"

        def fake_run(cmd, **_kwargs):
            if cmd[0] == "/usr/bin/ffmpeg" and "-encoders" in cmd:
                return _completed(stdout=bundled_output)
            if cmd[0] == "/system/bin/ffmpeg" and "-encoders" in cmd:
                return _completed(stdout=system_output)
            if "h264_nvenc" in cmd:
                return _completed(returncode=0)
            raise AssertionError(f"Commande inattendue: {cmd}")

        with patch("core.workflows.encode.hardware.subprocess.run", side_effect=fake_run), \
             patch.object(HardwareEncoderDetector, "_find_system_ffmpeg",
                          return_value="/system/bin/ffmpeg"), \
             patch.object(HardwareEncoderDetector, "_nvidia_ok", return_value=True):
            detected, used_ff = HardwareEncoderDetector().detect("/usr/bin/ffmpeg")

        assert detected == {"h264_nvenc"}
        assert used_ff == "/system/bin/ffmpeg"

    def test_detect_and_detect_software_reuse_encoders_cache(self):
        """
        Sur une même instance, la sortie `ffmpeg -encoders` doit être réutilisée
        entre detect() et detect_software() pour éviter un subprocess redondant.
        """
        encoders = """
        V....D hevc_nvenc           NVIDIA NVENC hevc encoder
        V....D libx265              libx265 H.265 / HEVC
        """
        calls: list[list[str]] = []

        def fake_run(cmd, **_kwargs):
            calls.append(list(cmd))
            if cmd == ["ffmpeg", "-hide_banner", "-encoders"]:
                return _completed(stdout=encoders)
            raise AssertionError(f"Commande inattendue: {cmd}")

        with patch("core.workflows.encode.hardware.subprocess.run", side_effect=fake_run), \
             patch("core.workflows.encode.hardware.sys.platform", "linux"), \
             patch.object(HardwareEncoderDetector, "_nvidia_ok", return_value=True), \
             _no_system_ff:
            detector = HardwareEncoderDetector()
            detected, _ff = detector.detect("ffmpeg")
            software = detector.detect_software("ffmpeg")

        assert detected == {"hevc_nvenc"}
        assert software == {"libx265"}
        assert calls.count(["ffmpeg", "-hide_banner", "-encoders"]) == 1
