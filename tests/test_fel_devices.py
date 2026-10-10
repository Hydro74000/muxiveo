"""Identités inter-API, occupation du pipe et choix matériel FEL par piste."""
from collections import Counter
from pathlib import Path
from unittest.mock import Mock

import pytest

from core.fel.devices import DeviceLoad, FelDevice, FelDevicePlan, pipeline_devices, probe_load, select_device
from core.workflows.encode.models import EncodePreset, VideoEncodeSettings

AMD = FelDevice("a"*32, "AMD intégré", 0x1002, 0, "integrated", 8 << 30)
NVIDIA = FelDevice("b"*32, "RTX A", 0x10DE, 1, "discrete", 16 << 30)
SECOND = FelDevice("c"*32, "RTX B", 0x10DE, 2, "discrete", 16 << 30)


def test_dedicated_gpu_preferred_over_unused_integrated_for_nvenc():
    devices = (AMD, NVIDIA)
    video = VideoEncodeSettings(codec="nvencc_hevc")
    occupied = pipeline_devices(video, devices, {})
    assert occupied == {NVIDIA.uuid}
    assert select_device(devices, {}, occupied, Counter()) == NVIDIA


def test_auto_priority_applies_to_rife_and_falls_back_when_memory_is_insufficient():
    video = VideoEncodeSettings(codec="libx265", interpolation={"enabled": True})
    occupied = pipeline_devices(video, (AMD, NVIDIA), {})
    assert select_device((AMD, NVIDIA), {}, occupied, Counter()) == NVIDIA
    assert select_device((AMD,), {}, frozenset(), Counter()) == AMD
    loads = {NVIDIA.uuid: DeviceLoad(10, 128 << 20)}
    assert select_device((AMD, NVIDIA), loads, occupied, Counter(), 768 << 20) == AMD
    assert select_device((NVIDIA,), loads, occupied, Counter(), 768 << 20) is None


def test_cuda_index_is_mapped_by_uuid_not_vulkan_index():
    devices = (AMD, NVIDIA, SECOND)
    loads = {NVIDIA.uuid: DeviceLoad(cuda_index=0), SECOND.uuid: DeviceLoad(cuda_index=1)}
    video = VideoEncodeSettings(codec="hevc_nvenc", extra_params="-gpu 0")
    assert pipeline_devices(video, devices, loads) == {NVIDIA.uuid}
    chosen = select_device((NVIDIA, SECOND), loads, frozenset({NVIDIA.uuid}), Counter())
    assert chosen == SECOND


def test_resolved_pipeline_device_wins_over_default():
    devices = (NVIDIA, SECOND)
    loads = {NVIDIA.uuid: DeviceLoad(cuda_index=0), SECOND.uuid: DeviceLoad(cuda_index=1)}
    video = VideoEncodeSettings(codec="nvencc_hevc")
    assert pipeline_devices(video, devices, loads, (("nvencc", "--device", "1"),)) == {SECOND.uuid}


def test_busy_memory_and_concurrent_reservation():
    loads = {NVIDIA.uuid: DeviceLoad(70, 1 << 30), SECOND.uuid: DeviceLoad(5, 8 << 30)}
    assert select_device((NVIDIA, SECOND), loads, frozenset(), Counter()) == SECOND
    assert select_device((NVIDIA, SECOND), loads, frozenset(), Counter(), 2 << 30) == SECOND
    assert select_device((NVIDIA, SECOND), {}, frozenset(), Counter({SECOND.uuid: 1})) == NVIDIA
    assert select_device((NVIDIA,), loads, frozenset(), Counter(), 2 << 30) is None
    saturated = {NVIDIA.uuid: DeviceLoad(95), SECOND.uuid: DeviceLoad(5)}
    assert select_device((NVIDIA, SECOND), saturated, frozenset({SECOND.uuid}), Counter()) == SECOND


