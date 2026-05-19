import pytest

import mediamanager


def test_startup_directory_from_argv_accepts_existing_directory(tmp_path) -> None:
    startup_dir = tmp_path / "videos"
    startup_dir.mkdir()

    assert mediamanager._startup_directory_from_argv(
        ["mediamanager.py", str(startup_dir)]
    ) == str(startup_dir)


def test_startup_directory_from_argv_rejects_missing_directory(tmp_path) -> None:
    with pytest.raises(ValueError, match="pas un dossier valide"):
        mediamanager._startup_directory_from_argv(
            ["mediamanager.py", str(tmp_path / "missing")]
        )
