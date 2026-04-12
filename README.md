# 🎬 Mediarecode

FULL Vibecoded App for Proof of Concept - no human code, only human prompts and eyes.

Interface graphique pour préparer des fichiers vidéo, remuxer sans perte, réencoder avec `ffmpeg`, et fusionner des métadonnées Dolby Vision / HDR10+.

Cette documentation correspond à **Mediarecode v1.3**.

## Vue rapide

| Outil | Usage | Qualité |
|-------|-------|---------|
| **Conteneur & Encodage** | sélectionner les pistes, les réordonner, éditer langue/titre/flags, gérer titre/tags/chapitres/pièces jointes, enrichir les tags via TMDB/IMDb, puis copier ou réencoder | copie = sans perte, encodage = avec recompression |
| **Fusion DoVi / HDR10+** | injecter les métadonnées HDR d'un fichier source dans le flux vidéo HEVC d'un autre fichier | sans perte |

> Les panneaux **Remuxage** et **Encodage** forment un seul workflow. Le panneau Remuxage prépare le conteneur ; le panneau Encodage décide comment traiter la vidéo et l'audio.

## Installation

### Prérequis

- **Python 3.10+**

`setup.py` installe ensuite **tous les autres prérequis** pour **Windows**, **Linux Fedora / RHEL**, **Linux Debian / Ubuntu** et **macOS**, y compris **PySide6** et les outils externes nécessaires.

### Cloner le dépôt

```bash
git clone <url-du-depot>
cd mediarecode
```

### Installer les dépendances et les outils

Le script `setup.py` installe automatiquement :

| Catégorie | Installé par `setup.py` |
|-----------|-------------------------|
| Dépendance Python | `PySide6` |
| Outils système | `ffmpeg`, `ffprobe`, `mediainfo` (+ `mkvtoolnix` optionnel, notamment pour le script CLI legacy) |
| Outils GitHub | `dovi_tool`, `hdr10plus_tool` |
| Notes plateforme | Debian/Ubuntu via `apt`, Fedora/RHEL via `dnf`, macOS via Homebrew, Windows via `winget` + binaires locaux |

> `setup.py` renseigne `config.ini` avec les chemins détectés.  
> Emplacement de `config.ini` : Linux/macOS `~/.config/mediarecode/config.ini` (XDG), Windows dev `./config.ini`, Windows packagé `%APPDATA%\mediarecode\config.ini`.

| Plateforme | Commande | Détails |
|------------|----------|---------|
| Linux Debian / Ubuntu | `python3 setup.py` | installe `ffmpeg`, `mkvtoolnix`, `mediainfo` via `apt`, puis `dovi_tool` et `hdr10plus_tool` depuis GitHub |
| Linux Fedora / RHEL | `python3 setup.py` | active RPM Fusion si nécessaire, installe `ffmpeg`, `mkvtoolnix`, `mediainfo` via `dnf`, puis les outils GitHub |
| macOS | `python3 setup.py` | installe `ffmpeg`, `mkvtoolnix`, `mediainfo` via Homebrew, puis `dovi_tool` et `hdr10plus_tool` |
| Windows | `py setup.py` | installe `ffmpeg`, `mkvtoolnix` et `mediainfo` via `winget`, place `dovi_tool` et `hdr10plus_tool` dans `mediarecode\tools`, puis renseigne `config.ini` avec les chemins détectés |

Options utiles du script :

| Option | Effet |
|--------|-------|
| `--dry-run` | affiche les actions sans les exécuter |
| `--no-github` | n'installe pas `dovi_tool` ni `hdr10plus_tool` |
| `--prefix PATH` | change le dossier d'installation des binaires GitHub |
| `--force` | relance les installations et régénère les chemins Windows dans `config.ini` |

`eac3to` est optionnel et non installable automatiquement. Il reste utile sous Windows pour certains traitements audio avancés.

### Lancer l'application

```bash
python3 main.py
```

Sous Windows, utilisez `py main.py`.

## Windows

### Windows Security / Controlled Folder Access

Sous Windows, les bibliothèques utilisateur comme **Videos**, **Documents**, **Pictures** et dossiers similaires peuvent être protégées par **Windows Security** via **Controlled Folder Access**.

