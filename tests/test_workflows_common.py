from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from core.workflows.common.attachments import (
    attachment_filename_from_meta,
    extension_for_mime,
    mime_for_path,
    sanitize_filename,
)
from core.workflows.common.ffmpeg_runtime import (
    cli_path,
    default_ffmpeg_thread_count,
    ffmpeg_progress_args,
    normalize_ffmpeg_thread_count,
    normalize_max_parallel_video_encodes,
)
from core.workflows.common.metadata import (
    disposition_value,
    normalize_track_language,
    normalize_track_language_from_track,
    resolve_global_tags,
)
from core.workflows.common.sync_rewrite import (
    SyncRewriteService,
    audio_bitrate_kbps_from_display_info,
    ui_sync_rewrite_label_for_track,
)
from core.workflows.common.timeline_sync import (
    append_strict_interleave_mux_flags,
    append_sync_inputs,
    needs_strict_interleave,
    sync_cleanup_paths,
)
from core.workflows.common.track_types import (
    TrackMetaEdit,
    TrackMetaPatch,
    TrackOffset,
    TrackTimeOffset,
    TrackType,
)


class TestFfmpegRuntime:
    def test_cli_path_preserves_named_pipe_strings(self):
        named_pipe = r"\\.\pipe\mre_sync"
        assert cli_path(named_pipe) == named_pipe

    def test_cli_path_normalizes_path_objects(self, tmp_path):
        assert cli_path(tmp_path / "file.mkv").endswith("/file.mkv")

    def test_default_ffmpeg_thread_count_uses_75_percent_rounded_up(self, monkeypatch):
        monkeypatch.setattr("core.workflows.common.ffmpeg_runtime.os.cpu_count", lambda: 8)
        assert default_ffmpeg_thread_count() == 6

    def test_normalize_ffmpeg_thread_count_preserves_zero(self):
        assert normalize_ffmpeg_thread_count(0) == 0

    def test_normalize_max_parallel_video_encodes_defaults_to_one(self):
        assert normalize_max_parallel_video_encodes(None) == 1
        assert normalize_max_parallel_video_encodes(0) == 1

    def test_ffmpeg_progress_args_are_stable(self):
        assert ffmpeg_progress_args() == ["-progress", "pipe:1", "-nostats"]


class TestCommonAttachments:
    def test_mime_for_path_and_extension_for_mime(self):
        assert mime_for_path(Path("cover.jpg")) == "image/jpeg"
        assert extension_for_mime("font/woff2") == ".woff2"

    def test_sanitize_filename_strips_directories(self):
        assert sanitize_filename("../cover.jpg", "fallback.bin") == "cover.jpg"

    def test_attachment_filename_from_meta_appends_suffix_when_missing(self):
        meta: dict[str, object] = {"filename": "cover", "mimetype": "image/jpeg"}
        assert attachment_filename_from_meta(meta, 3) == "cover.jpg"


class TestCommonMetadata:
    def test_resolve_global_tags_filters_blank_values_and_keeps_title(self):
        tags = resolve_global_tags({"GENRE": "Drama", "EMPTY": "   "}, file_title="Film")
        assert tags == {"GENRE": "Drama", "title": "Film"}

    def test_normalize_track_language_blank_defaults_to_und_when_requested(self):
        assert normalize_track_language("", default_und=True) == "und"

    def test_normalize_track_language_from_track_uses_title_context(self):
        track = SimpleNamespace(language="fr", title="VF")
        assert normalize_track_language_from_track(track) == "fr-FR"

    def test_disposition_value_supports_partial_flags(self):
        value = disposition_value(
            flag_default=True,
            flag_forced=False,
            flag_hearing_impaired=None,
            flag_visual_impaired=None,
            flag_original=None,
            flag_commentary=None,
            allow_partial=True,
        )
        assert value == "default"


