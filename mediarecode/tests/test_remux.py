"""
tests/test_remux.py — Tests du workflow de remuxage multi-source et de l'UI associée.

Plan de couverture :
    _pick_file_color :
        - Retourne une chaîne hex valide (#rrggbb, 7 chars)
        - Déterministe : même index → même couleur
        - Indices différents → couleurs différentes
        - Ni proche du noir ni proche du blanc (luminosité 0.1–0.9)
        - Angle doré : couleurs consécutives bien séparées en teinte (>90°)

    SourceFile :
        - Champ color par défaut ""
        - Champ color accepte une valeur hex

    TrackEntry :
        - Propriétés type_label / type_long pour chaque type de piste
        - Valeurs par défaut (file_id vide, enabled True)

    tracks_from_file_info :
        - file_id propagé à toutes les pistes générées
        - Ordre de sortie : vidéo → audio → sous-titres
        - display_info vidéo (résolution + HDR + fps décimal et fractionnaire)
        - display_info audio (canaux + kHz + kbps)
        - display_info sous-titre (flags forcé / défaut)
        - FileInfo sans pistes → liste vide

    RemuxWorkflow.build_command :
        - Source unique, toutes pistes activées → pas de flags de filtrage
        - Source unique, aucune vidéo activée → --no-video
        - Source unique, audio partiel → --audio-tracks avec TIDs
        - Source unique, aucun sous-titre → --no-subtitles partiel → --subtitle-tracks
        - Source sans vidéo du tout → --no-video N'EST PAS émis (bug-guard)
        - --no-chapters / --no-attachments respectés
        - --track-name émis si title modifié, absent si inchangé
        - --language émis si modifié, absent si inchangé
        - Deux sources : --track-order cross-source (fi:tid,...)
        - Deux sources : flags per-source indépendants
        - track_order vide → pas de --track-order dans la commande
        - --track-order précède les chemins de source dans la commande

    RemuxWorkflow.preview_command :
        - Sortie multi-ligne avec continuation \\
        - Chaque flag/valeur sur sa propre ligne

    RemuxWorkflow.validate :
        - sources vide → erreur immédiate (une seule)
        - Fichier source introuvable → erreur
        - source == output → erreur
        - Dossier de sortie inexistant → erreur
        - track_order vide → erreur "Aucune piste"
        - Configuration valide (fichiers réels) → liste vide
        - Plusieurs erreurs → toutes retournées

    _TrackTable — colonnes :
        - COL_LANG == 4, COL_INFO == 5 (Langue avant Info)
        - En-têtes : "Langue" en col 4, "Info" en col 5

    _TrackTable — tri par type (V → A → S) :
        - Pistes vidéo insérées avant audio (table vide)
        - Pistes audio insérées après vidéo, avant sous-titres
        - Pistes sous-titre insérées en dernier
        - Multi-source : V-file1, V-file2, A-file1, A-file2 (groupement par type)
        - _find_insert_position("video") → 0 sur table vide
        - _find_insert_position("audio") → après toutes les vidéos
        - _find_insert_position("subtitle") → après tous les audios

    _TrackTable — colonne source (carré coloré) :
        - Texte de la cellule est "█"
        - Couleur de premier plan correspond à source_color fournie
        - Couleur stockée dans UserRole (pour reconstruction après drag-drop)

    _TrackTable — filtre "sélectionnées seulement" :
        - set_filter_selected(False) → toutes les lignes visibles
        - set_filter_selected(True) → lignes décochées masquées
        - refresh_filter() reapplique le filtre après changement de case
        - Désactiver le filtre réaffiche les lignes masquées

    _TrackTable — ajustement de hauteur :
        - Table vide → hauteur de base (header + placeholder)
        - N ≤ 15 lignes → N * row_h + header + 4
        - N > 15 lignes → 15 * row_h + header + 4 (plafond)

    _TrackTable (widget Qt) — autres :
        - append_tracks ajoute les bonnes lignes
        - append_tracks ne supprime pas les lignes existantes
        - remove_tracks_by_file_id supprime uniquement les lignes concernées
        - clear_all vide complètement le tableau
        - current_tracks retourne les pistes dans l'ordre du tableau
        - current_tracks synchronise l'état enabled depuis la case à cocher
        - current_tracks synchronise language et title éditables
        - set_all_enabled active / désactive toutes les pistes

    _FileListWidget — taille automatique :
        - Hauteur initiale (vide) = _FILE_PH_H + _FILE_BAR_H
        - Hauteur avec N fichiers = N * _FILE_ROW_H + _FILE_BAR_H
        - file_count() retourne le bon nombre

    _TrackTable.update_audio_meta :
        - Met à jour language et title dans la cellule et dans l'objet TrackEntry
        - N'émet pas de signal itemChanged (blockSignals)
        - Cible uniquement la ligne correspondant à (file_id, mkv_tid)
        - Laisse les autres lignes intactes
        - Sans effet si (file_id, mkv_tid) introuvable

    _AttachmentItemWidget — balises cochées par défaut :
        - is_tag=False → case cochée
        - is_tag=True → case cochée (nouveau comportement)

Exécution :
    pytest tests/test_remux.py -v
"""

from __future__ import annotations

import colorsys
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

from core.inspector import (
    AudioTrack, FileInfo, HDRType, SubtitleTrack, VideoTrack,
)
from core.workflows.remux import (
    RemuxConfig, RemuxError, RemuxWorkflow, SourceInput,
    TrackEntry, tracks_from_file_info,
)
from ui.panels.remux_panel import (
    SourceFile, _FILE_BAR_H, _FILE_PH_H, _FILE_ROW_H,
    _AttachmentItemWidget, _FileListWidget, _TrackTable, _pick_file_color,
)


# ===========================================================================
# Helpers / fabriques
# ===========================================================================

def _video(
    index: int = 0,
    codec: str = "hevc",
    width: int = 1920,
    height: int = 1080,
    hdr_type: HDRType = HDRType.NONE,
    frame_rate: str | None = "23.976",
    language: str | None = None,
    title: str | None = None,
) -> VideoTrack:
    return VideoTrack(
        index=index, codec=codec, codec_long=codec,
        width=width, height=height,
        frame_rate=frame_rate,
        bit_depth=10, color_space=None,
        color_primaries=None, color_transfer=None, color_matrix=None,
        hdr_type=hdr_type, language=language, title=title,
    )


def _audio(
    index: int = 1,
    codec: str = "eac3",
    channels: int = 6,
    sample_rate: int = 48000,
    bit_rate: int | None = 640_000,
    language: str | None = "fra",
    title: str | None = None,
) -> AudioTrack:
    return AudioTrack(
        index=index, codec=codec, codec_long=codec,
        channels=channels, channel_layout=None,
        sample_rate=sample_rate, bit_rate=bit_rate,
        language=language, title=title,
    )


def _subtitle(
    index: int = 2,
    codec: str = "subrip",
    language: str | None = "fra",
    forced: bool = False,
    default: bool = False,
) -> SubtitleTrack:
    return SubtitleTrack(
        index=index, codec=codec, language=language,
        title=None, forced=forced, default=default,
    )


def _file_info(
    path: Path = Path("/tmp/film.mkv"),
    videos: list[VideoTrack] | None = None,
    audios: list[AudioTrack] | None = None,
    subs: list[SubtitleTrack] | None = None,
) -> FileInfo:
    return FileInfo(
        path=path,
        format="matroska",
        duration_s=7200.0,
        size_bytes=20_000_000_000,
        bit_rate=22_000_000,
        video_tracks=videos or [],
        audio_tracks=audios or [],
        subtitle_tracks=subs or [],
    )


