#!/usr/bin/env python3
"""Client minimal du worker ``muxiveo --cli serve`` (bibliothèque standard seule).

Lance le worker en processus enfant, inspecte un fichier, affiche les événements du job puis arrête
le worker proprement. Usage : ``python3 serve_client.py /chemin/film.mkv [commande du worker…]``
(défaut : ``muxiveo --cli serve``).
"""

from __future__ import annotations

import itertools
import json
import subprocess
import sys
import threading
from pathlib import Path
from queue import Empty, Queue
from typing import Any


class Worker:
    """Un worker = un client : requêtes JSON-RPC par ligne, réponses et notifications lues dans un fil."""

    def __init__(self, command: list[str]) -> None:
        # Commande du worker choisie par l'appelant de l'exemple (argv), sans shell.
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)  # nosec B603
        self.messages: Queue[dict[str, Any]] = Queue()
        self._ids = itertools.count(1)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        if self.process.stdout is None:
            raise RuntimeError("sortie du worker non capturée")
        for line in self.process.stdout:
            self.messages.put(json.loads(line))
        self.messages.put({"method": "eof"})

    def send(self, method: str, params: dict[str, Any] | None = None) -> int:
        request_id = next(self._ids)
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        if self.process.stdin is None:
            raise RuntimeError("entrée du worker non capturée")
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        self.process.stdin.flush()
        return request_id


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    source = Path(sys.argv[1]).resolve()
    worker = Worker(sys.argv[2:] or ["muxiveo", "--cli", "serve"])
    status = 1
    try:
        submit = worker.send("job.submit", {"operation": "media.inspect", "request": {"inputs": [source.name]},
                                            "base_dir": str(source.parent)})
        job = None
        while True:
            try:
                message = worker.messages.get(timeout=120)
            except Empty:
                print("délai dépassé : aucune réponse du worker")
                break
            if message.get("method") == "eof":
                print("le worker s'est arrêté")
                break
            if message.get("id") == submit:  # la réponse précède tout événement du job
                if "error" in message:
                    print("refusé :", message["error"].get("data") or message["error"]["message"])
                    break
                job = message["result"]["job"]
            elif message.get("method") == "job.event" and message["params"]["job"] == job:
                event = message["params"]
                detail = event.get("state") or event.get("message") or event.get("error", {}).get("message", "")
                print(event["seq"], event["type"], detail)
                if event["type"] == "result":
                    print("pistes :", [track["type"] for track in event["result"]["files"][0]["tracks"]])
                if event["type"] == "state" and event["state"] in ("succeeded", "failed", "cancelled"):
                    status = 0 if event["state"] == "succeeded" else 1
                    break
    finally:
        if worker.process.stdin is None:
            raise RuntimeError("entrée du worker non capturée")
        # Demande d'arrêt puis fin de stdin (arrêt même si la demande n'est pas lue) ; worker déjà arrêté : ignoré.
        for step in (lambda: worker.send("server.shutdown"), worker.process.stdin.close):
            try:
                step()
            except OSError:
                pass
        try:
            worker.process.wait(60)
        except subprocess.TimeoutExpired:
            worker.process.kill()
            worker.process.wait()
    return status


if __name__ == "__main__":
    raise SystemExit(main())
