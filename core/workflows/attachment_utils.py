"""
core/workflows/attachment_utils.py — Extraction et gestion des pièces jointes (attachments).

Fournit :
  MIME_BY_EXT            — extension → type MIME
  EXT_BY_MIME            — type MIME → extension
  mime_for(path)         — type MIME d'après l'extension
  ext_for_mime(mime)     — extension d'après le type MIME
  unique_attachment_path — chemin sans collision dans un répertoire
  AttachmentPicExtractor — extrait les flux ``attached_pic`` avec RAM → disk fallback
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

from core.subprocess_utils import subprocess_text_kwargs


# ---------------------------------------------------------------------------
# Tables MIME (union remux_ffmpeg + encode/workflow)
# ---------------------------------------------------------------------------

MIME_BY_EXT: dict[str, str] = {
    ".jpg":   "image/jpeg",
    ".jpeg":  "image/jpeg",
    ".png":   "image/png",
    ".gif":   "image/gif",
    ".bmp":   "image/bmp",
    ".webp":  "image/webp",
    ".tif":   "image/tiff",
    ".tiff":  "image/tiff",
    ".ttf":   "application/x-truetype-font",
    ".otf":   "font/otf",
    ".woff":  "font/woff",
    ".woff2": "font/woff2",
    ".txt":   "text/plain",
    ".xml":   "application/xml",
}

EXT_BY_MIME: dict[str, str] = {
    "image/jpeg":                    ".jpg",
    "image/png":                     ".png",
    "image/gif":                     ".gif",
    "image/bmp":                     ".bmp",
    "image/webp":                    ".webp",
    "image/tiff":                    ".tiff",
    "application/x-truetype-font":   ".ttf",
    "font/ttf":                      ".ttf",
    "font/otf":                      ".otf",
    "font/woff":                     ".woff",
    "font/woff2":                    ".woff2",
    "text/plain":                    ".txt",
    "application/xml":               ".xml",
}


def mime_for(path: Path) -> str:
    """Retourne le type MIME d'après l'extension du chemin."""
    return MIME_BY_EXT.get(path.suffix.lower(), "application/octet-stream")


def ext_for_mime(mimetype: str, fallback: str = ".bin") -> str:
    """Retourne l'extension d'après le type MIME."""
    return EXT_BY_MIME.get((mimetype or "").strip().lower(), fallback)


def unique_attachment_path(dest_dir: Path, filename: str) -> Path:
    """
    Retourne un chemin unique dans ``dest_dir`` sans collision de nom.

    Si ``filename`` est déjà libre, le retourne tel quel ; sinon ajoute
    un suffixe numérique croissant (``stem_1.ext``, ``stem_2.ext``, …).
    """
    candidate = dest_dir / filename
    if not candidate.exists():
        return candidate
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    for idx in range(1, 1000):
        alt = dest_dir / f"{stem}_{idx}{suffix}"
        if not alt.exists():
            return alt
    return dest_dir / f"{stem}_{os.getpid()}{suffix}"


# ---------------------------------------------------------------------------
# Extracteur de flux attached_pic
# ---------------------------------------------------------------------------

class AttachmentPicExtractor:
    """
    Extrait les flux ``attached_pic`` (couvertures MKV) vers des fichiers image.

    FFmpeg expose les couvertures MKV comme flux vidéo MJPEG avec
    ``disposition.attached_pic=1``. Un simple ``-map … -c:t copy`` ne peut pas
    les reconvertir en attachment MKV (le muxeur perd le flag). On extrait une
    frame et on la ré-attache via ``-attach``.

    Stratégie d'écriture (RAM → disk) :
      1. ``ram_dir`` (e.g. ``/dev/shm``) si fourni et accessible — aucun I/O disque.
      2. ``fallback_dir`` fourni lors de l'appel à :meth:`extract` — dernier recours.

    Nota : FIFO/named-pipe non applicable — ``ffmpeg -attach`` requiert un
    fichier seekable (``fstat`` pour la taille avant écriture de l'en-tête MKV).
    """

    def __init__(
        self,
        ffmpeg_bin: str,
        thread_args: list[str],
        log_cb: Callable[[str], None] | None = None,
        ram_dir: Path | None = None,
    ) -> None:
        self._ffmpeg = ffmpeg_bin
        self._thread_args = list(thread_args)
        self._log = log_cb or (lambda _: None)
        self._ram_dir = ram_dir

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    @staticmethod
    def default_ram_dir() -> Path | None:
        """
        Retourne le répertoire RAM disponible sur cette plateforme, ou ``None``.

        · Linux / macOS : ``/dev/shm`` (tmpfs)
        · Windows       : ``None`` (pas d'équivalent standard)
        """
        if sys.platform.startswith("win"):
            return None
        shm = Path("/dev/shm")
        if shm.is_dir() and os.access(shm, os.W_OK):
            return shm
        return None

    def extract(
        self,
        source: Path,
        stream_idx: int,
        fallback_dir: Path,
        filename_hint: str,
    ) -> Path:
        """
        Extrait le flux ``attached_pic`` ``stream_idx`` depuis ``source``.

        Tente d'abord ``ram_dir`` (si configuré), puis ``fallback_dir``.
        Retourne le chemin du fichier extrait.

        :raises RuntimeError: si l'extraction échoue sur tous les répertoires.
        """
        dirs_to_try: list[Path] = []
        if self._ram_dir is not None:
            dirs_to_try.append(self._ram_dir)
        if fallback_dir not in dirs_to_try:
            dirs_to_try.append(fallback_dir)

        last_exc: Exception | None = None
        for idx, dest_dir in enumerate(dirs_to_try):
            dest = unique_attachment_path(dest_dir, filename_hint)
            try:
                self._do_extract(source, stream_idx, dest)
                if idx > 0:
                    self._log(
                        f"Extraction attached_pic : RAM indisponible, "
                        f"fallback disque utilisé ({dest_dir})."
                    )
                return dest
            except Exception as exc:  # noqa: BLE001
                dest.unlink(missing_ok=True)
                last_exc = exc
                if idx < len(dirs_to_try) - 1:
                    self._log(
                        f"Extraction attached_pic vers {dest_dir} échouée "
                        f"({exc}), tentative suivante."
                    )

        assert last_exc is not None
        raise last_exc

    # ------------------------------------------------------------------
    # Implémentation interne
    # ------------------------------------------------------------------

    def _do_extract(self, source: Path, stream_idx: int, dest: Path) -> None:
        """Lance l'extraction FFmpeg d'une seule frame depuis le flux ``stream_idx``."""
        cmd = [
            self._ffmpeg,
            "-hide_banner", "-y",
            "-i", source.as_posix(),
            "-map", f"0:{stream_idx}",
            *self._thread_args,
            "-c", "copy",
            "-frames:v", "1",
            dest.as_posix(),
        ]
        self._log("$ " + " ".join(str(c) for c in cmd))
        result = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            timeout=60,
            **subprocess_text_kwargs(),
        )
        if result.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
            stderr = (result.stderr or "").strip()
            raise RuntimeError(
                f"Extraction attached_pic échouée "
                f"(source={source.name}, stream={stream_idx}): {stderr}"
            )
