"""Contrat FEL, décision indépendante du RPU, transport et repli par piste."""
from types import SimpleNamespace
import threading
from unittest.mock import Mock, patch

import pytest

from core import plugins
from core.dovi_profile_detector import DoviSubProfile as P
from core.fel.context import FelExecution
from core.fel.engine import FelCancelled, FelEngine, FelError, FelProducer, FelSource
from core.fel.pipeline import with_fel_input
from core.fel.preparation import fallback_video, prepare_fel
from core.pipeline_command import PipelineCommand
from core.workflows.encode.dovi_policy import resolve_dovi_plan, wants_fel_bake
from core.workflows.encode.models import EncodePreset, VideoEncodeSettings


@pytest.mark.parametrize("profile", [P.P7_FEL, P.P7_MEL, P.P5, P.P8_1, P.UNKNOWN])
def test_default_disabled_for_every_profile(profile):
    assert not wants_fel_bake(None, profile)
    assert not wants_fel_bake(False, profile)
    assert wants_fel_bake(True, profile)
    assert not wants_fel_bake(True, profile, "copy")


@pytest.mark.parametrize("choice", [None, False, True])
def test_preset_roundtrip(choice):
    p = EncodePreset(bake_dovi_fel=choice)
    restored = EncodePreset(**p.to_json_dict())
    assert restored.to_video_settings().bake_dovi_fel is choice
    assert "fel_context" not in p.to_json_dict()
    assert EncodePreset().bake_dovi_fel is None


def test_policy_without_copy_rpu():
    plan = resolve_dovi_plan(codec="libx264", copy_dv=False, dovi_profile="0", sub_profile=P.P7_FEL, bake_dovi_fel=True)
    assert plan.bake_fel and not plan.output_profile
    assert "confirmer" in " ".join(plan.notes)


def context(tmp_path):
    source = FelSource(Mock(), tmp_path / "original.mkv", 3, 2, "24000/1001")
    return FelExecution(source, tmp_path / "rpu.bin", "1000,400", "source")


def test_pipe_preserves_source_filters_and_identity(tmp_path):
    video = VideoEncodeSettings(fel_context=context(tmp_path))
    command = ["ffmpeg", "-hwaccel", "cuda", "-i", "original.mkv", "-map", "0:3", "-vf", "crop=64:64", "out.mkv"]
    result = with_fel_input(command, video)
    assert isinstance(result, PipelineCommand)
    assert result.producer.path == tmp_path / "original.mkv"
    assert result[result.index("-i")+1] == "pipe:0"
    assert result[result.index("-map")+1] == "0:0"
    assert "-hwaccel" not in result
    vf = result[result.index("-vf")+1]
    assert vf.index("matrix=bt2020nc") < vf.index("crop=64:64") < vf.rindex("dither=error_diffusion")
    assert command[2] == "cuda"


def test_pipe_precedes_rife(tmp_path):
    video = VideoEncodeSettings(fel_context=context(tmp_path))
    command = PipelineCommand(["ffmpeg", "-i", "pipe:0", "out.mkv"],
                              [["ffmpeg", "-i", "original.mkv", "-f", "yuv4mpegpipe", "-"], ["rife"]])
    result = with_fel_input(command, video, 5)
    assert result.producer.threads == 5
    assert result.upstream[0][result.upstream[0].index("-i")+1] == "pipe:0"
    assert result.upstream[1] == ["rife"]
    assert list(result) == list(command)


def test_filter_is_inserted_before_an_existing_pipe_output(tmp_path):
    video = VideoEncodeSettings(fel_context=context(tmp_path))
    command = ["ffmpeg", "-i", "original.mkv", "-f", "yuv4mpegpipe", "-"]
    result = with_fel_input(command, video)
    assert result.index("-vf") < result.index("yuv4mpegpipe")
    assert result[-1] == "-"


