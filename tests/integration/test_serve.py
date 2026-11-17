"""Worker ``serve`` réel (sous-processus, médias de synthèse) : jobs concurrents, analyse pendant un encodage,
annulation, refus d'une opération privilégiée, arrêt propre."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest


pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                reason="ffmpeg/ffprobe requis")

ROOT = Path(__file__).resolve().parents[2]


class _Worker:
    def __init__(self, tmp_path: Path, *args: str) -> None:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
               "XDG_CONFIG_HOME": str(tmp_path / "xdg-config")}
        self.process = subprocess.Popen([sys.executable, str(ROOT / "main.py"), "--cli", "serve", *args], cwd=ROOT,
                                        env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE)
        self.messages: list[dict[str, Any]] = []
        self._cond = threading.Condition()
        self._next = 0
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=lambda: self.process.stderr.read(), daemon=True).start()  # type: ignore[union-attr]

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            with self._cond:
                self.messages.append(json.loads(line))
                self._cond.notify_all()

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next += 1
        request_id = self._next
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method,
                                             "params": params or {}}).encode() + b"\n")
        self.process.stdin.flush()
        return self.wait(lambda m: m.get("id") == request_id)

    def wait(self, predicate: Callable[[dict[str, Any]], bool], timeout: float = 240) -> dict[str, Any]:
        with self._cond:
            assert self._cond.wait_for(lambda: any(predicate(m) for m in self.messages), timeout)
            return next(m for m in self.messages if predicate(m))

    def terminal(self, job: str) -> str:
        return self.wait(lambda m: m.get("method") == "job.event" and m["params"]["job"] == job
                         and m["params"]["type"] == "state"
                         and m["params"]["state"] in ("succeeded", "failed", "cancelled", "partial"))["params"]["state"]

    def submit(self, operation: str, request: dict[str, Any]) -> str:
        return self.request("job.submit", {"operation": operation, "request": request})["result"]["job"]


def test_concurrent_jobs_analysis_cancel_and_clean_shutdown(tmp_path):
    src = tmp_path / "source.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25", "-f", "lavfi",
                    "-i", "sine=frequency=500:sample_rate=48000", "-t", "10", "-c:v", "libx264", "-preset",
                    "ultrafast", "-c:a", "aac", "-shortest", "-y", str(src)], check=True)
    worker = _Worker(tmp_path, "--max-jobs", "2")
    try:
        info = worker.request("server.info")["result"]
        assert info["max_jobs"] == 2 and info["privileged"] is False
        encode = worker.submit("media.run", {"job": {
            "sources": [str(src)], "output": str(tmp_path / "encode.mkv"),
            "encode": {"video": {"default": {"codec": "libx264", "crf": 30, "encoder_preset": "placebo"}}}},
            "options": {"nfo": False}})
        remux = worker.submit("media.run", {"job": {"sources": [str(src)], "output": str(tmp_path / "remux.mkv")},
                                            "options": {"nfo": False}})
        worker.wait(lambda m: m.get("method") == "job.event" and m["params"]["job"] == encode
                    and m["params"]["type"] == "progress")
        inspect = worker.submit("media.inspect", {"inputs": [str(src)]})  # pendant l'encodage
        assert worker.terminal(inspect) == "succeeded"
        assert worker.terminal(remux) == "succeeded" and (tmp_path / "remux.mkv").is_file()
        assert worker.terminal(encode) == "succeeded" and (tmp_path / "encode.mkv").is_file()
        with worker._cond:
            ends = [m["params"]["job"] for m in worker.messages if m.get("method") == "job.event"
                    and m["params"]["type"] == "state" and m["params"]["state"] == "succeeded"]
        assert ends.index(inspect) < ends.index(encode)  # analyse servie pendant l'encodage, pas après

        slow = worker.submit("media.run", {"job": {
            "sources": [str(src)], "output": str(tmp_path / "annule.mkv"),
            "encode": {"video": {"default": {"codec": "libx264", "crf": 18, "encoder_preset": "placebo"}}}},
            "options": {"nfo": False}})
        worker.wait(lambda m: m.get("method") == "job.event" and m["params"]["job"] == slow
                    and m["params"]["type"] == "progress")
        assert worker.request("job.cancel", {"job": slow})["result"] == {"cancelled": True}
        assert worker.terminal(slow) == "cancelled" and not (tmp_path / "annule.mkv").exists()

        refused = worker.request("job.submit", {"operation": "config.set",
                                                "request": {"key": "ui.theme", "value": "light"}})["error"]
        assert refused["data"]["code"] == "forbidden"
        assert worker.request("server.shutdown")["result"] == {}
        assert worker.wait(lambda m: m.get("method") == "server.exiting")["params"] == {"reason": "shutdown"}
        assert worker.process.wait(60) == 0
    finally:
        if worker.process.poll() is None:
            worker.process.kill()
            worker.process.wait(10)