class TestCommonTimelineSync:
    def test_needs_strict_interleave_detects_foreign_audio_plus_subtitle(self):
        mapped_tracks = [
            SimpleNamespace(source_file_index=0, track=SimpleNamespace(track_type="video")),
            SimpleNamespace(source_file_index=1, track=SimpleNamespace(track_type="audio")),
            SimpleNamespace(source_file_index=0, track=SimpleNamespace(track_type="subtitle")),
        ]
        assert needs_strict_interleave(cast(Any, mapped_tracks)) is True

    def test_append_strict_interleave_mux_flags(self):
        cmd: list[str] = []
        append_strict_interleave_mux_flags(cmd)
        assert cmd == ["-max_interleave_delta", "0", "-max_muxing_queue_size", "9999"]

    def test_append_sync_inputs_uses_formats_and_sync_cleanup_paths_returns_paths(self, tmp_path):
        sync_path = tmp_path / "sync.mka"
        sync_path.touch()
        cmd: list[str] = []
        append_sync_inputs(cmd, [sync_path, "pipe:sync"], input_formats=["matroska", "nut"])
        assert cmd == ["-f", "matroska", "-i", str(sync_path), "-f", "nut", "-i", "pipe:sync"]
        assert sync_cleanup_paths([sync_path, "pipe:sync"]) == [sync_path]


