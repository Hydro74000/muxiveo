#!/usr/bin/env python3
# =============================================================================
# merge_dovi_hdr10plus.py
# Injecte le RPU Dolby Vision (Profile 8.1) et/ou les métadonnées HDR10+
# d'un fichier source (Film 2) sur le flux vidéo d'un fichier cible (Film 1)
# =============================================================================
# Usage :
#   ./merge_dovi_hdr10plus.py [OPTIONS]
#
# Options :
#   -1 <chemin>   Film 1 — cible (vidéo à enrichir)
#   -2 <chemin>   Film 2 — source (porteur DoVi et/ou HDR10+)
#   -w <chemin>   Dossier de travail (fichiers intermédiaires)
#   -o <chemin>   Dossier de sortie
#   -h            Affiche cette aide
#
# Comportement automatique :
#   - Détecte la présence de HEVC, DoVi et HDR10+ dans chaque fichier
#   - N'extrait que ce qui est nécessaire selon les formats détectés
#   - Si Film 1 est MKV : extrait le HEVC pour injection
#   - Si Film 1 est HEVC : utilise directement le fichier source
#   - Film 2 (MKV ou HEVC) : passé directement aux outils sans extraction
#
# Variables d'environnement supportées :
#   FILM1, FILM2, WORK_DIR, OUTPUT_DIR, OUTPUT_BASENAME, DOVI_MODE
#   Les arguments CLI ont priorité sur les variables d'environnement.
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
CYAN   = "\033[1;36m"


def log(msg: str) -> None:
    print(f"\n{BLUE}[INFO]{RESET}  {msg}")


def ok(msg: str) -> None:
    print(f"{GREEN}[OK]{RESET}    {msg}")


def warn(msg: str) -> None:
    print(f"{YELLOW}[WARN]{RESET}  {msg}")


def die(msg: str) -> None:
    print(f"{RED}[ERROR]{RESET} {msg}", file=sys.stderr)
    sys.exit(1)


def info_line(label: str, value: str) -> None:
    print(f"  {CYAN}{label:<18}{RESET} {value}")


# =============================================================================
# PARSING DES ARGUMENTS CLI
# Priorité : argument CLI > variable d'environnement > valeur par défaut
# =============================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=Path(sys.argv[0]).name,
        description="Injecte RPU Dolby Vision et/ou HDR10+ de Film 2 dans Film 1.",
        formatter_class=argparse.RawTextHelpFormatter,
        add_help=False,
    )
    parser.add_argument("-1", dest="film1",      metavar="<chemin>", help="Film 1 — cible")
    parser.add_argument("-2", dest="film2",      metavar="<chemin>", help="Film 2 — source DoVi/HDR10+")
    parser.add_argument("-w", dest="work_dir",   metavar="<chemin>", help="Dossier de travail")
    parser.add_argument("-o", dest="output_dir", metavar="<chemin>", help="Dossier de sortie")
    parser.add_argument("-h", "--help", action="store_true",         help="Affiche cette aide")

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
# =============================================================================

class Config:
    def __init__(self, args: argparse.Namespace) -> None:
        env = os.environ.get

        # Fichiers source
        self.film1 = Path(args.film1 or env("FILM1", "/media/films/film1.mkv"))
        self.film2 = Path(args.film2 or env("FILM2", "/media/films/film2.mkv"))

        # Dossiers
        self.work_dir   = Path(args.work_dir   or env("WORK_DIR",   "/tmp/dovi_merge"))
        self.output_dir = Path(args.output_dir or env("OUTPUT_DIR", str(self.film1.parent)))

        # Nom de sortie
        output_basename = env("OUTPUT_BASENAME", f"{self.film1.stem}_DOVI_HDR10PLUS")

        # Mode dovi_tool (-m flag global) :
        #   0 = rewrite untouched | 2 = force Profile 8.1 (supprime mapping)
        #   3 = Profile 5→8.1    | 5 = Profile 8.1 en préservant mapping luma/chroma
        self.dovi_mode = env("DOVI_MODE", "2")

        # Propriétés dérivées du format source
        self.film1_is_mkv = self.film1.suffix.lower() == ".mkv"
        self.film2_is_mkv = self.film2.suffix.lower() == ".mkv"

        # Chemins intermédiaires de travail
        self.film1_hevc      = self.work_dir / "film1.hevc"       # Extrait si film1 est MKV
        self.film2_rpu       = self.work_dir / "film2_rpu.bin"
        self.film2_hdr10plus = self.work_dir / "film2_hdr10plus.json"
        self.film1_with_dovi = self.work_dir / "film1_with_dovi.hevc"
        self.film1_final     = self.work_dir / "film1_final.hevc"
        self.output_mkv      = self.output_dir / f"{output_basename}.mkv"

        # Détection des formats (rempli par check_film2_hdr_formats)
        self.has_dovi     : bool = False
        self.has_hdr10plus: bool = False

    @property
    def film1_hevc_input(self) -> Path:
        """
        Chemin HEVC à utiliser comme entrée pour les outils d'injection.
        Si film1 est MKV → film1_hevc (extrait en étape 1).
        Si film1 est déjà HEVC → film1 directement, sans extraction.
        """
        return self.film1_hevc if self.film1_is_mkv else self.film1

    @property
    def injection_chain_final(self) -> Path:
        """
        Fichier HEVC final à muxer, selon les formats traités :
          DoVi + HDR10+ → film1_final     (sortie de inject hdr10+)
          DoVi seul     → film1_with_dovi (sortie de inject-rpu)
          HDR10+ seul   → film1_final     (sortie de inject hdr10+, entrée = film1_hevc_input)
        """
        if self.has_dovi and self.has_hdr10plus:
            return self.film1_final
        if self.has_dovi:
            return self.film1_with_dovi
        return self.film1_final  # HDR10+ seul