def test_nvencc_fel_pipe_keeps_timestamps_without_a_compressed_intermediate(tmp_path):
    from core.workflows.encode.runtime.nvencc import build_decode_pipe_cmd, build_nvencc_command
    video = VideoEncodeSettings(codec="nvencc_hevc", fel_context=context(tmp_path), preset="default")
    decode = with_fel_input(build_decode_pipe_cmd("ffmpeg", "original.mkv", nut=True), video)
    assert decode[decode.index("-c:v")+1] == "rawvideo"
    assert decode[decode.index("-fps_mode")+1] == "passthrough"
    assert decode[-3:] == ["-f", "nut", "-"]
    encode = build_nvencc_command("nvencc", video, tmp_path / "encoded.mkv")
    assert encode[encode.index("--input-format")+1] == "nut"
    assert encode[encode.index("--avsync")+1] == "vfr"


def test_fallback_restores_original_metadata(tmp_path):
    video = VideoEncodeSettings(fel_context=context(tmp_path), bake_dovi_fel=True,
                                max_cll="2000,600", static_hdr_light_level_source="fel_rpu_l1_estimate")
    fallback = fallback_video(video)
    assert fallback.max_cll == "1000,400"
    assert fallback.static_hdr_light_level_source == "source"
    assert fallback.fel_context is None and fallback.bake_dovi_fel is False
    assert video.fel_context is not None


def test_absent_engine_warns_without_extracting(tmp_path, monkeypatch):
    monkeypatch.setattr(FelEngine, "installed", Mock(side_effect=FelError("absente")))
    run, log = Mock(), Mock()
    video = VideoEncodeSettings(dovi_source_profile="p7_fel", bake_dovi_fel=True)
    result = prepare_fel(video, source=tmp_path / "src.mkv", work_dir=tmp_path, index=2,
                         ffmpeg="ffmpeg", dovi="dovi_tool", threads=2, run=run, capture=Mock(),
                         cancelled=lambda: False, log=log)
    assert result.fel_context is None and result.bake_dovi_fel is False
    run.assert_not_called()
    assert log.call_args.args[0] == "WARN"
    assert "#2" in log.call_args.args[1] and "repli BL" in log.call_args.args[1]


def test_classification_streaming_and_cancel(tmp_path):
    engine = FelEngine.__new__(FelEngine)
    classify = Mock(side_effect=[1, 3, 1])
    engine.lib = SimpleNamespace(mvo_fel_classify=classify)
    path = tmp_path / "rpu.bin"
    path.write_bytes((b"\0\0\0\1\x19example")*3)
    assert engine.classify(path, lambda: False) == "fel"
    with pytest.raises(FelCancelled):
        engine.classify(path, lambda: True)
    engine.lib.mvo_fel_classify = Mock(return_value=-1)
    with pytest.raises(FelError, match="invalide"):
        engine.classify(path, lambda: False)


def test_abi_manifest_rejects_incompatible():
    assert plugins.FEL.check({"abi": 1}) is None
    assert plugins.FEL.check({"abi": 2})


def test_unpublished_plugin_does_not_offer_a_development_release():
    assert plugins.target_version(plugins.FEL, None, "linux-x86_64") == ""
    feed = [{"version": "0.1.0", "abi": 1, "platforms": [{"platform": "linux-x86_64"}]}]
    assert plugins.target_version(plugins.FEL, feed, "linux-x86_64") == "0.1.0"
    assert plugins.target_version(plugins.FEL, feed, "macos-arm64") == ""


def test_loaded_version_survives_removal_until_released(tmp_path):
    base = tmp_path / plugins.FEL_PLUGIN_ID
    version = base / "0.1.0-test"
    version.mkdir(parents=True)
    (base / "current.json").write_text("{}")
    library = version / "library"
    library.write_bytes(b"loaded")
    lease = plugins.PluginLease(version)
    try:
        assert not plugins.remove(plugins.FEL, tmp_path)
        assert library.read_bytes() == b"loaded"
        assert not (base / "current.json").exists()
    finally:
        lease.release()
    assert plugins.remove(plugins.FEL, tmp_path)
    assert not version.exists()


