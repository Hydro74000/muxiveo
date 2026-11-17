"""Résultats typés : la sortie réelle de chaque opération respecte le schéma publié (valeurs comprises)."""

from __future__ import annotations

import typing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.inspector import (
    AttachmentInfo,
    AudioTrack,
    ChapterEntry,
    ChapterInfo,
    FileInfo,
    HDRType,
    SubtitleTrack,
    VideoTrack,
)
from core.service import ExecutionContext, ListSink, call
from core.service.catalog import CATALOG
from tests._json_schema import result_errors


def _ctx(config: Any = None, **kwargs: Any) -> ExecutionContext:
    return ExecutionContext.create(config or SimpleNamespace(), ListSink(), **kwargs)


def _info(path: Path) -> FileInfo:
    video = VideoTrack(0, "hevc", "H.265 / HEVC", 3840, 2160, "24000/1001", 10, "yuv420p10le", "bt2020",
                       "smpte2084", "bt2020nc", hdr_type=HDRType.HDR10, language="und", duration_s=1.0,
                       raw={"codec_type": "video"})
    audio = AudioTrack(1, "flac", "FLAC", 2, "stereo", 48000, None, "fra", "VF", duration_s=1.0)
    subtitle = SubtitleTrack(2, "subrip", "fre", None, forced=True)
    font = AttachmentInfo(3, 0, "police.ttf", "font/ttf", 16)
    return FileInfo(path=path, format="matroska,webm", duration_s=1.0, size_bytes=2048, bit_rate=16384,
                    video_tracks=[video], audio_tracks=[audio], subtitle_tracks=[subtitle], attachments=[font],
                    chapters=ChapterInfo([ChapterEntry(0.0, "Début"), ChapterEntry(0.5, "")]), frame_count=24,
                    tag_count=1, hdr_type=HDRType.HDR10, title="Film", global_tags={"COLLECTION": "Muxiveo"})


@pytest.fixture
def inspected(monkeypatch, tmp_path):
    source = tmp_path / "film.mkv"
    source.write_bytes(b"x")

    class _Inspector:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def inspect(self, path: Path) -> FileInfo:
            return _info(Path(path))

    monkeypatch.setattr("core.service.ops.media.FileInspector", _Inspector)
    monkeypatch.setattr("core.service.ops.extract.FileInspector", _Inspector)
    return source


def test_inspection_record_and_tracks(inspected):
    result = call(CATALOG, "media.inspect", {"inputs": [str(inspected)]}, _ctx())
    assert not result_errors("media.inspect", result)
    assert [track["type"] for track in result.files[0]["tracks"]] == ["video", "audio", "subtitle"]
    hdr = CATALOG.describe()["operations"]["media.inspect"]["result"]["properties"]["files"]["items"]
    assert hdr["properties"]["hdr_type"]["enum"] == [kind.label() for kind in HDRType]


def test_extraction_outputs(inspected, tmp_path):
    result = call(CATALOG, "media.extract", {"source": str(inspected), "all": True, "dry_run": True,
                                             "output_dir": str(tmp_path / "out")}, _ctx())
    assert {item["kind"] for item in result.outputs} >= {"video", "audio", "subtitle", "attachment"}
    assert not result_errors("media.extract", result)


class _Config:
    """Configuration factice : chaque réglage a une valeur ; provenances variées (clés facultatives)."""

    def __getattr__(self, name: str) -> Any:
        return f"valeur-{name}"

    def value_source(self, section: str, key: str) -> tuple[str, str | None, str | None]:
        return {"language": ("ini", "fre", "eng"), "theme": ("settings", None, "light")}.get(
            key, ("default", None, None))


def test_config_and_workdir_entries(monkeypatch, tmp_path):
    ctx = _ctx(ports={"app_config": _Config})
    result = call(CATALOG, "config.show", {"section": "ui"}, ctx)
    assert {"masked", "ini_value", "settings_value", "choices"} <= {key for e in result.entries for key in e}
    assert not result_errors("config.show", result)
    leftover = tmp_path / "work" / "ancien"
    leftover.mkdir(parents=True)
    (leftover / "f.bin").write_bytes(b"1234")
    monkeypatch.setattr("core.workdir.cleanable_work_dir_entries", lambda _root: [leftover])
    listed = call(CATALOG, "workdir.list", {}, _ctx(SimpleNamespace(work_dir=tmp_path / "work")))
    assert listed.entries == ({"path": str(leftover), "size_bytes": 4},)
    assert not result_errors("workdir.list", listed)