def _track(
    tid: int,
    track_type: str = "audio",
    codec: str = "EAC3",
    file_id: str = "abc",
    enabled: bool = True,
    title: str = "",
    language: str = "fra",
    orig_title: str = "",
    orig_language: str = "fra",
) -> TrackEntry:
    return TrackEntry(
        mkv_tid=tid, track_type=track_type, codec=codec,
        display_info="5.1  48 kHz",
        language=language, title=title, enabled=enabled,
        file_id=file_id,
        orig_language=orig_language, orig_title=orig_title,
    )


def _source(
    path: Path,
    file_index: int,
    tracks: list[TrackEntry],
) -> SourceInput:
    return SourceInput(path=path, file_index=file_index, tracks=tracks)


def _workflow() -> RemuxWorkflow:
    """RemuxWorkflow instancié avec un binaire factice (tests sans exécution)."""
    return RemuxWorkflow(mkvmerge_bin="mkvmerge")


# ===========================================================================
# TrackEntry — propriétés
# ===========================================================================

class TestTrackEntryProperties:

    def test_type_label_video(self):
        t = _track(0, track_type="video")
        assert t.type_label == "V"

    def test_type_label_audio(self):
        t = _track(1, track_type="audio")
        assert t.type_label == "A"

    def test_type_label_subtitle(self):
        t = _track(2, track_type="subtitle")
        assert t.type_label == "S"

    def test_type_label_unknown(self):
        t = _track(3, track_type="attachment")
        assert t.type_label == "?"

    def test_type_long_video(self):
        assert _track(0, "video").type_long == "Vidéo"

    def test_type_long_audio(self):
        assert _track(1, "audio").type_long == "Audio"

    def test_type_long_subtitle(self):
        assert _track(2, "subtitle").type_long == "Sous-titre"

    def test_file_id_default_empty(self):
        t = TrackEntry(
            mkv_tid=0, track_type="video", codec="HEVC",
            display_info="1080p", language="", title="",
        )
        assert t.file_id == ""

    def test_enabled_default_true(self):
        t = TrackEntry(
            mkv_tid=0, track_type="video", codec="HEVC",
            display_info="", language="", title="",
        )
        assert t.enabled is True


# ===========================================================================
# tracks_from_file_info
# ===========================================================================

class TestTracksFromFileInfo:

    def test_file_id_propagated_to_all_tracks(self):
        info = _file_info(
            videos=[_video(0)], audios=[_audio(1)], subs=[_subtitle(2)]
        )
        entries = tracks_from_file_info(info, file_id="uuid-xyz")
        assert all(e.file_id == "uuid-xyz" for e in entries)

    def test_order_video_then_audio_then_subtitle(self):
        info = _file_info(
            videos=[_video(0)], audios=[_audio(1)], subs=[_subtitle(2)]
        )
        entries = tracks_from_file_info(info)
        types = [e.track_type for e in entries]
        assert types == ["video", "audio", "subtitle"]

    def test_video_display_info_includes_resolution(self):
        info = _file_info(videos=[_video(0, width=3840, height=2160)])
        entries = tracks_from_file_info(info)
        assert "3840×2160" in entries[0].display_info

    def test_video_display_info_includes_hdr_label(self):
        info = _file_info(videos=[_video(0, hdr_type=HDRType.HDR10)])
        entries = tracks_from_file_info(info)
        assert "HDR10" in entries[0].display_info

    def test_video_display_info_sdr_has_no_hdr_label(self):
        info = _file_info(videos=[_video(0, hdr_type=HDRType.NONE)])
        entries = tracks_from_file_info(info)
        assert "SDR" not in entries[0].display_info
        assert "HDR" not in entries[0].display_info

    def test_video_fps_decimal_formatted(self):
        info = _file_info(videos=[_video(0, frame_rate="25")])
        entries = tracks_from_file_info(info)
        assert "25 fps" in entries[0].display_info

    def test_video_fps_fractional_converted(self):
        info = _file_info(videos=[_video(0, frame_rate="24000/1001")])
        entries = tracks_from_file_info(info)
        # 24000/1001 ≈ 23.976
        assert "23.976 fps" in entries[0].display_info

    def test_video_fps_zero_denominator_safe(self):
        info = _file_info(videos=[_video(0, frame_rate="24000/0")])
        # Ne doit pas lever ZeroDivisionError
        entries = tracks_from_file_info(info)
        assert entries[0].track_type == "video"

    def test_audio_display_info_includes_channels(self):
        info = _file_info(audios=[_audio(1, channels=6)])
        entries = tracks_from_file_info(info)
        assert "5.1" in entries[0].display_info

    def test_audio_display_info_excludes_sample_rate(self):
        # Le sample rate n'est plus inclus dans display_info (supprimé)
        info = _file_info(audios=[_audio(1, sample_rate=48000)])
        entries = tracks_from_file_info(info)
        assert "kHz" not in entries[0].display_info

    def test_audio_display_info_includes_bitrate(self):
        info = _file_info(audios=[_audio(1, bit_rate=640_000)])
        entries = tracks_from_file_info(info)
        assert "640 kbps" in entries[0].display_info

    def test_subtitle_forced_flag_in_full_info_label(self):
        # Les flags sont exposés via full_info_label (flags_label), pas display_info
        info = _file_info(subs=[_subtitle(2, forced=True)])
        entries = tracks_from_file_info(info)
        assert "forcé" in entries[0].full_info_label

    def test_subtitle_default_flag_in_full_info_label(self):
        info = _file_info(subs=[_subtitle(2, default=True)])
        entries = tracks_from_file_info(info)
        assert "défaut" in entries[0].full_info_label

    def test_subtitle_neither_forced_nor_default(self):
        info = _file_info(subs=[_subtitle(2, forced=False, default=False)])
        entries = tracks_from_file_info(info)
        assert entries[0].display_info == ""

    def test_empty_file_info_returns_empty_list(self):
        info = _file_info()
        assert tracks_from_file_info(info) == []

    def test_language_preserved_in_track_entry(self):
        info = _file_info(audios=[_audio(1, language="jpn")])
        entries = tracks_from_file_info(info)
        assert entries[0].language == "jpn"
        assert entries[0].orig_language == "jpn"

    def test_none_language_becomes_empty_string(self):
        info = _file_info(audios=[_audio(1, language=None)])
        entries = tracks_from_file_info(info)
        assert entries[0].language == ""


# ===========================================================================
# RemuxWorkflow.build_command
# ===========================================================================