@pytest.mark.parametrize("error", [OSError("disque plein"), FelError("EL tronquée"), FelCancelled()])
def test_producer_propagates_errors_except_closed_consumer(error):
    producer = FelProducer.__new__(FelProducer)
    producer.error = error
    with pytest.raises(type(error)):
        producer.check_error()
    producer.error = BrokenPipeError()
    producer.check_error()


@pytest.mark.parametrize("failed_thread", ["mvo-fel", "mvo-fel-cancel"])
def test_producer_thread_start_failure_releases_native_resources(monkeypatch, tmp_path, failed_thread):
    import threading
    from types import SimpleNamespace
    from core.fel.engine import FelSource

    lib = Mock()
    lib.mvo_fel_create.return_value = 1
    lib.mvo_fel_set_device.return_value = 0
    release = Mock()
    plan = Mock(acquire=Mock(return_value=("cpu", release)))
    source = FelSource(SimpleNamespace(lib=lib, capabilities={"device_selection": 1}), tmp_path / "source.mkv", 0,
                       device_plan=plan)
    start = threading.Thread.start

    def fail_start(thread):
        if thread.name == failed_thread:
            raise RuntimeError("thread indisponible")
        start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    output = Mock()
    with pytest.raises(RuntimeError, match="thread indisponible"):
        source.start(output, threading.Event())
    release.assert_called_once()
    lib.mvo_fel_destroy.assert_called_once_with(1)
    lib.mvo_fel_run.assert_not_called()
    output.close.assert_called_once()


@pytest.mark.parametrize("status,error_type", [(1, FelError), (4, OSError)])
def test_native_storage_errors_are_not_reconstruction_errors(status, error_type):
    lib = SimpleNamespace(mvo_fel_run=Mock(return_value=status), mvo_fel_error=Mock(return_value=b"source error"))
    producer = FelProducer.__new__(FelProducer)
    producer.source = SimpleNamespace(engine=SimpleNamespace(lib=lib), device_plan=None)
    producer._release_device = lambda: None
    producer.cancelled = threading.Event()
    producer._stop = threading.Event()
    producer._handle = 1
    producer.output = Mock()
    producer.error = None
    producer._run()
    with pytest.raises(error_type, match="source error"):
        producer.check_error()


def test_closed_logger_does_not_leak_gpu_reservation_or_pipe():
    lib = SimpleNamespace(mvo_fel_run=Mock(return_value=0), mvo_fel_backend=Mock(return_value=b"vulkan"))
    producer = FelProducer.__new__(FelProducer)
    producer.source = SimpleNamespace(engine=SimpleNamespace(lib=lib, capabilities={"device_selection": 1}),
                                     device_plan=SimpleNamespace(log=Mock(side_effect=RuntimeError("Qt fermé"))))
    producer._release_device = Mock()
    producer.cancelled = threading.Event()
    producer._stop = threading.Event()
    producer._handle = 1
    producer.frames = 12
    producer.output = Mock()
    producer.error = None
    producer._run()
    producer.check_error()
    producer._release_device.assert_called_once()
    producer.output.close.assert_called_once()


@pytest.mark.parametrize("kind", ["mel", "sans_el"])
def test_confirmation_without_fel_is_noop(tmp_path, monkeypatch, kind):
    import core.fel.preparation as preparation
    extract = Mock()
    monkeypatch.setattr(preparation, "extract_dovi_rpu", extract)
    engine = Mock()
    engine.classify.return_value = kind
    video = VideoEncodeSettings(bake_dovi_fel=True)
    log = Mock()
    result = prepare_fel(video, source=tmp_path / "original.mkv", work_dir=tmp_path, index=1,
                         ffmpeg="ffmpeg", dovi="dovi_tool", threads=2, run=Mock(), capture=Mock(),
                         cancelled=lambda: False, log=log, engine_loader=lambda: engine)
    assert result.fel_context is None and result.bake_dovi_fel is False
    assert video.bake_dovi_fel is True
    assert extract.call_args.kwargs["mode"] == "0"
    assert "sans effet" in log.call_args.args[1]


