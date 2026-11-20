"""Tests de core/nvencc_install.py : méthode, élévation, extraction .deb / ZIP, replis, étape du premier lancement."""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tarfile
import threading
import zipfile
from pathlib import Path

import pytest

from core import nvencc_install as nv
from core import plugins
from core.github_release import ReleaseAsset

_ASSET = ReleaseAsset("rigaya/NVEnc", "9.40", "nvencc_9.40_amd64.deb", "https://github.com/x", "0" * 64)


def _tar_xz(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _ar(members: dict[str, bytes]) -> bytes:
    out = bytearray(b"!<arch>\n")
    for name, data in members.items():
        header = f"{name + '/':<16}{0:<12}{0:<6}{0:<6}{100644:<8}{len(data):<10}`\n"
        out += header.encode("ascii") + data + (b"\n" if len(data) % 2 else b"")
    return bytes(out)


def _deb(path: Path, members: dict[str, bytes]) -> Path:
    path.write_bytes(_ar({
        "debian-binary": b"2.0\n",
        "control.tar.xz": _tar_xz({"./control": b"Package: nvencc\n"}),
        "data.tar.xz": _tar_xz(members),
    }))
    return path


@pytest.fixture
def user_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    return tmp_path


# ---------------------------------------------------------------------------
# Méthode et élévation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("platform", "atomic", "distro", "tools", "expected"),
    [
        ("win32", False, "unknown", set(), nv.METHOD_WINDOWS),
        ("linux", True, "fedora", {"dnf"}, nv.METHOD_USER),
        ("linux", False, "debian", {"apt"}, nv.METHOD_APT),
        ("linux", False, "fedora", {"dnf"}, nv.METHOD_DNF),
        ("linux", False, "fedora", set(), nv.METHOD_USER),
        ("linux", False, "unknown", {"apt", "dnf"}, nv.METHOD_USER),
    ],
)
def test_plan_method(monkeypatch, platform, atomic, distro, tools, expected):
    monkeypatch.setattr(nv.sys, "platform", platform)
    monkeypatch.setattr(nv, "is_atomic_distro", lambda: atomic)
    monkeypatch.setattr(nv, "linux_distro", lambda: distro)
    monkeypatch.setattr(nv.shutil, "which", lambda name, path=None: f"/usr/bin/{name}" if name in tools else None)
    assert nv.plan_method() == expected


def test_linux_distro_reads_os_release(tmp_path, monkeypatch):
    monkeypatch.setattr(nv.shutil, "which", lambda name, path=None: None)
    release = tmp_path / "os-release"
    release.write_text('ID=fedora\nVARIANT_ID=kinoite\n', encoding="utf-8")
    assert nv.linux_distro(release) == "fedora"
    release.write_text("ID=ubuntu\n", encoding="utf-8")
    assert nv.linux_distro(release) == "debian"
    assert nv.linux_distro(tmp_path / "absent") == "unknown"


