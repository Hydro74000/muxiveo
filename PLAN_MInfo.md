# PLAN_MInfo.md

## Mission
Remplacer la dépendance runtime au binaire `mediainfo` par un moteur Python natif intégré à Mediarecode, tout en conservant:
- un mode standalone (`minfo.py` et `python -m core.mediainfo_native`)
- un oracle C++ (MediaInfo CLI officiel) pour la parité dev/CI.

## Baseline verrouillée
- Snapshot de référence: `/var/home/hydromel/dev/MediaInfo/MediaInfo_CLI_CPP` (v26.01).
- Contrat final: parité bit-à-bit de sortie CLI.
- Runtime prod: 0 appel obligatoire au binaire `mediainfo`.
- Validation: matrice Linux/Windows/macOS avec corpus externe versionné et hashé.

## Constraint Pivot (2026-04-16)
- `runtime = stdlib Python only` pour `core/mediainfo_native`.
- `0 outil externe` pour parser/analyser au runtime (ni `ffprobe`, ni `mediainfo`, ni `ffmpeg`).
- Module standalone extractible hors Mediarecode.
- Contrat de sortie:
  - CLI: parité 1:1 avec MediaInfo C++.
  - Module: sortie interne structurée stable en plus des rendus.
- Oracle C++ autorisé uniquement en dev/CI pour validation de parité.

## Règle de maintenance du plan
- Les steps clos et validés restent présents avec leurs **grands titres** et un **résumé minimal utile**.
- Les détails d'exécution obsolètes sont supprimés au fur et à mesure pour économiser les tokens.
- Les éléments non terminés, blocages, écarts et prochaines actions ne sont jamais supprimés.
- À chaque jalon: mise à jour des scores (`strict/extended/expanded`), blocages restants, prochaines actions.

## Architecture cible
- Module: `core/mediainfo_native/`
  - façades publiques: `engine/`, `compat/`, `cli/`
  - sous-modules: `api/`, `options/`, `io/`, `parsers/`, `enrich/`, `renderers/`, `validation/`
  - `__main__.py`: exécution module
- Launcher standalone: `minfo.py`
- Intégration Mediarecode: inspection, workflows remux/encode/merge, NFO, scripts utilitaires.
- Validation:
  - `scripts/mediainfo_parity_matrix.py`
  - `scripts/verify_mediainfo_corpus.py`
  - manifestes corpus/parité (`real`, `extended`, `expanded`).

## Jalons
1. Shim runtime + API de compatibilité + standalone
2. Migration runtime Mediarecode
3. Config/packaging/doc (dépendance runtime retirée)
4. Harnais parité bit-à-bit + corpus versionné
5. Couverture fonctionnalités MediaInfo v26.01
6. Réécriture native pure post-pivot (suppression complète dépendance parser externe)

## Journal d'avancement

### Step 1 — Shim runtime + standalone
Status: `DONE`
- Livré: moteur natif initial, wrappers compat (`MediaInfo`, `MediaInfoList`), CLI module, launcher `minfo.py`.
- Validation de base exécutée (`compile`, `--Version`, `--Help`).

### Step 2 — Migration runtime Mediarecode
Status: `DONE (phase 1)`
- Flux runtime migrés vers le module natif (`inspector`, `workflows`, `mediamanager`, scripts merge).
- `core/runner.py`: retrait de `mediainfo` des outils runtime requis.
- `mediainfo_bin` conservé uniquement pour compat config/UI/oracle dev.

### Step 3 — Config / Packaging / Doc runtime
Status: `DONE (phase 1)`
- Retrait `pymediainfo` (`requirements.txt`, `setup.py`, packaging).
- `mediainfo` retiré des prérequis runtime; conservé en option oracle dev.
- Docs mises à jour vers moteur natif.
- CI build release alignée:
  - retrait de `pymediainfo` des workflows `.github/workflows/build-appimage.yml` et `.github/workflows/build-windows.yml`.
