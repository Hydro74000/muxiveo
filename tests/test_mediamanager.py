import os
import pytest
from PySide6.QtWidgets import QApplication

import mediamanager


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        app = QApplication([])
    return app


def test_clean_title():
    assert mediamanager.clean_title("") == ""
    assert mediamanager.clean_title(None) == ""
    assert (
        mediamanager.clean_title(
            "Oppenheimer.2023.MULTi.TRUEFRENCH.2160p.UHD.HDR.DV.HEVC"
        )
        == "Oppenheimer"
    )
    assert (
        mediamanager.clean_title("The Batman (2022)")
        == "The Batman"
    )
    assert (
        mediamanager.clean_title("Blade.Runner.2049.2017.1080p.mkv")
        == "Blade Runner 2049"
    )
    assert (
        mediamanager.clean_title("Movie_Name_2020")
        == "Movie Name"
    )
    assert (
        mediamanager.clean_title("[ReleaseGroup] Film Title (2021) [1080p]")
        == "Film Title"
    )
    assert (
        mediamanager.clean_title("Breaking.Bad.S01E01.FRENCH.720p.HDTV.x264")
        == "Breaking Bad S01E01"
    )


def test_parse_media_file_movies_and_series(tmp_path):
    # Movie with dots and year
    f_movie = tmp_path / "Inception.2010.1080p.mkv"
    f_movie.touch()
    meta_movie = mediamanager.parse_media_file(str(f_movie))
    assert meta_movie is not None
    assert meta_movie["type"] == "F"
    assert meta_movie["title"] == "Inception"
    assert meta_movie["path"] == str(f_movie)

    # Movie with underscores longer than 30 chars (ensure not treated as hash)
    long_name = "The_Lord_of_the_Rings_The_Fellowship_of_the_Ring_2001.mkv"
    f_long = tmp_path / long_name
    f_long.touch()
    meta_long = mediamanager.parse_media_file(str(f_long))
    assert meta_long is not None
    assert meta_long["type"] == "F"
    assert "Fellowship" in meta_long["title"]

    # Series standard pattern SxxExx
    f_series = tmp_path / "Severance.S01E03.1080p.mkv"
    f_series.touch()
    meta_series = mediamanager.parse_media_file(str(f_series))
    assert meta_series is not None
    assert meta_series["type"] == "S"
    assert meta_series["show"] == "Severance"
    assert meta_series["season"] == 1
    assert meta_series["episode"] == 3

    # Series directory fallback (including folder with tags)
    show_dir = tmp_path / "MyShow" / "Season 2 (1080p)"
    show_dir.mkdir(parents=True)
    f_fallback = show_dir / "Episode 05.mkv"
    f_fallback.touch()
    meta_fallback = mediamanager.parse_media_file(str(f_fallback))
    assert meta_fallback is not None
    assert meta_fallback["type"] == "S"
    assert meta_fallback["show"] == "Myshow"
    assert meta_fallback["season"] == 2
    assert meta_fallback["episode"] == 5

    # Sample file ignored
    f_sample = tmp_path / "sample.mkv"
    f_sample.touch()
    assert mediamanager.parse_media_file(str(f_sample)) is None

    # Hash file ignored
    f_hash = tmp_path / "e4d909c290d0fb1ca068ffaddf22cbd0.mkv"
    f_hash.touch()
    assert mediamanager.parse_media_file(str(f_hash)) is None


def test_file_sizes(tmp_path):
    f = tmp_path / "test.mkv"
    f.write_bytes(b"A" * 2048)
    size_str = mediamanager.get_file_size(str(f))
    assert "2.00 Ko" in size_str

    assert mediamanager.get_file_size(str(tmp_path / "nonexistent.mkv")) == "N/A"


def test_resolve_mediainfo_command():
    cmd = mediamanager._resolve_mediainfo_command()
    assert isinstance(cmd, list)
    assert len(cmd) > 0