def test_privilege_prefix(monkeypatch):
    monkeypatch.setattr(nv.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(nv.shutil, "which", lambda name, path=None: f"/usr/bin/{name}")
    assert nv.privilege_prefix(True, env={}) == ["sudo"]
    assert nv.privilege_prefix(False, env={"WAYLAND_DISPLAY": "wayland-0"}) == ["pkexec"]
    assert nv.privilege_prefix(False, env={}) is None
    monkeypatch.setattr(nv.os, "geteuid", lambda: 0, raising=False)
    assert nv.privilege_prefix(False, env={}) == []


def test_supported_platform():
    assert nv.supported_platform("linux", "x86_64")
    assert nv.supported_platform("win32", "AMD64")
    assert not nv.supported_platform("darwin", "arm64")
    assert not nv.supported_platform("linux", "aarch64")


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def test_extract_deb_binary(tmp_path):
    deb = _deb(tmp_path / "nvencc.deb", {"./usr/bin/nvencc": b"\x7fELF-nvencc", "./usr/share/doc/x": b"doc"})
    dest = nv.extract_deb_binary(deb, tmp_path / "out" / "nvencc")
    assert dest.read_bytes() == b"\x7fELF-nvencc"
    if sys.platform != "win32":  # aucun bit d'exécution sous Windows
        assert dest.stat().st_mode & 0o111


def test_extract_deb_binary_rejects_package_without_binary(tmp_path):
    deb = _deb(tmp_path / "nvencc.deb", {"./usr/bin/other": b"x"})
    with pytest.raises(nv.NvenccInstallError, match="absent"):
        nv.extract_deb_binary(deb, tmp_path / "nvencc")
    bad = tmp_path / "bad.deb"
    bad.write_bytes(b"PK\x03\x04")
    with pytest.raises(nv.NvenccInstallError, match="en-tête"):
        nv.extract_deb_binary(bad, tmp_path / "nvencc")


def test_extract_deb_binary_cancelled(tmp_path):
    deb = _deb(tmp_path / "nvencc.deb", {"./usr/bin/nvencc": b"x" * 10})
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(nv.NvenccCancelled):
        nv.extract_deb_binary(deb, tmp_path / "nvencc", cancel)


def test_extract_windows_zip_copies_x64_folder_only(tmp_path):
    archive = tmp_path / "Aviutl_NVEnc_9.40.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("exe_files/NVEncC/x64/NVEncC64.exe", b"MZ64")
        zf.writestr("exe_files/NVEncC/x64/nvrtc64.dll", b"DLL")
        zf.writestr("exe_files/NVEncC/x64/sub/ignored.txt", b"no")
        zf.writestr("exe_files/NVEncC/x86/NVEncC.exe", b"MZ32")
    dest = tmp_path / "tools"
    binary = nv.extract_windows_zip(archive, dest)
    assert binary == dest / "NVEncC64.exe" and binary.read_bytes() == b"MZ64"
    assert sorted(p.name for p in dest.iterdir()) == ["NVEncC64.exe", "nvrtc64.dll"]


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------

def _fake_downloads(monkeypatch, tmp_path, *, deb_members=None):
    """Téléchargements simulés : .deb synthétique, .rpm factice ; motifs demandés consignés."""
    requested: list[tuple[str, ...]] = []
    monkeypatch.setattr(nv, "_asset", lambda patterns: (requested.append(patterns), _ASSET)[1])
    members = deb_members or {"./usr/bin/nvencc": b"\x7fELF"}

    def download(asset, dest, progress, cancel):
        if dest.suffix == ".deb":
            _deb(dest, members)
        else:
            dest.write_bytes(b"rpm")
    monkeypatch.setattr(nv, "_download", download)
    return requested


def test_install_user_method(monkeypatch, user_dir):
    monkeypatch.setattr(nv.sys, "platform", "linux")
    monkeypatch.setattr(nv, "supported_platform", lambda: True)
    requested = _fake_downloads(monkeypatch, user_dir)
    installed = nv.install(nv.METHOD_USER)
    assert installed == nv.user_root() / "nvencc"
    assert nv.user_binary() == installed
    assert requested == [nv._DEB_ASSET]
    # Réinstallation (mise à jour) : remplacement en place, aucun reste.
    nv.install(nv.METHOD_USER)
    assert sorted(p.name for p in nv.user_root().parent.iterdir()) == ["nvencc"]


def test_install_apt_success(monkeypatch, user_dir):
    monkeypatch.setattr(nv.sys, "platform", "linux")
    monkeypatch.setattr(nv, "supported_platform", lambda: True)
    _fake_downloads(monkeypatch, user_dir)
    monkeypatch.setattr(nv, "privilege_prefix", lambda interactive=None: ["pkexec"])
    monkeypatch.setattr(nv.shutil, "which", lambda name, path=None: "/usr/bin/" + name if name in ("apt", "nvencc") else None)
    commands: list[list[str]] = []

    def run(cmd, **kwargs):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(nv.subprocess, "run", run)
    assert nv.install(nv.METHOD_APT) == Path("/usr/bin/nvencc")
    assert commands[0][:4] == ["pkexec", "/usr/bin/apt", "install", "-y"]
    assert commands[0][4].endswith(".deb")
    assert nv.user_binary() is None


def test_install_dnf_failure_falls_back_to_user(monkeypatch, user_dir):
    monkeypatch.setattr(nv.sys, "platform", "linux")
    monkeypatch.setattr(nv, "supported_platform", lambda: True)
    requested = _fake_downloads(monkeypatch, user_dir)
    monkeypatch.setattr(nv, "privilege_prefix", lambda interactive=None: ["sudo"])
    monkeypatch.setattr(nv.shutil, "which", lambda name, path=None: "/usr/bin/dnf" if name == "dnf" else None)
    monkeypatch.setattr(nv.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "refusé"))
    logs: list[tuple[str, str]] = []
    installed = nv.install(nv.METHOD_DNF, log=lambda level, msg: logs.append((level, msg)))
    assert installed == nv.user_root() / "nvencc"
    assert requested == [nv._RPM_ASSET, nv._DEB_ASSET]
    assert any(level == "WARN" and "dossier utilisateur" in msg for level, msg in logs)


def test_install_without_privilege_goes_to_user(monkeypatch, user_dir):
    monkeypatch.setattr(nv.sys, "platform", "linux")
    monkeypatch.setattr(nv, "supported_platform", lambda: True)
    requested = _fake_downloads(monkeypatch, user_dir)
    monkeypatch.setattr(nv, "privilege_prefix", lambda interactive=None: None)
    assert nv.install(nv.METHOD_APT) == nv.user_root() / "nvencc"
    assert requested == [nv._DEB_ASSET]


