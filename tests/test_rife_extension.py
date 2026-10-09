"""Extension mvo-rife côté Muxiveo : capacités du moteur, préréglages fournis, résolution de l'outil."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from core import plugins
from core.version import MVO_RIFE_CONTRACT
from core.workflows.encode.interpolation import (
    INTERPOLATION_PRESETS,
    InterpolationPreset,
    InterpolationSource,
    RifeCapabilities,
    _legacy_capabilities,
    build_rife_stage,
    clear_rife_version_cache,
    interpolation_preset,
    rife_capabilities,
    rife_presets,
    rife_support_errors,
)

_CAPS = {
    "name": "muxiveo-rife", "version": "1.7.0", "contract": MVO_RIFE_CONTRACT,
    "options": ["factor", "model", "engine", "tta", "ultra", "uhd", "trt-plugin", "large-motion", "selector"],
    "models": ["rife-v4.6", "rife-v4.15-mvo1"], "selector": {"format": 1, "status": "ok", "families": ["v46"]},
}


@pytest.fixture(autouse=True)
def _fresh_caches():
    clear_rife_version_cache()
    yield
    clear_rife_version_cache()


def _binary(tmp_path: Path, presets: dict | None = None) -> Path:
    binary = tmp_path / "muxiveo-rife"
    binary.write_bytes(b"binaire")
    if presets is not None:
        (tmp_path / "presets.json").write_text(json.dumps(presets), encoding="utf-8")
    return binary


def _run(version: str, caps: dict | None):
    def _fake(cmd, **_kw):
        if cmd[1] == "--version":
            return subprocess.CompletedProcess(cmd, 0, stdout=f"muxiveo-rife {version} (ncnn 1)\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(caps) if caps else "illisible", stderr="")
    return _fake


def test_capabilities_read_from_recent_binary(tmp_path):
    binary = _binary(tmp_path)
    with patch("core.workflows.encode.interpolation.subprocess.run", side_effect=_run("1.7.0", _CAPS)) as run:
        caps = rife_capabilities(str(binary))
        assert rife_capabilities(str(binary)) == caps  # cache
    assert [c.args[0][1] for c in run.call_args_list] == ["--version", "--capabilities"]
    assert caps is not None and caps.version == (1, 7, 0) and caps.contract == MVO_RIFE_CONTRACT
    assert caps.supports("ultra") and not caps.supports("selector-weights")
    assert caps.models == frozenset({"rife-v4.6", "rife-v4.15-mvo1"}) and caps.selector_status == "ok"


def test_capabilities_derived_for_older_binary(tmp_path):
    binary = _binary(tmp_path)
    with patch("core.workflows.encode.interpolation.subprocess.run", side_effect=_run("1.3.0", None)) as run:
        caps = rife_capabilities(str(binary))
    assert run.call_count == 1  # --version seulement
    assert caps == _legacy_capabilities((1, 3, 0))
    assert caps.supports("engine") and caps.supports("tta") and not caps.supports("ultra") and caps.models is None


def test_unreadable_capabilities_fall_back_to_version(tmp_path):
    binary = _binary(tmp_path)
    with patch("core.workflows.encode.interpolation.subprocess.run", side_effect=_run("1.7.0", None)):
        caps = rife_capabilities(str(binary))
    assert caps is not None and caps.supports("ultra") and caps.models is None


def test_presets_come_from_extension(tmp_path):
    binary = _binary(tmp_path, {"schema": 1, "presets": {
        "quality": {"engine": "hybrid", "model": "rife-v5-mvo2", "args": ["--large-motion", "12"]},
        "ultra": {"engine": "turbo", "model": "x"},          # moteur inconnu : préréglage intégré conservé
        "light": {"engine": "rife", "model": "rife-v5-lite", "args": "--uhd"},  # args invalides
        "inconnu": {"engine": "rife", "model": "m"},         # identifiant inconnu de Muxiveo : ignoré
    }})
    presets = rife_presets(str(binary))
    assert presets["quality"] == InterpolationPreset("hybrid", "rife-v5-mvo2", args=("--large-motion", "12"))
    assert presets["ultra"] == INTERPOLATION_PRESETS["ultra"] and presets["light"] == INTERPOLATION_PRESETS["light"]
    assert "inconnu" not in presets
    assert interpolation_preset("max", str(binary)).model == "rife-v5-mvo2"  # ancien nom migré
    stage = build_rife_stage(str(binary), quality="quality", source=InterpolationSource())
    assert stage[stage.index("--model") + 1] == "rife-v5-mvo2"
    assert stage[stage.index("--engine") + 1] == "hybrid" and stage[stage.index("--large-motion") + 1] == "12"


def test_presets_keep_muxiveo_interface_rules(tmp_path):
    binary = _binary(tmp_path, {"schema": 1, "presets": {"light": {"engine": "rife", "model": "rife-v5-lite"},
                                                          "ultra": {"engine": "hybrid", "model": "rife-v5"}}})
    presets = rife_presets(str(binary))
    assert presets["light"].force_fast and presets["ultra"].ultra
    assert "--uhd" in build_rife_stage(str(binary), quality="light", source=InterpolationSource())
    assert "--ultra" in build_rife_stage(str(binary), quality="ultra", source=InterpolationSource())


def test_presets_without_file_use_builtin_table(tmp_path):
    assert rife_presets(str(_binary(tmp_path))) == INTERPOLATION_PRESETS


def _caps(**kw) -> RifeCapabilities:
    base = dict(version=(1, 7, 0), contract=MVO_RIFE_CONTRACT, options=frozenset(_CAPS["options"]),
                models=frozenset(_CAPS["models"]), selector_status="ok")
    base.update(kw)
    return RifeCapabilities(**base)  # type: ignore[arg-type]


def test_support_errors_cover_contract_options_models_and_selector(tmp_path):
    rife = str(_binary(tmp_path))
    quality = InterpolationPreset("hybrid", "rife-v4.15-mvo1")
    ultra = InterpolationPreset("hybrid", "rife-v4.15-mvo1", ultra=True)
    assert rife_support_errors(_caps(), quality, tta=2, rife_bin=rife) == []
    assert rife_support_errors(None, quality, tta=1, rife_bin=rife) == []
    contract = rife_support_errors(_caps(contract=MVO_RIFE_CONTRACT + 1), quality, tta=1, rife_bin=rife)
    assert len(contract) == 1 and "contrat" in contract[0] and "page Extensions" in contract[0]
    missing = rife_support_errors(_caps(models=frozenset({"rife-v4.6"})), quality, tta=1, rife_bin=rife)
    assert len(missing) == 1 and "rife-v4.15-mvo1" in missing[0]
    selector = rife_support_errors(_caps(selector_status="fichier introuvable"), ultra, tta=1, rife_bin=rife)
    assert len(selector) == 1 and "Ultra" in selector[0] and "fichier introuvable" in selector[0]
    args = InterpolationPreset("hybrid", "rife-v4.6", args=("--nouvelle-option", "1"))
    unknown = rife_support_errors(_caps(), args, tta=1, rife_bin=rife)
    assert len(unknown) == 1 and "--nouvelle-option" in unknown[0]


def test_config_prefers_installed_extension(tmp_path, monkeypatch):
    from core.config import AppConfig

    config = AppConfig()
    before = config.tool_muxiveo_rife
    executable = tmp_path / "ext" / "muxiveo-rife"
    monkeypatch.setattr(plugins, "rife_executable", lambda *_a, **_k: executable)
    assert config.refresh_rife_tool() == str(executable) == config.tool_muxiveo_rife
    assert config.tool_commands()["muxiveo-rife"] == str(executable)
    monkeypatch.setattr(plugins, "rife_executable", lambda *_a, **_k: None)
    assert config.refresh_rife_tool() == before
