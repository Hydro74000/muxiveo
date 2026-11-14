"""Administration headless en configuration isolée : config set/unset/reset, workdir, nvencc status."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _cli(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "XDG_CONFIG_HOME": str(home / "xdg"),
           "XDG_DATA_HOME": str(home / "data"), "XDG_CACHE_HOME": str(home / "cache"),
           "MUXIVEO_CONFIG_HOME": str(home / "conf")}
    return subprocess.run([sys.executable, str(ROOT / "main.py"), "--cli", *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=120)


def _entry(home: Path, key: str) -> dict:
    result = _cli(home, "config", "get", key, "--json")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["entries"][0]


def test_config_set_unset_reset_follow_the_settings_panel(tmp_path):
    paths = _cli(tmp_path, "config", "path", "--json")
    assert json.loads(paths.stdout)["config_ini"] == str(tmp_path / "conf" / "config.ini")
    assert _cli(tmp_path, "config", "set", "ui.theme", "light").returncode == 0
    assert "theme = light" in (tmp_path / "conf" / "config.ini").read_text(encoding="utf-8")
    assert _entry(tmp_path, "ui.theme") | {"value": "light", "source": "ini"} == _entry(tmp_path, "ui.theme")
    # unset : la surcharge disparaît, le réglage enregistré (save) prend le relais.
    unset = _cli(tmp_path, "config", "unset", "ui.theme", "--json")
    assert unset.returncode == 0 and json.loads(unset.stdout)["warnings"]
    assert _entry(tmp_path, "ui.theme")["source"] == "settings"
    reset = _cli(tmp_path, "config", "reset", "ui.theme", "--json")
    assert json.loads(reset.stdout)["entries"][0] | {"value": "dark", "source": "default"} == \
        json.loads(reset.stdout)["entries"][0]
    for bad in (("ui.theme", "violet"), ("ui.ui_scale_percent", "500"), ("ui.check_updates", "peut-être")):
        refused = _cli(tmp_path, "config", "set", *bad)
        assert refused.returncode == 3, refused.stderr
    unknown = _cli(tmp_path, "config", "get", "theme")
    assert unknown.returncode == 2 and "ui.theme" in unknown.stderr
    plugins = _cli(tmp_path, "config", "set", "plugins.auto_update", "false")
    assert plugins.returncode == 0 and _entry(tmp_path, "plugins.auto_update")["value"] is False


def test_workdir_clean_respects_ownership(tmp_path):
    work = tmp_path / "work"
    (work / "a-moi").mkdir(parents=True)
    listed = _cli(tmp_path, "workdir", "list", "--work-dir", str(work), "--json")
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout)["entries"] == [], "racine non marquée : rien d'adopté"
    cleaned = _cli(tmp_path, "workdir", "clean", "--work-dir", str(work), "--dry-run", "--json")
    assert json.loads(cleaned.stdout)["dry_run"] is True and (work / "a-moi").is_dir()


def test_nvencc_status_is_readable(tmp_path):
    result = _cli(tmp_path, "nvencc", "status", "--json")
    assert result.returncode == 0, result.stderr
    assert {"supported", "nvidia", "method", "provenance"} <= set(json.loads(result.stdout))