def test_mediainfo_thread_parse_content():
    thread = mediamanager.MediaInfoThread("dummy.mkv")
    sample_mediainfo_output = """General
Complete name : /path/to/video.mkv
Format : Matroska
File size : 1.50 GiB
Duration : 1 h 30 min
Overall bit rate : 2 345 kb/s
Title : Test Movie

Video #1
Format : HEVC
Format profile : Main 10@L5.1@High
HDR format : Dolby Vision / HDR10
Width : 3840 pixels
Height : 2160 pixels
Frame rate : 24.000 FPS
Bit rate : 20.0 Mb/s

Audio #1
Format : AC-3
Commercial name : Dolby Digital
Title : French
Language : French
Bit rate : 384 kb/s
Channel(s) : 6 channels
Sampling rate : 48.0 kHz

Text #1
Format : UTF-8
Title : Forced
Language : French
Forced : Yes
"""
    parsed = thread.parse_content(sample_mediainfo_output)
    assert len(parsed) == 4
    assert parsed[0]["title"] == "General"
    assert parsed[0]["data"]["Format"] == "Matroska"
    assert parsed[0]["data"]["Title"] == "Test Movie"
    assert parsed[1]["title"] == "Video #1"
    assert parsed[1]["data"]["Format"] == "HEVC"
    assert parsed[1]["data"]["Width"] == "3840 pixels"
    assert parsed[2]["title"] == "Audio #1"
    assert parsed[2]["data"]["Language"] == "French"
    assert parsed[3]["title"] == "Text #1"
    assert parsed[3]["data"]["Forced"] == "Yes"


def test_mediamanager_ui_workflow(qapp, tmp_path):
    # Setup test files:
    # 2 copies of same movie (duplicate test)
    movie1 = tmp_path / "Matrix.1999.1080p.mkv"
    movie1.write_bytes(b"X" * 100)
    movie2 = tmp_path / "Matrix.1999.2160p.mkv"
    movie2.write_bytes(b"Y" * 200)

    # 1 series with 1 episode
    series_dir = tmp_path / "BreakingBad" / "Season 01"
    series_dir.mkdir(parents=True)
    ep1 = series_dir / "BreakingBad.S01E01.mkv"
    ep1.write_bytes(b"Z" * 150)

    win = mediamanager.MediaManager(startup_dir=str(tmp_path))
    win.scanner.wait(3000)
    qapp.processEvents()

    assert win.scanned_count == 3
    # Top level items: 1 Movie parent, 1 Series parent
    assert win.tree.topLevelItemCount() == 2

    # Verify Movie duplicate item has count 2
    movie_item = win.tree_items.get("F|Matrix")
    assert movie_item is not None
    assert movie_item.text(win.COL["nom"]) == "Matrix (2)"
    assert movie_item.childCount() == 2

    # Verify Series hierarchy
    show_item = win.tree_items.get("S|Breakingbad")
    assert show_item is not None
    assert show_item.childCount() == 1  # 1 season
    season_item = win.tree_items.get("S|Breakingbad|1")
    assert season_item is not None
    assert season_item.childCount() == 1  # 1 episode
    ep_item = win.tree_items.get("S|Breakingbad|1|1")
    assert ep_item is not None
    assert ep_item.childCount() == 1  # 1 file

    # Test sorting by Nom, Taille, Type
    win.handle_header_click(win.COL["nom"])
    qapp.processEvents()
    win.handle_header_click(win.COL["taille"])
    qapp.processEvents()
    win.handle_header_click(win.COL["type"])
    qapp.processEvents()

    # Verify delete button still exists on child after sorts
    file_child = movie_item.child(0)
    btn_widget = win.tree.itemWidget(file_child, win.COL["action"])
    assert btn_widget is not None

    # Test search filter (including recursive match on episode)
    win.filter_tree("Matrix")
    assert not movie_item.isHidden()
    assert show_item.isHidden()
    win.filter_tree("S01E01")
    assert movie_item.isHidden()
    assert not show_item.isHidden()
    win.filter_tree("")
    assert not movie_item.isHidden()
    assert not show_item.isHidden()

    # Test detail inspection
    win.draw_info(
        [
            {
                "title": "General",
                "data": {"Format": "Matroska", "Title": "Matrix"},
            }
        ]
    )
    assert win.details_layout.count() > 0

    # Test clean closing
    win.close()


