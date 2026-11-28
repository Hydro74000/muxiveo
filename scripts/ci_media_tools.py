"""Installe les outils épinglés des tests média après vérification du SHA-256 GitHub.

FFmpeg : build **master** de FFmpeg-Builds, la même que les paquets (Linux : archive
tar.xz, Windows : archive zip). ``--check-ffmpeg-master DOSSIER`` vérifie ensuite que
le ``ffmpeg`` / ``ffprobe`` résolus par le PATH sont bien ceux de ce dossier et une
build master (``ffmpeg version N-…``).
"""
from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.github_release import github_release_asset, verify_download  # noqa: E402


_WINDOWS = sys.platform == "win32"
_EXE = ".exe" if _WINDOWS else ""

TOOLS = {
    "dovi": ("quietvoid/dovi_tool", "2.3.2", "dovi_tool-2.3.2-x86_64-unknown-linux-musl.tar.gz", ("dovi_tool",)),
    "ffmpeg": (
        "FFmpeg-Builds/FFmpeg-Builds", "latest",
        "ffmpeg-master-latest-win64-gpl.zip" if _WINDOWS else "ffmpeg-master-latest-linux64-gpl.tar.xz",
        (f"ffmpeg{_EXE}", f"ffprobe{_EXE}"),
    ),
}


def _members(names: list[str], binaries: tuple[str, ...], archive_name: str) -> dict[str, str]:
    """Membre unique de l'archive pour chaque exécutable attendu."""
    selected: dict[str, str] = {}
    for binary in binaries:
        matches = [name for name in names if PurePosixPath(name).name == binary]
        if len(matches) != 1:
            raise RuntimeError(f"Archive {archive_name} : exécutable unique attendu pour {binary}")
        selected[binary] = matches[0]
    return selected


def _extract_tar(archive: Path, binaries: tuple[str, ...], destination: Path) -> None:
    with tarfile.open(archive) as source:
        regular = {member.name: member for member in source.getmembers() if member.isfile()}
        for binary, name in _members(list(regular), binaries, archive.name).items():
            stream = source.extractfile(regular[name])
            if stream is None:
                raise RuntimeError(f"Archive {archive.name} : lecture impossible de {binary}")
            with stream, (destination / binary).open("wb") as output:
                shutil.copyfileobj(stream, output)


def _extract_zip(archive: Path, binaries: tuple[str, ...], destination: Path) -> None:
    with zipfile.ZipFile(archive) as source:
        regular = [info.filename for info in source.infolist() if not info.is_dir()]
        for binary, name in _members(regular, binaries, archive.name).items():
            with source.open(name) as stream, (destination / binary).open("wb") as output:
                shutil.copyfileobj(stream, output)


def install(tool: str, destination: Path) -> None:
    """N'extrait que les exécutables réguliers attendus d'une archive vérifiée."""
    repo, tag, name, binaries = TOOLS[tool]
    asset = github_release_asset(repo, tag, name)
    with tempfile.TemporaryDirectory(prefix="muxiveo-ci-tools-") as directory:
        archive = Path(directory) / name
        request = urllib.request.Request(asset.url, headers={"User-Agent": "Muxiveo-CI"})
        with urllib.request.urlopen(request, timeout=120) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
        verify_download(asset, archive)
        destination.mkdir(parents=True, exist_ok=True)
        if name.endswith(".zip"):
            _extract_zip(archive, binaries, destination)
        else:
            _extract_tar(archive, binaries, destination)
        for binary in binaries:
            (destination / binary).chmod(0o755)
    print(f"{asset.repo} {asset.tag} / {asset.name} : SHA-256 {asset.sha256}", flush=True)


def check_ffmpeg_master(directory: Path) -> None:
    """Les outils résolus par le PATH sont ceux installés dans ``directory``, en build master."""
    expected_dir = directory.resolve()
    for tool in ("ffmpeg", "ffprobe"):
        resolved = shutil.which(tool)
        if resolved is None or Path(resolved).resolve().parent != expected_dir:
            raise SystemExit(f"{tool} résolu par le PATH : {resolved!r}, attendu dans {expected_dir}")
        line = subprocess.check_output([resolved, "-hide_banner", "-version"], text=True).splitlines()[0]
        print(f"{resolved} : {line}", flush=True)
        if not line.startswith(f"{tool} version N-"):
            raise SystemExit(f"{tool} n'est pas une build master : {line}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool", nargs="?", choices=TOOLS)
    parser.add_argument("destination", nargs="?", type=Path)
    parser.add_argument("--check-ffmpeg-master", type=Path, metavar="DOSSIER")
    args = parser.parse_args()
    if args.check_ffmpeg_master is not None:
        check_ffmpeg_master(args.check_ffmpeg_master)
        return
    if args.tool is None or args.destination is None:
        parser.error("outil et dossier de destination requis")
    install(args.tool, args.destination)


if __name__ == "__main__":
    main()
