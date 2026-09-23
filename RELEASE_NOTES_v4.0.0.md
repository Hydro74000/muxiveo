# Notes de version / Release Notes — Muxiveo v4.0.0

* [Français](#version-française)
* [English](#english-version)

---

<a name="version-française"></a>
# Version Française

*Journal complet des modifications depuis la dernière version stable de `main` (v3.1.2).*

---

## 1. Fonctionnalités

### Nouveautés / Suppressions
* **Studio d'Hybridation** : Nouvelle interface dédiée accessible en premier rang depuis la barre latérale (`ui/panels/hybrid_studio.py`).
* **Matrice multi-sources** : Appariement automatique des pistes par langue, métadonnées et détection de l'épisode de référence (`core/workflows/hybrid_matrix.py`).
* **Synchro Studio & Formes d'onde** : Dialogue interactif (`SyncStudioDialog`) et composant de rendu audio FFT (`WaveformView`) pour le calage temporel précis.
* **Modes d'affichage scindé et superposé** : Visualisation audio en double piste scindée ou superposition directe pour comparaison visuelle.
* **Synchronisation par sous-titres** : Moteur de corrélation heuristique et recalage temporel basé sur les répliques textuelles (`core/workflows/subtitle_sync.py`).
* **Détection et conversion de cadence dynamique** : Détection automatique des écarts PAL ↔ Cinéma et génération des filtres audio `atempo` et `asetrate` (`core/workflows/cadence.py`).
* **Discrimination acoustique automatique du pitch** : Analyse de hauteur spectrale (F0) pour choisir automatiquement entre conservation de tonalité (`atempo`) et pitch naturel (`asetrate`) (`core/workflows/cadence_pitch.py`).
* **Muxer Matroska natif** : Intégration complète d'un moteur d'assemblage et d'écriture MKV/EBML sans dépendance obligatoire à un outil externe (`core/workflows/ebml_writer.py`, `core/matroska/`).
* **Gestion BlockGroup et SimpleBlock** : Découpage, parsing et réécriture des blocs vidéo/audio avec prise en charge du lacing.
* **Indexation Cues native** : Génération directe des index de positionnement (Cues) pointant vers les images clés vidéo et les blocs de tête.
* **Extensions CLI headless** : Nouvelles commandes `hybrid`, `sync-scan`, `shift-subs`, et options `--auto-sync`, `--sync-method`, `--sync-reference-track` (`cli/hybrid.py`, `cli/main.py`).
* **Contrats de jobs exacts** : Export et réimport de jobs exacts (`exact-job.json`) et de calibrations multi-coupures (`calibration-multicut.json`).
* **Suppression de mkvmerge au runtime** : Remplacement du binaire externe par le moteur natif pour le parcours principal de remuxage MKV.

### Améliorations / Fix
* **Élargissement des fenêtres d'analyse acoustique** : Fenêtres étendues (60s / 30s) pour absorber les coupures publicitaires TV prolongées.
* **Repérage visuel et journalisation des coupures** : Tracé graphique des discontinuités temporelles et logs détaillés par segment.
* **Propagation globale de la synchronisation** : Application automatique du calage temporel calculé à l'ensemble des pistes associées à une source.
* **Zoom millimétrique et navigation inter-segments** : Zoom dynamique fluide et saut direct de segment en segment dans le visualiseur d'onde.
* **Pré-écoute A/B comparative** : Écoute instantanée des flux audio synchronisés directement dans l'interface de réglage.
* **Préservation stricte de la vidéo de référence** : Verrouillage de la piste vidéo de référence dans le `track_order` du plan d'assemblage hybride.
* **Calcul exact du ratio à la volée** : Dérivation dynamique du rapport de framerate à partir des fractions exactes du flux (`r_frame_rate`, ex: `24000/25025`).
* **Calage temporel donneur dans AudioSyncScanner** : Correction de la fenêtre temporelle de découpe de la piste donneuse.
* **Isolation des balises globales et des chapitres** : Utilisation de `-map_metadata:g -1:g` pour éviter la suppression accidentelle des chapitres lors du nettoyage des tags globaux.
* **Synchronisation dynamique des tags dans l'UI** : Maintien des éditions manuelles/TMDB (`_tag_edits`) lors du basculement des sources dans le panneau Remux.
* **Correction du signal de fin de synchronisation** : Fiabilisation du signal `_audio_sync_done` et sécurisation du cycle de vie des fenêtres modales.
* **Sélection fiable de la piste de référence** : Extraction robuste de `reference_entry` lors de sélections multiples dans Synchro Studio.
* **Mise à jour MediaManager** : Amélioration de l'affichage des détails médias, des playlists Blu-ray et de la sélection audio.

---

## 2. Performances

### Nouveautés / Suppressions
* **Parsing EBML par projection mémoire (mmap)** : Lecture des clusters Matroska via `mmap` avec conseil séquentiel (`posix_madvise`), accélérant le parcours des gros fichiers jusqu'à 5x.
* **Dérivation instantanée des statistiques de pistes** : Calcul immédiat des statistiques de pistes en sortie depuis les sources pour les pistes en copie directe (`passthrough`), sans réanalyse du fichier produit.
* **Option de régénération des statistiques** : Ajout d'une option configurable et bascule d'interface (`matroska_regenerate_statistics`) pour désactiver la régénération si non requise.

### Améliorations / Fix
* **Suppression des gels d'interface (stalls)** : Élimination des temps d'attente bloquants lors de l'édition et du réordonnancement des pistes dans le panneau Remux.
* **Optimisation du traitement par blocs** : Réduction de la charge CPU et mémoire lors de l'extraction des résumés de blocs Matroska.
* **Réduction de la contention GIL** : Ajustement de `scan_workers` par défaut à 1 pour optimiser le débit sur les lectures disques séquentielles.
* **Barrière de parallélisme sous macOS** : Synchronisation fiabilisée par barrière de threads pour les encodages vidéo simultanés.

---

## 3. Sécurité

### Nouveautés / Suppressions
* **Durcissement de l'installateur premier lancement** : Contrôle d'intégrité et validation stricte des chemins d'exécution lors du paramétrage initial de la plateforme.
* **Réparation automatique des outils Windows** : Détection et restauration automatique des dépendances corrompues ou manquantes dans l'environnement local.

### Améliorations / Fix
* **Isolation stricte des métadonnées de conteneur** : Prévention des fuites de données globales source lorsque l'utilisateur désactive l'export de métadonnées.
* **Encodage UTF-8 systématique sous Windows** : Forçage de l'encodage console et flux subprocess en UTF-8 (`PYTHONIOENCODING=utf-8`, `errors=replace`) pour éviter les plantages sur caractères non-ASCII.
* **Exclusion de libsystemd de l'AppImage** : Suppression de la bibliothèque embarquée pour éviter les conflits ABI et crashs sur distributions Linux hôtes.
* **Écriture atomique des sorties MKV** : Sécurisation de la finalisation des fichiers pour éviter toute corruption de conteneur en cas d'arrêt imprévu.

---

## 4. Autres (Interface, Packaging, CI, Documentation, i18n)

### Nouveautés / Suppressions
* **Design system et tokens visuels** : Uniformisation des styles graphiques, des boutons de workflow et des thèmes clair et sombre.
* **Support d'échelle DPI 125%** : Ajustement dynamique du padding, espacements et dimensions des boutons pour les résolutions intermédiaires.
* **Guide d'hybridation grand public** : Rédaction d'une documentation complète vulgarisée dans `docs/hybridization-guide.md`.
* **Documentation technique du muxer natif** : Rédaction des spécifications et garanties du muxer dans `docs/remux-native.md`.
* **Jeux d'exemples CLI** : Ajout d'exemples types documentés sous `docs/examples/`.
* **Workflow CI dédié à l'hybridation** : Ajout de `.github/workflows/ci-hybrid.yml` couvrant Linux, Windows et macOS.

### Améliorations / Fix
* **Couverture linguistique (i18n)** : Traduction complète français/anglais du catalogue pour toutes les nouvelles fonctionnalités dans `locales.json`.
* **Lisibilité des cases à cocher** : Renforcement du contraste visuel des formulaires et des checkboxes sur tous les thèmes.
* **Inclusion de la dépendance numpy** : Intégration systématique de `numpy>=1.24` dans `requirements.txt`, `setup.py`, `package.py`, `package_appimage.py` et `.github/workflows/release.yml`.
* **Découplage des tests unitaires** : Isolation des tests pour exécution propre en environnement sans outils multimédia préinstallés.
* **Nettoyage du suivi Git** : Mise à jour du `.gitignore` pour autoriser formellement les fichiers de documentation sous `docs/`.

---
---

<a name="english-version"></a>
# English Version

*Comprehensive changelog of changes since the last stable release on `main` (v3.1.2).*

---

## 1. Features

### New Features / Removals
* **Hybridization Studio**: New dedicated interface directly accessible from the primary sidebar navigation (`ui/panels/hybrid_studio.py`).
* **Multi-Source Matrix**: Dynamic track matching by language, metadata, and reference episode detection (`core/workflows/hybrid_matrix.py`).
* **Synchro Studio & Waveforms**: Interactive dialog (`SyncStudioDialog`) and FFT audio rendering component (`WaveformView`) for high-precision time alignment.
* **Split & Overlay Display Modes**: Dual-track split view or direct overlaid rendering for visual comparison.
* **Subtitle-Based Synchronization**: Heuristic correlation and timeline adjustment engine based on text dialogue cues (`core/workflows/subtitle_sync.py`).
* **Dynamic PAL ↔ Cinema Cadence Detection & Conversion**: Automatic framerate discrepancy detection and `atempo` / `asetrate` audio filter generation (`core/workflows/cadence.py`).
* **Automatic Acoustic Pitch Discrimination**: Spectral pitch analysis (F0) to automatically select between tone preservation (`atempo`) and natural pitch shift (`asetrate`) (`core/workflows/cadence_pitch.py`).
* **Native Matroska Muxer**: Full integration of an internal MKV/EBML assembly and writing engine without mandatory external tooling (`core/workflows/ebml_writer.py`, `core/matroska/`).
* **BlockGroup & SimpleBlock Handling**: Extraction, parsing, and writing of video/audio blocks with full lacing support.
* **Native Cues Indexing**: Direct generation of seek index entries (Cues) referencing video keyframes and leading cluster blocks.
* **Headless CLI Extensions**: New `hybrid`, `sync-scan`, `shift-subs` commands, alongside `--auto-sync`, `--sync-method`, and `--sync-reference-track` flags (`cli/hybrid.py`, `cli/main.py`).
* **Exact Job Contracts**: Serialization and restore support for exact job definitions (`exact-job.json`) and multi-cut calibration presets (`calibration-multicut.json`).
* **Removal of mkvmerge at Runtime**: Replaced external dependency with the native muxing pipeline for the primary MKV remux workflow.

### Improvements / Fixes
* **Expanded Acoustic Scan Windows**: Enlarged analysis windows (60s / 30s) to absorb long commercial TV breaks.
* **Visual Cut Detection & Logging**: Visual markers for timeline cuts and detailed per-segment logging.
* **Global Sync Propagation**: Automatic propagation of calculated time offsets to all tracks belonging to the same source.
* **Sub-millimeter Zoom & Inter-Segment Navigation**: Smooth dynamic zoom and direct jumping between cut segments in the waveform view.
* **Instant A/B Audio Preview**: Real-time listening and comparison of synchronized audio tracks directly within the calibration UI.
* **Strict Reference Video Preservation**: Ensured reference video preservation within the assembly plan `track_order`.
* **Exact On-the-Fly Ratio Computation**: Dynamic calculation of exact framerate ratios directly from raw stream fractions (`r_frame_rate`, e.g. `24000/25025`).
* **Donor Timing Alignment in AudioSyncScanner**: Fixed slice boundary alignment for donor audio streams.
* **Global Tags & Chapter Isolation**: Switched to `-map_metadata:g -1:g` to prevent chapter and stream metadata deletion when stripping container tags.
* **Dynamic UI Tag Synchronization**: Preserved manual/TMDB tag edits (`_tag_edits`) when toggling sources in the Remux panel.
* **Sync Completion Signal Fix**: Hardened the `_audio_sync_done` signal emission and secured modal dialog lifecycle.
* **Robust Reference Track Selection**: Reliable extraction of `reference_entry` during multi-source candidate selection in Synchro Studio.
* **MediaManager Upgrades**: Refined media details inspector, Blu-ray playlist parsing, and audio stream pre-selection.

---

## 2. Performance

### New Features / Removals
* **Memory-Mapped EBML Parsing (mmap)**: Matroska cluster parsing using `mmap` with sequential advisory (`posix_madvise`), yielding up to 5x speedup on large files.
* **Instant Passthrough Statistics Derivation**: Real-time derivation of output track statistics directly from source streams without re-reading the generated output file.
* **Configurable Statistics Regeneration**: Added configuration toggle and UI switch (`matroska_regenerate_statistics`) to bypass statistics generation when unneeded.

### Improvements / Fixes
* **Elimination of UI Freezes (Stalls)**: Removed blocking delays during track editing and reordering in the Remux panel.
* **Optimized Block Processing**: Lowered CPU and memory overhead during Matroska block summary extraction.
* **Reduced GIL Contention**: Set `scan_workers` default to 1 to maximize sequential disk throughput without GIL locking.
* **macOS Deterministic Parallelism Barrier**: Hardened thread barrier synchronization for concurrent multi-video encode pipelines.

---

## 3. Security

### New Features / Removals
* **Hardened First-Run Setup**: Path sanitization and environment validation during initial application bootstrap.
* **Automatic Windows Tool Recovery**: Proactive integrity check and automated repair for missing or damaged external binaries.

### Improvements / Fixes
* **Strict Container Metadata Isolation**: Eliminated source global metadata leakage when metadata exporting is disabled.
* **Systematic UTF-8 Console Encoding on Windows**: Enforced UTF-8 stream handling (`PYTHONIOENCODING=utf-8`, `errors=replace`) to avoid console crashes on non-ASCII characters.
* **libsystemd Stripped from AppImage**: Excluded bundled systemd library to prevent symbol conflicts and ABI crashes on host Linux distributions.
* **Atomic MKV Output Writing**: Protected output finalization with controlled staging and atomic replacement to prevent file corruption on abrupt termination.

---

## 4. Other (UI/UX, Packaging, CI, Documentation, i18n)

### New Features / Removals
* **Unified Design System & Tokens**: Streamlined visual hierarchy, workflow buttons, and unified dark/light themes.
* **125% High-DPI Scaling Support**: Adaptive button padding, spacing, and sizing optimized for fractional display scales.
* **End-User Hybridization Guide**: Added jargon-free guide in `docs/hybridization-guide.md`.
* **Native Muxer Technical Documentation**: Added architecture and migration reference in `docs/remux-native.md`.
* **CLI Example Workflows**: Provided reference configuration files under `docs/examples/`.
* **Dedicated CI Workflow for Hybridization**: Added `.github/workflows/ci-hybrid.yml` across Linux, Windows, and macOS.

### Improvements / Fixes
* **Comprehensive Bilingual i18n**: Full French/English localization for all newly introduced modules and settings in `locales.json`.
* **Enhanced Checkbox Contrast**: Improved checkbox visibility and form alignment across dark and light palettes.
* **Standardized numpy Dependency**: Enforced `numpy>=1.24` packaging across `requirements.txt`, `setup.py`, `package.py`, `package_appimage.py`, and `.github/workflows/release.yml`.
* **Decoupled Unit Test Fixtures**: Isolated mock environments allowing headless test suite runs without host media dependencies.
* **Repository Hygiene**: Updated `.gitignore` rules to officially track documentation under `docs/` and release notes.
