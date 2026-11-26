"""
tests/test_tmdb_cover_fetch.py — Jaquettes TMDB (D4, C6 ; plan de remédiation 5.2.0, lot 6).

HTTPS vérifié sans repli automatique, opt-in explicite seul ; durée totale et taille bornées ; aucun réseau dans le
fil Qt (clic : tâche de fond, survol : cache seul) ; ligne supprimée avant le retour → résultat ignoré ; cache borné.
"""

from __future__ import annotations

import ssl
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace
from typing import Any

import pytest

import core.workdir as workdir
import ui.panels.remux_panel.widgets.attachments as attachments
from core.tls import TlsVerificationError
from core.workdir import fetch_tmdb_image

URL = "https://image.tmdb.org/t/p/original/cover.jpg"


class _Response:
    def __init__(self, chunks: list[bytes], *, delay: float = 0.0, length: int | None = None) -> None:
        self._chunks = list(chunks)
        self._delay = delay
        self.headers = {"Content-Length": str(length)} if length is not None else {}

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self, _size: int = -1) -> bytes:
        if self._delay:
            time.sleep(self._delay)
        return self._chunks.pop(0) if self._chunks else b""

    read1 = read


def test_tls_failure_never_retries_unverified(monkeypatch):
    monkeypatch.delenv("MUXIVEO_TMDB_INSECURE_SSL", raising=False)
    calls: list[Any] = []

    def urlopen(req, timeout=None, context=None):
        calls.append(context)
        raise urllib.error.URLError(ssl.SSLCertVerificationError("certificate verify failed"))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(TlsVerificationError):
        fetch_tmdb_image(URL)
    assert calls == [None]  # une seule requête, vérifiée


def test_explicit_opt_in_uses_an_unverified_context(monkeypatch):
    monkeypatch.setenv("MUXIVEO_TMDB_INSECURE_SSL", "1")
    contexts: list[Any] = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None, context=None: contexts.append(context) or _Response([b"img"]))
    assert fetch_tmdb_image(URL) == b"img"
    assert contexts[0] is not None and contexts[0].verify_mode == ssl.CERT_NONE


def test_drip_response_hits_the_total_deadline(monkeypatch):
    monkeypatch.delenv("MUXIVEO_TMDB_INSECURE_SSL", raising=False)
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None, context=None: _Response([b"x"] * 1000, delay=0.02))
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        fetch_tmdb_image(URL, deadline=0.2)
    assert time.monotonic() - started < 2


def test_end_of_stream_after_the_deadline_is_refused(monkeypatch):
    monkeypatch.delenv("MUXIVEO_TMDB_INSECURE_SSL", raising=False)
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None, context=None: _Response([], delay=0.3))  # fin tardive
    with pytest.raises(TimeoutError):
        fetch_tmdb_image(URL, deadline=0.1)


def test_reads_never_ask_for_a_full_buffer(monkeypatch):
    """``read1`` préféré à ``read(n)`` : une réception au plus par appel (sinon ``read`` attend n octets)."""
    calls: list[str] = []

    class _Strict(_Response):
        def read(self, _size: int = -1) -> bytes:  # type: ignore[override]
            calls.append("read")
            return super().read(_size)

        def read1(self, _size: int = -1) -> bytes:
            calls.append("read1")
            return _Response.read(self, _size)

    monkeypatch.delenv("MUXIVEO_TMDB_INSECURE_SSL", raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None, context=None: _Strict([b"a", b"b"]))
    assert fetch_tmdb_image(URL) == b"ab" and set(calls) == {"read1"}


def test_oversized_response_is_refused(monkeypatch):
    monkeypatch.delenv("MUXIVEO_TMDB_INSECURE_SSL", raising=False)
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None, context=None: _Response([b"x" * 64] * 10))
    with pytest.raises(ValueError):
        fetch_tmdb_image(URL, max_bytes=100)
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None, context=None: _Response([b"x"], length=21 * 1024 * 1024))
    with pytest.raises(ValueError):
        fetch_tmdb_image(URL)  # taille annoncée au-delà de 20 Mio : refus sans lecture
    assert workdir.TMDB_IMAGE_MAX_BYTES == 20 * 1024 * 1024


