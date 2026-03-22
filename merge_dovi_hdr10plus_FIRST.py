#!/usr/bin/env python3
# =============================================================================
# merge_dovi_hdr10plus.py
# Injecte le RPU Dolby Vision (Profile 8.1) et les métadonnées HDR10+
# d'un fichier source (Film 2) sur le flux vidéo d'un fichier cible (Film 1)
# =============================================================================
# Usage :
#   ./merge_dovi_hdr10plus.py [OPTIONS]
#
# Options :
#   -1 <chemin>   Film 1 — cible (vidéo à enrichir)
#   -2 <chemin>   Film 2 — source (porteur DoVi/HDR10+)
#   -w <chemin>   Dossier de travail (fichiers intermédiaires)
#   -o <chemin>   Dossier de sortie
#   -h            Affiche cette aide
#
# Exemples :
#   ./merge_dovi_hdr10plus.py -1 /films/film1.mkv -2 /films/film2.mkv
#   ./merge_dovi_hdr10plus.py -1 /films/film1.mkv -2 /films/film2.mkv \
#                             -w /tmp/work -o /films/output
#
# Les variables d'environnement FILM1, FILM2, WORK_DIR, OUTPUT_DIR sont
# également supportées. Les arguments CLI ont priorité sur les variables
# d'environnement, qui ont elles-mêmes priorité sur les valeurs par défaut.
# =============================================================================

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# =============================================================================
# COULEURS / JOURNALISATION
# =============================================================================

RESET  = "\033[0m"
BLUE   = "\033[1;34m"
GREEN  = "\033[1;32m"
YELLOW = "\033[1;33m"
RED    = "\033[1;31m"


def log(msg: str) -> None:
    print(f"\n{BLUE}[INFO]{RESET}  {msg}")


def ok(msg: str) -> None:
    print(f"{GREEN}[OK]{RESET}    {msg}")


def warn(msg: str) -> None:
    print(f"{YELLOW}[WARN]{RESET}  {msg}")


def die(msg: str) -> None:
    print(f"{RED}[ERROR]{RESET} {msg}", file=sys.stderr)
    sys.exit(1)


# =============================================================================
# PARSING DES ARGUMENTS CLI
# Priorité : argument CLI > variable d'environnement > valeur par défaut
# =============================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=Path(sys.argv[0]).name,
        description="Injecte RPU Dolby Vision (Profile 8.1) et HDR10+ de Film 2 dans Film 1.",
        formatter_class=argparse.RawTextHelpFormatter,
        add_help=False,
    )
    parser.add_argument(
        "-1", dest="film1", metavar="<chemin>",
        help="Film 1 — cible (vidéo à enrichir)",
    )
    parser.add_argument(
        "-2", dest="film2", metavar="<chemin>",
        help="Film 2 — source (porteur DoVi/HDR10+)",
    )
    parser.add_argument(
        "-w", dest="work_dir", metavar="<chemin>",
        help="Dossier de travail (fichiers intermédiaires)",
    )
    parser.add_argument(
        "-o", dest="output_dir", metavar="<chemin>",
        help="Dossier de sortie",
    )
    parser.add_argument(
        "-h", "--help", action="store_true",
        help="Affiche cette aide",
    )

    args = parser.parse_args()

    if args.help:
        print()
        print(f"Usage : {parser.prog} [OPTIONS]")
        print()
        print("Options :")
        print("  -1 <chemin>   Film 1 — cible (vidéo à enrichir)")
        print("  -2 <chemin>   Film 2 — source (porteur DoVi/HDR10+)")
        print("  -w <chemin>   Dossier de travail (fichiers intermédiaires)")
        print("  -o <chemin>   Dossier de sortie")
        print("  -h            Affiche cette aide")
        print()
        print("Exemples :")
        print(f"  {parser.prog} -1 /films/film1.mkv -2 /films/film2.mkv")
        print(f"  {parser.prog} -1 /films/film1.mkv -2 /films/film2.mkv -w /tmp/work -o /films/output")
        print()
        sys.exit(0)

    return args