@pytest.mark.parametrize("provenance,expected", [("manual", "1000,400"), ("source", "2000,500")])
def test_original_l1_estimate_respects_manual_light_levels(tmp_path, monkeypatch, provenance, expected):
    import core.fel.preparation as preparation
    monkeypatch.setattr(preparation, "extract_dovi_rpu", Mock())
    estimate = Mock(return_value=SimpleNamespace(max_cll="2000,500"))
    monkeypatch.setattr(preparation, "estimate_static_hdr_from_rpu", estimate)
    engine = Mock()
    engine.classify.return_value = "fel"
    video = VideoEncodeSettings(bake_dovi_fel=True, inject_hdr_meta=True,
                                max_cll="1000,400", static_hdr_light_level_source=provenance)
    result = prepare_fel(video, source=tmp_path / "original.mkv", work_dir=tmp_path, index=1,
                         ffmpeg="ffmpeg", dovi="dovi_tool", threads=2, run=Mock(), capture=Mock(),
                         cancelled=lambda: False, log=Mock(), engine_loader=lambda: engine)
    assert result.max_cll == expected
    assert result.fel_context.original_rpu == tmp_path / "fel_original_1.bin"
    if provenance == "source":
        assert estimate.call_args.kwargs["prefer_l1"]
    else:
        estimate.assert_not_called()


def test_whole_track_retry_not_other_errors(tmp_path):
    from core.runner import TaskSignals
    from core.workflows.encode.models import EncodeConfig
    from core.workflows.encode.runtime.multi_video import MultiVideoPipelineRunner, PreparedVideoInput
    from core.workflows.encode.runtime_helpers import VideoTrackPrepSpec
    video = VideoEncodeSettings(fel_context=context(tmp_path))
    spec = VideoTrackPrepSpec(0, video, tmp_path / "in.mkv", 0, 0)
    cb = SimpleNamespace(check_cancelled=Mock(), log_info=Mock())
    runner = MultiVideoPipelineRunner(cb)
    prepared = PreparedVideoInput([], tmp_path / "out.mkv", "0:v:0")
    runner._prepare_multi_video_track = Mock(side_effect=[FelError("EL tronquée"), (prepared, [])])
    config = EncodeConfig(source=spec.source, output=tmp_path / "final.mkv", video=video)
    runner.prepare_multi_video_track(config=config, spec=spec, work_dir=tmp_path,
                                     total_tracks=1, thread_count=2, signals=TaskSignals(), run_cmd=Mock())
    calls = runner._prepare_multi_video_track.call_args_list
    assert len(calls) == 2
    assert calls[1].kwargs["spec"].video.fel_context is None
    assert calls[0].kwargs["work_dir"] != calls[1].kwargs["work_dir"]
    runner._prepare_multi_video_track = Mock(side_effect=OSError("Disque plein"))
    with pytest.raises(OSError):
        runner.prepare_multi_video_track(config=config, spec=spec, work_dir=tmp_path,
                                         total_tracks=1, thread_count=2, signals=TaskSignals(), run_cmd=Mock())
    assert runner._prepare_multi_video_track.call_count == 1


def seek_context(tmp_path, capabilities, chroma="left"):
    engine = SimpleNamespace(capabilities=capabilities)
    source = FelSource(engine, tmp_path / "original.mkv", 3, 2, "24000/1001")
    return FelExecution(source, tmp_path / "rpu.bin", "", "", chroma)


def test_input_seek_is_performed_by_the_plugin(tmp_path):
    """Aperçu à 20 min : aucune image reconstruite avant ce point."""
    video = VideoEncodeSettings(fel_context=seek_context(tmp_path, {"seek": 1}))
    command = ["ffmpeg", "-hide_banner", "-y", "-ss", "1234.5", "-i", "original.mkv", "-map", "0:3",
               "-frames:v", "1", "out.png"]
    result = with_fel_input(command, video)
    assert "-ss" not in result
    assert result.producer.start_us == 1_234_500_000
    assert "1234.5" in command


