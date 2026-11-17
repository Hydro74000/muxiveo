"""Paramètres libres de l'encodeur dans une requête de l'API : liste blanche des options de l'encodeur."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.workflows.encode.domain import api_extra_params as policy

_ENCODER_HELP = """Encoder libx264 [libx264 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10]:
libx264 AVOptions:
  -preset            <string>     E..V....... Set the encoding preset (default "medium")
  -passlogfile       <string>     E..V....... Filename for 2 pass stats
  -stats             <string>     E..V....... Filename for 2 pass stats
  -rc-lookahead      <int>        E..V....... Number of frames to look ahead
  -x264-params       <dictionary> E..V....... Override the x264 configuration
"""
_FULL_HELP = """Global options (affect whole program instead of just one file):
-y                  overwrite output files
-stats              print progress report during encoding
-progress <url>     write program-readable progress information
Per-file options (input and output):
-f <fmt>            force container format (auto-detected otherwise)
-passlogfile[:<stream_spec>] <prefix>  select two pass log file name prefix
AVCodecContext AVOptions:
  -b                 <int64>      E..VA...... set bitrate (in bits/s)
  -flags             <flags>      ED.VAS..... (codec flags)
  -dump_separator    <string>     ED.VAS..... set information dump field separator
  -g                 <int>        E..V....... set the group of picture (GOP) size
  -ar                <int>        ED...A..... set audio sampling rate (in Hz)
  -flags2            <flags>      .D.V....... (décodage seul)
AVFormatContext AVOptions:
  -probesize         <int64>      .D......... set probing size
"""
_NVENCC_HELP = """Usage: NVEncC.exe [Options] -i <input file> -o <output file>
-h,-? --help                    print help
-i,--input <filename>           set input filename
-o,--output <filename>          set output filename
-f,--output-format <string>     set output format of output file.
-m,--mux-option <string1>:<string2>
   --avsw [<string>]            set input to use avcodec + sw decoder
   --audio-file [<int>?][<string>:]<string>
   --audio-filter [<int>?]<string>
   --lookahead <int>            enable lookahead
   --(no-)aq                    enable spatial adaptive quantization
   --audio-source <string>      input extra audio file.
   --vpp-pad <int>,<int>,<int>,<int>
   --vpp-tweak [<param1>=<value>][,<param2>=<value>]...
   --log <string>               set log file name
   --option-file <string>       read commanline options written in file.
   --avsdll <string>            specifies AviSynth DLL location to use.