# =============================================================================
# CONFIGURATION
# Résolution par ordre de priorité :
#   1. Argument CLI   (-1 / -2 / -w / -o)
#   2. Variable env   (FILM1 / FILM2 / WORK_DIR / OUTPUT_DIR)
#   3. Valeur défaut
# =============================================================================

class Config:
    def __init__(self, args: argparse.Namespace) -> None:
        env = os.environ.get

        # Fichiers source
        self.film1 = Path(
            args.film1
            or env("FILM1", "/media/films/film1.mkv")
        )
        self.film2 = Path(
            args.film2
            or env("FILM2", "/media/films/film2.mkv")
        )

        # Dossiers
        self.work_dir = Path(
            args.work_dir
            or env("WORK_DIR", "/tmp/dovi_merge")
        )
        self.output_dir = Path(
            args.output_dir
            or env("OUTPUT_DIR", str(self.film1.parent))
        )

        # Nom de sortie
        output_basename = env(
            "OUTPUT_BASENAME",
            f"{self.film1.stem}_DOVI_HDR10PLUS",
        )

        # Mode dovi_tool (-m flag global) :
        #   0 = rewrite untouched | 2 = force Profile 8.1 (supprime mapping)
        #   3 = Profile 5→8.1    | 5 = Profile 8.1 en préservant mapping luma/chroma
        self.dovi_mode = env("DOVI_MODE", "2")

        # Chemins dérivés
        self.film1_hevc      = self.work_dir / "film1.hevc"
        self.film2_hevc      = self.work_dir / "film2.hevc"
        self.film2_rpu       = self.work_dir / "film2_rpu.bin"
        self.film2_hdr10plus = self.work_dir / "film2_hdr10plus.json"
        self.film1_with_dovi = self.work_dir / "film1_with_dovi.hevc"
        self.film1_final     = self.work_dir / "film1_final.hevc"
        self.output_mkv      = self.output_dir / f"{output_basename}.mkv"


# =============================================================================
# FONCTIONS UTILITAIRES
# =============================================================================

def run(cmd: list[str]) -> subprocess.CompletedProcess:
    """Lance une commande, lève une exception en cas d'échec."""
    result = subprocess.run(cmd, check=True, text=True)
    return result


