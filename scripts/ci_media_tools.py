"""Installe les outils épinglés des tests média après vérification du SHA-256 GitHub."""
from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath
import shutil
import sys
import tarfile
import tempfile
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.github_release import github_release_asset, verify_download  # noqa: E402


TOOLS = {
    "dovi": ("quietvoid/dovi_tool", "2.3.2", "dovi_tool-2.3.2-x86_64-unknown-linux-musl.tar.gz", ("dovi_tool",)),
    "ffmpeg": ("BtbN/FFmpeg-Builds", "latest", "ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz", ("ffmpeg", "ffprobe")),
}


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
        with tarfile.open(archive) as source:
            members = source.getmembers()
            selected = []
            for binary in binaries:
                matches = [member for member in members if PurePosixPath(member.name).name == binary]
                if len(matches) != 1 or not matches[0].isfile():
                    raise RuntimeError(f"Archive {name} : exécutable régulier unique attendu pour {binary}")
                selected.append((binary, matches[0]))
            destination.mkdir(parents=True, exist_ok=True)
            for binary, member in selected:
                stream = source.extractfile(member)
                if stream is None:
                    raise RuntimeError(f"Archive {name} : lecture impossible de {binary}")
                with stream, (destination / binary).open("wb") as output:
                    shutil.copyfileobj(stream, output)
                (destination / binary).chmod(0o755)
    print(f"{asset.repo} {asset.tag} / {asset.name} : SHA-256 {asset.sha256}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool", choices=TOOLS)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    install(args.tool, args.destination)


if __name__ == "__main__":
    main()
