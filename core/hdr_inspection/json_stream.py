"""Lecture en flux d'un tableau JSON volumineux (export RPU, JSON HDR10+).

Un film complet produit plusieurs centaines de Mo de JSON : chaque élément est décodé
puis rendu à l'appelant, sans charger le document entier en mémoire.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

_CHUNK = 1 << 22
# Un élément plus grand trahit un document invalide (un RPU fait quelques Ko).
_MAX_PENDING = 64 << 20
_SEPARATORS = " \t\r\n,"


class JsonStreamError(ValueError):
    """Document JSON tronqué ou invalide."""


def iter_json_array(path: Path, *, key: str | None = None) -> Iterator[object]:
    """Éléments du tableau racine, ou du tableau associé à ``key`` au premier niveau."""
    decoder = json.JSONDecoder()
    start = re.compile(r"\[") if key is None else re.compile(r'"%s"\s*:\s*\[' % re.escape(key))
    with Path(path).open(encoding="utf-8") as handle:
        buffer = ""
        pos = 0
        eof = False

        def fill() -> bool:
            nonlocal buffer, pos, eof
            chunk = handle.read(_CHUNK)
            if not chunk:
                eof = True
                return False
            buffer = buffer[pos:] + chunk
            pos = 0
            return True

        while True:
            match = start.search(buffer, pos)
            if match is not None:
                pos = match.end()
                break
            # Garder la fin : la clé peut chevaucher deux blocs.
            pos = max(pos, len(buffer) - 256)
            if not fill():
                raise JsonStreamError(f"Tableau JSON introuvable : {Path(path).name}")
        while True:
            while pos < len(buffer) and buffer[pos] in _SEPARATORS:
                pos += 1
            if pos >= len(buffer):
                if not fill():
                    raise JsonStreamError(f"JSON tronqué : {Path(path).name}")
                continue
            if buffer[pos] == "]":
                return
            try:
                item, end = decoder.raw_decode(buffer, pos)
            except json.JSONDecodeError as exc:
                if eof or len(buffer) - pos > _MAX_PENDING:
                    raise JsonStreamError(f"JSON invalide : {Path(path).name} ({exc.msg})") from exc
                fill()
                continue
            pos = end
            yield item


def read_json_head(path: Path, size: int = 1 << 16) -> str:
    """Début du document (en-têtes placés avant le grand tableau)."""
    with Path(path).open(encoding="utf-8", errors="replace") as handle:
        return handle.read(size)