def test_install_refuses_unverified_download(monkeypatch, user_dir):
    monkeypatch.setattr(nv.sys, "platform", "linux")
    monkeypatch.setattr(nv, "supported_platform", lambda: True)
    monkeypatch.setattr(nv, "_asset", lambda patterns: _ASSET)

    def bad_download(asset, dest, progress, cancel):
        raise plugins.PluginError("SHA-256 invalide")
    monkeypatch.setattr(plugins, "_download", bad_download)
    with pytest.raises(nv.NvenccInstallError, match="SHA-256"):
        nv.install(nv.METHOD_USER)
    assert nv.user_binary() is None


def test_install_refused_on_unsupported_platform(monkeypatch):
    monkeypatch.setattr(nv, "supported_platform", lambda: False)
    with pytest.raises(nv.NvenccInstallError):
        nv.install()


def test_remove_user(monkeypatch, user_dir):
    monkeypatch.setattr(nv.sys, "platform", "linux")
    root = nv.user_root()
    root.mkdir(parents=True)
    (root / "nvencc").write_bytes(b"x")
    assert nv.user_binary() == root / "nvencc"
    assert nv.remove_user() is True
    assert nv.user_binary() is None
    assert nv.remove_user() is True


# ---------------------------------------------------------------------------
# État et premier lancement
# ---------------------------------------------------------------------------

def test_read_version(monkeypatch):
    monkeypatch.setattr(nv, "run_probe", lambda cmd, timeout: subprocess.CompletedProcess(
        cmd, 0, "NVEnc (x64) 9.40 (r4221) by rigaya, Oct 31 2026\n", ""))
    assert nv.read_version("nvencc") == "9.40"
    monkeypatch.setattr(nv, "run_probe", lambda cmd, timeout: (_ for _ in ()).throw(FileNotFoundError()))
    assert nv.read_version("nvencc") == ""


def test_describe_provenance(monkeypatch, user_dir):
    monkeypatch.setattr(nv.sys, "platform", "linux")
    monkeypatch.setattr(nv, "read_version", lambda binary: "9.40")
    bundled = user_dir / "appdir" / "tools"
    bundled.mkdir(parents=True)
    (bundled / "nvencc").write_bytes(b"x")
    user = nv.user_root()
    user.mkdir(parents=True)
    (user / "nvencc").write_bytes(b"x")
    system = user_dir / "usr" / "bin" / "nvencc"
    system.parent.mkdir(parents=True)
    system.write_bytes(b"x")
    assert nv.describe(str(bundled / "nvencc"), bundled).provenance == nv.PROVENANCE_BUNDLED
    assert nv.describe(str(user / "nvencc"), bundled).provenance == nv.PROVENANCE_USER
    tool = nv.describe(str(system), bundled)
    assert tool.provenance == nv.PROVENANCE_SYSTEM and tool.version == "9.40"
    assert nv.describe(str(user_dir / "absent"), bundled) is None
    assert nv.describe("", None) is None


def test_first_run_step_without_nvidia(monkeypatch):
    monkeypatch.setattr(nv, "supported_platform", lambda: True)
    monkeypatch.setattr(nv, "nvidia_present", lambda: False)
    monkeypatch.setattr(nv, "install", lambda **kw: pytest.fail("aucune installation sans GPU NVIDIA"))
    assert nv.first_run_step() is None


def test_first_run_step_keeps_existing_tool(monkeypatch, user_dir):
    monkeypatch.setattr(nv, "supported_platform", lambda: True)
    monkeypatch.setattr(nv, "nvidia_present", lambda: True)
    monkeypatch.setattr(nv.shutil, "which", lambda name, path=None: "/usr/bin/nvencc" if name == "nvencc" else None)
    monkeypatch.setattr(nv, "install", lambda **kw: pytest.fail("NVEncC déjà présent"))
    assert nv.first_run_step() == Path("/usr/bin/nvencc")


def test_first_run_step_installs_and_never_raises(monkeypatch, user_dir):
    monkeypatch.setattr(nv, "supported_platform", lambda: True)
    monkeypatch.setattr(nv, "nvidia_present", lambda: True)
    monkeypatch.setattr(nv.shutil, "which", lambda name, path=None: None)
    monkeypatch.setattr(nv, "install", lambda **kw: Path("/x/nvencc"))
    assert nv.first_run_step() == Path("/x/nvencc")

    def failing(**kw):
        raise nv.NvenccInstallError("réseau")
    monkeypatch.setattr(nv, "install", failing)
    logs: list[tuple[str, str]] = []
    assert nv.first_run_step(log=lambda level, msg: logs.append((level, msg))) is None
    assert logs[-1][0] == "WARN" and "Extensions" in logs[-1][1]


