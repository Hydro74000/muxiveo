"""Tests unitaires pour la matrice d'hybridation multi-sources et le plan d'assemblage témoin."""
from __future__ import annotations

from pathlib import Path
import pytest

from core.workflows.hybrid_matrix import (
    HybridMatrix,
    HybridRecipe,
    MatrixEpisode,
    MatrixSource,
    SourceRole,
    parse_episode_key,
    prepare_matrix_episode,
)
from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry
from core.workflows.sync_calibration import SyncCalibration, SyncSegment
from cli.logging import Logger
from cli.options import CommonOptions


def test_parse_episode_key_formats():
    assert parse_episode_key("Breaking.Bad.S01E05.2160p.mkv") == (1, 5)
    assert parse_episode_key("series.s2e12.hdtv.mp4") == (2, 12)
    assert parse_episode_key("Lost.1x08.avi") == (1, 8)
    assert parse_episode_key("Episode_EP04.srt") == (1, 4)
    assert parse_episode_key("[Fansub] Anime_Show - 09 [1080p].ass") == (1, 9)
    assert parse_episode_key("bonus_feature_documentary.mkv") is None


def test_matrix_multi_sources_relay_and_missing(tmp_path):
    # Créer 4 dossiers sources :
    # 1. Master UHD (E01, E02, E03)
    # 2. Donneur DVD (E01, E02)
    # 3. Donneur TVRip (relais pour E03)
    # 4. Donneur Sous-titres (E01, E02, E03)
    master_dir = tmp_path / "Master_UHD"
    dvd_dir = tmp_path / "Donor_DVD"
    tvrip_dir = tmp_path / "Donor_TVRip"
    subs_dir = tmp_path / "Donor_Subs"

    for d in (master_dir, dvd_dir, tvrip_dir, subs_dir):
        d.mkdir()

    # Master
    (master_dir / "Show.S01E01.uhd.mkv").touch()
    (master_dir / "Show.S01E02.uhd.mkv").touch()
    (master_dir / "Show.S01E03.uhd.mkv").touch()

    # DVD (E01 et E02 seulement)
    (dvd_dir / "Show.S01E01.dvd.ac3").touch()
    (dvd_dir / "Show.S01E02.dvd.ac3").touch()

    # TVRip (relais pour E03)
    (tvrip_dir / "Show.S01E03.tvrip.eac3").touch()

    # Subs (fichiers externes .ass)
    (subs_dir / "Show.S01E01.fr.ass").touch()
    (subs_dir / "Show.S01E02.fr.ass").touch()
    (subs_dir / "Show.S01E03.fr.ass").touch()

    matrix = HybridMatrix()
    matrix.add_source(master_dir, role=SourceRole.MASTER, label="UHD Master")
    matrix.add_source(dvd_dir, role=SourceRole.DONOR_AUDIO, label="DVD VF", priority=10)
    matrix.add_source(tvrip_dir, role=SourceRole.DONOR_AUDIO, label="TVRip VF", priority=5)
    matrix.add_source(subs_dir, role=SourceRole.DONOR_SUBTITLE, label="Fansub ST", priority=8)

    episodes = matrix.scan()

    assert len(episodes) == 3

    # Vérification E01 : Master + DVD + Subs
    ep1 = episodes[0]
    assert ep1.season == 1 and ep1.episode == 1
    assert ep1.master_file.name == "Show.S01E01.uhd.mkv"
    donor_names = [p.name for _, p in ep1.donor_files]
    assert "Show.S01E01.dvd.ac3" in donor_names
    assert "Show.S01E01.fr.ass" in donor_names
    assert ep1.is_complete is True

    # Vérification E02 : Master + DVD + Subs
    ep2 = episodes[1]
    assert ep2.season == 1 and ep2.episode == 2
    assert ep2.master_file.name == "Show.S01E02.uhd.mkv"
    donor_names2 = [p.name for _, p in ep2.donor_files]
    assert "Show.S01E02.dvd.ac3" in donor_names2
    assert "Show.S01E02.fr.ass" in donor_names2
    assert ep2.is_complete is True

    # Vérification E03 : Relais automatique vers TVRip car absent du DVD !
    ep3 = episodes[2]
    assert ep3.season == 1 and ep3.episode == 3
    assert ep3.master_file.name == "Show.S01E03.uhd.mkv"
    donor_names3 = [p.name for _, p in ep3.donor_files]
    assert "Show.S01E03.tvrip.eac3" in donor_names3
    assert "Show.S01E03.fr.ass" in donor_names3
    assert ep3.is_complete is True


