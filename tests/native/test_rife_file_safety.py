"""Protection des fichiers RIFE, vérifiée sans modèle ni GPU Vulkan."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


RIFE_BIN = os.environ.get("MUXIVEO_RIFE_BIN") or shutil.which("muxiveo-rife") or ""
pytestmark = pytest.mark.skipif(not RIFE_BIN, reason="Binaire muxiveo-rife requis")


@pytest.mark.parametrize("alias", ["same", "relative", "hardlink", "symlink"])
def test_same_input_output_is_rejected_without_truncation(tmp_path: Path, alias: str) -> None:
    """Même fichier par nom ou identité : refus avant Vulkan et avant écriture."""
    payload = b"YUV4MPEG2 W2 H2 F25:1 Ip C420jpeg\nFRAME\n" + bytes(6)
    source = tmp_path / "entrée avec espace.y4m"
    source.write_bytes(payload)
    output = source
    output_arg = str(output)
    if alias == "relative":
        output_arg = source.name
    elif alias in {"hardlink", "symlink"}:
        output = tmp_path / "alias.y4m"
        try:
            if alias == "hardlink":
                os.link(source, output)
            else:
                output.symlink_to(source)
        except OSError as exc:
            pytest.skip(f"Lien {alias} indisponible : {exc}")
        output_arg = str(output)
    result = subprocess.run(
        [RIFE_BIN, "-i", str(source), "-o", output_arg, "--factor", "1"],
        cwd=tmp_path,
        # Pilote volontairement absent : le refus doit précéder Vulkan.
        env={**os.environ, "VK_ICD_FILENAMES": str(tmp_path / "absent-vulkan.json"),
             "VK_DRIVER_FILES": str(tmp_path / "absent-vulkan.json")},
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 1, result.stderr.decode("utf-8", errors="replace")
    assert "même fichier" in result.stderr.decode("utf-8")
    assert result.stdout == b""
    assert source.read_bytes() == payload
    assert output.read_bytes() == payload