def test_encode_presets_and_decision_profiles(tmp_path):
    config = SimpleNamespace(app_data_dir=tmp_path / "data", profiles_dir=tmp_path / "profiles")
    call(CATALOG, "encode_preset.save", {"preset": {"name": "4K", "codec": "libx265"}}, _ctx(config))
    (tmp_path / "data" / "encode_profiles" / "casse.json").write_text("{", encoding="utf-8")
    presets = call(CATALOG, "encode_preset.list", {}, _ctx(config))
    assert presets.presets and presets.load_errors
    assert not result_errors("encode_preset.list", presets)
    assert not result_errors("encode_preset.show", call(CATALOG, "encode_preset.show", {"name": "4K"}, _ctx(config)))
    profile = {"version": 1, "kind": "decision-profile", "name": "Film", "rules": []}
    saved = call(CATALOG, "profile.save", {"profile": profile}, _ctx(config))
    assert not result_errors("profile.save", saved)
    listed = call(CATALOG, "profile.list", {}, _ctx(config))
    assert listed.profiles and not result_errors("profile.list", listed)


def test_api_presets_keep_their_published_types(tmp_path):
    """Requête de l'API : types vérifiés à l'enregistrement et à l'import ; preset stocké non conforme signalé,
    jamais publié ; commandes historiques inchangées."""
    import json

    from core.service import ApiError

    config = SimpleNamespace(app_data_dir=tmp_path / "data")
    loose = {"name": "Contrat", "description": None, "crf": 18.5, "resize": {"enabled": 1}}
    with pytest.raises(ApiError) as caught:
        call(CATALOG, "encode_preset.save", {"preset": loose}, _ctx(config))
    assert caught.value.code.value == "invalid_request"
    assert {detail.path for detail in caught.value.details} == {"preset.description", "preset.crf",
                                                                "preset.resize.enabled"}
    source = tmp_path / "loose.json"
    source.write_text(json.dumps(loose), encoding="utf-8")
    with pytest.raises(ApiError) as caught:
        call(CATALOG, "encode_preset.import", {"file": str(source)}, _ctx(config))
    assert caught.value.code.value == "validation"
    historical = _ctx(config, api=False)  # CLI : le preset est enregistré comme avant
    call(CATALOG, "encode_preset.import", {"file": str(source)}, historical, allow_relative=True)
    call(CATALOG, "encode_preset.save", {"preset": {"name": "Propre", "crf": 20}}, _ctx(config))
    _write(tmp_path / "data" / "encode_profiles" / "broken.json", {"name": "broken", "codec": 7, "preset": "safe"})
    listed = call(CATALOG, "encode_preset.list", {}, _ctx(config))
    assert [entry["name"] for entry in listed.presets] == ["Propre"]
    errors = {Path(entry["path"]).name: entry["error"] for entry in listed.load_errors}
    assert set(errors) == {"broken.json", "Contrat.json"} and "preset.crf" in errors["Contrat.json"]
    assert not result_errors("encode_preset.list", listed)
    assert [entry["name"] for entry in call(CATALOG, "encode_preset.list", {}, historical).presets] == [
        "Contrat", "Propre"]
    with pytest.raises(ApiError) as caught:
        call(CATALOG, "encode_preset.show", {"name": "Contrat"}, _ctx(config))
    assert caught.value.code.value == "validation"
    named = call(CATALOG, "encode_preset.import", {"file": str(_write(tmp_path / "anonyme.json", {"crf": 22})),
                                                   "name": "Nommé"}, _ctx(config))
    assert named.name == "Nommé"


def _write(path: Path, data: Any) -> Path:
    import json

    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_bluray_titles(monkeypatch, tmp_path):
    stream = SimpleNamespace(kind="video", pid=4113, language="")
    title = SimpleNamespace(playlist_id=800, playlist_path=tmp_path / "00800.mpls", duration_s=5400.1234,
                            size_bytes=40_000_000_000, segments=[SimpleNamespace(clip_id="00001")], streams=[stream])
    monkeypatch.setattr("core.bluray.find_disc_root", lambda _path: tmp_path)
    monkeypatch.setattr("core.bluray.discover_titles", lambda _path, min_duration_s=0.0: [title])
    result = call(CATALOG, "media.bluray_titles", {"path": str(tmp_path)}, _ctx())
    assert not result_errors("media.bluray_titles", result)