Quand cette protection est active, Mediarecode peut être empêché d'écrire directement dans ces dossiers, même si :

- le dossier existe ;
- vous pouvez y accéder manuellement depuis l'Explorateur ;
- le chemin affiché dans l'application est correct.

Symptômes fréquents :

- popup **Sécurité Windows** indiquant que `mediarecode.exe` ou un outil comme `ffmpeg.exe` a été bloqué ;
- erreur `No such file or directory` lors d'un export vers `Videos` ou `Documents` ;
- succès de l'export vers un autre dossier non protégé, comme `Desktop` ou `%TEMP%`.

Au premier setup Windows, Mediarecode peut proposer d'ajouter ses exécutables à l'allowlist de Windows Security afin de pouvoir enregistrer directement dans ces bibliothèques protégées. Cette exception concerne l'application elle-même et, selon les cas, les outils d'écriture qu'elle utilise (`ffmpeg`; `mkvmerge` seulement pour usages legacy).

Sans cette exception, les exports directs vers **Videos**, **Documents**, **Pictures**, etc. peuvent rester bloqués.

Si vous refusez l'exception ou si vous devez la configurer manuellement :

1. Ouvrez **Sécurité Windows**.
2. Allez dans **Protection contre les virus et menaces**.
3. Ouvrez **Protection contre les ransomwares** puis **Gérer la protection contre les ransomwares**.
4. Entrez dans **Autoriser une application via l'accès contrôlé aux dossiers**.
5. Ajoutez `mediarecode.exe`.
6. Si nécessaire, ajoutez aussi `ffmpeg.exe` (et `mkvmerge.exe` uniquement si vous utilisez le script CLI legacy).

Après ajout à l'allowlist, redémarrez Mediarecode avant de retester un export vers `Videos` ou `Documents`.

## Distribution

L'application peut également être distribuée sous forme de binaire autonome via `package.py`.

| Cible | Commande | Artefact produit |
|-------|----------|-----------------|
| AppImage Linux | `python3 package.py` | `dist/Mediarecode-x86_64.AppImage` |
| Binaire Windows (natif) | `py package.py` | `dist/mediarecode/mediarecode.exe` |
| Installateur Windows (natif + NSIS) | `py package.py --nsis` | `dist/Mediarecode-Setup.exe` |
| Installateur Windows cross (depuis Linux) | `python3 package.py --windows` | `dist/Mediarecode-Setup.exe` via Wine + NSIS |

Options utiles de `package.py` :

