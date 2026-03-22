#!/usr/bin/env python3
# =============================================================================
# merge_dovi_hdr10plus.py
# Injecte le RPU Dolby Vision (Profile 8.1) et/ou les métadonnées HDR10+
# extraits de Film 2 (source) dans le flux vidéo de Film 1 (cible).
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
# Formats supportés :
#   Film 1 : .mkv ou .hevc — passé directement aux outils d'injection
#   Film 2 : .mkv ou .hevc — passé directement aux outils d'extraction
#   Toute autre extension provoque une erreur.
#
# Comportement automatique :
#   - Valide la présence d'un flux HEVC dans chaque fichier
#   - Détecte DoVi et/ou HDR10+ dans Film 2
#   - DoVi seul     → extrait RPU depuis Film 2, injecte dans Film 1
#   - HDR10+ seul   → extrait métadonnées depuis Film 2, injecte dans Film 1
#   - DoVi + HDR10+ → extrait les deux en parallèle, injecte les deux dans Film 1
#   - Propose de bypasser chaque étape si les fichiers intermédiaires existent déjà
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

SUPPORTED_EXTENSIONS = {".mkv", ".hevc"}


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
    print(f"  {CYAN}{label:<20}{RESET} {value}")


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
    parser.add_argument("-1", dest="film1",      metavar="<chemin>", help="Film 1 — cible (.mkv ou .hevc)")
    parser.add_argument("-2", dest="film2",      metavar="<chemin>", help="Film 2 — source DoVi/HDR10+ (.mkv ou .hevc)")
    parser.add_argument("-w", dest="work_dir",   metavar="<chemin>", help="Dossier de travail")
    parser.add_argument("-o", dest="output_dir", metavar="<chemin>", help="Dossier de sortie")
    parser.add_argument("-h", "--help", action="store_true",         help="Affiche cette aide")
    parser.add_argument(
        "--force", dest="force", action="store_true",
        help="Force le traitement même en cas d'incohérence détectée (frame count, HEVC, HDR)",
    )
    parser.add_argument(
        "--check-files", dest="check_files", action="store_true",
        help="Mode comparaison : analyse et compare les deux fichiers sans lancer la conversion",
    )

    args = parser.parse_args()

    if args.help:
        print()
        print(f"Usage : {parser.prog} [OPTIONS]")
        print()
        print("Options :")
        print("  -1 <chemin>   Film 1 — cible (.mkv ou .hevc)")
        print("  -2 <chemin>   Film 2 — source porteur DoVi/HDR10+ (.mkv ou .hevc)")
        print("  -w <chemin>   Dossier de travail (fichiers intermédiaires)")
        print("  -o <chemin>   Dossier de sortie")
        print("  -h            Affiche cette aide")
        print("  --check-files Mode comparaison (durée, framerate, frames extraites)")
        print("  --force       Force le traitement malgré une incohérence détectée")
        print()
        print("Exemples :")
        print(f"  {parser.prog} -1 /films/film1.mkv -2 /films/film2.mkv")
        print(f"  {parser.prog} -1 /films/film1.hevc -2 /films/film2.mkv -w /tmp/work -o /films/output")
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

        # Mode dovi_tool (-m flag global, placé avant la sous-commande) :
        #   0 = rewrite untouched | 2 = force Profile 8.1 (supprime mapping)
        #   3 = Profile 5→8.1    | 5 = Profile 8.1 en préservant mapping luma/chroma
        self.dovi_mode = env("DOVI_MODE", "2")

        # Extensions des fichiers source
        self.film1_ext    = self.film1.suffix.lower()
        self.film2_ext    = self.film2.suffix.lower()
        self.film1_is_mkv = self.film1_ext == ".mkv"

        # Chemins intermédiaires de travail
        # Film 1 MKV : flux HEVC extrait avant injection (dovi_tool/hdr10plus_tool
        # inject-rpu n'acceptent que du HEVC brut, pas de MKV).
        # Film 1 HEVC : utilisé directement sans extraction.
        self.film1_hevc      = self.work_dir / "film1.hevc"             # HEVC extrait de Film 1
        # Film 2 MKV + deux formats : HEVC extrait une fois pour éviter la contention I/O.
        self.film2_hevc      = self.work_dir / "film2.hevc"             # HEVC temporaire Film 2
        self.film2_rpu       = self.work_dir / "film2_rpu.bin"          # RPU DoVi de Film 2
        self.film2_hdr10plus = self.work_dir / "film2_hdr10plus.json"   # HDR10+ de Film 2
        # Sorties intermédiaires d'injection sur Film 1 :
        self.film1_with_dovi = self.work_dir / "film1_with_dovi.hevc"   # Film 1 + RPU
        self.film1_final     = self.work_dir / "film1_final.hevc"       # Film 1 + RPU + HDR10+
        self.output_mkv      = self.output_dir / f"{output_basename}.mkv"
        # Dossiers de frames — nommés d'après le stem des fichiers source
        self.frames1_dir     = self.work_dir / f"frames_{self.film1.stem}"
        self.frames2_dir     = self.work_dir / f"frames_{self.film2.stem}"

        # Mode force — ignore les erreurs d'incohérence de flux
        self.force: bool = getattr(args, "force", False)

        # Détection des formats HDR (rempli par check_film2_hdr_formats)
        self.has_dovi     : bool = False
        self.has_hdr10plus: bool = False

    @property
    def film1_hevc_input(self) -> Path:
        """
        Chemin HEVC à utiliser en entrée des outils d'injection.
        Si Film 1 est MKV → film1_hevc (extrait en phase d'extraction).
        Si Film 1 est déjà HEVC → film1 directement.
        """
        return self.film1_hevc if self.film1_is_mkv else self.film1

    @property
    def injection_chain_final(self) -> Path:
        """
        Fichier HEVC final à muxer dans le conteneur de sortie.
          DoVi + HDR10+ → film1_final     (Film 1 + RPU + HDR10+)
          DoVi seul     → film1_with_dovi (Film 1 + RPU)
          HDR10+ seul   → film1_final     (Film 1 + HDR10+)
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
    """Lance une commande externe, lève CalledProcessError en cas d'échec."""
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
    Si le fichier cible existe déjà, demande confirmation à l'utilisateur.
    Retourne True  → écraser et relancer l'étape.
    Retourne False → conserver le fichier existant et bypasser l'étape.
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
    DOIT être appelé dans le thread principal avant tout ThreadPoolExecutor
    (stdin ne peut pas être lu depuis des threads parallèles).
    Retourne {label: skip} où skip=True signifie « bypasser l'étape ».
    """
    skip: dict[str, bool] = {}
    for label, path in paths.items():
        overwrite = prompt_overwrite(path)
        skip[label] = not overwrite
        if not overwrite:
            ok(f"[{label}] Fichier existant conservé — étape bypassée.")
    return skip


# =============================================================================
# ERREUR AVEC PROPOSITION CHECKFILES
# =============================================================================

def die_or_checkfiles(msg: str, cfg: "Config") -> None:
    """
    Affiche une erreur bloquante en cas d'incohérence de flux.

    Comportement selon les flags actifs :
      --force       : affiche un avertissement et poursuit le traitement sans prompt.
      (aucun flag)  : propose de lancer --check-files ou de quitter.

    Utilisé pour les incohérences de flux uniquement (HEVC absent, HDR manquant,
    frame count incompatible) — pas pour les erreurs de configuration fatales.
    """
    if cfg.force:
        warn(f"[FORCE] Incohérence ignorée : {msg}")
        return

    print(f"\n{RED}[ERROR]{RESET} {msg}", file=sys.stderr)
    print()
    while True:
        choice = input(
            f"  {YELLOW}Voulez-vous lancer --check-files pour comparer les fichiers ?{RESET} "
            "[O/N]  > "
        ).strip().upper()
        if choice == "O":
            run_check_files(cfg)
            sys.exit(1)
        if choice in ("N", ""):
            sys.exit(1)
        print("  Répondre O (oui) ou N (non).")


# =============================================================================
# VÉRIFICATIONS PRÉLIMINAIRES
# =============================================================================

def check_deps() -> None:
    log("Vérification des dépendances...")
    tools = ["mkvmerge", "mediainfo", "dovi_tool", "hdr10plus_tool", "ffmpeg"]
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


def check_formats(cfg: Config) -> None:
    """
    Valide les extensions des deux fichiers.
    Formats acceptés : .mkv et .hevc (passés directement aux outils).
    Toute autre extension provoque une erreur — aucun outil de conversion
    d'extraction n'est disponible pour d'autres conteneurs.
    """
    log("Vérification des formats de fichiers...")

    if cfg.film1_ext not in SUPPORTED_EXTENSIONS:
        die(
            f"Film 1 : format non supporté '{cfg.film1_ext}'. "
            f"Formats acceptés : {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )
    ok(f"Film 1 — format : {cfg.film1_ext}")

    if cfg.film2_ext not in SUPPORTED_EXTENSIONS:
        die(
            f"Film 2 : format non supporté '{cfg.film2_ext}'. "
            f"Formats acceptés : {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )
    ok(f"Film 2 — format : {cfg.film2_ext}")


def check_hevc_streams(cfg: Config) -> None:
    """Vérifie que les deux fichiers contiennent bien un flux vidéo HEVC."""
    log("Vérification des flux HEVC...")

    def has_hevc(path: Path) -> bool:
        raw = run_output(["mediainfo", "--Inform=Video;%Format%", str(path)]).strip()
        return raw.upper() == "HEVC"

    if not has_hevc(cfg.film1):
        die_or_checkfiles(f"Film 1 ne contient pas de flux HEVC : {cfg.film1.name}", cfg)
    ok("Film 1 — flux HEVC détecté.")

    if not has_hevc(cfg.film2):
        die_or_checkfiles(f"Film 2 ne contient pas de flux HEVC : {cfg.film2.name}", cfg)
    ok("Film 2 — flux HEVC détecté.")


def check_film2_hdr_formats(cfg: Config) -> None:
    """
    Détecte la présence de Dolby Vision et/ou HDR10+ dans Film 2 (source).
    Met à jour cfg.has_dovi et cfg.has_hdr10plus.
    Arrête le script si aucun des deux formats n'est détecté.
    """
    log("Détection des formats HDR dans Film 2 (source)...")

    raw = run_output(
        ["mediainfo", "--Inform=Video;%HDR_Format%", str(cfg.film2)]
    ).strip()

    cfg.has_dovi      = "Dolby Vision" in raw
    cfg.has_hdr10plus = "SMPTE ST 2094" in raw

    dovi_str  = f"{GREEN}✓ Dolby Vision{RESET}" if cfg.has_dovi      else f"{YELLOW}✗ Dolby Vision{RESET}"
    hdr10_str = f"{GREEN}✓ HDR10+{RESET}"       if cfg.has_hdr10plus else f"{YELLOW}✗ HDR10+{RESET}"
    print(f"  {dovi_str}  |  {hdr10_str}")

    if not cfg.has_dovi and not cfg.has_hdr10plus:
        die_or_checkfiles(
            "Film 2 ne contient ni Dolby Vision ni HDR10+. Aucune opération possible.", cfg
        )

    if cfg.has_dovi and cfg.has_hdr10plus:
        ok("DoVi + HDR10+ détectés — les deux seront extraits de Film 2 et injectés dans Film 1.")
    elif cfg.has_dovi:
        ok("Dolby Vision uniquement — RPU extrait de Film 2 et injecté dans Film 1.")
    else:
        ok("HDR10+ uniquement — métadonnées extraites de Film 2 et injectées dans Film 1.")


def check_framecount(cfg: Config) -> None:
    """
    Compare les frame counts de Film 1 et Film 2.
    Tolérance de 4 frames (padding muxer en fin de fichier).
    """
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
        die_or_checkfiles(
            f"Différence de {abs_diff} frames trop importante — "
            "les deux fichiers ne semblent pas être le même contenu.",
            cfg,
        )


def prepare_dirs(cfg: Config) -> None:
    log("Préparation des dossiers...")
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    ok(f"Dossier de travail : {cfg.work_dir}")
    ok(f"Dossier de sortie  : {cfg.output_dir}")


# =============================================================================
# ÉTAPES DU WORKFLOW
# =============================================================================

# --- Tâches d'extraction des métadonnées de Film 2 (candidats au pool) ------

def _task_extract_rpu(cfg: Config, source: Path) -> str:
    """
    Tâche B — Extraction du RPU Dolby Vision.
    source : film2 direct (HEVC ou MKV seul format) ou film2_hevc (pré-extrait).
    """
    run(["dovi_tool", "extract-rpu", "-i", str(source), "-o", str(cfg.film2_rpu)])
    return f"RPU DoVi extrait → {cfg.film2_rpu.name}"


def _task_extract_hdr10plus(cfg: Config, source: Path) -> str:
    """
    Tâche C — Extraction des métadonnées HDR10+.
    source : film2 direct (HEVC ou MKV seul format) ou film2_hevc (pré-extrait).
    """
    run(["hdr10plus_tool", "extract", str(source), "-o", str(cfg.film2_hdr10plus)])
    return f"HDR10+ extrait → {cfg.film2_hdr10plus.name}"


def _run_pool(tasks: dict[str, callable], cfg: Config, errors: list[str]) -> None:
    """Lance un ensemble de tâches en parallèle et collecte les erreurs."""
    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = {executor.submit(fn, cfg): label for label, fn in tasks.items()}
        for future in as_completed(futures):
            label = futures[future]
            try:
                ok(f"[{label}] {future.result()}")
            except Exception as exc:
                errors.append(f"[{label}] {exc}")


def step_extract_parallel(cfg: Config) -> None:
    """
    Phase d'extraction — deux phases parallèles selon les dépendances.

    Graphe de dépendances :
                                              ┌─ B: dovi_tool extract-rpu ─────┐
      A: mkvextract film1 → film1.hevc ───────┤                                 ├─ inject
                                              └─ C: hdr10plus_tool extract ─────┘

    Stratégie selon les cas :

      Cas 1 — film2 est HEVC, ou un seul format :
        Phase unique tout en parallèle :  A + B + C

      Cas 2 — film2 est MKV et les deux formats sont nécessaires :
        Lire deux fois le même gros MKV simultanément = contention I/O.
        Phase 1 parallèle : A (film1.hevc) + D (film2.hevc)   ← deux MKV différents, OK
        Phase 2 parallèle : B (rpu) + C (hdr10+)              ← sur film2.hevc léger

    IMPORTANT : prompt_overwrite() doit être appelé dans le thread principal
    (stdin ne peut pas être lu depuis des threads parallèles).
    Tous les bypass sont donc résolus avant le lancement des pools.
    """
    log("Phase d'extraction...")

    both_needed  = cfg.has_dovi and cfg.has_hdr10plus
    film2_is_mkv = cfg.film2_ext == ".mkv"

    # ── Résolution de tous les bypass dans le thread principal ────────────────
    # Film 1 HEVC
    skip_film1 = False
    if cfg.film1_is_mkv:
        if not prompt_overwrite(cfg.film1_hevc):
            ok("A — film1.hevc existant conservé — extraction ignorée.")
            skip_film1 = True
    else:
        ok("A — Film 1 est déjà HEVC — utilisé directement.")
        skip_film1 = True  # Pas d'extraction nécessaire

    # Film 2 pré-extraction HEVC (Cas 2 uniquement)
    skip_film2_hevc = True  # Par défaut : pas de pré-extraction
    if both_needed and film2_is_mkv:
        if not prompt_overwrite(cfg.film2_hevc):
            ok("D — film2.hevc existant conservé — pré-extraction ignorée.")
        else:
            skip_film2_hevc = False  # Pré-extraction nécessaire

    # Métadonnées Film 2 (B et/ou C)
    meta_targets: dict[str, Path] = {}
    if cfg.has_dovi:
        meta_targets["B — RPU DoVi"] = cfg.film2_rpu
    if cfg.has_hdr10plus:
        meta_targets["C — HDR10+"]   = cfg.film2_hdr10plus
    skip_meta = resolve_skip_flags(meta_targets)

    # ── Exécution ─────────────────────────────────────────────────────────────
    errors: list[str] = []
    t_total = time.monotonic()

    if not (both_needed and film2_is_mkv):
        # ── Cas 1 : tout en parallèle — A + B + C ────────────────────────────
        source = cfg.film2
        phase1: dict[str, callable] = {}

        if not skip_film1:
            phase1["A — HEVC Film 1"] = lambda c: (
                run(["mkvextract", str(c.film1), "tracks", f"0:{c.film1_hevc}"]),
                f"film1.hevc extrait"
            )[1]

        if cfg.has_dovi and not skip_meta.get("B — RPU DoVi", False):
            phase1["B — RPU DoVi"] = lambda c, s=source: _task_extract_rpu(c, s)
        if cfg.has_hdr10plus and not skip_meta.get("C — HDR10+", False):
            phase1["C — HDR10+"]   = lambda c, s=source: _task_extract_hdr10plus(c, s)

        if phase1:
            log(f"Cas 1 — extraction parallèle ({len(phase1)} tâche(s) : {', '.join(phase1)})...")
            _run_pool(phase1, cfg, errors)
        else:
            ok("Cas 1 — toutes les extractions bypassées.")

    else:
        # ── Cas 2 : Phase 1 (A + D) puis Phase 2 (B + C) ────────────────────
        phase1: dict[str, callable] = {}
        if not skip_film1:
            phase1["A — HEVC Film 1"] = lambda c: (
                run(["mkvextract", str(c.film1), "tracks", f"0:{c.film1_hevc}"]),
                f"film1.hevc extrait"
            )[1]
        if not skip_film2_hevc:
            phase1["D — HEVC Film 2"] = lambda c: (
                run(["mkvextract", str(c.film2), "tracks", f"0:{c.film2_hevc}"]),
                f"film2.hevc extrait"
            )[1]

        if phase1:
            log(f"Cas 2 — Phase 1 : extraction HEVC parallèle ({', '.join(phase1)})...")
            _run_pool(phase1, cfg, errors)
        else:
            ok("Cas 2 — Phase 1 bypassée (fichiers existants).")

        if errors:
            die("Erreur en Phase 1 :\n  " + "\n  ".join(errors))

        # Phase 2 : B et C sur film2.hevc (léger, pas de contention)
        source = cfg.film2_hevc
        phase2: dict[str, callable] = {}
        if cfg.has_dovi and not skip_meta.get("B — RPU DoVi", False):
            phase2["B — RPU DoVi"] = lambda c, s=source: _task_extract_rpu(c, s)
        if cfg.has_hdr10plus and not skip_meta.get("C — HDR10+", False):
            phase2["C — HDR10+"]   = lambda c, s=source: _task_extract_hdr10plus(c, s)

        if phase2:
            log(f"Cas 2 — Phase 2 : extraction métadonnées parallèle ({', '.join(phase2)})...")
            _run_pool(phase2, cfg, errors)
        else:
            ok("Cas 2 — Phase 2 bypassée (fichiers existants).")

    if errors:
        die("Échec d'une ou plusieurs extractions :\n  " + "\n  ".join(errors))

    ok(f"Phase d'extraction terminée en {time.monotonic() - t_total:.1f}s")

    if errors:
        die("Échec d'une ou plusieurs extractions :\n  " + "\n  ".join(errors))

    ok(f"Extractions terminées en {time.monotonic() - t_start:.1f}s")


def step_inject_rpu(cfg: Config) -> None:
    """
    Injection du RPU Dolby Vision (extrait de Film 2) dans Film 1.
    Entrée vidéo : Film 1 directement (.mkv ou .hevc, dovi_tool accepte les deux).
    Sortie       : film1_with_dovi.hevc (HEVC avec RPU de Film 2 intégré).
    """
    if not cfg.has_dovi:
        return

    log(f"Injection RPU DoVi (mode {cfg.dovi_mode}) — Film 2 → Film 1...")
    if not prompt_overwrite(cfg.film1_with_dovi):
        ok(f"Fichier existant conservé — étape bypassée : {cfg.film1_with_dovi.name}")
        return

    # -m est un flag GLOBAL de dovi_tool, placé avant la sous-commande inject-rpu.
    # Mode 2 : convertit/normalise le RPU en Profile 8.1 (supprime le mapping).
    # Modes disponibles : 0=untouched | 2=P8.1 | 3=P5→8.1 | 5=P8.1+mapping
    run([
        "dovi_tool", "-m", cfg.dovi_mode, "inject-rpu",
        "-i", str(cfg.film1_hevc_input),  # HEVC brut requis (extrait si film1 est MKV)
        "-r", str(cfg.film2_rpu),          # RPU extrait de Film 2
        "-o", str(cfg.film1_with_dovi),
    ])
    ok(f"RPU de Film 2 injecté dans Film 1 → {cfg.film1_with_dovi.name}")


def step_inject_hdr10plus(cfg: Config) -> None:
    """
    Injection des métadonnées HDR10+ (extraites de Film 2) dans Film 1.
    Entrée vidéo : film1_with_dovi si DoVi déjà injecté, sinon Film 1 directement.
    Sortie       : film1_final.hevc (HEVC final enrichi).
    """
    if not cfg.has_hdr10plus:
        return

    log("Injection HDR10+ — Film 2 → Film 1...")
    if not prompt_overwrite(cfg.film1_final):
        ok(f"Fichier existant conservé — étape bypassée : {cfg.film1_final.name}")
        return

    # Si DoVi a déjà été injecté, on part de film1_with_dovi.
    # Sinon (HDR10+ seul), on part du HEVC brut de Film 1.
    hevc_input = cfg.film1_with_dovi if cfg.has_dovi else cfg.film1_hevc_input

    run([
        "hdr10plus_tool", "inject",
        "-i", str(hevc_input),            # Film 1 (potentiellement déjà enrichi avec DoVi)
        "-j", str(cfg.film2_hdr10plus),   # Métadonnées HDR10+ extraites de Film 2
        "-o", str(cfg.film1_final),
    ])
    ok(f"HDR10+ de Film 2 injecté dans Film 1 → {cfg.film1_final.name}")


def step_verify(cfg: Config) -> None:
    """
    Vérifie l'intégrité des RPU frames dans le HEVC final.
    Uniquement si DoVi a été traité — HDR10+ seul n'a pas de RPU à vérifier.
    """
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
    """
    Remuxage final : remplace la piste vidéo de Film 1 (MKV) par le HEVC enrichi,
    en conservant toutes les pistes audio, sous-titres et chapitres de Film 1.
    """
    log("Remuxage final avec mkvmerge...")

    final_hevc = cfg.injection_chain_final

    # Construction dynamique du --track-order à partir des pistes réelles de Film 1
    identify_output = run_output(["mkvmerge", "--identify", str(cfg.film1)])
    nb_tracks = sum(1 for line in identify_output.splitlines() if line.startswith("Track ID"))
    # Source 0 = Film 1 (pistes non-vidéo), source 1 = HEVC final (vidéo enrichie)
    parts = ["1:0"] + [f"0:{i}" for i in range(1, nb_tracks)]
    track_order = ",".join(parts)

    run([
        "mkvmerge",
        "-o",            str(cfg.output_mkv),
        "--no-video",    str(cfg.film1),    # Audio, subs, chapitres de Film 1
                         str(final_hevc),   # Vidéo HEVC enrichie (DoVi + HDR10+)
        "--track-order", track_order,
    ])
    ok(f"Fichier final créé : {cfg.output_mkv}")


def cleanup(cfg: Config) -> None:
    log("Nettoyage des fichiers intermédiaires...")
    for path in [
        cfg.film1_hevc,
        cfg.film2_hevc,
        cfg.film2_rpu,
        cfg.film2_hdr10plus,
        cfg.film1_with_dovi,
        cfg.film1_final,
    ]:
        path.unlink(missing_ok=True)

    try:
        cfg.work_dir.rmdir()
    except OSError:
        pass  # Non vide — on ignore (équivalent rmdir --ignore-fail-on-non-empty)

    ok("Nettoyage terminé.")


# =============================================================================
# MODE NETTOYAGE (--clean)
# =============================================================================

def run_clean(cfg: Config) -> None:
    """
    Mode --clean : efface tous les fichiers intermédiaires du workdir
    relatifs aux fichiers source demandés.

    Fichiers supprimés :
      - film2_rpu.bin
      - film2_hdr10plus.json
      - film1_with_dovi.hevc
      - film1_final.hevc
      - frames_{film1.stem}/   (dossier complet)
      - frames_{film2.stem}/   (dossier complet)
      - work_dir/              (si vide après nettoyage)
    """
    log(f"Mode nettoyage — workdir : {cfg.work_dir}")

    if not cfg.work_dir.exists():
        warn(f"Le dossier de travail n'existe pas : {cfg.work_dir}")
        return

    total_deleted = 0

    # Fichiers intermédiaires unitaires
    for path in [
        cfg.film1_hevc,
        cfg.film2_hevc,
        cfg.film2_rpu,
        cfg.film2_hdr10plus,
        cfg.film1_with_dovi,
        cfg.film1_final,
    ]:
        if path.exists():
            size_mb = path.stat().st_size / (1024 ** 2)
            path.unlink()
            ok(f"Supprimé : {path.name}  ({size_mb:.1f} Mo)")
            total_deleted += 1
        else:
            print(f"  {YELLOW}—{RESET}  {path.name}  (absent)")

    # Dossiers de frames
    for frames_dir in [cfg.frames1_dir, cfg.frames2_dir]:
        if frames_dir.exists():
            count = sum(1 for _ in frames_dir.glob('*.png'))
            shutil.rmtree(frames_dir)
            ok(f"Supprimé : {frames_dir.name}/  ({count} fichier(s))")
            total_deleted += 1
        else:
            print(f"  {YELLOW}—{RESET}  {frames_dir.name}/  (absent)")

    # Suppression du workdir si vide
    try:
        cfg.work_dir.rmdir()
        ok(f"Dossier de travail supprimé : {cfg.work_dir}")
    except OSError:
        warn(f"Dossier de travail non vide — conservé : {cfg.work_dir}")

    print()
    if total_deleted:
        ok(f"Nettoyage terminé — {total_deleted} élément(s) supprimé(s).")
    else:
        warn("Aucun fichier à nettoyer.")


# =============================================================================
# MAIN
# =============================================================================


# =============================================================================
# MODE COMPARAISON (--check-files)
# =============================================================================

def _compare_field(label: str, val1: str, val2: str, ok_if_equal: bool = True) -> None:
    """Affiche une ligne de comparaison colorée selon l'égalité des valeurs."""
    equal = val1.strip() == val2.strip()
    color = GREEN if (equal == ok_if_equal) else YELLOW
    status = "=" if equal else "≠"
    print(f"  {color}{status}{RESET}  {CYAN}{label:<16}{RESET}  Film 1: {val1:<20}  Film 2: {val2}")