class TestBuildCommand:

    def setup_method(self):
        self.wf = _workflow()

    def _cmd(self, config: RemuxConfig) -> list[str]:
        return self.wf.build_command(config)

    # --- Source unique, toutes pistes activées ---

    def test_single_source_output_first(self):
        src = _source(Path("/a.mkv"), 0, [_track(0, "video", file_id="id0")])
        cfg = RemuxConfig(
            sources=[src], output=Path("/out.mkv"),
            track_order=[(0, 0)],
        )
        cmd = self._cmd(cfg)
        assert cmd[0] == "mkvmerge"
        assert cmd[1] == "-o"
        assert cmd[2] == "/out.mkv"

    def test_single_source_no_filter_flags_when_all_enabled(self):
        """Quand toutes les pistes d'une source sont activées, pas de flags de filtrage."""
        tracks = [
            _track(0, "video", file_id="id0"),
            _track(1, "audio", file_id="id0"),
        ]
        src = _source(Path("/a.mkv"), 0, tracks)
        cfg = RemuxConfig(
            sources=[src], output=Path("/out.mkv"),
            track_order=[(0, 0), (0, 1)],
        )
        cmd = self._cmd(cfg)
        assert "--no-video" not in cmd
        assert "--no-audio" not in cmd
        assert "--video-tracks" not in cmd
        assert "--audio-tracks" not in cmd

    def test_single_source_no_video_when_all_video_disabled(self):
        tracks = [_track(0, "video", file_id="id0"), _track(1, "audio", file_id="id0")]
        src = _source(Path("/a.mkv"), 0, tracks)
        cfg = RemuxConfig(
            sources=[src], output=Path("/out.mkv"),
            track_order=[(0, 1)],  # vidéo exclue
        )
        cmd = self._cmd(cfg)
        assert "--no-video" in cmd

    def test_single_source_video_tracks_flag_for_partial_selection(self):
        tracks = [
            _track(0, "video", file_id="id0"),
            _track(2, "video", file_id="id0"),
        ]
        src = _source(Path("/a.mkv"), 0, tracks)
        cfg = RemuxConfig(
            sources=[src], output=Path("/out.mkv"),
            track_order=[(0, 0)],  # seulement TID 0
        )
        cmd = self._cmd(cfg)
        assert "--video-tracks" in cmd
        idx = cmd.index("--video-tracks")
        assert "0" in cmd[idx + 1]
        assert "2" not in cmd[idx + 1]

    def test_single_source_no_audio_when_all_audio_disabled(self):
        tracks = [_track(0, "video", file_id="id0"), _track(1, "audio", file_id="id0")]
        src = _source(Path("/a.mkv"), 0, tracks)
        cfg = RemuxConfig(
            sources=[src], output=Path("/out.mkv"),
            track_order=[(0, 0)],  # audio exclue
        )
        cmd = self._cmd(cfg)
        assert "--no-audio" in cmd

    def test_single_source_audio_tracks_flag_for_partial_selection(self):
        tracks = [
            _track(1, "audio", file_id="id0"),
            _track(2, "audio", file_id="id0"),
        ]
        src = _source(Path("/a.mkv"), 0, tracks)
        cfg = RemuxConfig(
            sources=[src], output=Path("/out.mkv"),
            track_order=[(0, 1)],  # seulement TID 1
        )
        cmd = self._cmd(cfg)
        assert "--audio-tracks" in cmd

    def test_single_source_no_subtitles_when_all_disabled(self):
        tracks = [_track(3, "subtitle", file_id="id0")]
        src = _source(Path("/a.mkv"), 0, tracks)
        cfg = RemuxConfig(
            sources=[src], output=Path("/out.mkv"),
            track_order=[],
        )
        cmd = self._cmd(cfg)
        assert "--no-subtitles" in cmd

    def test_subtitle_tracks_flag_for_partial_selection(self):
        tracks = [_track(3, "subtitle", file_id="id0"), _track(4, "subtitle", file_id="id0")]
        src = _source(Path("/a.mkv"), 0, tracks)
        cfg = RemuxConfig(
            sources=[src], output=Path("/out.mkv"),
            track_order=[(0, 3)],
        )
        cmd = self._cmd(cfg)
        assert "--subtitle-tracks" in cmd

    # --- Source sans piste d'un type → pas de flag --no-xxx ---

    def test_no_video_flag_not_emitted_for_audio_only_source(self):
        """Bug-guard : une source sans vidéo ne doit pas avoir --no-video."""
        tracks = [_track(1, "audio", file_id="id0")]
        src = _source(Path("/audio_only.mkv"), 0, tracks)
        cfg = RemuxConfig(
            sources=[src], output=Path("/out.mkv"),
            track_order=[(0, 1)],
        )
        cmd = self._cmd(cfg)
        assert "--no-video" not in cmd

    def test_no_audio_flag_not_emitted_for_video_only_source(self):
        tracks = [_track(0, "video", file_id="id0")]
        src = _source(Path("/video_only.mkv"), 0, tracks)
        cfg = RemuxConfig(
            sources=[src], output=Path("/out.mkv"),
            track_order=[(0, 0)],
        )
        cmd = self._cmd(cfg)
        assert "--no-audio" not in cmd

    # --- Options conteneur ---

    def test_no_chapters_flag(self):
        tracks = [_track(0, "video", file_id="id0")]
        src = _source(Path("/a.mkv"), 0, tracks)
        cfg = RemuxConfig(
            sources=[src], output=Path("/out.mkv"),
            track_order=[(0, 0)], keep_chapters=False,
        )
        cmd = self._cmd(cfg)
        assert "--no-chapters" in cmd

    def test_keep_chapters_by_default(self):
        tracks = [_track(0, "video", file_id="id0")]
        src = _source(Path("/a.mkv"), 0, tracks)
        cfg = RemuxConfig(
            sources=[src], output=Path("/out.mkv"),
            track_order=[(0, 0)], keep_chapters=True,
        )
        cmd = self._cmd(cfg)
        assert "--no-chapters" not in cmd

    def test_no_attachments_flag(self):
        tracks = [_track(0, "video", file_id="id0")]
        src = SourceInput(
            path=Path("/a.mkv"), file_index=0, tracks=tracks,
            attachment_count=2, selected_attachments=[],
        )
        cfg = RemuxConfig(
            sources=[src], output=Path("/out.mkv"),
            track_order=[(0, 0)],
        )
        cmd = self._cmd(cfg)
        assert "--no-attachments" in cmd

    # --- Métadonnées de pistes ---

    def test_track_name_emitted_when_title_modified(self):
        t = _track(1, "audio", file_id="id0", title="Français", orig_title="")
        src = _source(Path("/a.mkv"), 0, [t])
        cfg = RemuxConfig(
            sources=[src], output=Path("/out.mkv"),
            track_order=[(0, 1)],
        )
        cmd = self._cmd(cfg)
        assert "--track-name" in cmd
        idx = cmd.index("--track-name")
        assert cmd[idx + 1] == "1:Français"

    def test_track_name_not_emitted_when_title_unchanged(self):
        t = _track(1, "audio", file_id="id0", title="EAC-3", orig_title="EAC-3")
        src = _source(Path("/a.mkv"), 0, [t])
        cfg = RemuxConfig(
            sources=[src], output=Path("/out.mkv"),
            track_order=[(0, 1)],
        )
        cmd = self._cmd(cfg)
        assert "--track-name" not in cmd

    def test_language_emitted_when_modified(self):
        t = _track(1, "audio", file_id="id0", language="en", orig_language="fr")
        src = _source(Path("/a.mkv"), 0, [t])
        cfg = RemuxConfig(
            sources=[src], output=Path("/out.mkv"),
            track_order=[(0, 1)],
        )
        cmd = self._cmd(cfg)
        assert "--language-ietf" in cmd
        idx = cmd.index("--language-ietf")
        assert cmd[idx + 1] == "1:en"

    def test_language_not_emitted_when_unchanged(self):
        t = _track(1, "audio", file_id="id0", language="fr", orig_language="fr")
        src = _source(Path("/a.mkv"), 0, [t])
        cfg = RemuxConfig(
            sources=[src], output=Path("/out.mkv"),
            track_order=[(0, 1)],
        )
        cmd = self._cmd(cfg)
        assert "--language-ietf" not in cmd

    def test_language_cleared_emits_und(self):
        t = _track(1, "audio", file_id="id0", language="", orig_language="fr")
        src = _source(Path("/a.mkv"), 0, [t])
        cfg = RemuxConfig(
            sources=[src], output=Path("/out.mkv"),
            track_order=[(0, 1)],
        )
        cmd = self._cmd(cfg)
        assert "--language-ietf" in cmd
        idx = cmd.index("--language-ietf")
        assert cmd[idx + 1] == "1:und"

    def test_metadata_not_emitted_for_disabled_tracks(self):
        """--track-name et --language-ietf ne sont pas émis pour les pistes désactivées."""
        t = _track(1, "audio", file_id="id0",
                   title="Modified", orig_title="",
                   language="en", orig_language="fr")
        src = _source(Path("/a.mkv"), 0, [t])
        cfg = RemuxConfig(
            sources=[src], output=Path("/out.mkv"),
            track_order=[],  # TID 1 absent → désactivé
        )
        cmd = self._cmd(cfg)
        assert "--track-name" not in cmd
        assert "--language-ietf" not in cmd

    # --- Multi-source ---

    def test_two_sources_track_order_format(self):
        t0 = _track(0, "video", file_id="A")
        t1 = _track(1, "audio", file_id="B")
        src0 = _source(Path("/a.mkv"), 0, [t0])
        src1 = _source(Path("/b.mkv"), 1, [t1])
        cfg = RemuxConfig(
            sources=[src0, src1], output=Path("/out.mkv"),
            track_order=[(0, 0), (1, 1)],
        )
        cmd = self._cmd(cfg)
        assert "--track-order" in cmd
        idx = cmd.index("--track-order")
        assert cmd[idx + 1] == "0:0,1:1"

    def test_two_sources_both_paths_present(self):
        t0 = _track(0, "video", file_id="A")
        t1 = _track(1, "audio", file_id="B")
        src0 = _source(Path("/a.mkv"), 0, [t0])
        src1 = _source(Path("/b.mkv"), 1, [t1])
        cfg = RemuxConfig(
            sources=[src0, src1], output=Path("/out.mkv"),
            track_order=[(0, 0), (1, 1)],
        )
        cmd = self._cmd(cfg)
        assert "/a.mkv" in cmd
        assert "/b.mkv" in cmd

    def test_two_sources_independent_per_source_flags(self):
        """Source 0 : video seule désactivée. Source 1 : audio seul activé."""
        t0v = _track(0, "video", file_id="A")
        t0a = _track(1, "audio", file_id="A")
        t1a = _track(0, "audio", file_id="B")
        src0 = _source(Path("/a.mkv"), 0, [t0v, t0a])
        src1 = _source(Path("/b.mkv"), 1, [t1a])
        # Track order : audio de a.mkv + audio de b.mkv (vidéo de a exclue)
        cfg = RemuxConfig(
            sources=[src0, src1], output=Path("/out.mkv"),
            track_order=[(0, 1), (1, 0)],
        )
        cmd = self._cmd(cfg)
        # Source 0 doit avoir --no-video (car TID 0 absent de track_order pour fi=0)
        assert "--no-video" in cmd
        # Source 1 n'a pas de vidéo, donc --no-video NE DOIT PAS être dupliqué
        # (vérifie qu'il n'y en a qu'un seul)
        assert cmd.count("--no-video") == 1

    def test_track_order_precedes_source_paths(self):
        """--track-order doit apparaître avant les chemins de source."""
        t0 = _track(0, "video", file_id="A")
        src0 = _source(Path("/a.mkv"), 0, [t0])
        cfg = RemuxConfig(
            sources=[src0], output=Path("/out.mkv"),
            track_order=[(0, 0)],
        )
        cmd = self._cmd(cfg)
        to_idx  = cmd.index("--track-order")
        src_idx = cmd.index("/a.mkv")
        assert to_idx < src_idx

    def test_empty_track_order_no_track_order_flag(self):
        tracks = [_track(0, "video", file_id="id0")]
        src = _source(Path("/a.mkv"), 0, tracks)
        cfg = RemuxConfig(
            sources=[src], output=Path("/out.mkv"),
            track_order=[],
        )
        cmd = self._cmd(cfg)
        assert "--track-order" not in cmd

    def test_source_path_appears_in_command(self):
        tracks = [_track(0, "video", file_id="id0")]
        src = _source(Path("/my/film.mkv"), 0, tracks)
        cfg = RemuxConfig(
            sources=[src], output=Path("/out.mkv"),
            track_order=[(0, 0)],
        )
        cmd = self._cmd(cfg)
        assert "/my/film.mkv" in cmd


