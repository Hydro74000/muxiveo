"""Détections facultatives : absence, présence simulée et prérequis inutilisables.

Ces tests appellent les vrais détecteurs. Seules leurs entrées externes
(processus et fichiers d'extension) sont simulées, sans dépendre du GPU hôte.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from core import plugins
from core.version import MVO_RIFE_CONTRACT
from core.workflows.encode.hardware import HardwareEncoderDetector
from core.workflows.encode import hw_devices
from core.workflows.encode.interpolation import (
    clear_rife_version_cache,
    detect_gpu_acceleration as real_detect_gpu_acceleration,
)
from core.workflows.encode.runtime.nvencc_p5 import nvencc_p5_features
from core.workflows.encode.vulkan import detect_vulkan


def _completed(cmd, stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)


@pytest.mark.parametrize(
    "state,expected,options",
    [
        ("unconfigured", set(), []),
        ("missing", set(), ["--check-hw"]),
        ("no-gpu", set(), ["--check-hw"]),
        ("pre-ada", {"nvencc_h264", "nvencc_hevc"}, ["--check-hw"]),
        ("ada", {"nvencc_h264", "nvencc_hevc", "nvencc_av1"}, ["--check-hw"]),
        ("features-fallback", {"nvencc_hevc"}, ["--check-hw", "--check-features"]),
        ("reader-only", set(), ["--check-hw", "--check-features"]),
    ],
    ids=["unconfigured", "missing", "no-gpu", "pre-ada", "ada", "features-fallback", "reader-only"],
)
def test_nvencc_detection_registers_only_gpu_encoders(monkeypatch, state, expected, options):
    calls = []
    banner = "reader: avhw [H.264/AVC, H.265/HEVC, AV1]\n"

    def run(cmd, **kwargs):
        assert kwargs["check"] is False
        if cmd == ["ffmpeg", "-hide_banner", "-encoders"]:
            return _completed(cmd, stdout="V....D libx265 software encoder\n")
        assert kwargs["timeout"] > 0
        assert cmd[0] == "nvencc" and cmd[1] in ("--check-hw", "--check-features")
        calls.append(cmd[1])
        if state == "missing":
            raise FileNotFoundError("nvencc")
        if state == "no-gpu":
            # Une sortie partielle ne suffit pas lorsque la sonde échoue.
            return _completed(cmd, stdout=banner + "Codec: H.265/HEVC\n", stderr="No NVIDIA GPU", returncode=1)
        if state in ("pre-ada", "ada"):
            encoders = "Avaliable Codec(s)\nH.264/AVC\nH.265/HEVC\n"
            if state == "ada":
                encoders += "AV1\n"
            return _completed(cmd, stderr=banner + encoders)
        if state == "features-fallback" and cmd[1] == "--check-features":
            return _completed(cmd, stdout="Codec: H.265/HEVC\n")
        assert state in ("features-fallback", "reader-only")
        return _completed(cmd, stdout=banner)

    monkeypatch.setattr("core.workflows.encode.hardware.subprocess.run", run)
    monkeypatch.setattr(HardwareEncoderDetector, "_find_system_ffmpeg", lambda *_: None)
    detected, ffmpeg = HardwareEncoderDetector().detect(
        "ffmpeg", nvencc_bin=None if state == "unconfigured" else "nvencc",
    )
    assert (detected, ffmpeg) == (expected, "ffmpeg")
    assert calls == options


@pytest.mark.parametrize("state", ["missing", "without-libdovi", "without-libplacebo", "ready", "failed"])
def test_nvencc_p5_prerequisites(monkeypatch, state):
    def run(cmd, **kwargs):
        assert cmd == ["nvencc", "--check-features"]
        if state == "missing":
            raise FileNotFoundError("nvencc")
        features = f"libdovi: {'no' if state == 'without-libdovi' else 'yes'}\n"
        features += f"libplacebo: {'no' if state == 'without-libplacebo' else 'yes'}\n"
        return _completed(cmd, stderr=features, returncode=1 if state == "failed" else 0)

    monkeypatch.setattr("core.workflows.encode.runtime.nvencc_p5.subprocess.run", run)
    assert nvencc_p5_features("nvencc") is (state == "ready")


@pytest.mark.parametrize("codec", ["h264_nvenc", "hevc_qsv", "h264_amf"])
@pytest.mark.parametrize("present", [False, True], ids=["absent", "present"])
def test_windows_hardware_adapter_detection(monkeypatch, codec, present):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        # Le premier adaptateur ne correspond pas au matériel recherché.
        return _completed(cmd, returncode=0 if present and len(calls) == 2 else 1)

    hw_devices.select_windows_hwaccel_device.cache_clear()
    monkeypatch.setattr(hw_devices.subprocess, "run", run)
    try:
        selected = hw_devices.select_windows_hwaccel_device(codec, ffmpeg_bin="ffmpeg.exe", max_adapters=3)
    finally:
        hw_devices.select_windows_hwaccel_device.cache_clear()
    assert selected == ("1" if present else None)
    assert len(calls) == (2 if present else 3)
    for index, cmd in enumerate(calls):
        assert cmd[cmd.index("-c:v") + 1] == codec
        if codec.endswith("_nvenc"):
            assert cmd[cmd.index("-gpu") + 1] == str(index)
        elif codec.endswith("_qsv"):
            assert cmd[cmd.index("-qsv_device") + 1] == str(index)
        else:
            assert cmd[cmd.index("-init_hw_device") + 1] == f"d3d11va=mre_amf_probe{index}:{index}"


@pytest.mark.parametrize("state", ["absent", "sysfs-only", "dev-only", "present"])
def test_linux_hardware_nodes_require_both_sysfs_and_device(tmp_path, monkeypatch, state):
    sysfs = tmp_path / "sysfs"
    dev = tmp_path / "dev"
    if state in ("sysfs-only", "present"):
        vendor = sysfs / "renderD129" / "device" / "vendor"
        vendor.parent.mkdir(parents=True)
        vendor.write_text("0x8086\n", encoding="utf-8")
    if state in ("dev-only", "present"):
        dev.mkdir()
        (dev / "renderD129").touch()
    roots = {"/sys/class/drm": sysfs, "/dev/dri": dev}
    monkeypatch.setattr(hw_devices, "Path", lambda path: roots.get(path, Path(path)))
    monkeypatch.setattr(hw_devices.sys, "platform", "linux")
    hw_devices.detect_linux_render_nodes.cache_clear()
    try:
        nodes = hw_devices.detect_linux_render_nodes()
        selected = hw_devices.select_linux_hwaccel_device("hevc_qsv")
    finally:
        hw_devices.detect_linux_render_nodes.cache_clear()
    if state == "present":
        assert len(nodes) == 1 and nodes[0].vendor_id == "0x8086"
        assert selected == nodes[0].path == str(dev / "renderD129")
    else:
        assert nodes == () and selected is None


_VULKAN_LISTING = (
    "[Vulkan @ 0x1] 0: AMD Integrated (integrated) (0x1)\n"
    "[Vulkan @ 0x1] 1: NVIDIA RTX (discrete) (0x2)\n"
    "[Vulkan @ 0x1] 2: llvmpipe (software) (0x0)\n"
)


@pytest.mark.parametrize(
    "state,available,nlmeans,reason,steps",
    [
        ("missing", False, False, "FFmpeg introuvable", 1),
        ("without-filter", False, False, "sans filtres Vulkan", 1),
        ("no-device", False, False, "aucun périphérique Vulkan", 2),
        ("integrated-only", False, False, "aucun GPU dédié", 2),
        ("ready", True, True, "", 3),
        ("driver-failure", True, False, "Vulkan device lost", 3),
        ("probe-timeout", True, False, "sonde nlmeans_vulkan en échec", 3),
    ],
)
def test_vulkan_detection_probes_selected_discrete_gpu(monkeypatch, state, available, nlmeans, reason, steps):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        assert cmd[0] == "ffmpeg" and kwargs["timeout"] > 0
        if "-filters" in cmd:
            if state == "missing":
                raise FileNotFoundError("ffmpeg")
            return _completed(cmd, stdout="nlmeans" if state == "without-filter" else "nlmeans_vulkan")
        if "-v" in cmd:
            listing = _VULKAN_LISTING
            if state == "no-device":
                listing = "Cannot create Vulkan instance"
            elif state == "integrated-only":
                listing = "\n".join(line for line in listing.splitlines() if "(discrete)" not in line)
            return _completed(cmd, stderr=listing, returncode=1 if state == "no-device" else 0)
        assert cmd[cmd.index("-init_hw_device") + 1] == "vulkan=vk:1"
        assert cmd[cmd.index("-i") + 1] == "color=c=gray:s=64x64,format=p010le"
        assert cmd[cmd.index("-vf") + 1] == "hwupload,nlmeans_vulkan,hwdownload,format=p010le"
        if state == "probe-timeout":
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
        return _completed(cmd, stderr="Vulkan device lost" if state == "driver-failure" else "",
                          returncode=1 if state == "driver-failure" else 0)

    monkeypatch.setattr("core.workflows.encode.vulkan.subprocess.run", run)
    capability = detect_vulkan("ffmpeg")
    assert (capability.available, capability.nlmeans) == (available, nlmeans)
    assert reason in capability.reason
    assert bool(capability.reason) is (not nlmeans)
    assert len(calls) == steps
    if available:
        assert (capability.device, capability.index) == ("NVIDIA RTX", 1)
    else:
        assert (capability.device, capability.index) == ("", None)


@pytest.fixture
def simulated_rife(tmp_path, monkeypatch):
    """Vrai parcours résolution → version → capacités → liste des GPU."""
    binary = tmp_path / "muxiveo-rife"
    binary.write_bytes(b"simulated binary")
    calls = []
    clear_rife_version_cache()

    def install_probe(payload, *, returncode=0, error=None):
        def run(cmd, **kwargs):
            calls.append(cmd)
            assert cmd[0] == str(binary)
            if cmd[1:] == ["--version"]:
                return _completed(cmd, stdout="muxiveo-rife 1.7.0\n")
            if cmd[1:] == ["--capabilities"]:
                return _completed(cmd, stdout=json.dumps({
                    "contract": MVO_RIFE_CONTRACT, "options": ["nvof", "trt-plugin"], "models": [],
                }))
            assert cmd[1] == "--list-gpus"
            if error is not None:
                raise error
            return _completed(cmd, stdout=json.dumps(payload), returncode=returncode)

        monkeypatch.setattr("core.workflows.encode.interpolation.subprocess.run", run)
        return str(binary), calls

    yield install_probe
    clear_rife_version_cache()


@pytest.mark.parametrize(
    "state,nvof,compatible,ready",
    [
        ("no-gpu", False, False, False),
        ("non-nvidia", False, False, False),
        ("without-nvofa", False, True, False),
        ("without-plugin", True, True, False),
        ("ready", True, True, True),
        ("plugin-failure", True, True, False),
        ("incompatible-driver", True, False, False),
    ],
)
def test_rife_acceleration_with_and_without_hardware_and_plugin(simulated_rife, state, nvof, compatible, ready):
    status = {
        "non-nvidia": "GPU non NVIDIA", "without-nvofa": "GPU sans NVOFA",
        "without-plugin": "compatible", "ready": "prêt (1.0.0)",
        "plugin-failure": "bibliothèque TensorRT introuvable", "incompatible-driver": "pilote trop ancien",
    }.get(state, "")
    device = {
        "index": 0, "name": "AMD GPU" if state == "non-nvidia" else "NVIDIA RTX",
        "nvof": nvof, "nvof_status": status if not nvof else "disponible",
        "trt_compatible": compatible, "trt_status": status,
    }
    plugin = "" if state in ("without-plugin", "without-nvofa", "no-gpu") else "/plugins/trt"
    if plugin:
        device["trt"] = ready
    binary, calls = simulated_rife({"default": 0, "gpus": [] if state == "no-gpu" else [device]})
    capability = real_detect_gpu_acceleration(binary, trt_plugin=plugin)
    assert (capability.nvof.available, capability.trt.compatible, capability.trt.ready) == (nvof, compatible, ready)
    assert [cmd[1] for cmd in calls] == ["--version", "--capabilities", "--list-gpus"]
    assert calls[-1][2:] == (["--trt-plugin", plugin] if plugin else [])
    if state == "no-gpu":
        assert capability.nvof.reason == capability.trt.reason == "aucun GPU Vulkan"
    else:
        assert capability.nvof.device == capability.trt.device == device["name"]
        assert capability.trt.reason == status
        if not nvof:
            assert capability.nvof.reason == status


@pytest.mark.parametrize("configured", [False, True], ids=["unconfigured", "missing-file"])
def test_rife_absent_does_not_start_a_probe(tmp_path, monkeypatch, configured):
    def unexpected_run(*args, **kwargs):
        pytest.fail("Aucun processus ne doit être lancé sans muxiveo-rife")

    monkeypatch.setattr("core.workflows.encode.interpolation.subprocess.run", unexpected_run)
    binary = str(tmp_path / "missing-muxiveo-rife") if configured else None
    capability = real_detect_gpu_acceleration(binary, trt_plugin="/plugins/trt")
    assert not capability.nvof.available and not capability.trt.compatible and not capability.trt.ready
    assert capability.nvof.reason == capability.trt.reason == "muxiveo-rife introuvable"


@pytest.mark.parametrize("gpu,expected", [(-1, False), (1, True), (4, False)])
def test_rife_detects_acceleration_on_the_selected_gpu(simulated_rife, gpu, expected):
    binary, _ = simulated_rife({"default": 0, "gpus": [
        {"index": 0, "name": "AMD", "nvof": False, "trt_compatible": False},
        {"index": 1, "name": "NVIDIA RTX", "nvof": True, "trt_compatible": True, "trt": True},
    ]})
    capability = real_detect_gpu_acceleration(binary, trt_plugin="/plugins/trt", gpu=gpu)
    assert (capability.nvof.available, capability.trt.compatible, capability.trt.ready) == (expected,) * 3
    assert capability.nvof.device == capability.trt.device == {-1: "AMD", 1: "NVIDIA RTX", 4: ""}[gpu]


@pytest.mark.parametrize("state,reason", [
    ("failed", "liste des GPU en échec (code 1)"),
    ("timeout", "liste des GPU illisible (TimeoutExpired)"),
    ("invalid-default", "liste des GPU illisible (ValueError)"),
    ("null-default", "liste des GPU illisible (TypeError)"),
    ("invalid-gpus", "liste des GPU illisible (ValueError)"),
])
def test_rife_failed_probe_never_advertises_acceleration(simulated_rife, state, reason):
    payload = {"default": 0, "gpus": [{"index": 0, "nvof": True, "trt_compatible": True, "trt": True}]}
    if state == "invalid-default":
        payload["default"] = "invalid"
    elif state == "null-default":
        payload["default"] = None
    elif state == "invalid-gpus":
        payload["gpus"] = 42
    binary, _ = simulated_rife(
        payload, returncode=1 if state == "failed" else 0,
        error=subprocess.TimeoutExpired("muxiveo-rife", 90) if state == "timeout" else None,
    )
    capability = real_detect_gpu_acceleration(binary, trt_plugin="/plugins/trt")
    assert not capability.nvof.available and not capability.trt.compatible and not capability.trt.ready
    assert capability.nvof.reason == capability.trt.reason == reason


@pytest.mark.parametrize("spec,platform", [
    (spec, platform) for spec in plugins.EXTENSIONS.values() for platform in spec.files
])
@pytest.mark.parametrize("state", ["absent", "present", "missing-file", "incompatible", "bad-manifest"])
def test_installed_extension_detection_checks_real_files(tmp_path, monkeypatch, spec, platform, state):
    root = tmp_path / "plugins"
    directory = root / spec.id / spec.min_version
    main_file = directory / spec.files[platform]
    if state != "absent":
        directory.mkdir(parents=True)
        manifest = {"name": spec.id, "platform": platform, "version": spec.min_version}
        manifest["contract" if spec is plugins.RIFE else "abi"] = (
            MVO_RIFE_CONTRACT if spec is plugins.RIFE else plugins.TRT_PLUGIN_ABI
        ) + (1 if state == "incompatible" else 0)
        (directory / "manifest.json").write_text(
            "invalid json" if state == "bad-manifest" else json.dumps(manifest), encoding="utf-8",
        )
        (directory.parent / "current.json").write_text(
            json.dumps({"version": spec.min_version, "dir": directory.name}), encoding="utf-8",
        )
        if state != "missing-file":
            main_file.write_bytes(b"simulated extension")

    monkeypatch.setattr(plugins, "platform_tag", lambda: platform)
    installed = plugins.installed_plugin(spec, root)
    if state == "present":
        assert installed is not None and installed.path == directory and installed.version == spec.min_version
        assert plugins.main_file(spec, installed) == main_file
    else:
        assert installed is None
    if spec is plugins.RIFE:
        assert plugins.rife_executable(root) == (main_file if state == "present" else None)