def test_delete_full_season_and_cancellation(qapp, tmp_path, monkeypatch):
    # Setup series with Season 1 (2 episodes) and Season 2 (1 episode)
    s1_dir = tmp_path / "TheWire" / "Season 01"
    s1_dir.mkdir(parents=True)
    ep1 = s1_dir / "TheWire.S01E01.mkv"
    ep1.write_bytes(b"A" * 100)
    ep2 = s1_dir / "TheWire.S01E02.mkv"
    ep2.write_bytes(b"B" * 120)

    s2_dir = tmp_path / "TheWire" / "Season 02"
    s2_dir.mkdir(parents=True)
    ep3 = s2_dir / "TheWire.S02E01.mkv"
    ep3.write_bytes(b"C" * 150)

    win = mediamanager.MediaManager(startup_dir=str(tmp_path))
    win.scanner.wait(3000)
    qapp.processEvents()

    show_item = win.tree_items.get("S|Thewire")
    assert show_item is not None
    assert show_item.childCount() == 2
    season1_item = win.tree_items.get("S|Thewire|1")
    assert season1_item is not None
    assert season1_item.childCount() == 2

    # 1. Test cancellation (user answers No)
    monkeypatch.setattr(win, "_ask_delete_confirmation", lambda title, text: False)
    result = win.confirm_delete_item(season1_item)
    assert result is False
    assert ep1.exists()
    assert ep2.exists()
    assert season1_item.childCount() == 2

    # 2. Test confirmed deletion of Season 1
    monkeypatch.setattr(win, "_ask_delete_confirmation", lambda title, text: True)
    result = win.confirm_delete_item(season1_item)
    assert result is True

    # Files of Season 1 are gone on disk
    assert not ep1.exists()
    assert not ep2.exists()
    # Empty Season 1 folder cleaned up
    assert not s1_dir.exists()

    # Season 2 is completely preserved
    assert ep3.exists()
    assert s2_dir.exists()

    # Tree updated
    assert "S|Thewire|1" not in win.tree_items
    assert "S|Thewire|1|1" not in win.tree_items
    assert "S|Thewire|1|2" not in win.tree_items
    assert "S|Thewire|2" in win.tree_items
    assert show_item.childCount() == 1
    assert "1 saison" in show_item.text(win.COL["nom"])

    win.close()


def test_delete_full_movie_with_duplicates(qapp, tmp_path, monkeypatch):
    movie_dir = tmp_path / "Inception"
    movie_dir.mkdir(parents=True)
    m1 = movie_dir / "Inception.2010.1080p.mkv"
    m1.write_bytes(b"M1" * 100)
    nfo1 = movie_dir / "Inception.2010.1080p_mediainfo.nfo"
    nfo1.write_text("dummy nfo")

    m2 = movie_dir / "Inception.2010.2160p.mkv"
    m2.write_bytes(b"M2" * 200)

    win = mediamanager.MediaManager(startup_dir=str(tmp_path))
    win.scanner.wait(3000)
    qapp.processEvents()

    movie_item = win.tree_items.get("F|Inception")
    assert movie_item is not None
    assert movie_item.childCount() == 2

    monkeypatch.setattr(win, "_ask_delete_confirmation", lambda title, text: True)
    result = win.confirm_delete_item(movie_item)
    assert result is True

    # Both movie files and NFO deleted on disk
    assert not m1.exists()
    assert not nfo1.exists()
    assert not m2.exists()
    # Empty movie directory cleaned up
    assert not movie_dir.exists()

    # Tree and full_data cleaned up
    assert "F|Inception" not in win.tree_items
    assert "F|Inception" not in win.full_data
    assert win.tree.topLevelItemCount() == 0

    win.close()


def test_delete_full_series(qapp, tmp_path, monkeypatch):
    show_dir = tmp_path / "Fargo" / "Season 01"
    show_dir.mkdir(parents=True)
    ep1 = show_dir / "Fargo.S01E01.mkv"
    ep1.write_bytes(b"Fargo" * 50)

    win = mediamanager.MediaManager(startup_dir=str(tmp_path))
    win.scanner.wait(3000)
    qapp.processEvents()

    show_item = win.tree_items.get("S|Fargo")
    assert show_item is not None

    monkeypatch.setattr(win, "_ask_delete_confirmation", lambda title, text: True)
    result = win.confirm_delete_item(show_item)
    assert result is True

    assert not ep1.exists()
    assert not (tmp_path / "Fargo").exists()
    assert "S|Fargo" not in win.tree_items
    assert win.tree.topLevelItemCount() == 0

    win.close()


def test_delete_buttons_exist_on_all_levels(qapp, tmp_path):
    show_dir = tmp_path / "Lost" / "Season 01"
    show_dir.mkdir(parents=True)
    ep = show_dir / "Lost.S01E01.mkv"
    ep.write_bytes(b"Lost" * 20)

    win = mediamanager.MediaManager(startup_dir=str(tmp_path))
    win.scanner.wait(3000)
    qapp.processEvents()

    show_item = win.tree_items.get("S|Lost")
    season_item = win.tree_items.get("S|Lost|1")
    ep_item = win.tree_items.get("S|Lost|1|1")
    file_item = ep_item.child(0)

    for item, expected_tip in [
        (show_item, "Supprimer cette série complète"),
        (season_item, "Supprimer la Saison 01 complète"),
        (ep_item, "Supprimer l'Épisode 01"),
        (file_item, "Supprimer ce fichier vidéo"),
    ]:
        w = win.tree.itemWidget(item, win.COL["action"])
        assert w is not None
        btn = w.findChild(mediamanager.QPushButton)
        assert btn is not None
        assert btn.toolTip() == expected_tip

    win.close()

