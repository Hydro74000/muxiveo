from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from core.inspector import AttachmentInfo
from core.runner import TaskSignals
from core.workflows.remux_attachments import attachment_names, build_attachment_mapping
from core.workflows.remux_command import build_remux_command, preview_remux_command
from core.workflows.remux_mapping import (
    MappedTrack,
    metadata_context,
    resolve_mapped_tracks,
    requires_file_sync_fallback_for_offsets,
)
from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry
from core.workflows.remux_sync import bind_temp_cleanup, decide_strict_interleave_with_prescan


def _track(
    mkv_tid: int,
    track_type: str,
    *,
    codec: str = "copy",
    title: str = "",
    language: str = "und",
    entry_id: str | None = None,
    time_shift_ms: int = 0,
) -> TrackEntry:
    return TrackEntry(
        mkv_tid=mkv_tid,
        track_type=track_type,
        codec=codec,
        display_info=track_type,
        language=language,
        title=title,
        entry_id=entry_id or f"{track_type}-{mkv_tid}",
        time_shift_ms=time_shift_ms,
    )


class TestRemuxMappingModule:
    def test_resolve_mapped_tracks_preserves_output_type_indices(self, tmp_path):
        src = tmp_path / "in.mkv"
        src.touch()
        cfg = RemuxConfig(
            sources=[SourceInput(path=src, file_index=0, tracks=[_track(0, "video"), _track(1, "audio"), _track(2, "subtitle")])],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0), (0, 1), (0, 2)],
        )
        mapped = resolve_mapped_tracks(cfg)
        assert [(mt.track.track_type, mt.out_type_index) for mt in mapped] == [
            ("video", 0),
            ("audio", 0),
            ("subtitle", 0),
        ]

    def test_metadata_context_uses_chapter_input_when_tags_are_overridden(self, tmp_path):
        src = tmp_path / "in.mkv"
        src.touch()
        cfg = RemuxConfig(
            sources=[SourceInput(path=src, file_index=0, tracks=[_track(0, "video")])],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
            chapter_overrides=[SimpleNamespace(timecode_s=0.0, name="Intro")],
            tag_overrides={"TMDB_ID": "42"},
        )
        meta = metadata_context(cfg, chapter_input_index=1)
        assert meta.chapter_map == "1"
        assert meta.metadata_map == "1"
        assert meta.global_tags["TMDB_ID"] == "42"

    def test_requires_file_sync_fallback_for_offsets_detects_foreign_shift(self):
        mapped = [
            MappedTrack(0, 0, Path("a.mkv"), 0, _track(0, "video"), 0),
            MappedTrack(1, 1, Path("b.mkv"), 1, _track(1, "audio", time_shift_ms=90), 0),
        ]
        assert requires_file_sync_fallback_for_offsets(mapped) is True


class TestRemuxCommandModule:
    def test_build_remux_command_applies_strict_interleave_and_metadata(self, tmp_path):
        src_a = tmp_path / "a.mkv"
        src_b = tmp_path / "b.mkv"
        src_a.touch()
        src_b.touch()
        cfg = RemuxConfig(
            sources=[
                SourceInput(path=src_a, file_index=0, tracks=[_track(0, "video"), _track(2, "subtitle", codec="subrip")]),
                SourceInput(path=src_b, file_index=1, tracks=[_track(1, "audio", language="fr", title="VF")]),
            ],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0), (1, 1), (0, 2)],
            keep_chapters=False,
            tag_overrides={"GENRE": "Drama"},
        )
        cmd = build_remux_command(
            cfg,
            ffmpeg_bin="ffmpeg",
            ffmpeg_progress_args=["-progress", "pipe:1", "-nostats"],
            ffmpeg_thread_args=["-threads", "4"],
            cli_path=str,
            strict_interleave_override=True,
        )
        assert "-max_interleave_delta" in cmd
        assert "GENRE=Drama" in cmd
        assert "-metadata:s:a:0" in cmd
        assert "language=fr-FR" in cmd

    def test_preview_remux_command_formats_multiline_output(self, tmp_path):
        src = tmp_path / "in.mkv"
        src.touch()
        cfg = RemuxConfig(
            sources=[SourceInput(path=src, file_index=0, tracks=[_track(0, "video")])],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
        )
        preview = preview_remux_command(
            cfg,
            build_command=lambda *_args, **_kwargs: ["ffmpeg", "-i", str(src), str(tmp_path / "out.mkv")],
        )
        assert preview.startswith("ffmpeg")
        assert "\\\n" in preview


class TestRemuxSyncModule:
    def test_decide_strict_interleave_with_prescan_logs_foreign_offset(self):
        cfg = SimpleNamespace()
        mapped = [
            MappedTrack(0, 0, Path("a.mkv"), 0, _track(0, "video"), 0),
            MappedTrack(1, 1, Path("b.mkv"), 1, _track(1, "audio", time_shift_ms=100), 0),
        ]
        logs: list[tuple[str, str]] = []
        result = decide_strict_interleave_with_prescan(
            cast(Any, cfg),
            resolve_mapped_tracks=lambda _cfg: mapped,
            log_cb=lambda level, msg: logs.append((level, msg)),
        )
        assert result is True
        assert any("Décalage" in message for _, message in logs)

    def test_bind_temp_cleanup_removes_paths_once(self, tmp_path):
        temp_dir = tmp_path / "work"
        temp_dir.mkdir()
        (temp_dir / "marker.txt").write_text("x", encoding="utf-8")
        signals = TaskSignals()
        bind_temp_cleanup(signals, [temp_dir])
        signals.finished.emit("ok")
        assert not temp_dir.exists()


class TestRemuxAttachmentsModule:
    def test_build_attachment_mapping_and_attachment_names(self, tmp_path):
        src = tmp_path / "in.mkv"
        src.touch()
        attachment = AttachmentInfo(
            index=5,
            local_index=0,
            filename="cover",
            mimetype="image/jpeg",
            is_attached_pic=False,
        )
        cfg = RemuxConfig(
            sources=[SourceInput(path=src, file_index=0, tracks=[_track(0, "video")], selected_attachments=[attachment])],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
        )
        args = build_attachment_mapping(cfg)
        assert args == ["-map", "0:5", "-c:t", "copy", "-map_metadata:s:t", "-1"]
        assert attachment_names(attachment) == ("cover.jpg", "cover.jpg")
