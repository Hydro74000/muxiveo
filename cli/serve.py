"""Worker ``serve`` : JSON-RPC 2.0 sur stdio pour un client local unique (extension, outil local).

Un objet JSON par ligne UTF-8 (trame ≤ 16 Mio). Méthodes : ``server.info``, ``job.submit``, ``job.cancel``,
``job.get``, ``job.list``, ``server.shutdown`` ; notifications ``job.event`` (événement tel quel) et
``server.exiting`` (``reason`` : ``shutdown``, ``update``, ``client_gone``). Erreurs JSON-RPC standard, erreur
de l'API → ``-32000`` avec ``data`` = erreur publique.

Le protocole a ses propres descripteurs : stdout et stdin d'origine sont dupliqués, puis fd 1 → stderr et
fd 0 → ``os.devnull`` (aucun sous-processus n'hérite du protocole, toute impression parasite part sur stderr).
Fil principal : boucle Qt (des tâches y livrent leur fin) réveillée par un minuteur ; un fil lit stdin ; un fil
écrit. Écrire ne bloque jamais : file bornée en octets ; pleine (client qui ne lit plus) ou pipe fermé → client
parti, arrêt. Arrêt : un coordinateur annule et attend les jobs pendant que la boucle Qt continue.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import signal
import sys
import threading
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO

from core.jobs.json_io import json_default
from core.json_documents import parse_json_text
from core.service.catalog import CATALOG
from core.service.errors import ApiError
from core.service.events import EVENT_VERSION, Event, EventType
from core.service.manager import JobManager
from core.service.operations import API_VERSION, Catalog
from core.version import APP_VERSION

PROTOCOL_VERSION = 1
#: Taille maximale d'une trame (ligne JSON complète, fin de ligne comprise), entrante ou sortante.
MAX_FRAME = 16 * 1024 * 1024
#: Octets en attente d'écriture au-delà desquels le client est considéré parti (il ne lit plus).
MAX_QUEUED = 64 * 1024 * 1024
#: Délai accordé au vidage final de la file d'écriture.
FLUSH_TIMEOUT_S = 5.0
_WAKE_MS = 100
#: Longueur maximale d'un ``id`` texte (entiers : 64 bits signés) : toute réponse d'erreur reste sous :data:`MAX_FRAME`.
MAX_ID_LENGTH = 256
#: Longueur des textes conservés dans une réponse d'erreur remplacée faute de place.
_SHORT_TEXT = 4096

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR, API_ERROR = (
    -32700, -32600, -32601, -32602, -32603, -32000)


def _encode(message: dict[str, Any]) -> bytes:
    """Trame UTF-8 ; un substitut isolé (``\\ud800`` reçu du client) reste échappé : JSON valide, jamais d'erreur."""
    text = json.dumps(message, ensure_ascii=False, separators=(",", ":"), default=json_default)
    return text.encode("utf-8", "backslashreplace") + b"\n"


class _Output:
    """File d'écriture bornée en octets, vidée par un fil dédié ; ``send`` ne bloque jamais."""

    def __init__(self, stream: BinaryIO, on_gone: Callable[[], None], limit: int = MAX_QUEUED) -> None:
        self._stream = stream
        self._on_gone = on_gone
        self._limit = limit
        self._queue: collections.deque[bytes] = collections.deque()
        self._queued = 0
        self._busy = False
        self._gone = False
        self._cond = threading.Condition()
        threading.Thread(target=self._write_loop, name="serve-writer", daemon=True).start()

    def send(self, frame: bytes) -> None:
        with self._cond:
            if self._gone:
                return
            if self._queued + len(frame) > self._limit:
                self._gone = True  # le client ne lit plus : lignes suivantes abandonnées
                self._queue.clear()
                gone = True
            else:
                self._queue.append(frame)
                self._queued += len(frame)
                self._cond.notify()
                gone = False
        if gone:
            self._on_gone()

    def flush(self, timeout: float) -> None:
        """Attend que la file soit écrite (au plus ``timeout`` s ; un écrivain bloqué n'empêche pas la sortie)."""
        with self._cond:
            self._cond.wait_for(lambda: self._gone or (not self._queue and not self._busy), timeout)

    def _write_loop(self) -> None:
        while True:
            with self._cond:
                self._cond.wait_for(lambda: bool(self._queue) or self._gone)
                if self._gone:
                    return
                frame = self._queue.popleft()
                self._queued -= len(frame)
                self._busy = True
            try:
                self._stream.write(frame)
                self._stream.flush()
            except (OSError, ValueError):  # pipe fermé par le client
                with self._cond:
                    self._gone = True
                    self._busy = False
                    self._queue.clear()
                    self._cond.notify_all()
                self._on_gone()
                return
            with self._cond:
                self._busy = False
                self._cond.notify_all()