def test_input_seek_stays_in_ffmpeg_without_plugin_support(tmp_path):
    video = VideoEncodeSettings(fel_context=seek_context(tmp_path, {}))
    result = with_fel_input(["ffmpeg", "-ss", "12", "-i", "original.mkv", "out.png"], video)
    assert result[result.index("-ss")+1] == "12"
    assert result.producer.start_us == 0


@pytest.mark.parametrize("value,expected", [("12.5", 12_500_000), ("01:02:03.25", 3_723_250_000),
                                            ("02:03", 123_000_000), (".5", 500_000), ("abc", None), ("-3", None)])
def test_seek_values(value, expected):
    from core.fel.pipeline import seek_microseconds
    assert seek_microseconds(value) == expected


def test_conversion_uses_bl_chroma_location(tmp_path):
    from core.fel.direct import direct_filter
    video = VideoEncodeSettings(fel_context=seek_context(tmp_path, {}, "topleft"))
    result = with_fel_input(["ffmpeg", "-i", "original.mkv", "out.mkv"], video)
    assert "chromal=topleft" in result[result.index("-vf")+1]
    assert "chroma_location=topleft" in (direct_filter("", "topleft") or "")


def test_tonemap_is_reduced_once_at_bl_chroma_location(tmp_path):
    """SDR : le tone mapping sous-échantillonne déjà à la position du BL ; zscale suppose « left »
    sur des images non marquées et rééchantillonnerait la chroma une seconde fois."""
    from core.workflows.encode.domain.codecs import build_vf
    video = VideoEncodeSettings(fel_context=seek_context(tmp_path, {}, "topleft"), tonemap_to_sdr=True,
                                bit_depth="8", source_color_transfer="smpte2084")
    result = with_fel_input(["ffmpeg", "-i", "original.mkv", "-vf", build_vf(video), "out.mkv"], video)
    vf = result[result.index("-vf")+1]
    assert vf.count("chromal=") == 1 and "chromal=topleft,format=yuv420p,setparams=chroma_location=topleft" in vf


@pytest.mark.parametrize("location,copy_dv,sdr,expected", [
    ("topleft", False, False, "2"), ("left", True, False, "0"), ("bottom", True, False, "5"),
    ("topleft", False, True, "2"), ("left", False, True, "0"),
])
def test_nvencc_signals_fel_chroma_location(tmp_path, location, copy_dv, sdr, expected):
    """NVEncC lit les images FEL par pipe : la VUI suit la position du BL, et non la règle de la copie DV."""
    from core.workflows.encode.runtime.nvencc import build_nvencc_command
    video = VideoEncodeSettings(codec="nvencc_hevc", fel_context=seek_context(tmp_path, {}, location),
                                copy_dv=copy_dv, tonemap_to_sdr=sdr, bit_depth="8" if sdr else "10",
                                source_bit_depth=10, source_color_transfer="smpte2084")
    cmd = build_nvencc_command("nvencc", video, "out.hevc")
    assert cmd.count("--chromaloc") == 1 and cmd[cmd.index("--chromaloc")+1] == expected


def test_preparation_reads_bl_chroma_location(tmp_path, monkeypatch):
    import core.fel.preparation as preparation
    monkeypatch.setattr(preparation, "extract_dovi_rpu", Mock())
    engine = Mock()
    engine.classify.return_value = "fel"
    engine.devices.return_value = ()
    capture = Mock(return_value='{"programs": [], "streams": [{"chroma_location": "topleft", "side_data_list": [{}]}]}')
    video = VideoEncodeSettings(bake_dovi_fel=True, stream_index=4)
    result = prepare_fel(video, source=tmp_path / "original.mkv", work_dir=tmp_path, index=1,
                         ffmpeg="ffmpeg", ffprobe="ffprobe-x", dovi="dovi_tool", threads=2, run=Mock(),
                         capture=capture, cancelled=lambda: False, log=Mock(), engine_loader=lambda: engine)
    assert result.fel_context.chroma_location == "topleft"
    probe = capture.call_args.args[0]
    assert probe[0] == "ffprobe-x" and probe[probe.index("-select_streams")+1] == "4"