def test_matrix_tolerant_to_missing_episodes(tmp_path):
    master_dir = tmp_path / "Master"
    donor_dir = tmp_path / "Donor"
    master_dir.mkdir()
    donor_dir.mkdir()

    (master_dir / "Show.S01E01.mkv").touch()
    (master_dir / "Show.S01E02.mkv").touch()
    # Le donneur n'a que l'épisode 1
    (donor_dir / "Show.S01E01.mkv").touch()

    matrix = HybridMatrix()
    matrix.add_source(master_dir, role=SourceRole.MASTER)
    matrix.add_source(donor_dir, role=SourceRole.DONOR)

    episodes = matrix.scan()
    assert len(episodes) == 2
    assert episodes[0].is_complete is True
    assert episodes[0].status == "ready"
    assert episodes[1].is_complete is False
    assert episodes[1].status == "partial"
    assert "manquant" in episodes[1].status_message.lower()


def test_hybrid_recipe_from_witness():
    track_v = TrackEntry(0, "video", "HEVC", "", "", "", enabled=True, file_id="src0")
    track_a_vo = TrackEntry(1, "audio", "TRUEHD", "", "eng", "VO", enabled=True, file_id="src0")
    track_a_comm = TrackEntry(2, "audio", "AC3", "", "eng", "Comm", enabled=False, file_id="src0")
    master_src = SourceInput(Path("master.mkv"), 0, [track_v, track_a_vo, track_a_comm])

    track_d_vf = TrackEntry(0, "audio", "AC3", "", "fre", "VF", enabled=True, file_id="src1")
    track_d_sub_fr = TrackEntry(1, "subtitle", "SUBRIP", "", "fre", "Forced", enabled=True, file_id="src1")
    track_d_sub_es = TrackEntry(2, "subtitle", "SUBRIP", "", "spa", "Spanish", enabled=False, file_id="src1")
    donor_src = SourceInput(Path("donor.mkv"), 1, [track_d_vf, track_d_sub_fr, track_d_sub_es])

    remux_cfg = RemuxConfig(
        sources=[master_src, donor_src],
        output=Path("out.mkv"),
        track_order=[(0, 0, track_v.entry_id), (1, 0, track_d_vf.entry_id)],
        sync_mode="physical",
        sync_subtitles="mirror",
    )

    recipe = HybridRecipe.from_witness_config(remux_cfg, master_index=0)

    assert recipe.keep_master_video is True
    assert recipe.keep_master_audio_langs == ["eng"]
    assert recipe.donor_audio_langs == ["fre"]
    assert recipe.donor_sub_langs == ["fre"]
    assert recipe.sync_mode == "physical"
    assert recipe.sync_subtitles == "mirror"


