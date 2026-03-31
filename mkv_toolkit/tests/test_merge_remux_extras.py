"""
tests/test_merge_remux_extras.py — Tests unitaires pour MainWindow._merge_remux_extras.

Vérifie que les champs de l'EncodeConfig d'origine sont correctement préservés
(ou remplacés) lorsque _merge_remux_extras reconstruit un EncodeConfig enrichi.

Plan de couverture :
    file_title :
        - préservé quand il y a des pistes à fusionner
        - inchangé quand rien à fusionner (retour de l'objet original)

    extra_attachments :
        - préservés quand il y a des pistes à fusionner
        - inchangés quand rien à fusionner

    keep_chapters :
        - toujours synchronisé avec remux_cfg.keep_chapters (même si rien à fusionner)

    Retour sans reconstruction :
        - retourne encode_cfg inchangé si rien à fusionner ET keep_chapters identique
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.workflows.encode.models import AudioTrackSettings, EncodeConfig, VideoEncodeSettings
from core.workflows.remux import RemuxConfig, SourceInput, TrackEntry

# Appel unbound : _merge_remux_extras n'utilise pas self
from ui.main_window import MainWindow

_merge = MainWindow._merge_remux_extras


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _video_settings() -> VideoEncodeSettings:
    return VideoEncodeSettings()


def _encode_cfg(
    src: Path,
    output: Path,
    *,
    file_title: str = "",
    extra_attachments: list | None = None,
    keep_chapters: bool = True,
    audio_tracks: list | None = None,
) -> EncodeConfig:
    return EncodeConfig(
        source=src,
        output=output,
        video=_video_settings(),
        audio_tracks=audio_tracks or [],
        keep_chapters=keep_chapters,
        file_title=file_title,
        extra_attachments=extra_attachments or [],
    )


def _track(mkv_tid: int, track_type: str = "video") -> TrackEntry:
    return TrackEntry(
        mkv_tid=mkv_tid,
        track_type=track_type,
        codec="H264",
        display_info="",
        language="fra",
        title="Titre piste",
        orig_language="fra",
        orig_title="Titre piste",
    )


def _remux_cfg(
    src: Path,
    output: Path,
    *,
    keep_chapters: bool = True,
    copy_tags: bool = False,
    tracks: list | None = None,
) -> RemuxConfig:
    t = tracks or [_track(0)]
    source = SourceInput(
        path=src,
        file_index=0,
        tracks=t,
        selected_attachments=[],
        attachment_count=0,
        copy_tags=copy_tags,
    )
    return RemuxConfig(
        sources=[source],
        output=output,
        track_order=[(0, t[0].mkv_tid)],
        keep_chapters=keep_chapters,
    )


# ---------------------------------------------------------------------------
# file_title préservé quand il y a fusion
# ---------------------------------------------------------------------------

class TestFileTitlePreservedOnMerge:
    """
    Vérifie que file_title n'est jamais perdu dans l'EncodeConfig retourné
    par _merge_remux_extras, même quand la méthode reconstruit un nouveau
    EncodeConfig avec des pistes/balises/chapitres fusionnés.
    """

    def test_title_preserved_when_subtitles_merged(self, tmp_path, qt_app):
        """file_title préservé lorsque des sous-titres sont fusionnés depuis remux_cfg."""
        src = tmp_path / "src.mkv"
        src.touch()
        out = tmp_path / "out.mkv"

        sub_track = _track(2, "subtitle")
        enc = _encode_cfg(src, out, file_title="Mon Film Test")
        rmx = _remux_cfg(src, out, tracks=[_track(0, "video"), sub_track])

        result = _merge(None, enc, rmx)

        assert result.file_title == "Mon Film Test", \
            f"file_title perdu après fusion sous-titres : {result.file_title!r}"

    def test_title_preserved_when_keep_chapters_differs(self, tmp_path, qt_app):
        """file_title préservé lorsque keep_chapters diffère (force reconstruction)."""
        src = tmp_path / "src.mkv"
        src.touch()
        out = tmp_path / "out.mkv"

        enc = _encode_cfg(src, out, file_title="Film Chapitres", keep_chapters=True)
        rmx = _remux_cfg(src, out, keep_chapters=False)

        result = _merge(None, enc, rmx)

        assert result.file_title == "Film Chapitres", \
            f"file_title perdu suite à changement keep_chapters : {result.file_title!r}"

    def test_title_preserved_with_tag_sources(self, tmp_path, qt_app):
        """file_title préservé lorsque copy_tags est actif (tag_sources non vide)."""
        src = tmp_path / "src.mkv"
        src.touch()
        out = tmp_path / "out.mkv"

        enc = _encode_cfg(src, out, file_title="Film Tags")
        rmx = _remux_cfg(src, out, copy_tags=True)

        result = _merge(None, enc, rmx)

        assert result.file_title == "Film Tags", \
            f"file_title perdu avec tag_sources : {result.file_title!r}"

    def test_empty_title_preserved(self, tmp_path, qt_app):
        """file_title='' (vide) doit rester vide après fusion, ne pas devenir autre chose."""
        src = tmp_path / "src.mkv"
        src.touch()
        out = tmp_path / "out.mkv"

        enc = _encode_cfg(src, out, file_title="")
        rmx = _remux_cfg(src, out, copy_tags=True)

        result = _merge(None, enc, rmx)

        assert result.file_title == ""


# ---------------------------------------------------------------------------
# extra_attachments préservés quand il y a fusion
# ---------------------------------------------------------------------------

class TestExtraAttachmentsPreservedOnMerge:
    """
    Vérifie que extra_attachments n'est jamais perdu dans l'EncodeConfig retourné.
    """

    def test_extra_attachments_preserved_when_chapters_differ(self, tmp_path, qt_app):
        """extra_attachments préservés lorsque keep_chapters force une reconstruction."""
        src = tmp_path / "src.mkv"
        src.touch()
        out = tmp_path / "out.mkv"
        cover = tmp_path / "cover.jpg"
        cover.touch()

        enc = _encode_cfg(src, out, extra_attachments=[cover], keep_chapters=True)
        rmx = _remux_cfg(src, out, keep_chapters=False)

        result = _merge(None, enc, rmx)

        assert result.extra_attachments == [cover], \
            f"extra_attachments perdus : {result.extra_attachments!r}"

    def test_extra_attachments_preserved_with_tag_sources(self, tmp_path, qt_app):
        """extra_attachments préservés lorsque tag_sources est actif."""
        src = tmp_path / "src.mkv"
        src.touch()
        out = tmp_path / "out.mkv"
        cover = tmp_path / "cover.png"
        cover.touch()

        enc = _encode_cfg(src, out, extra_attachments=[cover])
        rmx = _remux_cfg(src, out, copy_tags=True)

        result = _merge(None, enc, rmx)

        assert result.extra_attachments == [cover], \
            f"extra_attachments perdus avec tag_sources : {result.extra_attachments!r}"


# ---------------------------------------------------------------------------
# Retour sans reconstruction (rien à fusionner)
# ---------------------------------------------------------------------------

class TestNoMergeReturnsOriginal:
    """
    Quand rien ne nécessite de fusion et que keep_chapters est identique,
    _merge_remux_extras doit retourner l'objet encode_cfg inchangé (même identité).
    """

    def test_returns_same_object_when_nothing_to_merge(self, tmp_path, qt_app):
        """Retourne encode_cfg inchangé si aucun sous-titre / attachement / balise / méta."""
        src = tmp_path / "src.mkv"
        src.touch()
        out = tmp_path / "out.mkv"

        # Piste vidéo sans langue ni titre → aucun TrackMetaEdit généré
        bare_track = TrackEntry(
            mkv_tid=0, track_type="video", codec="H264",
            display_info="", language="", title="",
            orig_language="", orig_title="",
        )
        enc = _encode_cfg(src, out, file_title="Film", keep_chapters=True)
        rmx = _remux_cfg(src, out, keep_chapters=True, tracks=[bare_track])

        result = _merge(None, enc, rmx)

        assert result is enc, "Devrait retourner l'objet original sans reconstruction"


# ---------------------------------------------------------------------------
# tag_overrides propagés depuis RemuxConfig
# ---------------------------------------------------------------------------

class TestTagOverridesPropagated:
    """
    Vérifie que tag_overrides de RemuxConfig est correctement propagé dans
    l'EncodeConfig résultant, et qu'il prend priorité sur tag_sources.
    """

    def _remux_with_tag_overrides(
        self, src: Path, out: Path, tags: dict
    ) -> RemuxConfig:
        source = SourceInput(
            path=src, file_index=0,
            tracks=[_track(0)],
            selected_attachments=[],
            attachment_count=0,
            copy_tags=True,
        )
        return RemuxConfig(
            sources=[source],
            output=out,
            track_order=[(0, 0)],
            keep_chapters=True,
            tag_overrides=tags,
        )

    def test_tag_overrides_propagated_to_encode_cfg(self, tmp_path, qt_app):
        """tag_overrides de RemuxConfig est transmis à EncodeConfig."""
        src = tmp_path / "src.mkv"
        src.touch()
        out = tmp_path / "out.mkv"
        tags = {"COLLECTION": "MyShow", "SEASON": "2"}

        enc = _encode_cfg(src, out, file_title="Film")
        rmx = self._remux_with_tag_overrides(src, out, tags)

        result = _merge(None, enc, rmx)

        assert result.tag_overrides == tags, \
            f"tag_overrides non propagé : {result.tag_overrides!r}"

    def test_tag_overrides_disables_tag_sources(self, tmp_path, qt_app):
        """Quand tag_overrides est défini, tag_sources doit être vide."""
        src = tmp_path / "src.mkv"
        src.touch()
        out = tmp_path / "out.mkv"

        enc = _encode_cfg(src, out)
        rmx = self._remux_with_tag_overrides(src, out, {"EPISODE": "3"})

        result = _merge(None, enc, rmx)

        assert result.tag_sources == [], \
            f"tag_sources devrait être vide quand tag_overrides présent : {result.tag_sources!r}"

    def test_empty_tag_overrides_still_propagated(self, tmp_path, qt_app):
        """tag_overrides={} (dict vide) est transmis — signifie 'supprimer les balises'."""
        src = tmp_path / "src.mkv"
        src.touch()
        out = tmp_path / "out.mkv"

        enc = _encode_cfg(src, out)
        rmx = self._remux_with_tag_overrides(src, out, {})

        result = _merge(None, enc, rmx)

        assert result.tag_overrides == {}, \
            f"tag_overrides={{}} non propagé : {result.tag_overrides!r}"

    def test_no_tag_overrides_uses_tag_sources(self, tmp_path, qt_app):
        """Sans tag_overrides, copy_tags=True produit des tag_sources normaux."""
        src = tmp_path / "src.mkv"
        src.touch()
        out = tmp_path / "out.mkv"

        enc = _encode_cfg(src, out)
        # RemuxConfig sans tag_overrides mais copy_tags=True
        rmx = _remux_cfg(src, out, copy_tags=True)

        result = _merge(None, enc, rmx)

        assert result.tag_sources == [src], \
            f"tag_sources attendu [{src}], obtenu : {result.tag_sources!r}"
        assert result.tag_overrides is None, \
            f"tag_overrides devrait être None : {result.tag_overrides!r}"