# ===========================================================================
# RemuxWorkflow.preview_command
# ===========================================================================

class TestPreviewCommand:

    def setup_method(self):
        self.wf = _workflow()

    def test_preview_has_backslash_continuation(self):
        tracks = [_track(0, "video", file_id="id0")]
        src = _source(Path("/a.mkv"), 0, tracks)
        cfg = RemuxConfig(
            sources=[src], output=Path("/out.mkv"),
            track_order=[(0, 0)],
        )
        preview = self.wf.preview_command(cfg)
        assert " \\\n" in preview

    def test_preview_starts_with_mkvmerge(self):
        tracks = [_track(0, "video", file_id="id0")]
        src = _source(Path("/a.mkv"), 0, tracks)
        cfg = RemuxConfig(
            sources=[src], output=Path("/out.mkv"),
            track_order=[(0, 0)],
        )
        preview = self.wf.preview_command(cfg)
        assert preview.startswith("mkvmerge")

    def test_preview_output_on_separate_line(self):
        tracks = [_track(0, "video", file_id="id0")]
        src = _source(Path("/a.mkv"), 0, tracks)
        cfg = RemuxConfig(
            sources=[src], output=Path("/out.mkv"),
            track_order=[(0, 0)],
        )
        preview = self.wf.preview_command(cfg)
        lines = preview.split(" \\\n")
        assert any("-o" in line and "/out.mkv" in line for line in lines)

    def test_preview_flag_with_value_on_same_line(self):
        """--track-order et sa valeur doivent être sur la même ligne."""
        tracks = [_track(0, "video", file_id="id0")]
        src = _source(Path("/a.mkv"), 0, tracks)
        cfg = RemuxConfig(
            sources=[src], output=Path("/out.mkv"),
            track_order=[(0, 0)],
        )
        preview = self.wf.preview_command(cfg)
        assert any("--track-order" in line and "0:0" in line
                   for line in preview.split(" \\\n"))


# ===========================================================================
# RemuxWorkflow.validate
# ===========================================================================

