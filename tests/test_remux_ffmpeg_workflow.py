"""
tests/test_remux_ffmpeg_workflow.py — Cahier de test backend remux FFmpeg.

Couverture visée :
1. Construction de commande
   - sortie MKV obligatoire
   - mapping des pistes selon track_order
   - écriture langue BCP-47 sur `language` (+ purge `language-ietf`)
   - mapping chapitres (copie source vs overrides ffmetadata)
   - tags globaux choisis
   - attachements manuels et issus des sources

2. Exécution réelle (intégration légère)
   - génération d'un MKV de test
   - remux complet via FfmpegRemuxWorkflow.run()
   - vérification ffprobe des tags, chapitres, langues et attachements

3. Cas sensible attachements source
   - extraction/re-attachement des streams "attachment" et "attached_pic"
   - validation du résultat final (mimetype/filename présents)
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, Qt

from core.inspector import AttachmentInfo, ChapterEntry
from core.runner import TaskSignals
from core.workflows.remux import RemuxConfig, RemuxError, SourceInput, TrackEntry
from core.workflows.remux_ffmpeg import FfmpegRemuxWorkflow
from core.workflows.remux_timeline_sync import LiveSyncNotSupportedError, SyncPreparedInput


def _track(
    tid: int,
    track_type: str,
    *,
    language: str = "",
    title: str = "",
    codec: str = "COPY",
) -> TrackEntry:
    return TrackEntry(
        mkv_tid=tid,
        track_type=track_type,
        codec=codec,
        display_info="",
        language=language,
        title=title,
        enabled=True,
        file_id="src-0",
        orig_language=language,
        orig_title=title,
    )


@pytest.fixture(autouse=True)
def _qt_app(qt_app):
    """Assure qu'une QApplication existe avant les tests de ce module."""
    return qt_app


def _wait_task(signals, timeout: float = 20.0) -> dict[str, object]:
    app = QCoreApplication.instance()
    assert app is not None, "Q(Core)Application non initialisée"
    state: dict[str, object] = {
        "finished": None,
        "failed": None,
        "cancelled": False,
        "progress": [],
    }
    done = {"value": False}

    signals.progress.connect(lambda msg: state["progress"].append(msg), Qt.ConnectionType.QueuedConnection)
    signals.finished.connect(lambda res: (state.__setitem__("finished", res), done.__setitem__("value", True)), Qt.ConnectionType.QueuedConnection)
    signals.failed.connect(lambda msg, exc: (state.__setitem__("failed", (msg, exc)), done.__setitem__("value", True)), Qt.ConnectionType.QueuedConnection)
    signals.cancelled.connect(lambda: (state.__setitem__("cancelled", True), done.__setitem__("value", True)), Qt.ConnectionType.QueuedConnection)

    deadline = time.monotonic() + timeout
    while not done["value"] and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    return state