def test_appconfig_falls_back_to_user_install(monkeypatch, tmp_path):
    """AppConfig : l'installation utilisateur sert seulement si aucun NVEncC n'est trouvé ailleurs."""
    from types import SimpleNamespace

    from core import config as config_mod

    user = tmp_path / "nvencc"
    user.write_bytes(b"x")
    monkeypatch.setattr(config_mod, "nvencc_user_binary", lambda: user)
    monkeypatch.setattr(config_mod.shutil, "which", lambda name, *a, **k: None)
    missing = SimpleNamespace(_resolve_tool_value=lambda *args: "NVEncC")
    assert config_mod.AppConfig._resolve_nvencc_tool(missing) == str(user)  # type: ignore[arg-type]
    system = tmp_path / "system-nvencc"
    system.write_bytes(b"x")
    found = SimpleNamespace(_resolve_tool_value=lambda *args: str(system))
    assert config_mod.AppConfig._resolve_nvencc_tool(found) == str(system)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Annulation de l'installation utilisateur (audit A02)
# ---------------------------------------------------------------------------

def _windows_zip(path: Path, size: int = 64) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("exe_files/NVEncC/x64/NVEncC64.exe", b"MZ" + b"\0" * size)
        zf.writestr("exe_files/NVEncC/x64/nvrtc64.dll", b"DLL")
    return path


def _previous_install() -> Path:
    root = nv.user_root()
    root.mkdir(parents=True)
    (root / "NVEncC64.exe").write_bytes(b"ancienne")
    return root


class _CancelAfter(threading.Event):
    """Jeton levé à partir du n-ième contrôle (annulation reçue au milieu d'une étape)."""

    def __init__(self, checks: int) -> None:
        super().__init__()
        self.remaining = checks

    def is_set(self) -> bool:
        self.remaining -= 1
        return self.remaining < 0 or super().is_set()


@pytest.mark.parametrize("position", ["before", "zip_open", "during_copy", "before_exchange"])
def test_windows_zip_cancel_keeps_previous_install(monkeypatch, user_dir, tmp_path, position):
    monkeypatch.setattr(nv.sys, "platform", "win32")
    archive = _windows_zip(tmp_path / "nvenc.zip", size=4 * nv._COPY_CHUNK)
    counter = _CancelAfter(10**6)
    nv._user_install(archive, None, counter, tmp_path / "count")  # nombre de contrôles d'une installation entière
    total = 10**6 - counter.remaining
    shutil.rmtree(nv.user_root())
    root = _previous_install()
    checks = {"before": 0, "zip_open": 1, "during_copy": 4, "before_exchange": total - 1}[position]
    with pytest.raises(nv.NvenccCancelled):
        nv._user_install(archive, None, _CancelAfter(checks), tmp_path)
    assert (root / "NVEncC64.exe").read_bytes() == b"ancienne"
    assert sorted(p.name for p in root.parent.iterdir()) == [root.name]  # staging supprimé


def test_windows_zip_cancel_after_exchange_keeps_new_install(monkeypatch, user_dir, tmp_path):
    monkeypatch.setattr(nv.sys, "platform", "win32")
    archive = _windows_zip(tmp_path / "nvenc.zip")
    root = _previous_install()
    cancel = threading.Event()
    original = nv._replace_user_root

    def replace(staging):
        original(staging)
        cancel.set()  # annulation arrivée après l'échange : installation acquise

    monkeypatch.setattr(nv, "_replace_user_root", replace)
    installed = nv._user_install(archive, None, cancel, tmp_path)
    assert installed == root / "NVEncC64.exe" and installed.read_bytes().startswith(b"MZ")


def test_deb_cancel_before_exchange_keeps_previous_install(monkeypatch, user_dir, tmp_path):
    monkeypatch.setattr(nv.sys, "platform", "linux")
    deb = _deb(tmp_path / "nvencc.deb", {"./usr/bin/nvencc": b"\x7fELF"})
    root = nv.user_root()
    root.mkdir(parents=True)
    (root / "nvencc").write_bytes(b"ancienne")
    cancel = threading.Event()
    original = nv.extract_deb_binary

    def extract(archive, dest, cancel_=None):
        result = original(archive, dest, cancel_)
        cancel.set()
        return result

    monkeypatch.setattr(nv, "extract_deb_binary", extract)
    with pytest.raises(nv.NvenccCancelled):
        nv._user_install(deb, None, cancel, tmp_path)
    assert (root / "nvencc").read_bytes() == b"ancienne"