class TestValidate:

    def setup_method(self):
        self.wf = _workflow()

    def test_empty_sources_returns_one_error(self):
        cfg = RemuxConfig(
            sources=[], output=Path("/out.mkv"), track_order=[],
        )
        errors = self.wf.validate(cfg)
        assert len(errors) == 1
        assert "source" in errors[0].lower()

    def test_source_file_not_found(self, tmp_path):
        missing = tmp_path / "absent.mkv"
        tracks = [_track(0, "video", file_id="id0")]
        src = _source(missing, 0, tracks)
        cfg = RemuxConfig(
            sources=[src], output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
        )
        errors = self.wf.validate(cfg)
        assert any("introuvable" in e for e in errors)

    def test_source_equals_output_error(self, tmp_path):
        f = tmp_path / "film.mkv"
        f.touch()
        tracks = [_track(0, "video", file_id="id0")]
        src = _source(f, 0, tracks)
        cfg = RemuxConfig(
            sources=[src], output=f,   # même chemin !
            track_order=[(0, 0)],
        )
        errors = self.wf.validate(cfg)
        assert any("différent" in e for e in errors)

    def test_output_dir_not_exists(self, tmp_path):
        f = tmp_path / "film.mkv"
        f.touch()
        tracks = [_track(0, "video", file_id="id0")]
        src = _source(f, 0, tracks)
        cfg = RemuxConfig(
            sources=[src],
            output=tmp_path / "inexistant_subdir" / "out.mkv",
            track_order=[(0, 0)],
        )
        errors = self.wf.validate(cfg)
        assert any("inexistant" in e or "sortie" in e.lower() for e in errors)

    def test_empty_track_order_error(self, tmp_path):
        f = tmp_path / "film.mkv"
        f.touch()
        tracks = [_track(0, "video", file_id="id0")]
        src = _source(f, 0, tracks)
        cfg = RemuxConfig(
            sources=[src], output=tmp_path / "out.mkv",
            track_order=[],
        )
        errors = self.wf.validate(cfg)
        assert any("piste" in e.lower() for e in errors)

    def test_valid_config_returns_empty_list(self, tmp_path):
        f = tmp_path / "film.mkv"
        f.touch()
        tracks = [_track(0, "video", file_id="id0")]
        src = _source(f, 0, tracks)
        cfg = RemuxConfig(
            sources=[src], output=tmp_path / "out.mkv",
            track_order=[(0, 0)],
        )
        errors = self.wf.validate(cfg)
        assert errors == []

    def test_multiple_errors_all_returned(self, tmp_path):
        """Fichier manquant + track_order vide → au moins 2 erreurs."""
        missing = tmp_path / "absent.mkv"
        tracks = [_track(0, "video", file_id="id0")]
        src = _source(missing, 0, tracks)
        cfg = RemuxConfig(
            sources=[src], output=tmp_path / "out.mkv",
            track_order=[],
        )
        errors = self.wf.validate(cfg)
        assert len(errors) >= 2


# ===========================================================================
# _TrackTable (widget Qt — nécessite QApplication)
# ===========================================================================

@pytest.fixture
def table(qt_app):
    """_TrackTable instanciée pour les tests (sans parent)."""
    t = _TrackTable()
    yield t
    t.close()


_COLOR_A = "#4f6ef7"   # couleur de test pour file_id "fid" / "A"
_COLOR_B = "#e05050"   # couleur de test pour file_id "B"


def _fill_table(
    table: _TrackTable,
    n: int = 3,
    file_id: str = "fid",
    track_type: str = "audio",
    color: str = _COLOR_A,
) -> list[TrackEntry]:
    """Ajoute n pistes du type donné dans la table et retourne les TrackEntry."""
    tracks = [
        _track(i, track_type, file_id=file_id, language="fra", title=f"Piste {i}")
        for i in range(n)
    ]
    table.append_tracks(color, tracks)
    return tracks


class TestTrackTable:

    def test_append_tracks_adds_correct_row_count(self, table):
        _fill_table(table, 3)
        assert table.rowCount() == 3

    def test_append_tracks_preserves_existing_rows(self, table):
        _fill_table(table, 2, file_id="A")
        _fill_table(table, 3, file_id="B")
        assert table.rowCount() == 5

    def test_remove_tracks_by_file_id_removes_only_target(self, table):
        _fill_table(table, 2, file_id="A")
        _fill_table(table, 3, file_id="B")
        table.remove_tracks_by_file_id("A")
        assert table.rowCount() == 3

    def test_remove_tracks_by_file_id_leaves_other_ids(self, table):
        _fill_table(table, 2, file_id="A")
        b_tracks = _fill_table(table, 1, file_id="B")
        table.remove_tracks_by_file_id("A")
        remaining = table.current_tracks()
        assert all(t.file_id == "B" for t in remaining)

    def test_remove_tracks_by_unknown_file_id_is_noop(self, table):
        _fill_table(table, 3, file_id="A")
        table.remove_tracks_by_file_id("nonexistent")
        assert table.rowCount() == 3

    def test_clear_all_empties_table(self, table):
        _fill_table(table, 4)
        table.clear_all()
        assert table.rowCount() == 0

    def test_current_tracks_returns_all_tracks(self, table):
        _fill_table(table, 3)
        tracks = table.current_tracks()
        assert len(tracks) == 3

    def test_current_tracks_preserves_file_id(self, table):
        _fill_table(table, 2, file_id="uuid-test")
        tracks = table.current_tracks()
        assert all(t.file_id == "uuid-test" for t in tracks)

    def test_current_tracks_syncs_enabled_state(self, table):
        """Décocher une case → enabled False dans current_tracks()."""
        _fill_table(table, 1)
        # Décoche la case à cocher (col COL_CHECK = 1)
        item = table.item(0, _TrackTable.COL_CHECK)
        item.setCheckState(Qt.CheckState.Unchecked)
        tracks = table.current_tracks()
        assert tracks[0].enabled is False

    def test_current_tracks_syncs_language(self, table):
        """Modifier la cellule Langue → language mis à jour dans current_tracks()."""
        _fill_table(table, 1)
        lang_item = table.item(0, _TrackTable.COL_LANG)
        lang_item.setText("zh-CN")
        tracks = table.current_tracks()
        assert tracks[0].language == "zh-CN"

    def test_current_tracks_syncs_title(self, table):
        _fill_table(table, 1)
        title_item = table.item(0, _TrackTable.COL_TITLE)
        title_item.setText("Nouveau titre")
        tracks = table.current_tracks()
        assert tracks[0].title == "Nouveau titre"

    def test_set_all_enabled_checks_all(self, table):
        """set_all_enabled(True) → toutes les cases cochées."""
        tracks = _fill_table(table, 3)
        # Décoche quelques cases d'abord
        table.item(0, _TrackTable.COL_CHECK).setCheckState(Qt.CheckState.Unchecked)
        table.item(2, _TrackTable.COL_CHECK).setCheckState(Qt.CheckState.Unchecked)

        table.set_all_enabled(True)
        result = table.current_tracks()
        assert all(t.enabled for t in result)

    def test_set_all_enabled_unchecks_all(self, table):
        """set_all_enabled(False) → toutes les cases décochées."""
        _fill_table(table, 3)
        table.set_all_enabled(False)
        result = table.current_tracks()
        assert all(not t.enabled for t in result)

    def test_source_column_shows_block_char(self, table):
        """La colonne Source affiche le caractère bloc '█' (carré coloré)."""
        tracks = [_track(0, "audio", file_id="fid")]
        table.append_tracks(_COLOR_A, tracks)
        src_item = table.item(0, _TrackTable.COL_SOURCE)
        assert src_item is not None
        assert src_item.text() == "█"

    def test_source_column_stores_color_in_user_role(self, table):
        """La couleur doit être stockée dans UserRole pour reconstruction drag-drop."""
        tracks = [_track(0, "audio", file_id="fid")]
        table.append_tracks(_COLOR_A, tracks)
        src_item = table.item(0, _TrackTable.COL_SOURCE)
        assert src_item.data(Qt.ItemDataRole.UserRole) == _COLOR_A

    def test_source_column_foreground_matches_color(self, table):
        """La couleur de premier plan de la cellule correspond à source_color."""
        tracks = [_track(0, "audio", file_id="fid")]
        table.append_tracks(_COLOR_A, tracks)
        src_item = table.item(0, _TrackTable.COL_SOURCE)
        assert src_item.foreground().color() == QColor(_COLOR_A)

    def test_type_column_shows_correct_label(self, table):
        tracks = [_track(0, "video", file_id="fid")]
        table.append_tracks(_COLOR_A, tracks)
        type_item = table.item(0, _TrackTable.COL_TYPE)
        assert type_item.text() == "V"

    def test_append_tracks_default_enabled(self, table):
        """Les pistes ajoutées sont cochées par défaut."""
        _fill_table(table, 1)
        item = table.item(0, _TrackTable.COL_CHECK)
        assert item.checkState() == Qt.CheckState.Checked