def _ffprobe_json(path: Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            "-show_chapters",
            str(path),
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    return json.loads(result.stdout or "{}")


def _tag_value(tags: dict | None, key: str) -> str | None:
    if not tags:
        return None
    for k, v in tags.items():
        if k.lower() == key.lower():
            return str(v)
    return None


class TestFfmpegRemuxWorkflowBuildCommand:

    def test_validate_rejects_non_mkv_output(self, tmp_path):
        wf = FfmpegRemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src = tmp_path / "in.mkv"
        src.touch()
        cfg = RemuxConfig(
            sources=[SourceInput(path=src, file_index=0, tracks=[_track(0, "video")])],
            output=tmp_path / "out.mp4",
            track_order=[(0, 0)],
        )

        errors = wf.validate(cfg)
        assert any(".mkv" in e for e in errors)

    def test_build_command_includes_muxing_application_metadata(self, tmp_path):
        wf = FfmpegRemuxWorkflow(
            ffmpeg_bin="ffmpeg",
            ffprobe_bin="ffprobe",
            writing_application="Mediarecode Test/1.0",
        )
        src = tmp_path / "in.mkv"
        src.touch()
        cfg = RemuxConfig(
            sources=[SourceInput(path=src, file_index=0, tracks=[_track(0, "video")])],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
            keep_chapters=False,
        )
        cmd = wf.build_command(cfg)
        flat = " ".join(cmd)
        assert "-metadata muxing_application=Mediarecode Test/1.0" in flat

    def test_build_command_includes_threads_argument(self, tmp_path):
        wf = FfmpegRemuxWorkflow(
            ffmpeg_bin="ffmpeg",
            ffprobe_bin="ffprobe",
            ffmpeg_threads=12,
        )
        src = tmp_path / "in.mkv"
        src.touch()
        cfg = RemuxConfig(
            sources=[SourceInput(path=src, file_index=0, tracks=[_track(0, "video")])],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
            keep_chapters=False,
        )

        cmd = wf.build_command(cfg)
        assert "-threads" in cmd
        assert cmd[cmd.index("-threads") + 1] == "12"

    def test_build_command_emits_language_and_clears_legacy_ietf_tag(self, tmp_path):
        wf = FfmpegRemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src = tmp_path / "in.mkv"
        src.touch()

        tracks = [_track(0, "video"), _track(1, "audio", language="fr", title="VF")]
        cfg = RemuxConfig(
            sources=[SourceInput(path=src, file_index=0, tracks=tracks)],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0), (0, 1)],
            keep_chapters=False,
            tag_overrides={"TMDB_ID": "42"},
        )

        cmd = wf.build_command(cfg)
        flat = " ".join(cmd)

        assert "-metadata:s:a:0 language=fr-FR" in flat
        assert "-metadata:s:a:0 language-ietf=" in flat
        assert "-map_chapters -1" in flat
        assert "-metadata TMDB_ID=42" in flat

    def test_build_command_with_chapter_overrides_maps_metadata_input(self, tmp_path):
        wf = FfmpegRemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src = tmp_path / "in.mkv"
        src.touch()

        cfg = RemuxConfig(
            sources=[SourceInput(path=src, file_index=0, tracks=[_track(0, "video")])],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
            chapter_overrides=[ChapterEntry(timecode_s=0.0, name="Intro")],
        )

        cmd = wf.build_command(
            cfg,
            extra_inputs=[Path("<chapitres.ffmetadata>")],
            chapter_input_index=1,
        )

        assert "-i" in cmd
        assert "<chapitres.ffmetadata>" in cmd
        assert cmd[cmd.index("-map_chapters") + 1] == "1"

    def test_build_command_with_tag_and_chapter_overrides_maps_metadata_from_chapter_input(self, tmp_path):
        wf = FfmpegRemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src = tmp_path / "in.mkv"
        src.touch()

        cfg = RemuxConfig(
            sources=[SourceInput(path=src, file_index=0, tracks=[_track(0, "video")])],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
            chapter_overrides=[ChapterEntry(timecode_s=0.0, name="Intro")],
            tag_overrides={"TMDB_ID": "42"},
        )

        cmd = wf.build_command(
            cfg,
            extra_inputs=[Path("<chapitres.ffmetadata>")],
            chapter_input_index=1,
        )

        assert cmd[cmd.index("-map_metadata") + 1] == "1"
        assert cmd[cmd.index("-map_chapters") + 1] == "1"

    def test_build_command_forces_matroska_demuxer_for_sync_inputs(self, tmp_path):
        wf = FfmpegRemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src = tmp_path / "in.mkv"
        sync = tmp_path / "sync_audio.mka"
        chapters = tmp_path / "chapters.ffmetadata"
        src.touch()
        sync.touch()
        chapters.touch()

        cfg = RemuxConfig(
            sources=[SourceInput(path=src, file_index=0, tracks=[_track(0, "video")])],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
            chapter_overrides=[ChapterEntry(timecode_s=0.0, name="Intro")],
        )

        cmd = wf.build_command(
            cfg,
            sync_inputs=[sync],
            extra_inputs=[chapters],
            chapter_input_index=2,
        )

        sync_idx = cmd.index(str(sync))
        assert cmd[sync_idx - 1] == "-i"
        assert cmd[sync_idx - 2] == "matroska"
        assert cmd[sync_idx - 3] == "-f"
        assert cmd[cmd.index("-map_chapters") + 1] == "2"

    def test_build_command_maps_source_metadata_when_copy_tags_is_enabled(self, tmp_path):
        wf = FfmpegRemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src = tmp_path / "in.mkv"
        src.touch()

        cfg = RemuxConfig(
            sources=[SourceInput(
                path=src,
                file_index=0,
                tracks=[_track(0, "video")],
                copy_tags=True,
            )],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
            keep_chapters=False,
            tag_overrides=None,
        )
        cmd = wf.build_command(cfg)
        assert cmd[cmd.index("-map_metadata") + 1] == "0"

    def test_build_command_drops_source_metadata_when_copy_tags_is_disabled(self, tmp_path):
        wf = FfmpegRemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src = tmp_path / "in.mkv"
        src.touch()

        cfg = RemuxConfig(
            sources=[SourceInput(
                path=src,
                file_index=0,
                tracks=[_track(0, "video")],
                copy_tags=False,
            )],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
            keep_chapters=False,
            tag_overrides=None,
        )
        cmd = wf.build_command(cfg)
        assert cmd[cmd.index("-map_metadata") + 1] == "-1"

    def test_build_command_multi_source_with_subtitles_enables_strict_interleave(self, tmp_path):
        """Le mux multi-source avec sous-titres a besoin d'un entrelacement strict
        pour éviter qu'une piste audio importée soit écrite très loin de la vidéo."""
        wf = FfmpegRemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src_a = tmp_path / "a.mkv"
        src_b = tmp_path / "b.mkv"
        src_a.touch()
        src_b.touch()

        cfg = RemuxConfig(
            sources=[
                SourceInput(path=src_a, file_index=0, tracks=[_track(0, "video"), _track(2, "subtitle")]),
                SourceInput(path=src_b, file_index=1, tracks=[_track(1, "audio")]),
            ],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0), (1, 1), (0, 2)],
            keep_chapters=False,
        )

        cmd = wf.build_command(cfg)
        assert cmd[cmd.index("-max_interleave_delta") + 1] == "0"
        assert cmd[cmd.index("-max_muxing_queue_size") + 1] == "9999"
        assert "-fflags" not in cmd
        assert "-copytb" not in cmd
        assert "-avoid_negative_ts" not in cmd

    def test_build_command_strict_interleave_override_false_disables_flags(self, tmp_path):
        wf = FfmpegRemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src_a = tmp_path / "a.mkv"
        src_b = tmp_path / "b.mkv"
        src_a.touch()
        src_b.touch()

        cfg = RemuxConfig(
            sources=[
                SourceInput(path=src_a, file_index=0, tracks=[_track(0, "video"), _track(2, "subtitle")]),
                SourceInput(path=src_b, file_index=1, tracks=[_track(1, "audio")]),
            ],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0), (1, 1), (0, 2)],
            keep_chapters=False,
        )

        cmd = wf.build_command(cfg, strict_interleave_override=False)
        assert "-max_muxing_queue_size" not in cmd
        assert "-max_interleave_delta" not in cmd

    def test_build_command_strict_interleave_override_true_enables_flags(self, tmp_path):
        wf = FfmpegRemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src = tmp_path / "in.mkv"
        src.touch()

        cfg = RemuxConfig(
            sources=[SourceInput(path=src, file_index=0, tracks=[_track(0, "video")])],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
            keep_chapters=False,
        )

        cmd = wf.build_command(cfg, strict_interleave_override=True)
        assert cmd[cmd.index("-max_interleave_delta") + 1] == "0"
        assert cmd[cmd.index("-max_muxing_queue_size") + 1] == "9999"

    def test_build_command_multi_source_without_subtitles_keeps_default_interleave(self, tmp_path):
        wf = FfmpegRemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src_a = tmp_path / "a.mkv"
        src_b = tmp_path / "b.mkv"
        src_a.touch()
        src_b.touch()

        cfg = RemuxConfig(
            sources=[
                SourceInput(path=src_a, file_index=0, tracks=[_track(0, "video")]),
                SourceInput(path=src_b, file_index=1, tracks=[_track(1, "audio")]),
            ],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0), (1, 1)],
            keep_chapters=False,
        )

        cmd = wf.build_command(cfg)
        assert "-max_muxing_queue_size" not in cmd
        assert "-max_interleave_delta" not in cmd

    def test_build_command_multi_source_with_subtitles_but_no_foreign_audio_keeps_default_interleave(self, tmp_path):
        wf = FfmpegRemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src_a = tmp_path / "a.mkv"
        src_b = tmp_path / "b.mkv"
        src_a.touch()
        src_b.touch()

        cfg = RemuxConfig(
            sources=[
                SourceInput(path=src_a, file_index=0, tracks=[_track(0, "video"), _track(1, "audio")]),
                SourceInput(path=src_b, file_index=1, tracks=[_track(2, "subtitle")]),
            ],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0), (0, 1), (1, 2)],
            keep_chapters=False,
        )

        cmd = wf.build_command(cfg)
        assert "-max_muxing_queue_size" not in cmd
        assert "-max_interleave_delta" not in cmd

    def test_prepare_sync_inputs_windows_prefers_mmap_fallback(self, tmp_path, monkeypatch):
        wf = FfmpegRemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src_a = tmp_path / "a.mkv"
        src_b = tmp_path / "b.mkv"
        src_a.touch()
        src_b.touch()

        cfg = RemuxConfig(
            sources=[
                SourceInput(path=src_a, file_index=0, tracks=[_track(0, "video"), _track(2, "subtitle")]),
                SourceInput(path=src_b, file_index=1, tracks=[_track(1, "audio")]),
            ],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0), (1, 1), (0, 2)],
            keep_chapters=False,
        )
        mapped = wf._resolve_mapped_tracks(cfg)

        class _FakeSyncer:
            def __init__(self, **_kwargs):
                pass

            def start_live_demux_session(self, **_kwargs):
                raise LiveSyncNotSupportedError("no live")

            def prepare_from_mapped_tracks_mmap(self, **_kwargs):
                return [SyncPreparedInput(key=(1, 1, "audio"), path=tmp_path / "mmap.mka", input_idx=2)]

            def prepare_from_mapped_tracks(self, **_kwargs):
                pytest.fail("temp fallback should not be used when mmap works")

        monkeypatch.setattr("core.workflows.remux_ffmpeg.MkvmergeLikeTimelineSync", _FakeSyncer)
        monkeypatch.setattr("core.workflows.remux_ffmpeg.os.name", "nt", raising=False)

        remapped, extra_inputs, live = wf._prepare_mkvmerge_like_sync_inputs(
            cfg,
            mapped,
            tmp_path,
            TaskSignals(),
        )
        assert live is None
        assert extra_inputs == [tmp_path / "mmap.mka"]
        audio = next(mt for mt in remapped if mt.track.track_type == "audio")
        assert audio.source_input_idx == 2
        assert audio.stream_index == 0

    def test_prepare_sync_inputs_windows_falls_back_to_temp_when_mmap_fails(self, tmp_path, monkeypatch):
        wf = FfmpegRemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src_a = tmp_path / "a.mkv"
        src_b = tmp_path / "b.mkv"
        src_a.touch()
        src_b.touch()

        cfg = RemuxConfig(
            sources=[
                SourceInput(path=src_a, file_index=0, tracks=[_track(0, "video"), _track(2, "subtitle")]),
                SourceInput(path=src_b, file_index=1, tracks=[_track(1, "audio")]),
            ],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0), (1, 1), (0, 2)],
            keep_chapters=False,
        )
        mapped = wf._resolve_mapped_tracks(cfg)

        class _FakeSyncer:
            def __init__(self, **_kwargs):
                pass

            def start_live_demux_session(self, **_kwargs):
                raise LiveSyncNotSupportedError("no live")

            def prepare_from_mapped_tracks_mmap(self, **_kwargs):
                raise RemuxError("mmap failed")

            def prepare_from_mapped_tracks(self, **_kwargs):
                return [SyncPreparedInput(key=(1, 1, "audio"), path=tmp_path / "temp.mka", input_idx=2)]

        monkeypatch.setattr("core.workflows.remux_ffmpeg.MkvmergeLikeTimelineSync", _FakeSyncer)
        monkeypatch.setattr("core.workflows.remux_ffmpeg.os.name", "nt", raising=False)

        remapped, extra_inputs, live = wf._prepare_mkvmerge_like_sync_inputs(
            cfg,
            mapped,
            tmp_path,
            TaskSignals(),
        )
        assert live is None
        assert extra_inputs == [tmp_path / "temp.mka"]
        audio = next(mt for mt in remapped if mt.track.track_type == "audio")
        assert audio.source_input_idx == 2
        assert audio.stream_index == 0

    def test_validate_rejects_invalid_selected_attachments(self, tmp_path):
        wf = FfmpegRemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        src = tmp_path / "in.mkv"
        src.touch()

        bad = AttachmentInfo(
            index=-1,
            local_index=-2,
            filename="font.ttf",
            mimetype="application/x-truetype-font",
            is_attached_pic=False,
        )
        cfg = RemuxConfig(
            sources=[SourceInput(
                path=src,
                file_index=0,
                tracks=[_track(0, "video")],
                selected_attachments=[bad, bad],
            )],
            output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
        )
        errors = wf.validate(cfg)
        assert any("index négatif" in e for e in errors)
        assert any("local_index négatif" in e for e in errors)
        assert any("dupliquée" in e for e in errors)


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe requis pour les tests d'intégration",
)
class TestFfmpegRemuxWorkflowIntegration:

    def _make_av_source(self, path: Path) -> None:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25",
                "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
                "-t", "1",
                "-c:v", "libx264",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-shortest",
                str(path),
            ],
            check=True,
        )

    def test_run_applies_language_chapters_tags_and_cover(self, tmp_path):
        src = tmp_path / "src.mkv"
        out = tmp_path / "out.mkv"
        cover = tmp_path / "cover.jpg"

        self._make_av_source(src)
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=blue:s=48x48:d=0.1",
                "-frames:v", "1",
                str(cover),
            ],
            check=True,
        )

        wf = FfmpegRemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        tracks = [_track(0, "video"), _track(1, "audio", language="fr", title="Français")]
        cfg = RemuxConfig(
            sources=[SourceInput(path=src, file_index=0, tracks=tracks)],
            output=out,
            track_order=[(0, 0), (0, 1)],
            chapter_overrides=[
                ChapterEntry(timecode_s=0.0, name="Intro"),
                ChapterEntry(timecode_s=0.5, name="Main"),
            ],
            extra_attachments=[cover],
            tag_overrides={"TMDB_ID": "777", "SOURCE": "pytest"},
            keep_chapters=True,
        )

        state = _wait_task(wf.run(cfg), timeout=30.0)
        assert state["failed"] is None, f"Remux failed: {state['failed']}"
        assert state["cancelled"] is False
        assert out.exists()

        probe = _ffprobe_json(out)

        assert _tag_value(probe.get("format", {}).get("tags", {}), "TMDB_ID") == "777"
        assert _tag_value(probe.get("format", {}).get("tags", {}), "SOURCE") == "pytest"
        assert len(probe.get("chapters", [])) == 2
        assert [_tag_value(ch.get("tags", {}), "title") for ch in probe.get("chapters", [])] == ["Intro", "Main"]

        audio_stream = next(s for s in probe.get("streams", []) if s.get("codec_type") == "audio")
        assert _tag_value(audio_stream.get("tags", {}), "language") == "fr-FR"
        assert _tag_value(audio_stream.get("tags", {}), "language-ietf") is None

        attached_cover = next(
            s for s in probe.get("streams", [])
            if s.get("codec_type") in {"attachment", "video"}
            and int((s.get("disposition") or {}).get("attached_pic", 0)) == 1
        )
        assert _tag_value(attached_cover.get("tags", {}), "mimetype") == "image/jpeg"

    def test_run_rehydrates_selected_source_attachments(self, tmp_path):
        base = tmp_path / "base.mkv"
        src = tmp_path / "src_with_att.mkv"
        out = tmp_path / "out_att.mkv"
        font = tmp_path / "test_font.ttf"
        cover = tmp_path / "cover.jpg"

        self._make_av_source(base)
        font.write_text("dummy-font", encoding="utf-8")
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=red:s=32x32:d=0.1",
                "-frames:v", "1",
                str(cover),
            ],
            check=True,
        )

        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner", "-loglevel", "error",
                "-i", str(base),
                "-map", "0",
                "-c", "copy",
                "-attach", str(font),
                "-metadata:s:t:0", "mimetype=application/x-truetype-font",
                "-metadata:s:t:0", "filename=test_font.ttf",
                "-attach", str(cover),
                "-metadata:s:t:1", "mimetype=image/jpeg",
                "-metadata:s:t:1", "filename=cover.jpg",
                str(src),
            ],
            check=True,
        )

        probe_src = _ffprobe_json(src)
        att_streams = [
            s for s in probe_src.get("streams", [])
            if s.get("codec_type") == "attachment"
            or int((s.get("disposition") or {}).get("attached_pic", 0)) == 1
        ]
        selected: list[AttachmentInfo] = []
        local_index = 0
        for s in att_streams:
            tags = s.get("tags") or {}
            selected.append(AttachmentInfo(
                index=int(s.get("index", -1)),
                local_index=local_index,
                filename=_tag_value(tags, "filename") or f"att_{local_index}",
                mimetype=_tag_value(tags, "mimetype") or "application/octet-stream",
                is_attached_pic=bool(int((s.get("disposition") or {}).get("attached_pic", 0))),
            ))
            local_index += 1

        wf = FfmpegRemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        cfg = RemuxConfig(
            sources=[SourceInput(path=src, file_index=0, tracks=[_track(0, "video")], selected_attachments=selected)],
            output=out,
            track_order=[(0, 0)],
            keep_chapters=False,
            tag_overrides={},
        )

        state = _wait_task(wf.run(cfg), timeout=30.0)
        assert state["failed"] is None, f"Remux failed: {state['failed']}"
        assert out.exists()

        probe_out = _ffprobe_json(out)
        out_attachments = [
            s for s in probe_out.get("streams", [])
            if s.get("codec_type") == "attachment"
            or int((s.get("disposition") or {}).get("attached_pic", 0)) == 1
        ]
        assert len(out_attachments) >= 2

        mimetypes = {
            _tag_value(s.get("tags", {}), "mimetype")
            for s in out_attachments
        }
        assert "application/x-truetype-font" in mimetypes
        assert "image/jpeg" in mimetypes

    def test_run_can_copy_source_global_tags_when_requested(self, tmp_path):
        src_plain = tmp_path / "src_plain.mkv"
        src_tagged = tmp_path / "src_tagged.mkv"
        out = tmp_path / "out_tag_copy.mkv"

        self._make_av_source(src_plain)
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner", "-loglevel", "error",
                "-i", str(src_plain),
                "-map", "0",
                "-c", "copy",
                "-metadata", "SOURCE=from_source",
                str(src_tagged),
            ],
            check=True,
        )

        wf = FfmpegRemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        cfg = RemuxConfig(
            sources=[SourceInput(
                path=src_tagged,
                file_index=0,
                tracks=[_track(0, "video"), _track(1, "audio", language="eng")],
                copy_tags=True,
            )],
            output=out,
            track_order=[(0, 0), (0, 1)],
            keep_chapters=False,
            tag_overrides=None,
        )
        state = _wait_task(wf.run(cfg), timeout=30.0)
        assert state["failed"] is None, f"Remux failed: {state['failed']}"
        assert out.exists()

        probe = _ffprobe_json(out)
        assert _tag_value(probe.get("format", {}).get("tags", {}), "SOURCE") == "from_source"

    def test_run_applies_custom_muxing_application_without_mkvpropedit(self, tmp_path):
        src = tmp_path / "src.mkv"
        out = tmp_path / "out_muxing_app.mkv"
        self._make_av_source(src)

        wf = FfmpegRemuxWorkflow(
            ffmpeg_bin="ffmpeg",
            ffprobe_bin="ffprobe",
            writing_application="MediaRecode v1.2.1 - test",
        )
        cfg = RemuxConfig(
            sources=[SourceInput(path=src, file_index=0, tracks=[_track(0, "video"), _track(1, "audio", language="eng")])],
            output=out,
            track_order=[(0, 0), (0, 1)],
            keep_chapters=False,
            tag_overrides=None,
        )
        state = _wait_task(wf.run(cfg), timeout=30.0)
        assert state["failed"] is None, f"Remux failed: {state['failed']}"
        probe = _ffprobe_json(out)
        assert _tag_value(probe.get("format", {}).get("tags", {}), "MUXING_APPLICATION") == "MediaRecode v1.2.1 - test"