class TestCommonSyncRewrite:
    def test_shift_srt_positive_and_negative_clamps_or_drops(self):
        text = (
            "1\n00:00:00,200 --> 00:00:00,800\nA\n\n"
            "2\n00:00:01,000 --> 00:00:02,000\nB\n"
        )
        shifted = SyncRewriteService._shift_srt(text, -500)
        assert "00:00:00,000 --> 00:00:00,300" in shifted
        assert "00:00:00,500 --> 00:00:01,500" in shifted

        dropped = SyncRewriteService._shift_srt(text, -900)
        assert "A" not in dropped
        assert "00:00:00,100 --> 00:00:01,100" in dropped

    def test_shift_webvtt_drops_complete_cue_before_zero(self):
        text = (
            "WEBVTT\n\n"
            "00:00:00.100 --> 00:00:00.400\nOld\n\n"
            "00:00:01.000 --> 00:00:02.000\nKeep\n"
        )
        shifted = SyncRewriteService._shift_webvtt(text, -500)
        assert "Old" not in shifted
        assert "00:00:00.500 --> 00:00:01.500" in shifted

    def test_shift_ass_respects_format_columns(self):
        text = (
            "[Events]\n"
            "Format: Layer, Start, End, Style, Text\n"
            "Dialogue: 0,0:00:01.00,0:00:02.00,Default,Hello\n"
        )
        shifted = SyncRewriteService._shift_ass(text, 250)
        assert "Dialogue: 0,0:00:01.25,0:00:02.25,Default,Hello" in shifted

    def test_ui_label_marks_rewrite_and_object_audio_fallback(self):
        sub = SimpleNamespace(track_type="subtitle", codec="subrip", time_shift_ms=120)
        audio = SimpleNamespace(track_type="audio", codec="eac3", time_shift_ms=120, display_info="5.1  640 kbps")
        atmos = SimpleNamespace(track_type="audio", codec="eac3", time_shift_ms=120, display_info="5.1  768 kbps  Atmos")

        assert ui_sync_rewrite_label_for_track(sub, enabled=False) == ""
        assert ui_sync_rewrite_label_for_track(sub, enabled=True) == "Sync réelle"
        assert ui_sync_rewrite_label_for_track(audio, enabled=True) == "Sync réelle · audio réencodé"
        assert ui_sync_rewrite_label_for_track(atmos, enabled=True) == "Offset"

    def test_audio_bitrate_is_parsed_from_display_info(self):
        assert audio_bitrate_kbps_from_display_info("5.1  640 kbps") == 640
        assert audio_bitrate_kbps_from_display_info("stereo  128 kb/s") == 128
        assert audio_bitrate_kbps_from_display_info("stereo") is None

    def test_audio_rewrite_preserves_source_codec_and_bitrate(self, tmp_path, monkeypatch):
        service = SyncRewriteService(
            ffmpeg_bin="ffmpeg",
            ffprobe_bin="ffprobe",
            audio_bitrate_per_channel={"eac3": 96},
        )
        monkeypatch.setattr(
            service,
            "_probe_stream",
            lambda _source, _stream_index: {
                "codec_name": "eac3",
                "codec_long_name": "E-AC-3",
                "profile": "",
                "channels": 6,
                "bit_rate": "640000",
                "tags": {},
            },
        )
        seen: dict[str, object] = {}

        def fake_run(cmd, destination, _error_prefix):
            seen["cmd"] = cmd
            destination.write_bytes(b"audio")

        monkeypatch.setattr(service, "_run_checked", fake_run)

        prepared = service.maybe_materialize(
            source_path=tmp_path / "in.mkv",
            stream_index=1,
            track_type="audio",
            codec="eac3",
            display_info="5.1  640 kbps",
            offset_ms=250,
            tmp_dir=tmp_path,
            input_idx=2,
            preserve_source_audio_params=True,
        )

        assert prepared is not None
        assert prepared.codec == "eac3"
        assert prepared.bitrate_kbps == 640
        cmd = cast(list[str], seen["cmd"])
        assert cmd[cmd.index("-c:a") + 1] == "eac3"
        assert cmd[cmd.index("-b:a") + 1] == "640k"
        assert cmd[cmd.index("-f") + 1] == "matroska"

    def test_audio_rewrite_can_use_explicit_target_params(self, tmp_path, monkeypatch):
        service = SyncRewriteService(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        monkeypatch.setattr(
            service,
            "_probe_stream",
            lambda _source, _stream_index: {
                "codec_name": "eac3",
                "codec_long_name": "E-AC-3",
                "profile": "",
                "channels": 6,
                "bit_rate": "640000",
                "tags": {},
            },
        )
        seen: dict[str, object] = {}

        def fake_run(cmd, destination, _error_prefix):
            seen["cmd"] = cmd
            destination.write_bytes(b"audio")

        monkeypatch.setattr(service, "_run_checked", fake_run)

        prepared = service.maybe_materialize(
            source_path=tmp_path / "in.mkv",
            stream_index=1,
            track_type="audio",
            codec="eac3",
            display_info="5.1  640 kbps",
            offset_ms=-500,
            tmp_dir=tmp_path,
            input_idx=2,
            preserve_source_audio_params=False,
            audio_target_codec="aac",
            audio_target_bitrate_kbps=384,
        )

        assert prepared is not None
        assert prepared.codec == "aac"
        assert prepared.bitrate_kbps == 384
        cmd = cast(list[str], seen["cmd"])
        assert "atrim=start=0.500,asetpts=PTS-STARTPTS" in cmd
        assert cmd[cmd.index("-c:a") + 1] == "aac"
        assert cmd[cmd.index("-b:a") + 1] == "384k"
        assert cmd[cmd.index("-f") + 1] == "matroska"

    def test_subtitle_rewrite_forces_matroska_muxer_for_mks(self, tmp_path, monkeypatch):
        source = tmp_path / "in.mkv"
        source.touch()
        service = SyncRewriteService(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        seen: list[list[str]] = []

        def fake_run(cmd, destination, _error_prefix):
            seen.append(list(cmd))
            if str(destination).endswith("_raw.srt"):
                destination.write_text("1\n00:00:01,000 --> 00:00:02,000\nBonjour\n", encoding="utf-8")
            else:
                destination.write_bytes(b"sub")

        monkeypatch.setattr(service, "_run_checked", fake_run)

        prepared = service.maybe_materialize(
            source_path=source,
            stream_index=6,
            track_type="subtitle",
            codec="subrip",
            offset_ms=250,
            tmp_dir=tmp_path,
            input_idx=2,
            token="subtitle",
        )

        assert prepared is not None
        assert len(seen) == 2
        wrap_cmd = seen[1]
        assert wrap_cmd[-3:] == ["-f", "matroska", str(prepared.path)]


class TestCommonTrackTypes:
    def test_track_type_values_and_compat_aliases(self, tmp_path):
        offset = TrackOffset(track_type=TrackType.AUDIO.value, source_path=tmp_path / "a.mkv", stream_index=1, offset_ms=125)
        patch = TrackMetaPatch(track_order=2, language="fr-FR", title="VF")
        assert TrackType.SUBTITLE.value == "subtitle"
        assert isinstance(offset, TrackTimeOffset)
        assert isinstance(patch, TrackMetaEdit)