# ===========================================================================
# _pick_file_color
# ===========================================================================

class TestPickFileColor:

    def test_returns_hex_string(self):
        c = _pick_file_color(0)
        assert c.startswith("#")
        assert len(c) == 7

    def test_deterministic(self):
        assert _pick_file_color(3) == _pick_file_color(3)

    def test_different_indices_different_colors(self):
        colors = [_pick_file_color(i) for i in range(10)]
        assert len(set(colors)) == 10

    def test_not_near_black(self):
        """Luminosité > 0.1 pour tous les indices 0–19."""
        for i in range(20):
            c = _pick_file_color(i)
            r = int(c[1:3], 16) / 255
            g = int(c[3:5], 16) / 255
            b = int(c[5:7], 16) / 255
            _, l, _ = colorsys.rgb_to_hls(r, g, b)
            assert l > 0.1, f"index {i}: couleur {c} trop sombre (l={l:.2f})"

    def test_not_near_white(self):
        """Luminosité < 0.9 pour tous les indices 0–19."""
        for i in range(20):
            c = _pick_file_color(i)
            r = int(c[1:3], 16) / 255
            g = int(c[3:5], 16) / 255
            b = int(c[5:7], 16) / 255
            _, l, _ = colorsys.rgb_to_hls(r, g, b)
            assert l < 0.9, f"index {i}: couleur {c} trop claire (l={l:.2f})"

    def test_golden_angle_spread(self):
        """Les 8 premières couleurs couvrent le cercle chromatique (plage > 200°)."""
        hues = []
        for i in range(8):
            c = _pick_file_color(i)
            r = int(c[1:3], 16) / 255
            g = int(c[3:5], 16) / 255
            b = int(c[5:7], 16) / 255
            h, _, _ = colorsys.rgb_to_hls(r, g, b)
            hues.append(h * 360)
        span = max(hues) - min(hues)
        assert span > 200, f"plage de teinte insuffisante : {span:.1f}°"


# ===========================================================================
# SourceFile.color
# ===========================================================================

class TestSourceFileColor:

    def test_color_default_empty(self):
        sf = SourceFile(id="x", path=Path("/tmp/film.mkv"))
        assert sf.color == ""

    def test_color_can_be_set(self):
        sf = SourceFile(id="x", path=Path("/tmp/film.mkv"), color="#aabbcc")
        assert sf.color == "#aabbcc"


# ===========================================================================
# _TrackTable — ordre des colonnes
# ===========================================================================

class TestTrackTableColumnOrder:

    def test_col_lang_is_4(self):
        assert _TrackTable.COL_LANG == 4

    def test_col_title_is_5(self):
        assert _TrackTable.COL_TITLE == 5

    def test_col_info_is_6(self):
        assert _TrackTable.COL_INFO == 6

    def test_header_langue_at_index_4(self):
        assert _TrackTable._HEADERS[4] == "Langue"

    def test_header_titre_at_index_5(self):
        assert _TrackTable._HEADERS[5] == "Titre"

    def test_header_info_at_index_6(self):
        assert _TrackTable._HEADERS[6] == "Info"


# ===========================================================================
# _TrackTable — tri par type (V → A → S)
# ===========================================================================

class TestTrackTableSort:

    def test_video_inserted_before_audio(self, table):
        audio = _track(1, "audio", file_id="f1")
        video = _track(0, "video", file_id="f1")
        table.append_tracks(_COLOR_A, [audio])
        table.append_tracks(_COLOR_A, [video])
        tracks = table.current_tracks()
        types = [t.track_type for t in tracks]
        assert types == ["video", "audio"]

    def test_audio_inserted_before_subtitle(self, table):
        sub = _track(2, "subtitle", file_id="f1")
        audio = _track(1, "audio", file_id="f1")
        table.append_tracks(_COLOR_A, [sub])
        table.append_tracks(_COLOR_A, [audio])
        tracks = table.current_tracks()
        types = [t.track_type for t in tracks]
        assert types == ["audio", "subtitle"]

    def test_full_order_v_a_s(self, table):
        entries = [
            _track(2, "subtitle", file_id="f1"),
            _track(1, "audio", file_id="f1"),
            _track(0, "video", file_id="f1"),
        ]
        table.append_tracks(_COLOR_A, entries)
        types = [t.track_type for t in table.current_tracks()]
        assert types == ["video", "audio", "subtitle"]

    def test_multifile_grouped_by_type(self, table):
        """V-f1, V-f2, A-f1, A-f2 : les vidéos précèdent tous les audios."""
        v1 = _track(0, "video", file_id="f1")
        a1 = _track(1, "audio", file_id="f1")
        v2 = _track(0, "video", file_id="f2")
        a2 = _track(1, "audio", file_id="f2")
        table.append_tracks(_COLOR_A, [v1, a1])
        table.append_tracks(_COLOR_B, [v2, a2])
        types = [t.track_type for t in table.current_tracks()]
        assert types == ["video", "video", "audio", "audio"]

    def test_find_insert_position_video_empty_table(self, table):
        assert table._find_insert_position(0) == 0   # order 0 = video

    def test_find_insert_position_audio_after_video(self, table):
        _fill_table(table, 2, track_type="video")
        assert table._find_insert_position(1) == 2   # order 1 = audio

    def test_find_insert_position_subtitle_after_audio(self, table):
        _fill_table(table, 1, track_type="video")
        _fill_table(table, 2, track_type="audio")
        assert table._find_insert_position(2) == 3   # order 2 = subtitle


# ===========================================================================
# _TrackTable — filtre "sélectionnées seulement"
# ===========================================================================

class TestTrackTableFilter:

    def test_filter_off_all_rows_visible(self, table):
        _fill_table(table, 3)
        table.set_filter_selected(False)
        hidden = [table.isRowHidden(r) for r in range(table.rowCount())]
        assert not any(hidden)

    def test_filter_on_hides_unchecked_rows(self, table):
        _fill_table(table, 3)
        table.item(1, _TrackTable.COL_CHECK).setCheckState(Qt.CheckState.Unchecked)
        table.set_filter_selected(True)
        assert table.isRowHidden(1)
        assert not table.isRowHidden(0)
        assert not table.isRowHidden(2)

    def test_filter_on_checked_rows_remain_visible(self, table):
        _fill_table(table, 3)
        table.set_filter_selected(True)
        hidden = [table.isRowHidden(r) for r in range(table.rowCount())]
        assert not any(hidden)

    def test_disable_filter_restores_hidden_rows(self, table):
        _fill_table(table, 3)
        table.item(0, _TrackTable.COL_CHECK).setCheckState(Qt.CheckState.Unchecked)
        table.set_filter_selected(True)
        assert table.isRowHidden(0)
        table.set_filter_selected(False)
        assert not table.isRowHidden(0)

    def test_refresh_filter_hides_newly_unchecked(self, table):
        _fill_table(table, 2)
        table.set_filter_selected(True)
        table.item(0, _TrackTable.COL_CHECK).setCheckState(Qt.CheckState.Unchecked)
        table.refresh_filter()
        assert table.isRowHidden(0)
        assert not table.isRowHidden(1)


