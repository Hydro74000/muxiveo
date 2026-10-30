"""Essai réel de la mise à jour intégrée macOS (CI) : applications figées par PyInstaller.

Deux bundles construits par package.py, de contenus différents : l'ancien est « installé » dans un dossier
temporaire, le nouveau est fourni en DMG. L'auxiliaire est l'exécutable figé de l'ancienne application
(`--apply-update`), lancé depuis le bundle qu'il échange, comme en production (`core/update_install.py`).

Scénarios :
- réussite : échange, contrôle de la nouvelle version, relance, compte rendu, sauvegarde de l'ancienne ;
- retour arrière : contrôle refusé après l'échange, l'auxiliaire traverse deux échanges ;
- ménage par l'auxiliaire figé (`--apply-update cleanup`), comme au démarrage suivant.

Usage : python scripts/ci_macos_update_e2e.py --old-app <Muxiveo.app> --dmg <Muxiveo-X.dmg>
        --old-version A --new-version B
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess  # nosec B404
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import update_apply  # noqa: E402
from core.update_install import InstallKind, _swap_commands  # noqa: E402
from core.version import APP_MACOS_BUNDLE_ID  # noqa: E402

HELPER_TIMEOUT_S = 600.0
PROBE_TIMEOUT_S = 180.0


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"ÉCHEC : {message}")


def executable(app: Path) -> Path:
    """Exécutable déclaré par Info.plist, présent avec sa casse exacte."""
    with (app / "Contents" / "Info.plist").open("rb") as source:
        name = plistlib.load(source)["CFBundleExecutable"]
    check(name in os.listdir(app / "Contents" / "MacOS"), f"exécutable {name!r} absent (casse exacte) de {app}")
    return app / "Contents" / "MacOS" / name


def announced(app: Path) -> str:
    result = subprocess.run(  # nosec B603  # exécutable du bundle construit par ce job
        [str(executable(app)), "--cli", "version"], capture_output=True, text=True, timeout=PROBE_TIMEOUT_S,
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen"), check=False,
    )
    check(result.returncode == 0, f"{app} --cli version : code {result.returncode}\n{result.stdout}{result.stderr}")
    return result.stdout.strip().splitlines()[-1]


def install_old(old_app: Path, parent: Path) -> Path:
    parent.mkdir(parents=True)
    target = parent / update_apply.MACOS_APP_NAME
    subprocess.run(["ditto", str(old_app), str(target)], check=True)  # nosec B603 B607
    return target


def prepare(install: Path, dmg: Path, version: str, relaunched: Path) -> Path:
    """Préparation de l'application (`download_update`) : DMG copié dans le dossier de travail, `prepare`."""
    work = Path(tempfile.mkdtemp(prefix=update_apply.WORKDIR_PREFIX, dir=install.parent))
    (work / update_apply.WORKDIR_TAG).touch()
    archive = work / dmg.name
    shutil.copyfile(dmg, archive)
    probe, _relaunch = _swap_commands(InstallKind.MACOS_APP)
    # Relance témoin : `open -n` démarrerait l'interface sur le runner.
    update_apply.prepare(update_apply.KIND_MACOS, archive, work, install, version, probe,
                         ["/usr/bin/touch", str(relaunched)], APP_MACOS_BUNDLE_ID)
    return work


def run_helper(install: Path, arguments: list[str], cwd: Path, log: Path, *, wait_for_app: bool) -> int:
    """Auxiliaire figé de l'application installée, détaché comme `update_apply.spawn_detached` (journal en plus).

    `wait_for_app` : l'application qui se ferme est simulée par un processus de courte durée.
    """
    command = [str(executable(install)), "--apply-update", *arguments]
    application = subprocess.Popen(["/bin/sleep", "2"]) if wait_for_app else None  # nosec B603 B607
    if application is not None:
        command += ["--pid", str(application.pid)]
    with log.open("wb") as output:
        helper = subprocess.Popen(  # nosec B603  # exécutable du bundle construit par ce job
            command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True,
        )
        if application is not None:
            application.wait()          # récolté : un zombie passerait pour vivant (os.kill)
        code = helper.wait(timeout=HELPER_TIMEOUT_S)
    print(f"--- {' '.join(arguments[:1])} : code {code}")
    print(log.read_text(encoding="utf-8", errors="replace"))
    return code