def test_download_cover_uses_the_bounded_fetch(tmp_path, monkeypatch):
    monkeypatch.setattr(workdir, "fetch_tmdb_image", lambda url: b"jpeg")
    assert workdir.download_tmdb_cover(URL, "cover.jpg", tmp_path).read_bytes() == b"jpeg"


# ── Interface ───────────────────────────────────────────────────────────────


@pytest.fixture
def covers(monkeypatch):
    fresh = attachments._TmdbCovers()
    monkeypatch.setattr(attachments, "_TMDB_COVERS", [fresh])
    return fresh


def _row(qt_app):
    return attachments._AttachmentItemWidget("", is_tmdb_pending=True, tmdb_cover_url=URL,
                                             tmdb_cover_filename="cover.jpg")


def test_click_never_blocks_the_qt_thread_and_opens_on_arrival(qt_app, covers, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(workdir, "fetch_tmdb_image",
                        lambda url, **_k: release.wait(5) and b"\xff\xd8jpeg")
    opened: list[Any] = []

    class _Dialog:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            opened.append(kwargs.get("image_bytes"))

        def exec(self) -> int:
            return 0

    monkeypatch.setattr(attachments, "_ImageAttachmentDialog", _Dialog)
    row = _row(qt_app)
    started = time.monotonic()
    row._open_attachment_viewer()
    assert time.monotonic() - started < 0.5 and opened == []  # téléchargement en tâche de fond
    assert row._image_pixmap() is None  # survol : cache seul, aucun réseau
    release.set()
    deadline = time.monotonic() + 5
    while not opened and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)
    assert opened == [b"\xff\xd8jpeg"] and covers.cached(URL) == b"\xff\xd8jpeg"
    row.deleteLater()


def test_row_deleted_before_the_result_is_ignored(qt_app, covers, monkeypatch):
    release = threading.Event()
    done = threading.Event()

    def fetch(url, **_k):
        release.wait(5)
        return b"img"

    monkeypatch.setattr(workdir, "fetch_tmdb_image", fetch)
    covers.finished.connect(lambda *_a: done.set())
    warnings: list[Any] = []
    opened: list[Any] = []
    monkeypatch.setattr(attachments.QMessageBox, "warning", lambda *a, **k: warnings.append(a))
    monkeypatch.setattr(attachments, "_ImageAttachmentDialog",
                        lambda *a, **k: opened.append(k) or SimpleNamespace(exec=lambda: 0))
    row = _row(qt_app)
    row._open_attachment_viewer()
    row.discard()  # ligne retirée par le panneau (destruction différée par Qt)
    release.set()
    deadline = time.monotonic() + 5
    while not done.is_set() and time.monotonic() < deadline:  # signal remis par la boucle Qt
        qt_app.processEvents()
        time.sleep(0.01)
    assert done.is_set()
    for _ in range(20):
        qt_app.processEvents()
        time.sleep(0.005)
    assert warnings == [] and opened == []  # aucune réaction d'une ligne supprimée (ni erreur, ni visionneuse)


def test_tls_error_message_explains_the_opt_in(qt_app, covers, monkeypatch):
    def fetch(url, **_k):
        raise TlsVerificationError("certificat TLS refusé pour image.tmdb.org")

    monkeypatch.setattr(workdir, "fetch_tmdb_image", fetch)
    shown: list[str] = []
    monkeypatch.setattr(attachments.QMessageBox, "warning", lambda _parent, _title, text: shown.append(text))
    row = _row(qt_app)
    row._open_attachment_viewer()
    deadline = time.monotonic() + 5
    while not shown and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)
    assert shown and "MUXIVEO_TMDB_INSECURE_SSL=1" in shown[0]
    row.deleteLater()


def test_cover_cache_is_bounded():
    cache = attachments._TmdbCovers()
    for index in range(attachments._TmdbCovers.CAPACITY + 5):
        cache._store(f"{URL}?{index}", b"x")
    assert cache.cached(f"{URL}?0") is None and cache.cached(f"{URL}?{attachments._TmdbCovers.CAPACITY + 4}")
    assert len(cache._items) == attachments._TmdbCovers.CAPACITY