def run_output(cmd: list[str], check: bool = True) -> str:
    """Lance une commande et retourne sa sortie stdout+stderr combinées."""
    result = subprocess.run(
        cmd,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return result.stdout or ""


# =============================================================================
# GESTION DES FICHIERS EXISTANTS
# =============================================================================

def prompt_overwrite(path: Path) -> bool:
    """
    Si le fichier existe déjà, demande à l'utilisateur ce qu'il veut faire.
    Retourne True  = écraser / relancer l'étape.
    Retourne False = réutiliser le fichier existant / bypasser l'étape.
    """
    if not path.exists():
        return True  # Fichier absent → on procède normalement

    size_mb = path.stat().st_size / (1024 ** 2)
    print(f"\n{YELLOW}[EXISTS]{RESET}  {path.name}  ({size_mb:.1f} Mo)")
    print(f"          {path}")
    while True:
        choice = input("  [O] Écraser et relancer  |  [G] Garder et bypasser  > ").strip().upper()
        if choice in ("O", ""):
            return True   # Écraser
        if choice == "G":
            return False  # Garder / bypasser
        print("  Répondre O (écraser) ou G (garder).")


def resolve_skip_flags(paths: dict[str, Path]) -> dict[str, bool]:
    """
    Résout les décisions d'écrasement pour un ensemble de fichiers cibles.
    Doit être appelé dans le thread principal avant tout ThreadPoolExecutor.

    Retourne un dict {label: skip} où skip=True signifie « bypasser l'étape ».
    """
    skip: dict[str, bool] = {}
    for label, path in paths.items():
        overwrite = prompt_overwrite(path)
        skip[label] = not overwrite
        if not overwrite:
            ok(f"[{label}] Fichier existant conservé — étape bypassée.")
    return skip


# =============================================================================
# VÉRIFICATIONS
# =============================================================================

def check_deps() -> None:
    log("Vérification des dépendances...")
    tools = ["mkvextract", "mkvmerge", "mediainfo", "dovi_tool", "hdr10plus_tool"]
    missing = [t for t in tools if shutil.which(t) is None]
    if missing:
        die(f"Outils manquants : {', '.join(missing)}")
    ok("Toutes les dépendances sont présentes.")


def check_files(cfg: Config) -> None:
    log("Vérification des fichiers source...")
    if not cfg.film1.is_file():
        die(f"Film 1 introuvable : {cfg.film1}")
    if not cfg.film2.is_file():
        die(f"Film 2 introuvable : {cfg.film2}")
    ok(f"Film 1 : {cfg.film1}")
    ok(f"Film 2 : {cfg.film2}")


def check_framecount(cfg: Config) -> None:
    log("Comparaison des frame counts...")

    def get_framecount(path: Path) -> int:
        raw = run_output(
            ["mediainfo", f"--Inform=Video;%FrameCount%", str(path)]
        ).strip()
        if not re.fullmatch(r"\d+", raw):
            die(f"Impossible de lire le frame count de {path.name} (retour : '{raw}')")
        return int(raw)

    fc1 = get_framecount(cfg.film1)
    fc2 = get_framecount(cfg.film2)

    print(f"  Film 1 : {fc1} frames")
    print(f"  Film 2 : {fc2} frames")

    abs_diff = abs(fc2 - fc1)

    if fc1 == fc2:
        ok("Frame counts identiques.")
    elif abs_diff <= 4:
        warn(f"Différence de {abs_diff} frames — tolérable (padding muxer en fin de fichier).")
        warn("Vérification visuelle recommandée sur les premières frames.")
    else:
        die(f"Différence de {abs_diff} frames trop importante. "
            "Les deux fichiers ne semblent pas être le même contenu.")


def prepare_dirs(cfg: Config) -> None:
    log("Préparation des dossiers...")
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    ok(f"Dossier de travail : {cfg.work_dir}")
    ok(f"Dossier de sortie  : {cfg.output_dir}")


# =============================================================================
# ÉTAPES DU WORKFLOW
# =============================================================================

# --- Tâches d'extraction (exécutées en parallèle) ---------------------------

def _task_extract_film1_hevc(cfg: Config) -> str:
    """Tâche parallèle A — Extraction du flux HEVC de Film 1."""
    run(["mkvextract", str(cfg.film1), "tracks", f"0:{cfg.film1_hevc}"])
    return f"Film 1 HEVC extrait : {cfg.film1_hevc}"


def _task_extract_rpu(cfg: Config) -> str:
    """Tâche parallèle B — Extraction du RPU Dolby Vision depuis Film 2 (MKV direct)."""
    run(["dovi_tool", "extract-rpu", "-i", str(cfg.film2), "-o", str(cfg.film2_rpu)])
    return f"RPU extrait : {cfg.film2_rpu}"


def _task_extract_hdr10plus(cfg: Config) -> str:
    """Tâche parallèle C — Extraction HDR10+ depuis Film 2 (via HEVC intermédiaire)."""
    run(["mkvextract", str(cfg.film2), "tracks", f"0:{cfg.film2_hevc}"])
    run(["hdr10plus_tool", "extract", "-i", str(cfg.film2_hevc), "-o", str(cfg.film2_hdr10plus)])
    cfg.film2_hevc.unlink(missing_ok=True)
    return f"HDR10+ extrait : {cfg.film2_hdr10plus} (HEVC Film 2 intermédiaire supprimé)"


def step_extract_parallel(cfg: Config) -> None:
    """
    Étapes 1-2-3 — Extraction parallèle des trois sources indépendantes.

    Graphe de dépendances :
        Tâche A : mkvextract film1  → film1.hevc         ┐
        Tâche B : dovi_tool rpu     → film2_rpu.bin       ├─ aucune dépendance entre elles
        Tâche C : mkvextract film2  → HDR10+.json         ┘
        Étape 4 : inject-rpu        (attend A + B)
        Étape 5 : inject HDR10+     (attend C + étape 4)

    Les décisions d'écrasement sont résolues dans le thread principal AVANT
    le lancement du pool — stdin ne peut pas être lu depuis des threads.
    """
    log("Étapes 1-2-3 — Extraction parallèle (HEVC Film 1 / RPU / HDR10+)...")

    # Résolution des flags de bypass dans le thread principal
    all_tasks = {
        "A — HEVC Film 1": (_task_extract_film1_hevc, cfg.film1_hevc),
        "B — RPU DoVi"   : (_task_extract_rpu,        cfg.film2_rpu),
        "C — HDR10+"     : (_task_extract_hdr10plus,  cfg.film2_hdr10plus),
    }
    skip = resolve_skip_flags({label: path for label, (_, path) in all_tasks.items()})

    # Ne soumettre que les tâches non bypassées
    active = {label: fn for label, (fn, _) in all_tasks.items() if not skip[label]}

    if not active:
        ok("Toutes les extractions bypassées — fichiers existants utilisés.")
        return

    t_start = time.monotonic()
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=len(active)) as executor:
        futures = {executor.submit(fn, cfg): label for label, fn in active.items()}
        for future in as_completed(futures):
            label = futures[future]
            try:
                result = future.result()
                ok(f"[{label}] {result}")
            except Exception as exc:
                errors.append(f"[{label}] {exc}")

    if errors:
        die("Échec d'une ou plusieurs extractions parallèles :\n  " + "\n  ".join(errors))

    elapsed = time.monotonic() - t_start
    ok(f"Extractions actives terminées en {elapsed:.1f}s")