def test_plan_keeps_device_between_passes_releases_and_honours_cpu(monkeypatch):
    monkeypatch.setattr("core.fel.devices.probe_load", lambda _: {})
    plan = FelDevicePlan("auto", (NVIDIA, SECOND))
    first, release = plan.acquire()
    other = FelDevicePlan("auto", (NVIDIA, SECOND))
    second, release_other = other.acquire()
    try:
        assert first != second
        release()
        release()
        again, release_again = plan.acquire()
        assert again == first
        release_again()
    finally:
        release()
        release_other()
    cpu = FelDevicePlan("cpu", (NVIDIA,))
    choice, release_cpu = cpu.acquire()
    assert choice == "cpu"
    release_cpu()


@pytest.mark.parametrize("choice", ["auto", "cpu", NVIDIA.uuid])
def test_device_profile_roundtrip(choice):
    preset = EncodePreset(fel_device=choice)
    restored = EncodePreset(**preset.to_json_dict()).to_video_settings()
    assert restored.fel_device == choice
    assert EncodePreset().fel_device == "auto"


def test_rife_device_names_are_matched_without_assuming_an_index(monkeypatch):
    monkeypatch.setattr("core.fel.devices.subprocess.run", Mock(return_value=Mock(
        returncode=0, stdout='{"default":0,"gpus":[{"index":0,"name":"RTX B"}]}')))
    video = VideoEncodeSettings(interpolation={"enabled": True})
    assert pipeline_devices(video, (NVIDIA, SECOND), {}, (("muxiveo-rife",),)) == {SECOND.uuid}


def test_load_distinguishes_compute_and_codec_engines_and_keeps_unknown_values(monkeypatch):
    monkeypatch.setattr("core.fel.devices.subprocess.run", Mock(return_value=Mock(
        returncode=0, stdout=f"0, GPU-{NVIDIA.uuid}, 12, 99, N/A, 8192\n"
                            f"1, GPU-{SECOND.uuid}, N/A, N/A, 4, N/A\n")))
    loads = probe_load((NVIDIA, SECOND))
    assert loads[NVIDIA.uuid] == DeviceLoad(12, 8 << 30, 0, 99, None)
    assert loads[SECOND.uuid] == DeviceLoad(None, None, 1, None, 4)
    # Un NVENC occupé ne signifie pas que le calcul Vulkan est saturé.
    assert select_device((NVIDIA,), loads, frozenset(), Counter()) == NVIDIA


def test_sysfs_does_not_assign_readable_load_from_another_gpu(monkeypatch, tmp_path):
    nodes = [tmp_path / "card0", tmp_path / "card1"]
    for node in nodes:
        node.mkdir()
        (node / "vendor").write_text("0x1002")
    (nodes[1] / "gpu_busy_percent").write_text("99")
    monkeypatch.setattr(Path, "glob", lambda *_: iter(nodes))
    assert probe_load((AMD,)) == {}


def test_encoder_reservation_ignores_device_of_upstream_stage():
    loads = {NVIDIA.uuid: DeviceLoad(cuda_index=0), SECOND.uuid: DeviceLoad(cuda_index=1)}
    commands = (("preparation", "--device", "1"), ("nvencc", "--device=0"))
    assert pipeline_devices(VideoEncodeSettings(codec="nvencc_hevc"), (NVIDIA, SECOND), loads, commands) == {NVIDIA.uuid}


def test_slow_inventory_does_not_hold_global_reservation_lock(monkeypatch):
    from core.fel import devices

    def inventory(*_):
        assert devices._LOCK.acquire(blocking=False)
        devices._LOCK.release()
        return frozenset()

    monkeypatch.setattr(devices, "probe_load", lambda _: {})
    monkeypatch.setattr(devices, "pipeline_devices", inventory)
    plan = FelDevicePlan("auto", (NVIDIA,), VideoEncodeSettings())
    _, release = plan.acquire()
    release()


def test_logging_failure_releases_gpu_reservation():
    from core.fel import devices

    before = devices._ACTIVE.copy()
    plan = FelDevicePlan(NVIDIA.uuid, (NVIDIA,), log=Mock(side_effect=RuntimeError("Qt fermé")))
    with pytest.raises(RuntimeError, match="Qt fermé"):
        plan.acquire()
    assert devices._ACTIVE == before
