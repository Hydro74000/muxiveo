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


class TestCommonTrackTypes:
    def test_track_type_values_and_compat_aliases(self, tmp_path):
        offset = TrackOffset(track_type=TrackType.AUDIO.value, source_path=tmp_path / "a.mkv", stream_index=1, offset_ms=125)
        patch = TrackMetaPatch(track_order=2, language="fr-FR", title="VF")
        assert TrackType.SUBTITLE.value == "subtitle"
        assert isinstance(offset, TrackTimeOffset)
        assert isinstance(patch, TrackMetaEdit)