| Option | Effet |
|--------|-------|
| `--onefile` | binaire monolithique (lent au démarrage, ignoré pour AppImage) |
| `--exe` | force le packaging `.exe` sur Linux (PyInstaller natif, sans AppImage) |
| `--windows` | cross-compile un installateur Windows depuis Linux via Wine + NSIS |
| `--skip-wine` | réutilise `dist/mediarecode-win/` existant (saute l'étape Wine/PyInstaller) |
| `--clean` | nettoie tous les artefacts de build (`build/`, `dist/`, `.wine_build/`, `*.AppImage`…) |

## Interface et usage

### Tableau de bord

Le tableau de bord affiche :

- l'état des outils externes détectés
- les dossiers configurés (travail, sortie, app data)
- les encodeurs logiciels vus par `ffmpeg -encoders`
- les encodeurs matériels réellement testés au runtime (`NVENC`, `AMF`, `VAAPI`, `QSV`)

> Les encodeurs matériels ne sont pas marqués disponibles simplement parce qu'ils apparaissent dans `ffmpeg`. L'application lance un probe réel pour confirmer qu'ils fonctionnent. Les probes sont exécutés en parallèle pour minimiser le délai au démarrage.

### Conteneur & Encodage

Le workflow unifié permet de :

- ajouter une ou plusieurs sources MKV / MP4
- inspecter vidéo, audio, sous-titres, chapitres, pièces jointes et tags MKV
- activer, exclure et réordonner les pistes
- éditer langue, titre et flags de chaque piste
- définir le titre du conteneur, les balises globales, les chapitres et les pièces jointes
- ouvrir une fenêtre de recherche **TMDB** depuis le panneau balises pour rechercher film/série (préremplissage auto depuis titre/nom de fichier)
- détecter automatiquement les motifs de série (`SxxExx`, `x`) pour préremplir saison/épisode et positionner la recherche sur **Séries** si pertinent
- injecter les métadonnées TMDB dans les tags MKV (`DATE_RELEASED`, `GENRE`, `DIRECTOR`, `CAST`, `SUBTITLE`, `SYNOPSIS`, `COUNTRY`, `URL`, `DESCRIPTION`, `COLLECTION`, `SEASON`, `EPISODE`)
- préparer une cover TMDB en mode différé (URL + nom de fichier) ; le téléchargement réel est fait au lancement du workflow
- remplacer automatiquement le **titre du conteneur** par le titre formaté TMDB lors de la validation (film : `Titre (Année)`, série : `Titre - SxxExx - Titre épisode`)
- choisir pour chaque piste audio un mode `copy`, `aac`, `eac3` ou `flac`
- choisir pour la vidéo `copy`, `libx265`, `libx264`, `libsvtav1`, `NVENC`, `AMF`, `VAAPI` ou `QSV`
- backend de remux nominal : `ffmpeg`

Modes d'exécution :

| Condition | Mode | Outils utilisés |
|-----------|------|-----------------|
| vidéo en `copy`, audio en `copy`, aucune transformation HDR | **Remuxage pur** | `ffmpeg` (langues BCP47 via tag `language`, purge `language-ietf`, chapitres, tags globaux, pièces jointes, champ `Muxing Application`) |
| tout autre cas | **Encodage** | `ffmpeg` en passe de sortie unique (encodage/remux final + chapitres, tags, langue/titre de pistes, `Muxing Application`) |

Backend remux `ffmpeg` (par défaut) :

- sortie limitée à `MKV`
- écrit la langue de piste en BCP47 sur `language` (ex. `fr-FR`) et purge le champ legacy `language-ietf` pour éviter les doublons incohérents
- permet la recopie ou l'édition des chapitres
- permet d'écrire les tags globaux choisis
- permet de recopier les pièces jointes source sélectionnées et d'ajouter des fichiers externes (cover incluse)
- télécharge la cover TMDB différée juste avant l'exécution (dans le dossier temporaire du process), puis nettoie ce dossier en fin de run
- purge explicitement les balises techniques source `ENCODER` et `CREATION_TIME` avant écriture des métadonnées de sortie
- force le champ segment **Muxing Application** avec la valeur custom de l'application (tag historique `muxing-application`) directement via FFmpeg

Limites connues du backend remux `ffmpeg` :

- pas de support des structures XML avancées de tags Matroska (cibles hiérarchiques fines)
- pas d'édition du flag Matroska `track-enabled` (non exposé par FFmpeg)
- le champ `ENCODER`/WritingApp reste géré par FFmpeg (seul `Muxing Application` est forcé)

Les options HDR disponibles côté encodage sont :

- injection de métadonnées HDR10 statiques
- tone mapping HDR vers SDR
- copie DoVi / HDR10+ depuis la source avec workflow multi-étapes

### Fusion DoVi / HDR10+

Ce panneau prend :

- **Film 1** : la vidéo cible à enrichir (`.mkv` ou `.hevc`)
- **Film 2** : la source HDR contenant Dolby Vision et/ou HDR10+ (`.mkv` ou `.hevc`)

Règles importantes :

- Film 1 et Film 2 doivent contenir de la **vidéo HEVC**
- Film 2 doit contenir **Dolby Vision** et/ou **HDR10+**
- l'écart de frame count doit être **<= 4 images**
- le remux final conserve l'audio et les sous-titres de Film 1

Le workflow UI Fusion DoVi/HDR10+ est désormais **FFmpeg-only** pour l'extraction HEVC et le remux final (plus de dépendance MKVToolNix dans ce panneau).

Profils Dolby Vision proposés :

| Profil | Effet |
|--------|-------|
| **Profile 8.1** | normalise l'injection en profil 8.1, recommandé pour les remux UHD |
| **Mode 0** | conserve le profil source sans réécriture |

### Paramètres

Le panneau **Paramètres** est un éditeur complet de `config.ini` intégré à l'interface. Il regroupe :

- **Interface** : thème (`dark` / `light`), langue, nombre maximal de lignes de log, panneau affiché au démarrage
- **Chemins** : dossier de travail, dossier de sortie, dossier app data
- **Remux** : backend `ffmpeg` (nominal)
- **Outils externes** : chemins explicites pour chaque outil (`ffmpeg`, `ffprobe`, `mediainfo`, `dovi_tool`, `hdr10plus_tool`, etc.)
- **Encodage** : profil DoVi, compat-id, buffer RAM
- **Métadonnées** : auth TMDB via clé API v3 (`tmdb_api_key`) ou token Bearer v4 (`tmdb_bearer_token`)

Les changements sont appliqués section par section ou en une seule fois via le bouton **Sauvegarder toute la configuration**. Un rechargement depuis `config.ini` est possible sans redémarrer l'application.

## Thèmes

L'application supporte deux thèmes visuels, sélectionnables dans le panneau Paramètres :

| Thème | Description |
|-------|-------------|
| `dark` (défaut) | fond sombre, accents bleus |
| `light` | fond clair, contrastes adaptés |

Le changement de thème est appliqué immédiatement sans redémarrage.

## Localisation

L'interface est traduite en **français** et **anglais**. La langue active est détectée automatiquement depuis la locale système au premier lancement, puis peut être modifiée dans le panneau Paramètres.

Les textes de l'application sont centralisés dans `locales.json`.

Les tags de langue saisis (pistes audio, sous-titres) utilisent des codes RFC 5646 / BCP47 (ex. `fr`, `fr-FR`, `en-US`). Les libellés non standards comme `French` ne sont pas acceptés.

## Configuration

### Priorité des réglages

L'application résout ses paramètres dans cet ordre :

1. `config.ini` (Linux/macOS : `~/.config/mediarecode/config.ini` ; Windows dev : racine du projet ; Windows packagé : `%APPDATA%\mediarecode\config.ini`)
2. les valeurs persistées par l'interface (`QSettings`)
3. les valeurs par défaut internes

Sous Windows, `setup.py` et le démarrage de l'application peuvent auto-détecter les outils et renseigner automatiquement la section `[tools]` de `config.ini`.

### Paramètres principaux

| Paramètre | Défaut | Description |
|-----------|--------|-------------|
| `work_dir` | `/tmp/mediarecode_work` sur Linux/macOS, `%TEMP%\mediarecode_work` sur Windows | dossier des fichiers temporaires |
| `output_dir` | dossier Vidéos de l'OS | dossier de sortie par défaut |
| `theme` | `dark` | thème visuel (`dark` ou `light`) |
| `language` | auto-détecté | langue de l'interface (`fra` ou `eng`) |
| `startup_panel` | `dashboard` | panneau ouvert au démarrage (`dashboard`, `container`, `encoding`, `dovi`, `settings`) |
| `backend` (section `[remux]`) | `ffmpeg` | backend de remux (`ffmpeg`) |
| `tmdb_api_key` | vide | clé API TMDB v3 utilisée par la recherche IMDb/TMDB |
| `tmdb_bearer_token` | vide | token Bearer TMDB v4 (utilisé si `tmdb_api_key` est vide, ou via `MEDIARECODE_TMDB_BEARER_TOKEN`) |
| `ram_buffer_enabled` | `true` | autorise l'usage de `/dev/shm` pour les HEVC intermédiaires si disponible |
| `ram_buffer_threshold_pct` | `15` | pourcentage minimal de RAM libre à conserver pour activer ce buffer |

### Buffer RAM

Le workflow d'encodage peut placer les fichiers HEVC temporaires en RAM pour limiter les E/S disque.

Conditions d'utilisation :

- `ram_buffer_enabled = true`
- un répertoire RAM-backed disponible et inscriptible (`/dev/shm`)
- après allocation estimée, la RAM libre reste au-dessus du seuil `ram_buffer_threshold_pct`

Comportement :

- si toutes les conditions sont remplies, les intermédiaires HEVC sont écrits en RAM
- sinon, fallback automatique vers le dossier temporaire sur disque (`work_dir` ou temporaire système)
- la décision est réévaluée à chaque allocation

Limite Windows :

- l'application ne s'appuie pas sur un backend RAM standard sur Windows
- le chemin par défaut reste donc disque pour garantir un comportement stable et compatible avec les outils externes (`ffmpeg`, `dovi_tool`, `hdr10plus_tool`) qui attendent des chemins de fichiers classiques

### Outils configurables

Vous pouvez définir explicitement dans `config.ini` :

- `ffmpeg`, `ffprobe`
- `mkvmerge`, `mkvextract`, `mkvinfo` (optionnels, principalement pour le script CLI legacy)
- `mediainfo`
- `dovi_tool`, `hdr10plus_tool`
- `eac3to`

Exemple :

```ini
[paths]
output_dir = /mnt/nas/videos

[tools]
ffmpeg = /opt/ffmpeg/bin/ffmpeg
dovi_tool = /usr/local/bin/dovi_tool

[remux]
backend = ffmpeg

[ui]
theme = light
language = eng
startup_panel = container

[metadata]
tmdb_api_key = <VOTRE_CLE_API_TMDB_V3>
tmdb_bearer_token = <VOTRE_TOKEN_BEARER_TMDB_V4>
```

## Workflows

### Conteneur & Encodage — Routage global

```mermaid
flowchart TD
    A["Sources MKV ou MP4"] --> B["Inspection via ffprobe"]
    B --> C["Edition conteneur dans RemuxPanel"]
    C --> D["Options video, audio et HDR dans EncodePanel"]
    D --> E{"Video copy\nAudio copy\nAucune transformation HDR"}

    E -->|Oui| R0["WORKFLOW TYPE - REMUX"]
    R0 --> R1["Workflow remux FFmpeg"]
    R1 --> Z["Sortie MKV"]

    E -->|Non| E0["WORKFLOW TYPE - ENCODE"]
    E0 --> E1["Workflow encode FFmpeg"]
    E1 --> Z
```

### Backend remux `ffmpeg` — Branches internes

```mermaid
flowchart TD
    A["WORKFLOW TYPE - REMUX"] --> B["STEP 1 - Validation configuration"]
    B --> C["STEP 2 - Preparation workspace et attachments"]
    C --> D["STEP 3 - Preparation des attachments"]
    D --> E["STEP 4 - Analyse du mapping et pre-scan ffprobe"]
    E --> F{"Risque multi-source detecte"}
    F -->|Oui| G["STEP 5 - Sync timeline multi-source\nFIFO, Named Pipe ou fallback"]
    F -->|Non| H["STEP 5 - Sync timeline non requise"]
    G --> I["STEP 6 - Gestion des chapitres"]
    H --> I
    I --> J["STEP 7 - Construction commande ffmpeg remux"]
    J --> K["STEP 8 - Execution du remux ffmpeg"]
    K --> L["Sortie MKV"]
```

### Encode workflow — Branches internes

```mermaid
flowchart TD
    A["WORKFLOW TYPE - ENCODE"] --> B["STEP 1 - Validation configuration"]
    B --> C["STEP 2 - Preparation workspace et attachments"]
    C --> D["STEP 3 - Normalisation des options HDR dynamiques"]
    D --> E["STEP 4 - Routage du workflow"]
    E --> F{"Injection fichier\nDoVi ou HDR10+\nnecessaire"}

    F -->|Oui| K["STEP 5 - Extraction des metadata dynamiques"]
    K --> L["STEP 6 - Encodage video seule vers enc.hevc"]
    L --> M["STEP 7 - Injection HDR10+ et ou DoVi"]
    M --> N["STEP 8 - Encapsulation timeline video"]
    N --> O["STEP 9 - Reconstruction finale MKV\nSync timeline si risque detecte"]

    F -->|Non| G{"Codec video = copy"}
    G -->|Oui| H["STEP 5 - Construction commande directe\nvideo passthrough"]
    H --> I["STEP 6 - Execution ffmpeg en single pass\nvideo copy, audio copy ou encode,\nsync timeline si risque detecte"]

    G -->|Non| J["STEP 5 - Construction de la commande directe"]
    J --> P{"Quality mode = SIZE"}
    P -->|Oui| Q["STEP 6 - Execution ffmpeg en 2 passes\nsync timeline si risque detecte"]
    P -->|Non| R["STEP 6 - Execution ffmpeg en single pass\nsync timeline si risque detecte"]

    I --> Z["Sortie MKV"]
    Q --> Z
    R --> Z
    O --> Z
```

Lecture rapide :
- Les demandes `copy_hdr10plus` et `copy_dv` sont evaluees apres normalisation source.
- L'injection "fichier" n'est requise que si une copie DoVi ou HDR10+ reste demandee et que la video n'est pas en `copy`.
- Si `codec=copy`, le workflow reste en sortie directe ffmpeg, y compris si l'audio est reencode.
- Dans ce cas, une demande DoVi ou HDR10+ encore active est simplement preservee par passthrough video, sans pipeline d'injection.
- Le 2-pass existe seulement pour `codec!=copy` avec `quality_mode=SIZE`.
- Le chemin injection utilise `enc.hevc`, puis `enc_wrapped.mkv`, puis un remux final ffmpeg.
- La sync timeline multi-source est activee uniquement en cas de risque detecte par pre-scan ffprobe des sous-titres ; sinon, le flux reste en chemin direct.
- En mode TMDB, la cover est resolue en URL lors de la recherche puis telechargee uniquement au lancement du workflow.

### Fusion DoVi / HDR10+

```mermaid
flowchart TD
    A([Film 1 + Film 2]) --> B[Validation\nfichiers, extensions, outils,\nHEVC Film 1/2, HDR dans Film 2]
    B --> C[Comparaison frame count\ntolerance <= 4]
    C --> D{Film 2 = MKV\net DoVi + HDR10+ ?}

    D -->|Oui| E1[Phase 1 parallel\nextract HEVC Film 1 si MKV\n+ extract HEVC Film 2]
    E1 --> E2[Phase 2 parallel\nextract RPU + HDR10+\ndepuis film2.hevc]
    D -->|Non| E3[Extraction parallel directe\nHEVC Film 1 si MKV\n+ RPU/HDR10+ depuis Film 2]

    E2 --> F
    E3 --> F

    F{DoVi present ?}
    F -->|Oui| G[dovi_tool inject-rpu]
    F -->|Non| H
    G --> H

    H{HDR10+ present ?}
    H -->|Oui| I[hdr10plus_tool inject]
    H -->|Non| J
    I --> J

    J{DoVi present ?}
    J -->|Oui| K[Verification RPU frames]
    J -->|Non| L
    K --> L

    L[ffmpeg final\nvideo injectee + audio/subs/metadata Film 1\nmap_metadata/map_chapters] --> M[Nettoyage]
    M --> N([Sortie MKV])
```

### Détection HDR d'un fichier

```mermaid
flowchart TD
    A([Fichier video]) --> B[ffprobe JSON\nstreams + format + chapitres]
    B --> C{Conteneur Matroska/WebM ?}
    C -->|Oui| C1[Enrichissement ffprobe MKV\nlanguage-ietf/language + tag_count]
    C -->|Non| C2[Pas d enrichissement MKV]
    C1 --> D[Normalisation langues IETF]
    C2 --> D

    D --> E{side_data HDR ?}
    E -->|Dolby Vision| D1[Dolby Vision]
    E -->|HDR10+| D2[HDR10+]
    E -->|Aucun| F[Fallback mediainfo]

    F --> G{HDR_Format}
    G -->|Dolby Vision| D1
    G -->|SMPTE ST 2094| D2
    G -->|Autre ou vide| H{Color transfer}

    H -->|smpte2084| I[HDR10]
    H -->|arib-std-b67| J[HLG]
    H -->|autre| K[SDR]
```

## Outils externes

| Outil | Rôle principal |
|-------|----------------|
| `ffprobe` | analyse des flux, chapitres et métadonnées |
| `mediainfo` | frame count et informations HDR fines |
| `ffmpeg` | encodage, remux par défaut, copie de flux, écriture metadata/chapitres/tags en sortie directe |
| `mkvmerge` | outil optionnel pour usages legacy (script CLI historique) |
| `mkvextract` | outil optionnel pour usages legacy (script CLI historique) |
| `mkvpropedit` | legacy/optionnel, non utilisé dans les workflows principaux actuels |
| `dovi_tool` | extraction, injection et vérification Dolby Vision |
| `hdr10plus_tool` | extraction et injection HDR10+ |
| `nvidia-smi` | fallback de détection NVENC sous Linux |

---

*Mediarecode v1.2*