def test_producer_applies_start_or_releases_context(tmp_path):
    lib = Mock()
    lib.mvo_fel_create.return_value = 7
    lib.mvo_fel_set_start.return_value = -1
    source = FelSource(SimpleNamespace(lib=lib, capabilities={"seek": 1}), tmp_path / "s.mkv", 0, start_us=5)
    with pytest.raises(FelError, match="Départ"):
        source.start(Mock(), threading.Event())
    lib.mvo_fel_set_start.assert_called_once_with(7, 5)
    lib.mvo_fel_destroy.assert_called_once_with(7)


@pytest.mark.parametrize("descriptor", [9, None])
def test_producer_writes_natively_to_the_pipe_when_supported(descriptor):
    """fd_output : le plugin écrit sur le pipe ; sans descripteur, callback historique."""
    import io
    lib = SimpleNamespace(mvo_fel_run=Mock(return_value=0), mvo_fel_run_fd=Mock(return_value=0))
    producer = FelProducer.__new__(FelProducer)
    producer.source = SimpleNamespace(engine=SimpleNamespace(lib=lib, capabilities={"fd_output": 1}), device_plan=None)
    producer._release_device = lambda: None
    producer.cancelled = threading.Event()
    producer._stop = threading.Event()
    producer._handle = 1
    producer.output = Mock()
    if descriptor is None:
        producer.output.fileno.side_effect = io.UnsupportedOperation()
    else:
        producer.output.fileno.return_value = descriptor
    producer.error = None
    producer._run()
    assert producer.error is None
    if descriptor is None:
        lib.mvo_fel_run.assert_called_once()
        lib.mvo_fel_run_fd.assert_not_called()
    else:
        assert lib.mvo_fel_run_fd.call_args.args[:2] == (1, descriptor)
        lib.mvo_fel_run.assert_not_called()
    producer.output.close.assert_called_once()


FINAL_CAPABILITIES = {"seek": 1, "output_formats": ["gbrp16le", "yuv420p16le", "yuv420p10le", "yuv420p"]}


@pytest.mark.parametrize("chain,expected", [
    ("", "yuv420p10le"),
    ("crop=iw-0-0:ih-280-280:0:280,sidedata=mode=delete:type=CONTENT_LIGHT_LEVEL", "yuv420p10le"),
    ("setparams=color_trc=smpte2084,format=p010,hwupload,scale_cuda=1920:1080", "yuv420p10le"),
    ("crop=iw-1-0:ih-0-0:1:0", None),
    ("scale=1920:1080", None),
    ("fps=24000/1001", None),
])
def test_final_format_only_when_pixels_are_untouched_before_quantization(chain, expected):
    from core.fel.pipeline import final_output_format
    assert final_output_format(chain, 10) == expected


def test_final_format_is_produced_by_the_plugin(tmp_path):
    video = VideoEncodeSettings(fel_context=seek_context(tmp_path, FINAL_CAPABILITIES, "topleft"), bit_depth="10")
    result = with_fel_input(["ffmpeg", "-i", "original.mkv", "-vf", "crop=iw-0-0:ih-8-8:0:8", "out.mkv"], video)
    vf = result[result.index("-vf")+1]
    assert "zscale" not in vf and vf.startswith("setparams=range=tv:") and vf.endswith("crop=iw-0-0:ih-8-8:0:8")
    assert (result.producer.pixel_format, result.producer.chroma_location, result.producer.dither) == (
        "yuv420p10le", "topleft", "blue")