def test_capabilities_sections(monkeypatch):
    from core import plugins
    from core.workflows.encode import hardware, interpolation, vulkan

    class _Detector:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        def detect_software(self, _ffmpeg: str) -> list[str]:
            return ["libx265", "libx264"]

        def detect(self, _ffmpeg: str, **_kwargs: Any) -> tuple[list[str], str]:
            return ["hevc_nvenc"], "ffmpeg"

        def detect_presets(self, _ffmpeg: str, codecs: Any) -> dict[str, list[str]]:
            return {codec: ["slow", "fast"] for codec in codecs}

    monkeypatch.setattr(hardware, "HardwareEncoderDetector", _Detector)
    monkeypatch.setattr(vulkan, "detect_vulkan", lambda *_a: vulkan.VulkanCapability(
        True, "RTX", 0, True, "", (vulkan.VulkanDevice(0, "RTX", "discrete"),)))
    monkeypatch.setattr(interpolation, "detect_gpu_acceleration", lambda *_a, **_k: interpolation.GpuAcceleration(
        interpolation.NvofCapability(True, "RTX"), interpolation.TrtCapability(True, False, "RTX", "absente")))
    monkeypatch.setattr(interpolation, "rife_capabilities", lambda *_a: interpolation.RifeCapabilities(
        (1, 9, 0), 1, frozenset({"--restore"}), None, "ok", frozenset({"r2"})))
    monkeypatch.setattr(plugins, "installed_plugin", lambda *_a, **_k: None)
    result = call(CATALOG, "system.capabilities", {},
                  _ctx(SimpleNamespace(tool_ffmpeg="ffmpeg", tool_muxiveo_rife="muxiveo-rife", tool_nvencc="")))
    assert result.rife["models"] is None and not result_errors("system.capabilities", result)


def test_plugin_statuses(monkeypatch):
    from core import plugins
    from core.workflows.encode import interpolation

    monkeypatch.setattr(plugins, "platform_tag", lambda *_a, **_k: "linux-x86_64")
    monkeypatch.setattr(plugins, "fetch_feed", lambda *_a, **_k: None)
    monkeypatch.setattr("core.service.ops.plugins.detect_gpu_acceleration", lambda *_a, **_k: interpolation.GpuAcceleration(
        interpolation.NvofCapability(), interpolation.TrtCapability(True, False, "RTX", "")))
    result = call(CATALOG, "plugins.list", {}, _ctx(SimpleNamespace(tool_muxiveo_rife="", trt_enabled=True)))
    assert {item["name"] for item in result.extensions} == set(plugins.EXTENSIONS)
    assert not result_errors("plugins.list", result)


def test_tmdb_search_and_details():
    from core.media_info_fetcher import MediaDetails, MediaSearchResult

    class _Fetcher:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def search(self, _query: str, **_kwargs: Any) -> list[MediaSearchResult]:
            return [MediaSearchResult(27205, "Inception", "2010", "movie", "…")]

        def get_details(self, *_args: Any, **_kwargs: Any) -> MediaDetails:
            return MediaDetails(title="Inception", year="2010", cover_bytes=b"jpeg")

    ctx = _ctx(ports={"tmdb_fetcher": _Fetcher})
    assert not result_errors("tmdb.search", call(CATALOG, "tmdb.search", {"query": "Inception"}, ctx))
    details = call(CATALOG, "tmdb.details", {"tmdb_id": 27205}, _ctx(ports={"tmdb_fetcher": _Fetcher}))
    assert "cover_bytes" not in details.details and not result_errors("tmdb.details", details)


def test_fel_verdict_codes_follow_the_analyzer():
    from core.hdr_inspection.fel_analyzer import VERDICTS
    from core.service.ops.hdr import FelVerdict

    assert typing.get_args(typing.get_type_hints(FelVerdict)["code"]) == VERDICTS


@pytest.mark.parametrize("operation, payload, lister", [
    ("encode_preset.save", {"preset": {"name": "Même", "crf": 18}}, ("core.workflows.encode.profiles.ProfileManager",
                                                                     "names")),
    ("profile.save", {"profile": {"name": "Même"}}, ("core.profiles.decision.DecisionProfileManager", "load")),
])
def test_saving_without_force_never_replaces_a_concurrent_save(tmp_path, monkeypatch, operation, payload, lister):
    """Deux enregistrements simultanés du même nom : le contrôle préalable passe pour les deux, l'écriture
    revérifie sous son verrou (le second est refusé, le premier conservé)."""
    from core.jobs.errors import JobError

    config = SimpleNamespace(app_data_dir=tmp_path / "data", profiles_dir=tmp_path / "profiles")
    call(CATALOG, operation, payload, _ctx(config))
    monkeypatch.setattr(f"{lister[0]}.{lister[1]}", lambda *_args, **_kwargs: [] if lister[1] == "names" else None)
    with pytest.raises(JobError) as caught:
        call(CATALOG, operation, payload, _ctx(config))
    assert "déjà enregistré" in str(caught.value)