def run_check_files(cfg: Config) -> None:
    """
    Mode --check-files : compare les deux fichiers sans lancer la conversion.

    Opérations :
      1. Durée des streams vidéo (mediainfo)
      2. Framerates (mediainfo)
      3. Frame counts (mediainfo)
      4. Extraction des 50 premières frames de chaque film (ffmpeg) dans le work_dir
      5. Affichage du chemin des frames extraites pour consultation visuelle
    """
    log("Mode comparaison — analyse des deux fichiers...")

    # Vérifications minimales nécessaires
    check_files(cfg)
    check_formats(cfg)
    check_hevc_streams(cfg)
    prepare_dirs(cfg)

    # ── Durée ────────────────────────────────────────────────────────────────
    log("Durée des streams vidéo...")
    dur1 = run_output(["mediainfo", "--Inform=Video;%Duration%", str(cfg.film1)]).strip()
    dur2 = run_output(["mediainfo", "--Inform=Video;%Duration%", str(cfg.film2)]).strip()

    def ms_to_hms(ms_str: str) -> str:
        try:
            ms = int(float(ms_str))
            h, rem = divmod(ms, 3_600_000)
            m, rem = divmod(rem, 60_000)
            s, ms  = divmod(rem, 1_000)
            return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"
        except ValueError:
            return ms_str

    dur1_hms = ms_to_hms(dur1)
    dur2_hms = ms_to_hms(dur2)
    _compare_field("Durée (ms)", dur1, dur2)
    print(f"           {'Film 1':>8} : {dur1_hms}")
    print(f"           {'Film 2':>8} : {dur2_hms}")

    delta_ms: int | None = None
    try:
        delta_ms = abs(int(float(dur1)) - int(float(dur2)))
        delta_str = ms_to_hms(str(delta_ms))
        color = GREEN if delta_ms <= 200 else YELLOW
        print(f"           {color}Delta    : {delta_str} ({delta_ms} ms){RESET}")
    except ValueError:
        warn("Impossible de calculer le delta de durée.")

    # ── Framerate ────────────────────────────────────────────────────────────
    log("Framerates...")
    fps1 = run_output(["mediainfo", "--Inform=Video;%FrameRate%", str(cfg.film1)]).strip()
    fps2 = run_output(["mediainfo", "--Inform=Video;%FrameRate%", str(cfg.film2)]).strip()
    _compare_field("FrameRate", fps1, fps2)

    # ── Frame count ──────────────────────────────────────────────────────────
    log("Frame counts...")
    fc1_raw = run_output(["mediainfo", "--Inform=Video;%FrameCount%", str(cfg.film1)]).strip()
    fc2_raw = run_output(["mediainfo", "--Inform=Video;%FrameCount%", str(cfg.film2)]).strip()
    _compare_field("FrameCount", fc1_raw, fc2_raw)
    try:
        fc_delta = abs(int(fc1_raw) - int(fc2_raw))
        color = GREEN if fc_delta <= 4 else YELLOW
        print(f"           {color}Delta    : {fc_delta} frame(s){RESET}")
    except ValueError:
        warn("Impossible de calculer le delta de frame count.")

    # ── Extraction des 50 premières frames ───────────────────────────────────
    log("Extraction des 50 premières frames (ffmpeg)...")

    frames1_dir = cfg.frames1_dir
    frames2_dir = cfg.frames2_dir
    frames1_dir.mkdir(parents=True, exist_ok=True)
    frames2_dir.mkdir(parents=True, exist_ok=True)

    frames1_pattern = str(frames1_dir / "frame%03d.png")
    frames2_pattern = str(frames2_dir / "frame%03d.png")

    errors: list[str] = []

    def extract_frames(path: Path, pattern: str, label: str) -> str:
        # run_output capture stdout+stderr — évite la pollution du terminal par ffmpeg
        run_output([
            "ffmpeg", "-y",
            "-i",        str(path),
            "-map",      "0:v",
            "-frames:v", "50",
            "-q:v",      "1",
            pattern,
        ])
        return f"50 frames extraites — {label}"

    t_start = time.monotonic()
    tasks = {
        "Film 1": (cfg.film1, frames1_pattern),
        "Film 2": (cfg.film2, frames2_pattern),
    }

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            executor.submit(extract_frames, path, pattern, label): label
            for label, (path, pattern) in tasks.items()
        }
        for future in as_completed(futures):
            label = futures[future]
            try:
                ok(f"[{label}] {future.result()}")
            except Exception as exc:
                errors.append(f"[{label}] {exc}")

    if errors:
        warn("Erreur lors de l'extraction des frames : " + " | ".join(errors))
    else:
        ok(f"Extraction terminée en {time.monotonic() - t_start:.1f}s")

    # ── Affichage des chemins de consultation ────────────────────────────────
    print()
    print("=============================================")
    print("  Frames extraites — chemins de consultation")
    print("=============================================")
    info_line("Film 1 (50 frames) :", str(frames1_dir))
    info_line("Film 2 (50 frames) :", str(frames2_dir))
    print("=============================================")
    print()

