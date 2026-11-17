"""Worker ``serve`` : protocole JSON-RPC, ordre réponse / événements, bornes des trames, client parti, arrêt."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from PySide6.QtCore import QObject, Qt, Signal

from cli.serve import MAX_FRAME, run_server
from core.service import ExecutionContext, Model, operation
from core.service.operations import Catalog

_GATE = threading.Event()


@dataclass(frozen=True)
class _Request(Model):
    text: str = ""
    repeat: int = 1


@dataclass(frozen=True)
class _Result(Model):
    text: str = ""
    restart_required: bool = False


@operation("t.echo", request=_Request, result=_Result, summary="Écho")
def _echo(request: _Request, _ctx: ExecutionContext) -> _Result:
    return _Result(text=request.text * request.repeat)


@operation("t.block", request=_Request, result=_Result, summary="Bloquante", queue="job")
def _block(_request: _Request, ctx: ExecutionContext) -> _Result:
    while not _GATE.wait(0.01):
        ctx.cancel.raise_if_cancelled()
    return _Result()


@operation("t.secret", request=_Request, result=_Result, summary="Privilégiée", privileged=True, queue="exclusive")
def _secret(_request: _Request, _ctx: ExecutionContext) -> _Result:
    return _Result()


@operation("t.update", request=_Request, result=_Result, summary="Mise à jour", queue="exclusive")
def _update(_request: _Request, _ctx: ExecutionContext) -> _Result:
    return _Result(restart_required=True)


class _MainThread(QObject):
    """Exécute une fonction dans le fil de l'application (fin de tâche livrée par la boucle Qt)."""

    call = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.call.connect(lambda function: function(), Qt.ConnectionType.QueuedConnection)


_MAIN: dict[str, _MainThread] = {}


@operation("t.qt", request=_Request, result=_Result, summary="Fin livrée par la boucle Qt")
def _qt(_request: _Request, ctx: ExecutionContext) -> _Result:
    ctx.cancel.wait()  # attend l'arrêt, puis a besoin de la boucle Qt pour se terminer
    delivered = threading.Event()
    _MAIN["caller"].call.emit(delivered.set)
    assert delivered.wait(5), "boucle Qt arrêtée pendant l'arrêt du worker"
    ctx.cancel.raise_if_cancelled()
    return _Result()


_CATALOG = Catalog([_echo, _block, _secret, _update, _qt])


class _Session:
    """Client sur tubes : écrit des trames, lit les réponses dans un fil ; le serveur tourne dans le fil Qt."""

    def __init__(self) -> None:
        server_in, self._client_in = os.pipe()
        self._client_out, server_out = os.pipe()
        self.server_in = os.fdopen(server_in, "rb")
        self.server_out = os.fdopen(server_out, "wb")
        self.writer = os.fdopen(self._client_in, "wb")
        self.reader = os.fdopen(self._client_out, "rb")
        self.messages: list[dict[str, Any]] = []
        self._cond = threading.Condition()
        self._next = 0
        self._reading: threading.Thread | None = None

    def start_reading(self) -> None:
        def loop() -> None:
            for line in self.reader:
                with self._cond:
                    self.messages.append(json.loads(line))
                    self._cond.notify_all()

        self._reading = threading.Thread(target=loop, daemon=True)
        self._reading.start()

    def close(self) -> None:
        """Fermeture sans blocage : un flux n'est jamais fermé pendant qu'un fil le lit ou y écrit."""
        if self._reading is None:
            _quiet(self.reader.close)  # écrivain du serveur éventuellement bloqué : réveillé par EPIPE
        _quiet(self.server_out.close)
        if self._reading is not None:
            self._reading.join(5)  # fin de fichier : le fil de lecture se termine
            _quiet(self.reader.close)

    def send(self, method: str, params: dict[str, Any] | None = None, *, notification: bool = False) -> int:
        self._next += 1
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notification:
            message["id"] = self._next
        self.raw(json.dumps(message).encode() + b"\n")
        return self._next

    def raw(self, data: bytes) -> None:
        self.writer.write(data)
        self.writer.flush()

    def wait(self, predicate: Callable[[dict[str, Any]], bool], timeout: float = 10) -> dict[str, Any]:
        with self._cond:
            assert self._cond.wait_for(lambda: any(predicate(m) for m in self.messages), timeout), self.messages[-5:]
            return next(m for m in self.messages if predicate(m))

    def reply(self, request_id: Any) -> dict[str, Any]:
        return self.wait(lambda m: m.get("id") == request_id and ("result" in m or "error" in m))

    def events(self, job: str) -> list[dict[str, Any]]:
        with self._cond:
            return [m["params"] for m in self.messages if m.get("method") == "job.event" and m["params"]["job"] == job]

    def run(self, script: Callable[[_Session], None], *, read: bool = True, **options: Any) -> int:
        """Serveur dans le fil courant (Qt) ; ``script`` joue le client dans un autre fil (fin de stdin à la fin)."""
        if read:
            self.start_reading()
        failure: list[BaseException] = []

        def client() -> None:
            try:
                script(self)
            except BaseException as exc:  # noqa: BLE001 - relancée dans le fil du test
                failure.append(exc)
            finally:
                try:
                    self.writer.close()  # fin de stdin : arrêt du worker
                except OSError:
                    pass

        thread = threading.Thread(target=client, daemon=True)
        thread.start()
        watchdog = threading.Timer(60, lambda: os.close(self._client_in))
        watchdog.start()
        try:
            code = run_server(self.server_in, self.server_out, catalog=_CATALOG,
                              config_factory=lambda: SimpleNamespace(), **options)
        finally:
            watchdog.cancel()
        thread.join(10)
        if failure:
            raise failure[0]
        return code