# ===========================================================================
# _TrackTable — ajustement de hauteur (_adjust_height)
# ===========================================================================

class TestTrackTableHeight:

    def test_empty_table_has_positive_height(self, table):
        assert table.height() > 0

    def test_adding_rows_increases_height(self, table):
        h0 = table.height()
        _fill_table(table, 5)
        assert table.height() > h0

    def test_height_capped_at_15_rows(self, table):
        _fill_table(table, 15)
        h15 = table.height()
        _fill_table(table, 5)   # 20 rows total
        h20 = table.height()
        assert h15 == h20, "La hauteur doit être plafonnée à 15 lignes visibles"

    def test_removing_rows_reduces_height(self, table):
        _fill_table(table, 5, file_id="A")
        h5 = table.height()
        table.remove_tracks_by_file_id("A")
        assert table.height() < h5

    def test_clear_all_resets_to_base_height(self, table):
        h0 = table.height()
        _fill_table(table, 5)
        table.clear_all()
        assert table.height() == h0


# ===========================================================================
# _FileListWidget — taille automatique
# ===========================================================================

@pytest.fixture
def file_list(qt_app):
    w = _FileListWidget()
    yield w
    w.close()


def _make_sf(idx: int = 0, color: str = "#ff0000") -> SourceFile:
    return SourceFile(
        id=f"id-{idx}",
        path=Path(f"/tmp/film{idx}.mkv"),
        color=color,
    )


class TestFileListWidgetSize:
    """
    Vérifie la contrainte de hauteur fixe posée par setFixedHeight.

    On utilise maximumHeight() (mis à jour immédiatement par setFixedHeight)
    plutôt que height() (qui n'est recalculé qu'après un cycle de layout).
    """

    def test_initial_height_is_placeholder_plus_bar(self, file_list):
        expected = _FILE_PH_H + _FILE_BAR_H
        assert file_list.maximumHeight() == expected

    def test_one_file_height(self, file_list):
        file_list.add_file(_make_sf(0))
        expected = 1 * _FILE_ROW_H + _FILE_BAR_H
        assert file_list.maximumHeight() == expected

    def test_three_files_height(self, file_list):
        for i in range(3):
            file_list.add_file(_make_sf(i))
        expected = 3 * _FILE_ROW_H + _FILE_BAR_H
        assert file_list.maximumHeight() == expected

    def test_remove_reduces_height(self, file_list):
        sf0 = _make_sf(0)
        sf1 = _make_sf(1)
        file_list.add_file(sf0)
        file_list.add_file(sf1)
        file_list.remove_file(sf0.id)
        expected = 1 * _FILE_ROW_H + _FILE_BAR_H
        assert file_list.maximumHeight() == expected

    def test_remove_last_restores_placeholder_height(self, file_list):
        sf = _make_sf(0)
        file_list.add_file(sf)
        file_list.remove_file(sf.id)
        expected = _FILE_PH_H + _FILE_BAR_H
        assert file_list.maximumHeight() == expected

    def test_file_count_tracks_additions(self, file_list):
        assert file_list.file_count() == 0
        file_list.add_file(_make_sf(0))
        file_list.add_file(_make_sf(1))
        assert file_list.file_count() == 2

    def test_file_count_tracks_removals(self, file_list):
        sf = _make_sf(0)
        file_list.add_file(sf)
        file_list.remove_file(sf.id)
        assert file_list.file_count() == 0


# ===========================================================================
# _TrackTable.update_audio_meta (synchronisation depuis EncodePanel)
# ===========================================================================

class TestTrackTableUpdateAudioMeta:

    def test_updates_language_cell(self, table):
        """update_audio_meta met à jour la cellule Langue."""
        tracks = [_track(5, "audio", file_id="fid", language="fra")]
        table.append_tracks(_COLOR_A, tracks)
        table.update_audio_meta("fid", 5, "jpn", "")
        lang_item = table.item(0, _TrackTable.COL_LANG)
        assert lang_item.text() == "jpn"

    def test_updates_title_cell(self, table):
        """update_audio_meta met à jour la cellule Titre."""
        tracks = [_track(5, "audio", file_id="fid", title="Original")]
        table.append_tracks(_COLOR_A, tracks)
        table.update_audio_meta("fid", 5, "fra", "Nouveau titre")
        title_item = table.item(0, _TrackTable.COL_TITLE)
        assert title_item.text() == "Nouveau titre"

    def test_updates_track_entry_language(self, table):
        """L'objet TrackEntry est aussi mis à jour (pas seulement la cellule UI)."""
        tracks = [_track(5, "audio", file_id="fid", language="fra")]
        table.append_tracks(_COLOR_A, tracks)
        table.update_audio_meta("fid", 5, "eng", "")
        result = table.current_tracks()
        assert result[0].language == "eng"

    def test_updates_track_entry_title(self, table):
        tracks = [_track(5, "audio", file_id="fid", title="Avant")]
        table.append_tracks(_COLOR_A, tracks)
        table.update_audio_meta("fid", 5, "fra", "Après")
        result = table.current_tracks()
        assert result[0].title == "Après"

    def test_no_item_changed_signal_emitted(self, table):
        """update_audio_meta doit bloquer les signaux pour éviter une boucle de sync."""
        tracks = [_track(5, "audio", file_id="fid")]
        table.append_tracks(_COLOR_A, tracks)
        emitted: list = []
        table.itemChanged.connect(lambda _: emitted.append(True))
        table.update_audio_meta("fid", 5, "jpn", "X")
        assert emitted == []

    def test_does_not_affect_other_rows(self, table):
        """Seule la ligne ciblée est modifiée, les autres restent intactes."""
        t0 = _track(1, "audio", file_id="fid", language="fra", title="Piste 1")
        t1 = _track(2, "audio", file_id="fid", language="eng", title="Piste 2")
        table.append_tracks(_COLOR_A, [t0, t1])
        table.update_audio_meta("fid", 1, "jpn", "Modifiée")
        result = table.current_tracks()
        other = next(t for t in result if t.mkv_tid == 2)
        assert other.language == "eng"
        assert other.title == "Piste 2"

    def test_unknown_file_id_is_noop(self, table):
        """file_id inconnu → aucune modification."""
        tracks = [_track(1, "audio", file_id="fid", language="fra")]
        table.append_tracks(_COLOR_A, tracks)
        table.update_audio_meta("unknown", 1, "jpn", "X")
        result = table.current_tracks()
        assert result[0].language == "fra"

    def test_unknown_mkv_tid_is_noop(self, table):
        """mkv_tid inconnu → aucune modification."""
        tracks = [_track(1, "audio", file_id="fid", language="fra")]
        table.append_tracks(_COLOR_A, tracks)
        table.update_audio_meta("fid", 99, "jpn", "X")
        result = table.current_tracks()
        assert result[0].language == "fra"


# ===========================================================================
# _AttachmentItemWidget — case cochée par défaut (tags et attachements)
# ===========================================================================