- Setup Windows aligné runtime natif:
  - retrait de l’auto-remplissage `mediainfo` dans `setup.py` (`WINDOWS_CONFIG_TOOL_ORDER`).
  - `mediainfo` reste uniquement un outil oracle dev/CI configuré manuellement.

### Step 4 — Harnais parité + corpus
Status: `DONE (CI multi-OS verte)`
- Livré:
  - `scripts/mediainfo_parity_matrix.py` (comparaison bit-à-bit oracle vs natif, environnement figé, rapports JSON)
  - `scripts/mediainfo_parity_gate.py` (gate CI: fail si `failed>0`, `skipped>0`, ou rapport vide)
  - `scripts/verify_mediainfo_corpus.py`
  - manifests corpus/parité (`example`, `real`, `extended`, `expanded`)
  - workflow CI multi-OS: `.github/workflows/mediainfo-parity-multi-os.yml`
  - workflow CI renforcé: step `Parity gate (no failed/no skipped)` sur chaque OS.
  - auto-résolution oracle dans `mediainfo_parity_matrix.py` (`MEDIAINFO_ORACLE_BIN` -> `PATH` -> binaire local `MediaInfo_CLI_CPP`).
  - CI parity multi-OS épurée des dépendances `ffmpeg/ffprobe` (oracle `mediainfo` uniquement).
  - workflow parité rendu réutilisable (`on.workflow_call`).
  - archivage CI consolidé:
    - job `aggregate-reports` qui télécharge les artifacts OS, valide la présence des rapports, publie `mediainfo-parity-reports-all-os`.
  - gate release bloquant activé:
    - `.github/workflows/build-appimage.yml` -> job `parity-gate` (reusable workflow) + `build` dépendant de `needs: parity-gate`
    - `.github/workflows/build-windows.yml` -> job `parity-gate` (reusable workflow) + `build` dépendant de `needs: parity-gate`
- Corpus réel actuel validé (hash+taille): `8/8 OK`.
- État mesures Linux local:
  - strict: `39/39`
  - extended: `44/44`
  - expanded: `96/96`