class _ProtocolError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Server:
    """Traitement des trames du client et relais des événements des jobs."""

    def __init__(self, out_stream: BinaryIO, *, catalog: Catalog, config_factory: Callable[[], Any],
                 allow_privileged: bool, max_jobs: int, max_analyses: int, queue_limit: int = MAX_QUEUED) -> None:
        self._out = _Output(out_stream, lambda: self.stop("client_gone"), queue_limit)
        self._info = {"protocol": PROTOCOL_VERSION, "api_version": API_VERSION, "event_version": EVENT_VERSION,
                      "app_version": APP_VERSION, "privileged": allow_privileged, "max_jobs": max_jobs,
                      "max_analyses": max_analyses}
        self.manager = JobManager(catalog, config_factory, _Relay(self), max_jobs=max_jobs,
                                  max_analyses=max_analyses, allow_privileged=allow_privileged)
        self._lock = threading.Lock()
        self._stopping = False
        #: Levé par le coordinateur quand les jobs sont terminés : la boucle Qt peut s'arrêter.
        self.stopped = threading.Event()

    # -- Sortie -----------------------------------------------------------------------------------------------

    def _send(self, message: dict[str, Any], *, too_large: Callable[[int], dict[str, Any]]) -> None:
        """Toute trame sortante ≤ :data:`MAX_FRAME` : sinon remplacée (``too_large``), remplacement compris."""
        frame = _encode(message)
        if len(frame) > MAX_FRAME:
            frame = _encode(too_large(len(frame)))
        if len(frame) > MAX_FRAME:  # filet : remplacements bornés par construction (id borné, textes coupés)
            frame = _encode(_error_message(None, INTERNAL_ERROR, "Trame trop volumineuse."))
        self._out.send(frame)

    def respond(self, request_id: Any, result: Any) -> None:
        message = {"jsonrpc": "2.0", "id": request_id, "result": result}
        self._send(message, too_large=lambda size: _error_message(
            request_id, API_ERROR, "Réponse trop volumineuse.",
            {"code": "response_too_large", "message": f"Réponse de {size} octets (maximum {MAX_FRAME}).",
             "size": size}))

    def fail(self, request_id: Any, code: int, message: str, data: Any = None) -> None:
        def shorter(_size: int) -> dict[str, Any]:  # erreur répétant une très longue valeur du client
            text = _shorten(message)
            api_code = data.get("code") if isinstance(data, dict) else None
            return _error_message(request_id, code, text,
                                  {"code": api_code, "message": text} if isinstance(api_code, str) else None)

        self._send(_error_message(request_id, code, message, data), too_large=shorter)

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params},
                   too_large=lambda size: {"jsonrpc": "2.0", "method": method, "params": {}})

    def relay(self, event: Event) -> None:
        payload = event.to_dict()

        def replacement(size: int) -> dict[str, Any]:
            envelope = {key: payload[key] for key in ("v", "job", "parent", "seq", "ts")}
            return {"jsonrpc": "2.0", "method": "job.event", "params": {
                **envelope, "type": EventType.WARNING.value, "code": "event_too_large", "event_type": event.type.value,
                "size": size, "message": f"Événement {event.type.value} trop volumineux ({size} octets) : non transmis."}}

        self._send({"jsonrpc": "2.0", "method": "job.event", "params": payload}, too_large=replacement)
        if event.type is EventType.RESULT and event.parent is None and event.data.get("result", {}).get(
                "restart_required"):
            self.stop("update")  # mise à jour installée : le client relance le worker

    # -- Entrée -----------------------------------------------------------------------------------------------

    def handle(self, line: bytes) -> None:
        """Une trame du client (sans sa fin de ligne)."""
        if not line.strip():
            return
        try:  # JSON standard : NaN / Infinity refusés, imbrication excessive refusée
            message = parse_json_text(line.decode("utf-8"))
        except ValueError as exc:  # UnicodeDecodeError compris
            self.fail(None, PARSE_ERROR, f"JSON illisible : {_shorten(str(exc))}")
            return
        try:
            method, params, request_id, notification = _request_parts(message)
        except _ProtocolError as error:  # requête invalide : réponse avec un id nul si illisible
            self.fail(_valid_id(message), error.code, error.message)
            return
        try:
            self._dispatch(method, params, request_id, notification)
        except _ProtocolError as error:
            if not notification:  # une notification n'a jamais de réponse, même en erreur
                self.fail(request_id, error.code, error.message)
        except ApiError as error:
            if not notification:
                self.fail(request_id, API_ERROR, error.message, error.to_dict())
        except Exception as exc:  # noqa: BLE001 - défaut imprévu : réponse d'erreur, le lecteur continue
            if not notification:
                self.fail(request_id, INTERNAL_ERROR, f"Erreur interne : {type(exc).__name__}: {exc}")

    def _dispatch(self, method: str, params: Any, request_id: Any, notification: bool) -> None:
        if not isinstance(params, dict):
            raise _ProtocolError(INVALID_PARAMS, "params : objet attendu.")

        def reply(result: Any) -> None:
            if not notification:
                self.respond(request_id, result)

        if method == "server.info":
            reply(dict(self._info))
        elif method == "job.submit":
            operation = params.get("operation")
            request = params.get("request", {})
            base_dir = params.get("base_dir")
            if not isinstance(operation, str) or not isinstance(request, dict) or not (
                    base_dir is None or isinstance(base_dir, str)):
                raise _ProtocolError(INVALID_PARAMS, "job.submit : operation (texte), request (objet), base_dir "
                                                     "(texte, facultatif) attendus.")
            # Réponse enfilée dès l'admission : elle précède tout événement du job.
            self.manager.submit(operation, request, base_dir=Path(base_dir) if base_dir is not None else None,
                                on_admitted=lambda job: reply({"job": job}))
        elif method in ("job.cancel", "job.get"):
            job = params.get("job")
            if not isinstance(job, str):
                raise _ProtocolError(INVALID_PARAMS, f"{method} : job (texte) attendu.")
            reply({"cancelled": self.manager.cancel(job)} if method == "job.cancel"
                  else self.manager.get(job).to_dict())
        elif method == "job.list":
            reply({"jobs": [summary.to_dict() for summary in self.manager.list()]})
        elif method == "server.shutdown":
            reply({})
            self.stop("shutdown")
        else:
            raise _ProtocolError(METHOD_NOT_FOUND, f"Méthode inconnue : {method}")

    # -- Arrêt ------------------------------------------------------------------------------------------------

    def stop(self, reason: str) -> None:
        """Déclenche l'arrêt (une seule fois) : coordinateur dans son propre fil, boucle Qt maintenue."""
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
        threading.Thread(target=self._coordinate, args=(reason,), name="serve-shutdown", daemon=True).start()

    def _coordinate(self, reason: str) -> None:
        try:
            self.notify("server.exiting", {"reason": reason})
            self.manager.shutdown()
        finally:
            self.stopped.set()

    def finish(self) -> None:
        self._out.flush(FLUSH_TIMEOUT_S)