def result_of(work: Path) -> dict:
    path = work / update_apply.RESULT_FILE
    check(path.is_file(), f"compte rendu absent : {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def wait_for(path: Path, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        check(time.monotonic() < deadline, f"relance non effectuée : {path}")
        time.sleep(0.2)


def scenario_success(args: argparse.Namespace, workspace: Path) -> None:
    parent = workspace / "success" / "Applications"
    install = install_old(args.old_app, parent)
    check(announced(install) == f"Muxiveo {args.old_version}", "ancienne version installée inattendue")
    relaunched = workspace / "relaunched-success"
    work = prepare(install, args.dmg, args.new_version, relaunched)
    code = run_helper(install, ["commit", str(work)], work, workspace / "success.log", wait_for_app=True)
    result = result_of(work)
    check(code == 0 and result.get("ok") is True, f"mise à jour refusée : {result}")
    check(announced(install) == f"Muxiveo {args.new_version}", "nouvelle version non installée")
    backup = Path(str(result.get("backup")))
    check(backup.resolve() == (work / "previous" / update_apply.MACOS_APP_NAME).resolve(),
          f"sauvegarde inattendue : {backup}")
    check(announced(backup) == f"Muxiveo {args.old_version}", "ancienne version non conservée")
    wait_for(relaunched)
    reports = update_apply.collect_results(parent)
    check([report.get("version") for report in reports] == [args.new_version], f"compte rendu : {reports}")
    check(work.is_dir(), "dernière sauvegarde supprimée")
    print("Réussite : échange, contrôle, relance et compte rendu vérifiés.")


def scenario_rollback(args: argparse.Namespace, workspace: Path) -> None:
    parent = workspace / "rollback" / "Applications"
    install = install_old(args.old_app, parent)
    relaunched = workspace / "relaunched-rollback"
    work = prepare(install, args.dmg, args.new_version, relaunched)
    # Version attendue différente : le contrôle refuse la nouvelle version une fois échangée.
    prepared = work / update_apply.PREPARED_FILE
    doc = json.loads(prepared.read_text(encoding="utf-8"))
    doc["version"] = f"{args.new_version}-refusee"
    prepared.write_text(json.dumps(doc), encoding="utf-8")
    code = run_helper(install, ["commit", str(work)], work, workspace / "rollback.log", wait_for_app=True)
    result = result_of(work)
    check(code == 1 and result.get("ok") is False, f"contrôle non refusé : {result}")
    check(result.get("rollback_incomplete") is False and result.get("cleanup_safe") is True,
          f"retour arrière incomplet : {result}")
    check(announced(install) == f"Muxiveo {args.old_version}", "ancienne version non rétablie")
    wait_for(relaunched)
    print("Retour arrière : ancienne version rétablie et relancée.")

    # Ménage confié au démarrage suivant à l'auxiliaire figé (`start_update_cleanup`).
    cleanup = Path(tempfile.mkdtemp(prefix=update_apply.CLEANUP_PREFIX))
    (cleanup / update_apply.CLEANUP_FILE).write_text(json.dumps([str(work.absolute())]), encoding="utf-8")
    code = run_helper(install, ["cleanup", str(cleanup)], cleanup.parent, workspace / "cleanup.log",
                      wait_for_app=False)
    check(code == 0 and not work.exists() and not cleanup.exists(), "ménage non effectué")
    print("Ménage : dossier de l'essai refusé supprimé.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mise à jour intégrée macOS par l'auxiliaire figé (CI).")
    parser.add_argument("--old-app", type=Path, required=True)
    parser.add_argument("--dmg", type=Path, required=True)
    parser.add_argument("--old-version", required=True)
    parser.add_argument("--new-version", required=True)
    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        print("error: macOS seulement", file=sys.stderr)
        return 2
    args.old_app, args.dmg = args.old_app.resolve(), args.dmg.resolve()
    check(announced(args.old_app) == f"Muxiveo {args.old_version}", "ancienne application construite inattendue")
    workspace = Path(tempfile.mkdtemp(prefix="muxiveo-update-e2e-", dir=os.environ.get("RUNNER_TEMP")))
    scenario_success(args, workspace)
    scenario_rollback(args, workspace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
