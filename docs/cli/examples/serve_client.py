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
from queue import Queue
from typing import Any


class Worker:
    """Un worker = un client : requêtes JSON-RPC par ligne, réponses et notifications lues dans un fil."""

    def __init__(self, command: list[str]) -> None:
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.messages: Queue[dict[str, Any]] = Queue()
        self._ids = itertools.count(1)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.messages.put(json.loads(line))
        self.messages.put({"method": "eof"})

    def send(self, method: str, params: dict[str, Any] | None = None) -> int:
        request_id = next(self._ids)
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        self.process.stdin.flush()
        return request_id


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    source = Path(sys.argv[1]).resolve()
    worker = Worker(sys.argv[2:] or ["muxiveo", "--cli", "serve"])
    submit = worker.send("job.submit", {"operation": "media.inspect", "request": {"inputs": [source.name]},
                                        "base_dir": str(source.parent)})
    job = None
    while True:
        message = worker.messages.get(timeout=120)
        if message.get("id") == submit:  # la réponse précède tout événement du job
            if "error" in message:
                print("refusé :", message["error"])
                break
            job = message["result"]["job"]
        elif message.get("method") == "job.event" and message["params"]["job"] == job:
            event = message["params"]
            print(event["seq"], event["type"], event.get("state") or event.get("message") or "")
            if event["type"] == "result":
                files = event["result"]["files"]
                print("pistes :", [track["type"] for track in files[0]["tracks"]])
            if event["type"] == "state" and event["state"] in ("succeeded", "failed", "cancelled"):
                break
    worker.send("server.shutdown")
    return worker.process.wait(60)


if __name__ == "__main__":
    raise SystemExit(main())