@pytest.fixture
def session(qt_app):
    _GATE.clear()
    _MAIN["caller"] = _MainThread()
    created = _Session()
    yield created
    _GATE.set()
    created.close()


def _quiet(close: Callable[[], None]) -> None:
    try:
        close()
    except (OSError, ValueError):
        pass


def _state(event: dict[str, Any]) -> str | None:
    return event.get("state") if event["type"] == "state" else None


def test_protocol_errors_then_info_and_shutdown(session):
    def script(client: _Session) -> None:
        client.raw(b"{pas du json\n")
        assert client.wait(lambda m: m.get("error", {}).get("code") == -32700)["id"] is None
        client.raw(b"[1, 2]\n")
        client.wait(lambda m: m.get("error", {}).get("code") == -32600)
        client.send("job.inconnu", notification=True)  # notification : jamais de réponse
        assert client.reply(client.send("job.inconnu"))["error"]["code"] == -32601
        client.raw(b'{"jsonrpc":"2.0","id":9,"method":"job.get","params":[1]}\n')
        assert client.reply(9)["error"]["code"] == -32602
        info = client.reply(client.send("server.info"))["result"]
        assert info["protocol"] == 1 and info["privileged"] is False and info["max_jobs"] == 1
        assert client.reply(client.send("server.shutdown"))["result"] == {}
        assert client.wait(lambda m: m.get("method") == "server.exiting")["params"] == {"reason": "shutdown"}

    assert session.run(script) == 0


def test_submit_answer_precedes_the_job_events(session):
    def script(client: _Session) -> None:
        request = client.send("job.submit", {"operation": "t.echo", "request": {"text": "bonjour"}})
        job = client.reply(request)["result"]["job"]
        events = client.wait(lambda m: m.get("method") == "job.event" and _state(m["params"]) == "succeeded")
        with client._cond:
            first = next(i for i, m in enumerate(client.messages) if m.get("method") == "job.event"
                         and m["params"]["job"] == job)
            answer = next(i for i, m in enumerate(client.messages) if m.get("id") == request)
        assert answer < first and events["params"]["job"] == job
        assert [_state(e) for e in client.events(job) if _state(e)] == ["queued", "running", "succeeded"]
        assert client.reply(client.send("job.get", {"job": job}))["result"]["result"] == {
            "text": "bonjour", "restart_required": False}
        assert [item["job"] for item in client.reply(client.send("job.list"))["result"]["jobs"]] == [job]

    session.run(script)


def test_api_errors_and_cancellation(session):
    def script(client: _Session) -> None:
        unknown = client.reply(client.send("job.submit", {"operation": "t.nope"}))["error"]
        assert unknown["code"] == -32000 and unknown["data"]["code"] == "unknown_operation"
        secret = client.reply(client.send("job.submit", {"operation": "t.secret"}))["error"]
        assert secret["data"]["code"] == "forbidden"
        relative = client.reply(client.send("job.submit", {"operation": "t.echo", "base_dir": "relatif"}))["error"]
        assert relative["data"]["code"] == "invalid_request"
        assert client.reply(client.send("job.get", {"job": "absent"}))["error"]["data"]["code"] == "invalid_request"
        job = client.reply(client.send("job.submit", {"operation": "t.block"}))["result"]["job"]
        client.wait(lambda m: m.get("method") == "job.event" and _state(m["params"]) == "running")
        assert client.reply(client.send("job.cancel", {"job": job}))["result"] == {"cancelled": True}
        client.wait(lambda m: m.get("method") == "job.event" and _state(m["params"]) == "cancelled")

    session.run(script)