- Reste à faire:
  - activer l’obligation de succès de ce gate dans les branch protection / release policy GitHub.
  - blocage courant (2026-04-17): `gh workflow run mediainfo-parity-multi-os.yml --ref levelup` renvoie `404 workflow not found on default branch` tant que le workflow/parity scripts ne sont pas publiés côté remote.
  - mise à jour 2026-04-17 (run remote réel #24578692465, branch `levelup`):
    - workflow publié/pushé, exécution multi-OS déclenchée via `push`.
    - état run: `failure` (Linux+macOS mismatch strict, Windows tool path, aggregate fail).
    - causes observées:
      - Linux: oracle système Ubuntu = `MediaInfoLib v24.01` (drift vs baseline v26.01) => mismatches massifs.
      - Windows: `mediainfo` non trouvé après install choco (PATH runner).
      - macOS: 9 mismatches strict liés aux champs `File_Created_Date*` présents côté natif mais absents oracle.
    - correctifs appliqués:
      - workflow Linux: ajout repo MediaArea (`repo-mediaarea_1.0-26_all.deb`) avant `apt install mediainfo`.
      - workflow Windows: ajout des chemins `Chocolatey/bin`, `C:\\Program Files\\MediaInfo`, `...\\CLI` au `GITHUB_PATH`.
      - moteur natif: émission conditionnelle de `File_Created_Date*` désactivée sur macOS.
    - validation locale post-correctifs:
      - `no_external_guard`: OK
      - parité: strict `39/39`, extended `44/44`, expanded `96/96`
  - mise à jour 2026-04-17 (runs remote successifs):
    - `#24579151773`: Linux `OK`, macOS `OK`, Windows bloqué (`tool_not_found` puis dérive oracle/path), `aggregate-reports` KO.
    - correctifs appliqués et pushés:
      - normalisation EOL corpus Windows: `.gitattributes` (`mediarecode_corpus_real/*.srt text eol=lf`).
      - `scripts/mediainfo_parity_matrix.py`: `shlex.split(..., posix=(os.name != "nt"))` pour Windows.
      - workflow parity:
        - résolution oracle via `MEDIAINFO_ORACLE_BIN` explicite,
        - compat Linux `MINFO_EMIT_FILE_CREATED_DATE=0`,
        - résolution oracle Windows durcie (candidats CLI + validation `--Version` avec timeout).
    - `#24580023155`: échec lane Windows au step `Resolve oracle binary path` (`null-valued expression` sur `.Trim()`).
    - correctif appliqué:
      - workflow parity: résolution oracle Windows réécrite en PowerShell pur (sans `python -c`), candidats `Chocolatey/Program Files/where/Get-Command`, validation `--Version` avant export `MEDIAINFO_ORACLE_BIN`.
    - `#24580283567`: lane Windows bloqué sur `Resolve oracle binary path` (process hang sur candidat non-CLI).
    - correctif appliqué (2e passe):
      - test oracle PowerShell avec timeout dur (5s) + kill process.
      - suppression du candidat GUI (`C:\\Program Files\\MediaInfo\\mediainfo.exe`) pour éviter blocage.
    - `#24580547088`: échec rapide Ubuntu au step `Resolve oracle binary path` (candidat `Get-Command` non résolu).
    - correctif appliqué (3e passe):
      - fallback `Get-Command` élargi (`Path/Source/Definition`).
      - ajout chemins standards Linux/macOS (`/usr/bin`, `/usr/local/bin`, `/opt/homebrew/bin`).
    - correctif appliqué (4e passe):
      - split du step oracle:
        - Windows: résolution PowerShell avec timeout/kill.
        - Linux/macOS: résolution shell native `command -v mediainfo` + validation `--Version`.
    - `#24580664819`: Linux/macOS OK, Windows KO (oracle non résolu malgré install choco).
    - correctif appliqué (5e passe):
      - discovery récursive des binaires sous `C:\\ProgramData\\chocolatey\\lib\\mediainfo\\tools`.
      - log explicite des candidats testés en cas d’échec.
    - `#24580805412`: Windows KO persistant, candidats visibles mais aucun binaire CLI valide (`MediaInfo.exe` GUI détecté).
    - correctif appliqué (6e passe):
      - install Windows basculée vers `choco install mediainfo-cli` (package CLI dédié) au lieu de `mediainfo`.
    - `#24580929622`: Windows KO persistant au step oracle (résolution toujours négative).
    - correctif appliqué (7e passe):
      - résolution Windows élargie aux noms `mediainfo`, `MediaInfo`, `mediainfo-cli` (`Get-Command` + `where.exe`).
      - scan récursif étendu vers `C:\\ProgramData\\chocolatey\\lib\\mediainfo-cli\\tools`.
    - correctif appliqué (8e passe):
      - résolution oracle Windows déplacée dès l’étape install:
        - tentative choco + discovery multi-chemins,
        - fallback téléchargement officiel `MediaInfo_CLI_26.01_Windows_x64.zip` si besoin,
        - export anticipé `MEDIAINFO_ORACLE_BIN` dans `GITHUB_ENV`.
      - step de résolution Windows conserve un short-circuit sur `MEDIAINFO_ORACLE_BIN` valide.
    - `#24581068423`: KO Windows persistant; diagnostic log: chemins Windows hardcodés avec doubles `\\` (littéraux invalides en PowerShell), `MEDIAINFO_ORACLE_BIN` exporté invalide.
    - correctif appliqué (9e passe):
      - normalisation de tous les chemins Windows hardcodés en backslash simple (`C:\...`).
    - `#24581204016`: KO Windows persistant; install résout oracle, mais la re-détection dédiée Windows échoue ensuite.
    - correctif appliqué (10e passe):
      - suppression de la re-détection Windows redondante.
      - step `Resolve oracle binary path (Windows)` réduit à un check simple de `MEDIAINFO_ORACLE_BIN` (fourni par l’install).
    - `#24581332108`: résolution oracle Windows OK, mais parité stricte Windows à `30/39` (9 mismatches).
    - diagnostic:
      - écarts concentrés sur `File_Created_Date*` / `File_Modified_Date*` (millisecondes absentes côté natif) et dérivés `PBCore2` (`instantiationDate` + annotations).
    - correctif appliqué (11e passe):
      - moteur natif: dates fichier avec millisecondes sur Windows (`General` + `SubRip` + création date container via `st_ctime`).
      - `PBCore` (`utc_file_date_iso`) accepte et propage les timestamps `...SSS UTC` -> `...SSS Z`.
    - diagnostic résiduel (`#24581616915`): 3 écarts Windows restants (`real_hevc_eac3_sub`) limités à un décalage `+1ms` (`.159` vs `.160`).
    - correctif appliqué (12e passe):
      - suppression des arrondis ms (`round`) sur timestamps fichier au profit du tronquage `int(...)` aligné oracle.
    - validation remote finale:
      - run `#24581776172` (branche `levelup`) `SUCCESS`:
        - `parity (ubuntu-latest)`: `SUCCESS`
        - `parity (windows-latest)`: `SUCCESS`
        - `parity (macos-latest)`: `SUCCESS`
        - `aggregate-reports`: `SUCCESS` (`mediainfo-parity-reports-all-os` publié)
      - scores validés sur la matrice: strict/extended/expanded verts sur les 3 OS.

### Step 5 — Couverture complète MediaInfo v26.01
Status: `IN_PROGRESS`
- Avancement consolidé:
  - `Option` / `Option_Static` avancés + aliases + help/info.
  - Renderers `Text/JSON/XML` verrouillés sur corpus strict/extended.
  - Renderers spécialisés (`EBUCore`, `PBCore/PBCore2`, `MPEG-7`) branchés et alignés sur les mêmes champs machine.
  - Parser natif MP4/MKV/WebM enrichi (tags, chapitres, UIDs, champs vidéo/audio clés).
- Résultat consolidé:
  - strict: `39/39`
  - extended: `44/44`
  - expanded: `96/96`
- Non terminé:
  - couverture au-delà du corpus sprint pour tendre vers v26.01 complet.

## Step 6+ — Réécriture native pure (post-pivot)

### Step 6.0 — Pivot + Scaffolding modulaire
Status: `DONE`
- Arborescence modulaire en place (`api/options/io/parsers/enrich/renderers/validation`).
- `legacy_ffprobe_engine.py` et `legacy/ffprobe_reference.py` supprimés.
- Runtime rebranché vers `engine/native_engine_core.py` + `api/engine.py`.
- Guard architecture actif: `validation/no_external_guard.py` (0 violation).
- Parité de référence conservée après pivot:
  - strict `39/39`, extended `44/44`, expanded `96/96`.

### Step 6.1 — Suppression dépendance parser externe
Status: `IN_PROGRESS`
- Fait:
  - suppression des hooks hérités (`_probe_bundle`, `_run_ffprobe_json`, `_build_report`) et du paramètre `ffprobe_bin` runtime.
  - `report()` de base verrouillé hors `api/engine.py`.
  - cleanup structurel du monolithe:
    - suppression `ContainerKind`, `ProbeBundle`, `SourceValueEnricher`
    - suppression helpers locaux container/EBML morts
  - logique container déplacée/maintenue dans parseurs natifs dédiés (`parsers/container/*`).
  - API modèle alignée:
    - `from_report()` canonique
    - `from_legacy_report()` conservé en alias compat.
  - dispatch runtime des renderers branché sur `core/mediainfo_native/renderers/*`:
    - `Text/JSON/XML` via modules avec logique extraite,
    - `EBUCore/PBCore/MPEG-7` via bridges modules,
    - comportement conservé (parité inchangée).
  - extraction `Inform` hors monolithe:
    - `renderers/inform.py` implémente `parse_inform_expression()` et `render_inform()`;
    - `query_inform()` runtime rebranché sur ce module;
    - wrappers internes `_split_inform_expression` / `_render_template` conservés en délégation compat.
- Validation:
  - `py_compile`: OK
  - `no_external_guard`: OK
  - parité Linux: strict `39/39`, extended `44/44`, expanded `96/96`
- Mise à jour 2026-04-17 (tranche modulaire continue):
  - validation post-extraction `Inform`:
    - strict `39/39`
    - extended `44/44`
    - expanded `96/96`
- Mise à jour 2026-04-17 (tranche expanded prioritaire):
  - fermeture complète des écarts `expanded` restants sur Linux local:
    - `raw Text` (HDR/HLG + ordre champs),
    - `EBUCore` (tracks attrs + champs couleur/HDR),
    - `PBCore2` (durées/timeStart/annotations/langues/titres),
    - `MPEG-7` (VideoType/AudioVisualType, classification/langues, term IDs AVC, layout strict/relaxed/extended).
  - scores actuels consolidés: strict `39/39`, extended `44/44`, expanded `96/96`.
- Mise à jour 2026-04-17 (tranche renderers structurants):
  - extraction effective hors monolithe:
    - `renderers/text.py`: logique `Text` + `Language=raw`
    - `renderers/json.py`: compactage/orientation oracle
    - `renderers/xml.py`: layout XML oracle
  - `MediaReport.render_text/render_text_raw/render_json/render_xml` délègue désormais aux modules.
  - `MediaInfoEngine.render()` passe par `report.render_*` (fin de l’appel direct module non injecté).
  - validation locale:
    - `py_compile`: OK
    - `no_external_guard`: OK
    - gate parité: OK (`real 39/39`, `extended 44/44`, `expanded 96/96`)
  - tranche dates Windows:
    - formatage dates fichier harmonisé (ms) sans régression Linux locale.
    - oracle utilisé: `/var/home/hydromel/dev/MediaInfo/MediaInfo_CLI_CPP/MediaInfo/Project/GNU/CLI/mediainfo`
- Mise à jour 2026-04-17 (tranche modèle + extraction commune):
  - `api/model.py` enrichi avec vues de rendu (`ReportView`, `ReportTrackView`, `to_report_view`).
  - `Text/JSON/XML` passent désormais par la vue structurée issue de `api/model` avant rendu.
  - extraction de primitives spécialisées vers `renderers/specialized_common.py`:
    - `public_fields()`
    - `duration_iso8601_from_ms()`
  - `native_engine_core.py` délègue ces primitives au module dédié.
  - validation locale:
    - `py_compile`: OK
    - `no_external_guard`: OK
    - gate parité: OK (`real 39/39`, `extended 44/44`, `expanded 96/96`)
- Mise à jour 2026-04-17 (tranche renderer spécialisé EBUCore):
  - extraction du corps `EBUCore` vers `renderers/ebucore.py` (fin du bridge simple).
  - `native_engine_core.py` conserve un wrapper compat `_render_ebucore()` qui délègue au module.
  - `MediaInfoEngine.render()` injecte explicitement les dépendances de rendu EBUCore.
  - validation locale:
    - `py_compile`: OK
    - `no_external_guard`: OK
    - gate parité: OK (`real 39/39`, `extended 44/44`, `expanded 96/96`)
- Mise à jour 2026-04-17 (tranche renderer spécialisé PBCore):
  - extraction du corps `PBCore/PBCore2` vers `renderers/pbcore.py` (fin du bridge simple).
  - `native_engine_core.py` conserve un wrapper compat `_render_pbcore()` qui délègue au module.
  - `MediaInfoEngine.render()` injecte explicitement les dépendances de rendu PBCore.
  - validation locale:
    - `py_compile`: OK
    - `no_external_guard`: OK
    - gate parité: OK (`real 39/39`, `extended 44/44`, `expanded 96/96`)
- Mise à jour 2026-04-17 (tranche renderer spécialisé MPEG-7):
  - extraction du corps `MPEG-7` vers `renderers/mpeg7.py` (fin du bridge simple).
  - `native_engine_core.py` conserve un wrapper compat `_render_mpeg7()` qui délègue au module.
  - `MediaInfoEngine.render()` injecte explicitement les dépendances de rendu MPEG-7.
  - validation locale:
    - `py_compile`: OK
    - `no_external_guard`: OK
    - gate parité: OK (`real 39/39`, `extended 44/44`, `expanded 96/96`)
- Mise à jour 2026-04-17 (tranche source unique modèle):
  - `MediaInfoEngine.render()` construit `report_view` depuis `api/model` (`from_report` -> `to_report_view`).
  - `JSON/XML` et `EBUCore/PBCore/MPEG-7` utilisent ce `report_view` comme source de rendu.
  - validation locale:
    - `py_compile`: OK
    - `no_external_guard`: OK
    - gate parité: OK (`real 39/39`, `extended 44/44`, `expanded 96/96`)
- Mise à jour 2026-04-17 (tranche options + CLI helptext):
  - extraction des constantes et metadata options vers `options/store.py`:
    - `OPTION_DEFAULTS`, `OPTION_ALIASES`, `OUTPUT_ALIASES`
    - `info_parameters_text`, `info_output_formats_text`, `info_options_text`, `option_help_text`
  - `native_engine_core.py` rebranché sur `options/store` (normalisation + infos/options help).
  - extraction des textes `--Help`/`--Help-Output` vers `cli/helptext.py`; `cli/entrypoint.py` délègue au module.
  - `core/mediainfo_native/__init__.py`: suppression dépendance `subprocess` (compat via `CompatCompletedProcess`).
  - `validation/no_external_guard.py`: fin de l’exception transitoire `__init__.py`.
  - validation locale:
    - `py_compile`: OK
    - `no_external_guard`: OK
    - gate parité: OK (`real 39/39`, `extended 44/44`, `expanded 96/96`)
- Mise à jour 2026-04-18 (tranche IO dates partagées):
  - extraction de la logique date fichier vers `io/file_dates.py`:
    - `epoch_ms_from_stat_mtime()`
    - `format_file_dates_from_ms()`
  - `api/engine.py` et `engine/native_engine_core.py` rebranchés sur ce module (suppression duplication locale).
  - validation locale:
    - `py_compile`: OK
    - `no_external_guard`: OK
    - gate parité: OK (`real 39/39`, `extended 44/44`, `expanded 96/96`)
- Mise à jour 2026-04-18 (tranche constantes codec):
  - extraction du mapping labels codec vers `renderers/codec_labels.py`.
  - `native_engine_core.py` délègue désormais `_codec_label()` au module dédié.
  - validation locale:
    - `py_compile`: OK
    - `no_external_guard`: OK
    - gate parité: OK (`real 39/39`, `extended 44/44`, `expanded 96/96`)
- Reste à faire:
  - maintenir la parité verte pendant l’extraction modulaire (tests à chaque sous-jalon).

## Écarts connus
- Scores actifs (runtime natif, Linux local):
  - strict: `39/39`
  - extended: `44/44`
  - expanded: `96/96`
- Écarts restants sur corpus courant Linux: `0`.
- Écarts restants sur matrice CI multi-OS (strict/extended/expanded): `0`.
- Couverture v26.01 complète non atteinte hors corpus sprint.

## Prochaines étapes prévues
1. Rendre le gate parity multi-OS strictement obligatoire côté policy GitHub (required checks/release policy).
2. Continuer Step 6.1: extraire les constantes de schéma/rendering restantes du monolithe `engine/native_engine_core.py`.
3. Rejouer la matrice (`strict/extended/expanded`) à chaque sous-jalon d’extraction modulaire.
4. Poursuivre la couverture v26.01 hors corpus sprint (formats/cas limites supplémentaires).