class TestAttachmentItemWidgetDefaultChecked:

    def test_attachment_checked_by_default(self, qt_app):
        """Un attachement normal (is_tag=False) est coché par défaut."""
        w = _AttachmentItemWidget(file_id="fid")
        assert w.enabled is True
        w.close()

    def test_tag_checked_by_default(self, qt_app):
        """Un widget de balises (is_tag=True) est coché par défaut (nouveau comportement)."""
        tags = {"COLLECTION": "MyShow", "SEASON": "1", "EPISODE": "2"}
        w = _AttachmentItemWidget(file_id="fid", is_tag=True, tags=tags)
        assert w.enabled is True
        assert w.tag_count == 3
        w.close()


# ===========================================================================
# RemuxWorkflow.build_command — balise Title du segment (file_title)
# ===========================================================================

class TestBuildCommandFileTitle:
    """
    Vérifie que --title est toujours émis dans build_command, avec la valeur
    exacte fournie dans RemuxConfig.file_title (chaîne vide incluse).
    """

    def setup_method(self):
        self.wf = _workflow()

    def _simple_cfg(self, title: str) -> RemuxConfig:
        t = _track(0, "video", file_id="id0")
        src = _source(Path("/a.mkv"), 0, [t])
        return RemuxConfig(
            sources=[src], output=Path("/out.mkv"),
            track_order=[(0, 0)],
            file_title=title,
        )

    def _cmd(self, cfg: RemuxConfig) -> list[str]:
        return self.wf.build_command(cfg)

    def test_title_flag_present(self):
        """--title est toujours présent dans la commande."""
        cmd = self._cmd(self._simple_cfg("Mon Film"))
        assert "--title" in cmd

    def test_title_value_correct(self):
        """La valeur suivant --title correspond exactement à file_title."""
        cmd = self._cmd(self._simple_cfg("Mon Film"))
        idx = cmd.index("--title")
        assert cmd[idx + 1] == "Mon Film"

    def test_title_empty_string(self):
        """file_title='' → --title '' (vide, pour effacer un titre existant)."""
        cmd = self._cmd(self._simple_cfg(""))
        assert "--title" in cmd
        idx = cmd.index("--title")
        assert cmd[idx + 1] == ""

    def test_title_before_source_file(self):
        """--title doit précéder le chemin du fichier source dans la commande."""
        cmd = self._cmd(self._simple_cfg("Film"))
        assert cmd.index("--title") < cmd.index("/a.mkv")

    def test_title_after_output(self):
        """--title apparaît après -o OUTPUT, avant les sources."""
        cmd = self._cmd(self._simple_cfg("Film"))
        o_idx = cmd.index("-o")
        t_idx = cmd.index("--title")
        src_idx = cmd.index("/a.mkv")
        assert o_idx < t_idx < src_idx


# ===========================================================================
# RemuxWorkflow.build_command — pièces jointes manuelles (extra_attachments)
# ===========================================================================

class TestBuildCommandExtraAttachments:
    """
    Vérifie le comportement de --attach-file dans build_command :
      - --attach-file est émis pour chaque chemin dans extra_attachments
      - --attach-file précède tous les chemins de fichiers source
      - --attachment-name cover est émis pour les fichiers dont le stem est "cover"
      - --attachment-name n'est PAS émis pour les autres fichiers
      - Plusieurs attachements → autant de paires --attach-file
    """

    def setup_method(self):
        self.wf = _workflow()

    def _cfg(self, extras: list[Path]) -> RemuxConfig:
        t = _track(0, "video", file_id="id0")
        src = _source(Path("/a.mkv"), 0, [t])
        return RemuxConfig(
            sources=[src], output=Path("/out.mkv"),
            track_order=[(0, 0)],
            extra_attachments=extras,
        )

    def _cmd(self, cfg: RemuxConfig) -> list[str]:
        return self.wf.build_command(cfg)

    def test_attach_file_present(self):
        """--attach-file est émis pour un attachement manuel."""
        cmd = self._cmd(self._cfg([Path("/extra/poster.jpg")]))
        assert "--attach-file" in cmd

    def test_attach_file_path_correct(self):
        """La valeur suivant --attach-file est le chemin absolu du fichier."""
        cmd = self._cmd(self._cfg([Path("/extra/poster.jpg")]))
        idx = cmd.index("--attach-file")
        assert cmd[idx + 1] == "/extra/poster.jpg"

    def test_attach_file_before_source(self):
        """--attach-file doit précéder le chemin du fichier source (option globale)."""
        cmd = self._cmd(self._cfg([Path("/extra/poster.jpg")]))
        assert cmd.index("--attach-file") < cmd.index("/a.mkv")

    def test_multiple_attachments(self):
        """Plusieurs attachements → autant d'occurrences de --attach-file."""
        extras = [Path("/extra/poster.jpg"), Path("/extra/notes.txt")]
        cmd = self._cmd(self._cfg(extras))
        count = sum(1 for a in cmd if a == "--attach-file")
        assert count == 2

    def test_multiple_attachments_all_before_source(self):
        """Tous les --attach-file doivent précéder le fichier source."""
        extras = [Path("/extra/poster.jpg"), Path("/extra/notes.txt")]
        cmd = self._cmd(self._cfg(extras))
        src_idx = cmd.index("/a.mkv")
        attach_indices = [i for i, a in enumerate(cmd) if a == "--attach-file"]
        for ai in attach_indices:
            assert ai < src_idx, f"--attach-file à l'index {ai} suit la source à {src_idx}"

    def test_no_attach_file_when_empty(self):
        """extra_attachments=[] → aucun --attach-file dans la commande."""
        cmd = self._cmd(self._cfg([]))
        assert "--attach-file" not in cmd

    def test_cover_jpg_gets_attachment_name(self):
        """Fichier nommé cover.jpg → --attachment-name cover émis."""
        cmd = self._cmd(self._cfg([Path("/extra/cover.jpg")]))
        assert "--attachment-name" in cmd
        idx = cmd.index("--attachment-name")
        assert cmd[idx + 1] == "cover"

    def test_cover_png_gets_attachment_name(self):
        """Fichier nommé cover.png → --attachment-name cover émis."""
        cmd = self._cmd(self._cfg([Path("/extra/cover.png")]))
        assert "--attachment-name" in cmd
        idx = cmd.index("--attachment-name")
        assert cmd[idx + 1] == "cover"

    def test_cover_case_insensitive(self):
        """Fichier nommé COVER.JPG (majuscules) → --attachment-name cover émis."""
        cmd = self._cmd(self._cfg([Path("/extra/COVER.JPG")]))
        assert "--attachment-name" in cmd
        idx = cmd.index("--attachment-name")
        assert cmd[idx + 1] == "cover"

    def test_cover_attachment_name_before_attach_file(self):
        """--attachment-name doit précéder --attach-file pour le même fichier."""
        cmd = self._cmd(self._cfg([Path("/extra/cover.jpg")]))
        assert cmd.index("--attachment-name") < cmd.index("--attach-file")

    def test_non_cover_file_no_attachment_name(self):
        """Fichier non nommé cover → pas de --attachment-name."""
        cmd = self._cmd(self._cfg([Path("/extra/poster.jpg")]))
        assert "--attachment-name" not in cmd

    def test_mixed_cover_and_regular(self):
        """Mix cover + autre : --attachment-name uniquement pour cover."""
        extras = [Path("/extra/cover.jpg"), Path("/extra/notes.txt")]
        cmd = self._cmd(self._cfg(extras))
        assert "--attachment-name" in cmd
        idx = cmd.index("--attachment-name")
        assert cmd[idx + 1] == "cover"
        # Un seul --attachment-name
        assert sum(1 for a in cmd if a == "--attachment-name") == 1