# =============================================================================
# FONCTIONS UTILITAIRES
# =============================================================================

def run(cmd: list[str]) -> subprocess.CompletedProcess:
    """Lance une commande, lève une exception en cas d'échec."""
    return subprocess.run(cmd, check=True, text=True)


def run_output(cmd: list[str], check: bool = True) -> str:
    """Lance une commande et retourne sa sortie stdout+stderr combinées."""
    result = subprocess.run(
        cmd, check=check, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
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
            return True
        if choice == "G":
            return False
        print("  Répondre O (écraser) ou G (garder).")


def resolve_skip_flags(paths: dict[str, Path]) -> dict[str, bool]:
    """
    Résout les décisions d'écrasement pour un ensemble de fichiers cibles.
    DOIT être appelé dans le thread principal avant tout ThreadPoolExecutor.
    Retourne {label: skip} où skip=True → bypasser l'étape.
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


def check_hevc_streams(cfg: Config) -> None:
    """Vérifie que les deux fichiers contiennent bien un flux vidéo HEVC."""
    log("Vérification des flux HEVC...")

    def has_hevc(path: Path) -> bool:
        raw = run_output(["mediainfo", "--Inform=Video;%Format%", str(path)]).strip()
        return raw.upper() == "HEVC"

    if not has_hevc(cfg.film1):
        die(f"Film 1 ne contient pas de flux HEVC : {cfg.film1.name}")
    ok(f"Film 1 — flux HEVC détecté.")

    if not has_hevc(cfg.film2):
        die(f"Film 2 ne contient pas de flux HEVC : {cfg.film2.name}")
    ok(f"Film 2 — flux HEVC détecté.")


def check_film2_hdr_formats(cfg: Config) -> None:
    """
    Détecte la présence de Dolby Vision et HDR10+ dans Film 2.
    Met à jour cfg.has_dovi et cfg.has_hdr10plus.
    Arrête le script si aucun des deux n'est présent.
    """
    log("Détection des formats HDR dans Film 2...")

    raw = run_output(
        ["mediainfo", "--Inform=Video;%HDR_Format%", str(cfg.film2)]
    ).strip()

    cfg.has_dovi      = "Dolby Vision" in raw
    cfg.has_hdr10plus = "SMPTE ST 2094" in raw

    dovi_str     = f"{GREEN}✓ Dolby Vision{RESET}"     if cfg.has_dovi      else f"{YELLOW}✗ Dolby Vision{RESET}"
    hdr10p_str   = f"{GREEN}✓ HDR10+{RESET}"           if cfg.has_hdr10plus else f"{YELLOW}✗ HDR10+{RESET}"
    print(f"  {dovi_str}  |  {hdr10p_str}")

    if not cfg.has_dovi and not cfg.has_hdr10plus:
        die("Film 2 ne contient ni Dolby Vision ni HDR10+. Aucune opération possible.")

    if cfg.has_dovi and cfg.has_hdr10plus:
        ok("DoVi + HDR10+ détectés — les deux seront extraits et injectés.")
    elif cfg.has_dovi:
        ok("Dolby Vision uniquement — seul le RPU sera extrait et injecté.")
    else:
        ok("HDR10+ uniquement — seules les métadonnées HDR10+ seront extraites et injectées.")


def check_framecount(cfg: Config) -> None:
    log("Comparaison des frame counts...")

    def get_framecount(path: Path) -> int:
        raw = run_output(
            ["mediainfo", "--Inform=Video;%FrameCount%", str(path)]
        ).strip()
        if not re.fullmatch(r"\d+", raw):
            die(f"Impossible de lire le frame count de {path.name} (retour : '{raw}')")
        return int(raw)

    fc1 = get_framecount(cfg.film1)
    fc2 = get_framecount(cfg.film2)
    abs_diff = abs(fc2 - fc1)

    print(f"  Film 1 : {fc1} frames")
    print(f"  Film 2 : {fc2} frames")

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

# --- Tâches d'extraction (candidats au pool parallèle) ----------------------

def _task_extract_film1_hevc(cfg: Config) -> str:
    """
    Tâche A — Extraction du flux HEVC de Film 1.
    Appelée uniquement si film1 est un fichier MKV.
    Si film1 est déjà un HEVC, cfg.film1_hevc_input retourne film1 directement.
    """
    run(["mkvextract", str(cfg.film1), "tracks", f"0:{cfg.film1_hevc}"])
    return f"Film 1 HEVC extrait : {cfg.film1_hevc}"


def _task_extract_rpu(cfg: Config) -> str:
    """
    Tâche B — Extraction du RPU Dolby Vision depuis Film 2.
    dovi_tool accepte MKV et HEVC directement — pas d'extraction intermédiaire.
    """
    run(["dovi_tool", "extract-rpu", "-i", str(cfg.film2), "-o", str(cfg.film2_rpu)])
    return f"RPU extrait : {cfg.film2_rpu}"


def _task_extract_hdr10plus(cfg: Config) -> str:
    """
    Tâche C — Extraction des métadonnées HDR10+ depuis Film 2.
    hdr10plus_tool accepte MKV et HEVC directement — pas d'extraction intermédiaire.
    """
    run(["hdr10plus_tool", "extract", str(cfg.film2), "-o", str(cfg.film2_hdr10plus)])
    return f"HDR10+ extrait : {cfg.film2_hdr10plus}"


def step_extract_parallel(cfg: Config) -> None:
    """
    Phase d'extraction — jusqu'à 3 tâches parallèles selon les formats détectés.

    Graphe de dépendances :
      A : film1 HEVC (si MKV)    ┐
      B : RPU DoVi (si has_dovi)  ├─ indépendantes → parallèles
      C : HDR10+   (si has_h10+) ┘
      Étape 4 : inject-rpu   (attend A + B)
      Étape 5 : inject HDR10+ (attend C + étape 4 ou A seul)

    Règles :
      - Film 1 MKV → tâche A active  |  Film 1 HEVC → A ignorée (fichier utilisé directement)
      - Film 2 MKV ou HEVC → B et C passent film2 directement aux outils (pas d'extraction HEVC)
      - Les décisions d'écrasement sont résolues dans le thread principal AVANT le pool.
    """
    log("Phase d'extraction parallèle...")

    # Construction du catalogue de tâches selon la configuration détectée
    all_tasks: dict[str, tuple] = {}

    if cfg.film1_is_mkv:
        all_tasks["A — HEVC Film 1"] = (_task_extract_film1_hevc, cfg.film1_hevc)
    else:
        ok("A — Film 1 est déjà HEVC — utilisé directement, extraction ignorée.")

    if cfg.has_dovi:
        all_tasks["B — RPU DoVi"] = (_task_extract_rpu, cfg.film2_rpu)

    if cfg.has_hdr10plus:
        all_tasks["C — HDR10+"]   = (_task_extract_hdr10plus, cfg.film2_hdr10plus)

    if not all_tasks:
        ok("Aucune extraction nécessaire.")
        return

    # Résolution des flags de bypass (thread principal — stdin safe)
    skip = resolve_skip_flags({label: path for label, (_, path) in all_tasks.items()})
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
                ok(f"[{label}] {future.result()}")
            except Exception as exc:
                errors.append(f"[{label}] {exc}")

    if errors:
        die("Échec d'une ou plusieurs extractions :\n  " + "\n  ".join(errors))

    ok(f"Extractions actives terminées en {time.monotonic() - t_start:.1f}s")


def step_inject_rpu(cfg: Config) -> None:
    """
    Étape inject-rpu — uniquement si DoVi détecté.
    Entrée  : cfg.film1_hevc_input (film1_hevc si MKV, film1 si déjà HEVC)
    Sortie  : cfg.film1_with_dovi
    """
    if not cfg.has_dovi:
        return

    log(f"Injection RPU DoVi (mode {cfg.dovi_mode}) dans Film 1...")
    if not prompt_overwrite(cfg.film1_with_dovi):
        ok(f"Fichier existant conservé — étape bypassée : {cfg.film1_with_dovi.name}")
        return

    # -m est un flag GLOBAL de dovi_tool, placé avant la sous-commande.
    # Mode 2 : convertit/normalise en Profile 8.1 (supprime le mapping).
    run([
        "dovi_tool", "-m", cfg.dovi_mode, "inject-rpu",
        "-i", str(cfg.film1_hevc_input),
        "-r", str(cfg.film2_rpu),
        "-o", str(cfg.film1_with_dovi),
    ])
    ok(f"RPU injecté : {cfg.film1_with_dovi.name}")


def step_inject_hdr10plus(cfg: Config) -> None:
    """
    Étape inject hdr10+ — uniquement si HDR10+ détecté.
    Entrée  : film1_with_dovi si DoVi traité, sinon film1_hevc_input directement.
    Sortie  : cfg.film1_final
    """
    if not cfg.has_hdr10plus:
        return

    log("Injection métadonnées HDR10+ dans Film 1...")
    if not prompt_overwrite(cfg.film1_final):
        ok(f"Fichier existant conservé — étape bypassée : {cfg.film1_final.name}")
        return

    # Entrée : résultat de inject-rpu si DoVi traité, sinon HEVC direct de film1
    hevc_input = cfg.film1_with_dovi if cfg.has_dovi else cfg.film1_hevc_input

    run([
        "hdr10plus_tool", "inject",
        "-i", str(hevc_input),
        "-j", str(cfg.film2_hdr10plus),
        "-o", str(cfg.film1_final),
    ])
    ok(f"HDR10+ injecté : {cfg.film1_final.name}")


def step_verify(cfg: Config) -> None:
    """Vérification de l'intégrité des RPU frames — uniquement si DoVi traité."""
    if not cfg.has_dovi:
        return

    log("Vérification de l'intégrité des RPU frames...")

    # check=False : on ne plante pas si dovi_tool ne trouve pas "rpu frames"
    raw = run_output(
        ["dovi_tool", "info", "-i", str(cfg.injection_chain_final)], check=False
    )

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
    log("Remuxage final avec mkvmerge...")

    # Le HEVC final à injecter dans le conteneur
    final_hevc = cfg.injection_chain_final

    # Construire le --track-order dynamiquement depuis le nombre de pistes de film1
    identify_output = run_output(["mkvmerge", "--identify", str(cfg.film1)])
    nb_tracks = sum(1 for line in identify_output.splitlines() if line.startswith("Track ID"))
    parts = ["1:0"] + [f"0:{i}" for i in range(1, nb_tracks)]
    track_order = ",".join(parts)

    run([
        "mkvmerge",
        "-o",            str(cfg.output_mkv),
        "--no-video",    str(cfg.film1),
                         str(final_hevc),
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

    try:
        cfg.work_dir.rmdir()
    except OSError:
        pass  # Non vide — on ignore (rmdir --ignore-fail-on-non-empty)

    ok("Nettoyage terminé.")


# =============================================================================
# MAIN
# =============================================================================

def main() -> None:
    args = parse_args()
    cfg  = Config(args)

    # Vérifications préliminaires (avant banner pour détecter les formats)
    check_deps()
    check_files(cfg)
    check_hevc_streams(cfg)
    check_film2_hdr_formats(cfg)   # → renseigne cfg.has_dovi / cfg.has_hdr10plus
    check_framecount(cfg)

    # Banner résumé
    film1_mode = "MKV → extraction HEVC" if cfg.film1_is_mkv else "HEVC direct (pas d'extraction)"
    film2_mode = "MKV (direct)"          if cfg.film2_is_mkv else "HEVC (direct)"
    hdr_ops    = " + ".join(filter(None, [
        "DoVi RPU"  if cfg.has_dovi      else "",
        "HDR10+"    if cfg.has_hdr10plus else "",
    ]))

    print()
    print("=============================================")
    print("  merge_dovi_hdr10plus.py")
    print("=============================================")
    info_line("Film 1 (cible) :",  str(cfg.film1))
    info_line("Film 2 (source) :", str(cfg.film2))
    info_line("Film 1 mode :",     film1_mode)
    info_line("Film 2 mode :",     film2_mode)
    info_line("Opérations HDR :",  hdr_ops)
    info_line("Mode DoVi :",       f"{cfg.dovi_mode} (0=untouched|2=P8.1|3=P5→8.1|5=P8.1+map)")
    info_line("Dossier travail :", str(cfg.work_dir))
    info_line("Fichier sortie :",  cfg.output_mkv.name)
    print("=============================================")

    prepare_dirs(cfg)
    step_extract_parallel(cfg)
    step_inject_rpu(cfg)
    step_inject_hdr10plus(cfg)
    step_verify(cfg)
    step_remux(cfg)
    cleanup(cfg)

    print()
    print("=============================================")
    ok("TERMINÉ — Fichier de sortie :")
    print(f"  {cfg.output_mkv}")
    print("=============================================")
    print()


if __name__ == "__main__":
    main()