def test_matrix_fuzzy_movie_matching(tmp_path):
    master_dir = tmp_path / "UHD_Movies"
    donor_dir = tmp_path / "Bluray_VF"
    master_dir.mkdir()
    donor_dir.mkdir()

    # Master: films 4K avec tags complets
    (master_dir / "The.Matrix.1999.2160p.UHD.Remux.mkv").touch()
    (master_dir / "The.Matrix.Reloaded.2003.2160p.mkv").touch()
    (master_dir / "Star.Wars.Episode.IV.A.New.Hope.1977.Remux.mkv").touch()

    # Donneur: noms différents, tags différents, extensions différentes
    (donor_dir / "Matrix.1999.MULTi.1080p.mkv").touch()
    (donor_dir / "Matrix.Reloaded.2003.FRENCH.mkv").touch()
    (donor_dir / "Star Wars - A New Hope (1977) [1080p].mkv").touch()

    from core.workflows.hybrid_matrix import MatchingMode
    matrix = HybridMatrix(matching_mode=MatchingMode.FUZZY)
    matrix.add_source(master_dir, role=SourceRole.MASTER, label="4K UHD")
    matrix.add_source(donor_dir, role=SourceRole.DONOR, label="BluRay VF")

    items = matrix.scan()
    assert len(items) == 3
    for item in items:
        assert item.is_complete is True
        assert item.status == "ready"

    # Vérification des appariements
    matched_pairs = {item.master_file.name: item.donor_files[0][1].name for item in items}
    assert matched_pairs["The.Matrix.1999.2160p.UHD.Remux.mkv"] == "Matrix.1999.MULTi.1080p.mkv"
    assert matched_pairs["The.Matrix.Reloaded.2003.2160p.mkv"] == "Matrix.Reloaded.2003.FRENCH.mkv"
    assert matched_pairs["Star.Wars.Episode.IV.A.New.Hope.1977.Remux.mkv"] == "Star Wars - A New Hope (1977) [1080p].mkv"


def test_matrix_order_matching(tmp_path):
    master_dir = tmp_path / "Master_Files"
    donor_dir = tmp_path / "Donor_Files"
    master_dir.mkdir()
    donor_dir.mkdir()

    # Noms non standards ou arbitraires
    (master_dir / "Part_01_Introduction.mkv").touch()
    (master_dir / "Part_02_Development.mkv").touch()
    (master_dir / "Part_03_Conclusion.mkv").touch()

    (donor_dir / "01_Intro_VF.mka").touch()
    (donor_dir / "02_Dev_VF.mka").touch()
    (donor_dir / "03_Concl_VF.mka").touch()

    from core.workflows.hybrid_matrix import MatchingMode
    matrix = HybridMatrix(matching_mode=MatchingMode.ORDER)
    matrix.add_source(master_dir, role=SourceRole.MASTER)
    matrix.add_source(donor_dir, role=SourceRole.DONOR)

    items = matrix.scan()
    assert len(items) == 3
    assert items[0].master_file.name == "Part_01_Introduction.mkv"
    assert items[0].donor_files[0][1].name == "01_Intro_VF.mka"
    assert items[1].master_file.name == "Part_02_Development.mkv"
    assert items[1].donor_files[0][1].name == "02_Dev_VF.mka"
    assert items[2].master_file.name == "Part_03_Conclusion.mkv"
    assert items[2].donor_files[0][1].name == "03_Concl_VF.mka"


def test_matrix_candidate_helpers(tmp_path):
    master_dir = tmp_path / "Master"
    donor1_dir = tmp_path / "Donor1"
    donor2_dir = tmp_path / "Donor2"
    for d in (master_dir, donor1_dir, donor2_dir):
        d.mkdir()

    (master_dir / "Movie1.mkv").touch()
    (master_dir / "Movie2.mkv").touch()
    (donor1_dir / "Audio1.ac3").touch()
    (donor2_dir / "Sub2.srt").touch()

    matrix = HybridMatrix()
    matrix.add_source(master_dir, role=SourceRole.MASTER)
    matrix.add_source(donor1_dir, role=SourceRole.DONOR_AUDIO, label="VF")
    matrix.add_source(donor2_dir, role=SourceRole.DONOR_SUBTITLE, label="ST")

    masters = matrix.get_all_master_candidates()
    assert len(masters) == 2

    donors = matrix.get_all_donor_candidates()
    assert len(donors) == 2
    donor_paths = [p.name for _, p in donors]
    assert "Audio1.ac3" in donor_paths
    assert "Sub2.srt" in donor_paths