def test_pixel_filters_keep_rgb_reconstruction(tmp_path):
    video = VideoEncodeSettings(fel_context=seek_context(tmp_path, FINAL_CAPABILITIES), bit_depth="10")
    result = with_fel_input(["ffmpeg", "-i", "original.mkv", "-vf", "scale=1920:1080", "out.mkv"], video)
    assert result.producer.pixel_format == "gbrp16le"
    assert "zscale=matrix=bt2020nc" in result[result.index("-vf")+1]


def test_producer_requests_final_output_or_releases_context(tmp_path):
    lib = Mock()
    lib.mvo_fel_create.return_value = 7
    lib.mvo_fel_set_output.return_value = -1
    source = FelSource(SimpleNamespace(lib=lib, capabilities=FINAL_CAPABILITIES), tmp_path / "s.mkv", 0,
                       pixel_format="yuv420p10le", chroma_location="topleft", dither="blue")
    with pytest.raises(FelError, match="Format de sortie"):
        source.start(Mock(), threading.Event())
    lib.mvo_fel_set_output.assert_called_once_with(7, b"yuv420p10le", b"topleft", b"blue")
    lib.mvo_fel_destroy.assert_called_once_with(7)


FEED_CAPABILITIES = {**FINAL_CAPABILITIES, "timestamps": ["source", "zero"]}


def test_nvencc_reads_the_plugin_without_a_copy_stage(tmp_path):
    from core.fel.pipeline import direct_feed
    from core.workflows.encode.runtime.nvencc import build_decode_pipe_cmd
    video = VideoEncodeSettings(codec="nvencc_hevc", bit_depth="10",
                                fel_context=seek_context(tmp_path, FEED_CAPABILITIES, "topleft"))
    decode = with_fel_input(build_decode_pipe_cmd("ffmpeg", "original.mkv", nut=True), video)
    feed = direct_feed(decode)
    assert feed is not None and feed.timestamps == "zero" and feed.pixel_format == "yuv420p10le"
    assert decode.producer.timestamps == "source"


@pytest.mark.parametrize("vf,capabilities", [("crop=iw-0-0:ih-8-8:0:8", FEED_CAPABILITIES),
                                             ("", FINAL_CAPABILITIES), ("scale=1920:1080", FEED_CAPABILITIES)])
def test_copy_stage_is_kept_when_it_does_something(tmp_path, vf, capabilities):
    from core.fel.pipeline import direct_feed
    from core.workflows.encode.runtime.nvencc import build_decode_pipe_cmd
    video = VideoEncodeSettings(codec="nvencc_hevc", bit_depth="10", fel_context=seek_context(tmp_path, capabilities))
    decode = with_fel_input(build_decode_pipe_cmd("ffmpeg", "original.mkv", nut=True, vf=vf or None), video)
    assert direct_feed(decode) is None


def test_even_crop_moves_into_the_plugin_and_nvencc_reads_it_directly(tmp_path):
    from core.fel.pipeline import direct_feed
    from core.workflows.encode.runtime.nvencc import build_decode_pipe_cmd
    capabilities = {**FEED_CAPABILITIES, "crop": 1}
    video = VideoEncodeSettings(codec="nvencc_hevc", bit_depth="10", fel_context=seek_context(tmp_path, capabilities))
    decode = with_fel_input(build_decode_pipe_cmd("ffmpeg", "original.mkv", nut=True,
                                                  vf="crop=iw-0-0:ih-8-8:0:8"), video)
    assert "crop=" not in decode[decode.index("-vf")+1]
    assert decode.producer.crop == (0, 8, 0, 8)
    feed = direct_feed(decode)
    assert feed is not None and feed.crop == (0, 8, 0, 8)


PRECISION_CAPABILITIES = {**FINAL_CAPABILITIES, "precision": ["exact", "standard", "fast"],
                          "device_features": ["float64"]}