def step_inject_rpu(cfg: Config) -> None:
    log(f"Étape 4/7 — Injection du RPU DoVi (mode {cfg.dovi_mode}) dans Film 1...")
    if not prompt_overwrite(cfg.film1_with_dovi):
        ok(f"Fichier existant conservé — étape bypassée : {cfg.film1_with_dovi}")
        return
    # -m est un flag GLOBAL de dovi_tool, placé avant la sous-commande inject-rpu.
    # Mode 2 : convertit/normalise le RPU en Profile 8.1 (supprime le mapping).
    # Modes disponibles : 0=untouched | 2=Profile 8.1 | 3=P5→8.1 | 5=8.1+mapping
    run([
        "dovi_tool", "-m", cfg.dovi_mode, "inject-rpu",
        "-i", str(cfg.film1_hevc),
        "-r", str(cfg.film2_rpu),
        "-o", str(cfg.film1_with_dovi),
    ])
    ok(f"RPU injecté : {cfg.film1_with_dovi}")


def step_inject_hdr10plus(cfg: Config) -> None:
    log("Étape 5/7 — Injection des métadonnées HDR10+ dans Film 1...")
    if not prompt_overwrite(cfg.film1_final):
        ok(f"Fichier existant conservé — étape bypassée : {cfg.film1_final}")
        return
    run([
        "hdr10plus_tool", "inject",
        "-i", str(cfg.film1_with_dovi),
        "-j", str(cfg.film2_hdr10plus),
        "-o", str(cfg.film1_final),
    ])
    ok(f"HDR10+ injecté : {cfg.film1_final}")