def test_frame_limits_in_and_out(session):
    def script(client: _Session) -> None:
        envelope = len(json.dumps({"jsonrpc": "2.0", "method": "server.info", "params": {"pad": ""}, "id": 1001}))
        fitting = {"jsonrpc": "2.0", "method": "server.info", "params": {"pad": "x" * (MAX_FRAME - envelope - 1)},
                   "id": 1001}
        client.raw(json.dumps(fitting).encode() + b"\n")  # exactement 16 Mio, fin de ligne comprise
        assert "result" in client.reply(1001)
        client.raw(json.dumps({**fitting, "id": 1002, "params": {"pad": "x" * (MAX_FRAME - envelope)}}).encode()
                   + b"\n")
        client.wait(lambda m: m.get("error", {}).get("code") == -32600)
        assert "result" in client.reply(client.send("server.info"))  # trame suivante lue normalement
        big = client.reply(client.send("job.submit", {"operation": "t.echo",
                                                      "request": {"text": "y", "repeat": MAX_FRAME}}))["result"]["job"]
        warning = client.wait(lambda m: m.get("method") == "job.event" and m["params"].get("code") == "event_too_large")
        assert warning["params"]["event_type"] == "result" and warning["params"]["size"] > MAX_FRAME
        client.wait(lambda m: m.get("method") == "job.event" and _state(m["params"]) == "succeeded")
        error = client.reply(client.send("job.get", {"job": big}))["error"]
        assert error["code"] == -32000 and error["data"]["code"] == "response_too_large"

    session.run(script)


def test_a_client_that_stops_reading_is_considered_gone(session):
    def script(client: _Session) -> None:
        for _ in range(4000):  # aucune lecture : la file d'écriture déborde
            try:
                client.send("server.info")
            except OSError:
                return

    started = time.monotonic()
    assert session.run(script, read=False, queue_limit=256 * 1024) == 0
    assert time.monotonic() - started < 30


def test_a_closed_output_pipe_stops_the_worker(session):
    session.reader.close()

    def script(client: _Session) -> None:
        client.send("server.info")
        time.sleep(0.5)

    assert session.run(script, read=False) == 0


def test_end_of_input_cancels_jobs_while_the_qt_loop_still_runs(session):
    holder: dict[str, str] = {}

    def script(client: _Session) -> None:
        holder["job"] = client.reply(client.send("job.submit", {"operation": "t.qt"}))["result"]["job"]
        client.wait(lambda m: m.get("method") == "job.event" and _state(m["params"]) == "running")

    session.run(script)
    states = [_state(event) for event in session.events(holder["job"]) if _state(event)]
    assert states[-1] == "cancelled"
    assert session.wait(lambda m: m.get("method") == "server.exiting")["params"] == {"reason": "client_gone"}


@pytest.mark.skipif(os.name == "nt", reason="signal POSIX")
def test_sigterm_stops_the_worker(session):
    def script(client: _Session) -> None:
        client.reply(client.send("server.info"))
        os.kill(os.getpid(), signal.SIGTERM)
        assert client.wait(lambda m: m.get("method") == "server.exiting")["params"] == {"reason": "shutdown"}

    previous = signal.getsignal(signal.SIGTERM)
    assert session.run(script, handle_signals=True) == 0
    assert signal.getsignal(signal.SIGTERM) is previous


def test_an_installed_update_exits_the_worker(session):
    def script(client: _Session) -> None:
        client.reply(client.send("job.submit", {"operation": "t.update"}))
        assert client.wait(lambda m: m.get("method") == "server.exiting")["params"] == {"reason": "update"}
        time.sleep(0.2)  # le worker s'arrête sans server.shutdown

    assert session.run(script) == 0


def test_stray_output_never_reaches_the_protocol(tmp_path):
    code = (
        "import subprocess, sys\n"
        "from cli.serve import _protect_streams\n"
        "protocol_in, protocol_out = _protect_streams()\n"
        "print('parasite')\n"
        "subprocess.run([sys.executable, '-c', 'print(\"enfant\"); import sys; print(repr(sys.stdin.read()))'])\n"
        "protocol_out.write(b'{\"jsonrpc\":\"2.0\"}\\n'); protocol_out.flush()\n"
        "print(protocol_in.readline().decode().strip())\n"
    )
    result = subprocess.run([sys.executable, "-c", code], input=b"ligne du client\n", capture_output=True,
                            cwd=os.getcwd(), timeout=60, check=True)
    assert result.stdout == b'{"jsonrpc":"2.0"}\n'
    stderr = result.stderr.decode()
    assert "parasite" in stderr and "enfant" in stderr and "''" in stderr  # stdin de l'enfant : os.devnull
    assert "ligne du client" in stderr  # stdin d'origine lu par le protocole seulement