@pytest.mark.parametrize("capabilities,expected", [(PRECISION_CAPABILITIES, b"standard"), (FINAL_CAPABILITIES, None)])
def test_producer_sends_precision_when_supported(tmp_path, capabilities, expected):
    lib = Mock()
    lib.mvo_fel_create.return_value = 7
    lib.mvo_fel_set_precision.return_value = -1
    source = FelSource(SimpleNamespace(lib=lib, capabilities=capabilities), tmp_path / "s.mkv", 0, precision="standard")
    if expected is None:
        # Plugin antérieur : rien à transmettre (le plan a retenu le CPU, toujours exact).
        assert "précision" not in source.describe()
        return
    assert "précision Standard" in source.describe()
    with pytest.raises(FelError, match="Précision"):
        source.start(Mock(), threading.Event())
    lib.mvo_fel_set_precision.assert_called_once_with(7, expected)
    lib.mvo_fel_destroy.assert_called_once_with(7)


@pytest.mark.parametrize("capabilities,workers,expected", [
    ({**FINAL_CAPABILITIES, "render_workers": [1, 4]}, 1, 1),
    ({**FINAL_CAPABILITIES, "render_workers": [2, 4]}, 1, 2),
    ({**FINAL_CAPABILITIES, "render_workers": [1, 4]}, None, None),
    (FINAL_CAPABILITIES, 1, None),
])
def test_producer_sends_render_workers_when_supported(tmp_path, capabilities, workers, expected):
    lib = Mock()
    lib.mvo_fel_create.return_value = 7
    lib.mvo_fel_set_render_workers.return_value = -1
    source = FelSource(SimpleNamespace(lib=lib, capabilities=capabilities), tmp_path / "s.mkv", 0,
                       render_workers=workers)
    if expected is None:
        # Plugin antérieur ou défaut du plugin : rien à transmettre.
        with patch("core.fel.engine.threading.Thread"):
            source.start(Mock(), threading.Event())
        lib.mvo_fel_set_render_workers.assert_not_called()
        return
    with pytest.raises(FelError, match="images en vol"):
        source.start(Mock(), threading.Event())
    lib.mvo_fel_set_render_workers.assert_called_once_with(7, expected)
    lib.mvo_fel_destroy.assert_called_once_with(7)


@pytest.mark.parametrize("interpolation,expected", [(True, 1), (False, None)])
def test_preparation_uses_one_frame_in_flight_with_interpolation(tmp_path, monkeypatch, interpolation, expected):
    import core.fel.preparation as preparation
    from core.workflows.encode.models import FrameInterpolationSettings
    monkeypatch.setattr(preparation, "extract_dovi_rpu", Mock())
    engine = Mock()
    engine.classify.return_value = "fel"
    engine.devices.return_value = ()
    video = VideoEncodeSettings(bake_dovi_fel=True, interpolation=FrameInterpolationSettings(enabled=interpolation))
    result = prepare_fel(video, source=tmp_path / "original.mkv", work_dir=tmp_path, index=1,
                         ffmpeg="ffmpeg", dovi="dovi_tool", threads=2, run=Mock(), capture=Mock(return_value="{}"),
                         cancelled=lambda: False, log=Mock(), engine_loader=lambda: engine)
    assert result.fel_context.source.render_workers == expected


def test_device_list_uses_features_and_ignores_unknown_keys():
    import ctypes
    import json
    payload = json.dumps([{"uuid": "a"*32, "name": "RTX", "vendor": 4318, "index": 0, "kind": "discrete",
                           "memory_bytes": 1 << 34, "float64": True, "futur": 1}]).encode() + b"\0"

    def listing(buffer, _capacity):
        if buffer:
            ctypes.memmove(buffer, payload, len(payload))
        return len(payload)

    engine = object.__new__(FelEngine)
    engine.capabilities = {"device_selection": 1, "device_features": ["float64"]}
    engine.lib = SimpleNamespace(mvo_fel_devices_features=listing, mvo_fel_devices=Mock(side_effect=AssertionError))
    (device,) = engine.devices()
    assert device.float64 and device.name == "RTX"