class _Relay:
    """Destination des événements du gestionnaire : notifications ``job.event``."""

    def __init__(self, server: Server) -> None:
        self._server = server

    def emit(self, event: Event) -> None:
        self._server.relay(event)


def _error_message(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _shorten(text: str) -> str:
    return text if len(text) <= _SHORT_TEXT else text[:_SHORT_TEXT] + "…"


def _valid_id(message: Any) -> Any:
    """``id`` utilisable dans une réponse (texte d'au plus :data:`MAX_ID_LENGTH` caractères, entier 64 bits ou
    null), sinon null."""
    request_id = message.get("id") if isinstance(message, dict) else None
    if isinstance(request_id, str):
        return request_id if len(request_id) <= MAX_ID_LENGTH else None
    if isinstance(request_id, int) and not isinstance(request_id, bool):
        return request_id if -2 ** 63 <= request_id < 2 ** 63 else None
    return None


def _request_parts(message: Any) -> tuple[str, Any, Any, bool]:
    """Méthode, paramètres, ``id`` et nature (notification : sans ``id``) d'une requête JSON-RPC 2.0 valide."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        raise _ProtocolError(INVALID_REQUEST, "Requête JSON-RPC 2.0 attendue (objet, jsonrpc, method).")
    if "id" in message and message["id"] is not None and _valid_id(message) is None:
        raise _ProtocolError(INVALID_REQUEST, f"id : texte (au plus {MAX_ID_LENGTH} caractères), entier 64 bits "
                                              "ou null attendu.")
    return message["method"], message.get("params", {}), message.get("id"), "id" not in message


def _read_loop(stream: BinaryIO, server: Server) -> None:
    """Fil lecteur : une trame par ligne ; trame trop grande ignorée (erreur -32600) ; fin de stdin (ou fin
    imprévue du lecteur) → arrêt : le worker ne reste jamais sans lecteur."""
    try:
        while True:
            line = stream.readline(MAX_FRAME + 1)
            if not line:
                break
            if len(line) > MAX_FRAME:
                while line and not line.endswith(b"\n"):  # reste de la trame ignoré
                    line = stream.readline(MAX_FRAME)
                server.fail(None, INVALID_REQUEST, f"Trame trop volumineuse (maximum {MAX_FRAME} octets).")
                continue
            try:
                server.handle(line.rstrip(b"\r\n"))
            except Exception:  # noqa: BLE001 - défaut d'une trame : signalé sur stderr, les suivantes restent lues
                traceback.print_exc()
    except (OSError, ValueError):
        pass
    finally:
        server.stop("client_gone")


def run_server(in_stream: BinaryIO, out_stream: BinaryIO, *, catalog: Catalog = CATALOG,
               config_factory: Callable[[], Any], allow_privileged: bool = False, max_jobs: int = 1,
               max_analyses: int = 2, queue_limit: int = MAX_QUEUED, handle_signals: bool = False) -> int:
    """Sert un client jusqu'à l'arrêt ; à appeler depuis le fil de l'application Qt (boucle principale)."""
    from PySide6.QtCore import QCoreApplication, QTimer

    app = QCoreApplication.instance() or QCoreApplication([sys.argv[0]])
    server = Server(out_stream, catalog=catalog, config_factory=config_factory, allow_privileged=allow_privileged,
                    max_jobs=max_jobs, max_analyses=max_analyses, queue_limit=queue_limit)
    previous: dict[signal.Signals, Any] = {}
    # Un signal ne fait que noter la demande (affectation simple : aucun verrou dans le gestionnaire, qui peut
    # interrompre le fil principal n'importe où, y compris un premier arrêt) ; le minuteur la traite.
    requested = [False]
    if handle_signals:
        for number in (signal.SIGINT, signal.SIGTERM):
            previous[number] = signal.signal(number, lambda *_args: requested.__setitem__(0, True))

    def wake() -> None:  # gestionnaires de signaux Python exécutés ; demande d'arrêt et fin surveillées
        if requested[0]:
            server.stop("shutdown")
        if server.stopped.is_set():
            app.quit()

    waker = QTimer()
    waker.setInterval(_WAKE_MS)
    waker.timeout.connect(wake)
    waker.start()
    threading.Thread(target=_read_loop, args=(in_stream, server), name="serve-reader", daemon=True).start()
    try:
        while not server.stopped.is_set():
            app.exec()  # rendue par quit() une fois l'arrêt terminé (ou par un quit() étranger : on reprend)
    finally:
        waker.stop()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    server.finish()
    return 0


def _protect_streams() -> tuple[BinaryIO, BinaryIO]:
    """Descripteurs du protocole (copies de stdin / stdout) ; fd 1 → stderr, fd 0 → ``os.devnull``."""
    sys.stdout.flush()
    protocol_out = os.fdopen(os.dup(1), "wb")
    protocol_in = os.fdopen(os.dup(0), "rb")
    os.dup2(2, 1)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    sys.stdout = sys.stderr
    return protocol_in, protocol_out


def cmd_serve(args: argparse.Namespace, config: Any, logger: Any) -> int:
    """``serve`` : worker JSON-RPC sur stdio (voir le module)."""
    from core.config import AppConfig

    first = [config]

    def config_factory() -> Any:  # configuration déjà lue au lancement, relue ensuite (après une exclusive)
        return first.pop() if first else AppConfig()

    protocol_in, protocol_out = _protect_streams()
    return run_server(protocol_in, protocol_out, config_factory=config_factory,
                      allow_privileged=bool(args.allow_privileged), max_jobs=args.max_jobs,
                      max_analyses=args.max_analyses, handle_signals=True)


__all__ = ["MAX_FRAME", "MAX_QUEUED", "PROTOCOL_VERSION", "Server", "cmd_serve", "run_server"]