"""


@pytest.fixture(autouse=True)
def _canned_help(monkeypatch):
    def fake(binary: str, *args: str) -> str:
        if binary == "nvencc":
            return _NVENCC_HELP
        if args[-1] == "full":
            return _FULL_HELP
        return _ENCODER_HELP if args[-1] == "encoder=libx264" else "Codec 'x' is not recognized by FFmpeg.\n"

    monkeypatch.setattr(policy, "_help_text", fake)
    monkeypatch.setattr(policy.shutil, "which", lambda _name: None)
    policy._codec_context_and_masked.cache_clear()
    yield
    policy._codec_context_and_masked.cache_clear()


def _errors(codec: str, params: str) -> list[str]:
    return policy.api_extra_params_errors(codec, params, ffmpeg="ffmpeg", nvencc="nvencc")


def test_ffmpeg_encoder_and_codec_options_pass_masked_ones_do_not():
    assert policy.ffmpeg_encoder_option_names("ffmpeg", "libx264") == frozenset(
        {"preset", "passlogfile", "rc-lookahead", "x264-params", "b", "g", "flags", "dump_separator",
         "q", "pix_fmt", "force_key_frames"})  # « stats » masquée, « ar » audio ; options standard d'encodage
    assert _errors("libx264", "-rc-lookahead 40 -x264-params aq-mode=3 -passlogfile /tmp/p -g 48 -b:v 5M") == []
    assert _errors("libx264", "-flags -cgop -q:v 24 -qscale:v 24 -pix_fmt yuv420p") == []  # valeur « -cgop »


@pytest.mark.parametrize("params", ["-i http://h/x", "-progress tcp://h:1", "-f matroska", "-stats x.mkv",
                                    "-/x264-params f.txt", "-filter_complex nullsink", "-y", "sortie2.mkv",
                                    "-g 48 sortie2.mkv extra", "-dump_separator -g /absent/extra.mkv", "-g"])
def test_anything_outside_the_encoder_is_refused(params):
    assert _errors("libx264", params)


def test_param_string_codecs_and_copy_are_left_to_the_syntax_check():
    assert _errors("libx265", "aq-mode=3:psy-rd=2") == []
    assert _errors("copy", "-i x") == []


def test_unknown_encoder_refuses_free_parameters():
    assert _errors("hevc_nvenc", "-rc-lookahead 32") == [
        "options de l'encodeur hevc_nvenc illisibles : paramètres libres refusés"]


@pytest.mark.parametrize("params", ["--vpp-pad 2,2,2,2", "--vpp-tweak brightness=0.1", "--audio-source a.mka",
                                    "--option-file opts.txt", "--avsdll x.dll", "-i x.mkv", "--log l.txt",
                                    "--input-option a:b", "--inconnue 1", "--audio-file 1?/tmp/x.mka",
                                    "--audio-filter volume=0", "-f matroska", "--output-format=matroska", "-m a:b",
                                    "--mux-option=a:b", "--avsw", "--lookahead 32 sortie.mkv"])
def test_nvencc_inputs_logs_sources_filters_and_loaders_are_refused(params):
    assert _errors("nvencc_hevc", params)


def test_nvencc_encoder_options_pass():
    assert _errors("nvencc_hevc", "--lookahead 32 --aq") == []
    assert _errors("nvencc_hevc", "--no-aq --lookahead=16") == []


def test_effective_settings_are_checked_whatever_their_origin():
    """Contrôle sur les réglages effectifs : un preset qui apporte ``-i URL`` est refusé comme une requête."""
    from core.jobs.encode import _refuse_foreign_extra_params
    from core.service.errors import ApiError, ErrorCode

    config = SimpleNamespace(video_tracks=[SimpleNamespace(codec="libx264", extra_params="-g 48"),
                                           SimpleNamespace(codec="libx264", extra_params="-i http://h/x")])
    with pytest.raises(ApiError) as caught:
        _refuse_foreign_extra_params(config, ffmpeg="ffmpeg", nvencc="nvencc")
    assert caught.value.code is ErrorCode.VALIDATION and len(caught.value.details) == 1


@pytest.mark.parametrize("field, value", [
    ("yadif_mode", "send_frame,drawtext=textfile=/etc/passwd"), ("yadif_parity", "auto:1"),
    ("yadif_deint", "all;metadata=mode=print:file=/tmp/x"), ("yadif_deint", "all[out]"), ("yadif_mode", "a=b")])
def test_filter_fields_cannot_inject_a_filter(field, value):
    import dataclasses

    from core.workflows.encode.models import VideoEncodeSettings, VideoFilterSettings
    from core.workflows.encode.planning.validation import filter_field_errors

    filters = dataclasses.replace(VideoFilterSettings(), yadif_enabled=True, **{field: value})
    problems = filter_field_errors(VideoEncodeSettings(codec="libx264", filters=filters))
    assert len(problems) == 1 and field in problems[0]


def test_default_and_gui_filter_values_stay_valid():
    import dataclasses

    from core.workflows.encode.models import VideoEncodeSettings, VideoFilterSettings
    from core.workflows.encode.planning.validation import filter_field_errors

    assert filter_field_errors(VideoEncodeSettings()) == []
    for mode in ("send_frame", "send_field"):
        for parity in ("auto", "tff", "bff"):
            filters = dataclasses.replace(VideoFilterSettings(), yadif_enabled=True, yadif_mode=mode,
                                          yadif_parity=parity)
            assert filter_field_errors(VideoEncodeSettings(codec="libx265", filters=filters)) == []


def test_tonemap_algorithm_is_bounded_by_its_engine():
    from core.workflows.encode.models import VideoEncodeSettings
    from core.workflows.encode.planning.validation import filter_field_errors

    assert filter_field_errors(VideoEncodeSettings(codec="libx265", tonemap_to_sdr=True, tonemap_algorithm="hable")) == []
    assert filter_field_errors(VideoEncodeSettings(codec="nvencc_hevc", tonemap_to_sdr=True,
                                                   tonemap_algorithm="bt2390")) == []  # libplacebo
    assert filter_field_errors(VideoEncodeSettings(codec="libx265", tonemap_to_sdr=True,
                                                   tonemap_algorithm="hable:desat=0,drawbox"))
    assert filter_field_errors(VideoEncodeSettings(codec="libx265", tonemap_to_sdr=True, tonemap_algorithm="bt2390"))


def test_the_workflow_validation_checks_the_effective_filter_fields(tmp_path):
    """Chemin réel : ``EncodeWorkflow.validate`` sur les réglages effectifs (bloc ``encode``, preset, interface)."""
    import dataclasses

    from core.workflows.encode import EncodeWorkflow
    from core.workflows.encode.models import EncodeConfig, VideoEncodeSettings, VideoFilterSettings

    source = tmp_path / "source.mkv"
    source.write_bytes(b"x")
    filters = dataclasses.replace(VideoFilterSettings(), yadif_enabled=True,
                                  yadif_deint="all,metadata=mode=print:file=/dev/null")
    config = EncodeConfig(source=source, output=tmp_path / "out.mkv",
                          video=VideoEncodeSettings(codec="libx264", filters=filters), audio_tracks=[],
                          copy_subtitles=False, duration_s=60.0)
    errors = EncodeWorkflow(ffmpeg_bin="ffmpeg").validate(config)
    assert any("yadif_deint" in error for error in errors)