def main() -> None:
    args = parse_args()
    cfg  = Config(args)

    # Modes alternatifs — court-circuitent le workflow de conversion
    if args.clean:
        run_clean(cfg)
        return

    if args.check_files:
        run_check_files(cfg)
        return

    # Vérifications préliminaires — dans l'ordre logique
    check_deps()
    check_files(cfg)
    check_formats(cfg)              # Extension .mkv ou .hevc uniquement
    check_hevc_streams(cfg)         # Flux HEVC présent dans les deux fichiers
    check_film2_hdr_formats(cfg)    # DoVi et/ou HDR10+ présents dans Film 2
    check_framecount(cfg)           # Frame counts compatibles

    # Résumé des opérations
    hdr_ops = " + ".join(filter(None, [
        "DoVi RPU" if cfg.has_dovi      else "",
        "HDR10+"   if cfg.has_hdr10plus else "",
    ]))

    print()
    print("=============================================")
    print("  merge_dovi_hdr10plus.py")
    print("=============================================")
    info_line("Film 1 (cible) :",  str(cfg.film1))
    info_line("Film 2 (source) :", str(cfg.film2))
    info_line("Film 1 format :",   cfg.film1_ext)
    info_line("Film 2 format :",   cfg.film2_ext)
    info_line("Opérations HDR :",  f"{hdr_ops}  (extraits de Film 2 → injectés dans Film 1)")
    info_line("Mode DoVi :",       f"{cfg.dovi_mode}  (0=untouched | 2=P8.1 | 3=P5→8.1 | 5=P8.1+map)")
    if cfg.force:
        info_line("Mode :", f"{YELLOW}--force actif — incohérences ignorées{RESET}")
    info_line("Dossier travail :", str(cfg.work_dir))
    info_line("Fichier sortie :",  cfg.output_mkv.name)
    print("=============================================")

    prepare_dirs(cfg)
    step_extract_parallel(cfg)   # Extraction métadonnées Film 2 (B et/ou C, en parallèle)
    step_inject_rpu(cfg)         # Injection RPU DoVi dans Film 1 (si DoVi)
    step_inject_hdr10plus(cfg)   # Injection HDR10+ dans Film 1 (si HDR10+)
    step_verify(cfg)             # Vérification intégrité RPU frames (si DoVi)
    step_remux(cfg)              # Remuxage final
    cleanup(cfg)                 # Nettoyage intermédiaires

    print()
    print("=============================================")
    ok("TERMINÉ — Fichier de sortie :")
    print(f"  {cfg.output_mkv}")
    print("=============================================")
    print()


if __name__ == "__main__":
    main()