def step_verify(cfg: Config) -> None:
    log("Étape 6/7 — Vérification de l'intégrité des RPU frames...")

    # check=False : équivalent du "|| true" bash — on ne plante pas si dovi_tool
    # ne trouve pas la ligne "rpu frames" dans sa sortie.
    raw = run_output(["dovi_tool", "info", "-i", str(cfg.film1_final)], check=False)

    rpu_frames: int | None = None
    match = re.search(r"rpu frames[^\d]*(\d+)", raw, re.IGNORECASE)
    if match:
        rpu_frames = int(match.group(1))

    fc1_raw = run_output(
        ["mediainfo", "--Inform=Video;%FrameCount%", str(cfg.film1)]
    ).strip()
    fc1 = int(fc1_raw) if re.fullmatch(r"\d+", fc1_raw) else None

    print(f"  RPU frames injectés : {rpu_frames if rpu_frames is not None else '<non détecté>'}")
    print(f"  Frame count Film 1  : {fc1 if fc1 is not None else '<non détecté>'}")

    if rpu_frames is None:
        warn("Impossible de lire le nombre de RPU frames — vérification manuelle recommandée.")
    elif fc1 is None:
        warn("Impossible de lire le frame count — vérification manuelle recommandée.")
    elif rpu_frames == fc1:
        ok("RPU frames = Frame count. Intégrité confirmée.")
    elif abs(rpu_frames - fc1) <= 4:
        warn(f"Différence de {abs(rpu_frames - fc1)} frames — tolérable.")
    else:
        die(f"Désalignement critique : {rpu_frames} RPU frames pour {fc1} frames vidéo.")


def step_remux(cfg: Config) -> None:
    log("Étape 7/7 — Remuxage final avec mkvmerge...")

    # Compter les pistes de Film 1 pour construire le --track-order dynamiquement
    identify_output = run_output(["mkvmerge", "--identify", str(cfg.film1)])
    nb_tracks = sum(1 for line in identify_output.splitlines() if line.startswith("Track ID"))

    # Piste 0 = HEVC final (source externe, index 1 dans mkvmerge)
    # Pistes 1..nb_tracks-1 = pistes non-vidéo de Film 1 (source index 0)
    parts = ["1:0"] + [f"0:{i}" for i in range(1, nb_tracks)]
    track_order = ",".join(parts)

    run([
        "mkvmerge",
        "-o",          str(cfg.output_mkv),
        "--no-video",  str(cfg.film1),
                       str(cfg.film1_final),
        "--track-order", track_order,
    ])
    ok(f"Fichier final créé : {cfg.output_mkv}")


def cleanup(cfg: Config) -> None:
    log("Nettoyage des fichiers intermédiaires...")
    for path in [
        cfg.film1_hevc,
        cfg.film2_rpu,
        cfg.film2_hdr10plus,
        cfg.film1_with_dovi,
        cfg.film1_final,
    ]:
        path.unlink(missing_ok=True)

    # Supprimer le dossier de travail seulement s'il est vide
    try:
        cfg.work_dir.rmdir()
    except OSError:
        pass  # Non vide ou autre erreur — on ignore, comme rmdir --ignore-fail-on-non-empty

    ok("Nettoyage terminé.")


# =============================================================================
# MAIN
# =============================================================================

def main() -> None:
    args = parse_args()
    cfg  = Config(args)

    print()
    print("=============================================")
    print("  merge_dovi_hdr10plus.py")
    print("=============================================")
    print(f"  Film 1 (cible)  : {cfg.film1}")
    print(f"  Film 2 (source) : {cfg.film2}")
    print(f"  Dossier travail : {cfg.work_dir}")
    print(f"  Dossier sortie  : {cfg.output_dir}")
    print(f"  Fichier sortie  : {cfg.output_mkv.name}")
    print(f"  Mode DoVi       : {cfg.dovi_mode} "
          f"(0=untouched | 2=Profile 8.1 | 3=P5→8.1 | 5=8.1+mapping)")
    print("=============================================")

    check_deps()
    check_files(cfg)
    check_framecount(cfg)
    prepare_dirs(cfg)
    step_extract_parallel(cfg)       # Étapes 1-2-3 en parallèle
    step_inject_rpu(cfg)             # Étape 4 — attend A + B
    step_inject_hdr10plus(cfg)       # Étape 5 — attend C + étape 4
    step_verify(cfg)                 # Étape 6
    step_remux(cfg)                  # Étape 7
    cleanup(cfg)

    print()
    print("=============================================")
    ok("TERMINÉ — Fichier de sortie :")
    print(f"  {cfg.output_mkv}")
    print("=============================================")
    print()


if __name__ == "__main__":
    main()