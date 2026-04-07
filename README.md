# 🎬 Mediarecode

FULL Vibecoded App for Proof of Concept - no human code, only human prompts and eyes.

Custom GUI de [mkvtoolnix](https://github.com/nmaier/mkvtoolnix) qui ajoute des options d'encodage video et audio :
Interface graphique pour préparer des fichiers vidéo, remuxer sans perte, réencoder avec `ffmpeg`, et fusionner des métadonnées Dolby Vision / HDR10+.

Cette documentation correspond à **Mediarecode v1.2**.

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
| Outils système | `ffmpeg`, `ffprobe`, `mkvmerge`, `mkvextract`, `mkvinfo`, `mkvpropedit`, `mediainfo` |
| Outils GitHub | `dovi_tool`, `hdr10plus_tool` |
| Notes plateforme | Debian/Ubuntu via `apt`, Fedora/RHEL via `dnf`, macOS via Homebrew, Windows via `winget` + binaires locaux |

> `setup.py` renseigne `config.ini` avec les chemins détectés.  
> Emplacement de `config.ini` : Linux/macOS `~/.config/mediarecode/config.ini` (XDG), Windows dev `./config.ini`, Windows packagé `config.ini` à côté de l'exécutable.

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
- remplacer automatiquement le **titre du conteneur** par le titre formaté TMDB lors de la validation (film : `Titre (Année)`, série : `Titre - SxxExx - Titre épisode`)
- choisir pour chaque piste audio un mode `copy`, `aac`, `eac3` ou `flac`
- choisir pour la vidéo `copy`, `libx265`, `libx264`, `libsvtav1`, `NVENC`, `AMF`, `VAAPI` ou `QSV`

Modes d'exécution :

| Condition | Mode | Outils utilisés |
|-----------|------|-----------------|
| vidéo en `copy`, audio en `copy`, aucune transformation HDR | **Remuxage pur** | `mkvmerge`, puis `mkvpropedit` pour les tags MKV / writing-app si nécessaire |
| tout autre cas | **Encodage** | `ffmpeg`, puis `mkvpropedit` pour chapitres personnalisés, tags MKV et métadonnées de pistes |

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

Profils Dolby Vision proposés :

| Profil | Effet |
|--------|-------|
| **Profile 8.1** | normalise l'injection en profil 8.1, recommandé pour les remux UHD |
| **Mode 0** | conserve le profil source sans réécriture |

### Paramètres

Le panneau **Paramètres** est un éditeur complet de `config.ini` intégré à l'interface. Il regroupe :

- **Interface** : thème (`dark` / `light`), langue, nombre maximal de lignes de log, panneau affiché au démarrage
- **Chemins** : dossier de travail, dossier de sortie, dossier app data
- **Outils externes** : chemins explicites pour chaque outil (`ffmpeg`, `mkvmerge`, `dovi_tool`, etc.)
- **Encodage** : profil DoVi, compat-id, buffer RAM
- **Métadonnées** : clé API TMDB v3 (`tmdb_api_key`)

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

Les tags de langue saisis (pistes audio, sous-titres) sont normalisés automatiquement vers le format RFC 5646 / ISO 639-2 : une saisie comme `fr`, `french` ou `French` est convertie en `fra`.

## Configuration

### Priorité des réglages

L'application résout ses paramètres dans cet ordre :

1. `config.ini` (Linux/macOS : `~/.config/mediarecode/config.ini` ; Windows dev : racine du projet ; Windows packagé : dossier de l'exécutable)
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
| `tmdb_api_key` | vide | clé API TMDB v3 utilisée par la recherche IMDb/TMDB |
| `ram_buffer_enabled` | `true` | autorise l'usage de `/dev/shm` pour les HEVC intermédiaires si disponible |
| `ram_buffer_threshold_pct` | `15` | pourcentage minimal de RAM libre à conserver pour activer ce buffer |

### Outils configurables

Vous pouvez définir explicitement dans `config.ini` :

- `ffmpeg`, `ffprobe`
- `mkvmerge`, `mkvextract`, `mkvinfo`, `mkvpropedit`
- `mediainfo`
- `dovi_tool`, `hdr10plus_tool`
- `eac3to`

Exemple :

```ini
[paths]
output_dir = /mnt/nas/videos

[tools]
ffmpeg = /opt/ffmpeg/bin/ffmpeg
mkvpropedit = /usr/bin/mkvpropedit
dovi_tool = /usr/local/bin/dovi_tool

[ui]
theme = light
language = eng
startup_panel = container

[metadata]
tmdb_api_key = <VOTRE_CLE_API_TMDB_V3>
```

## Workflows

### Conteneur & Encodage

```mermaid
flowchart TD
    A([Sources MKV / MP4]) --> B[Inspection ffprobe + mkvmerge]
    B --> C[RemuxPanel]

    subgraph REMUX[RemuxPanel]
        C --> C1[Choix et ordre des pistes]
        C --> C2[Langue, titre, flags]
        C --> C3[Titre du conteneur]
        C --> C4[Tags MKV et pièces jointes]
        C --> C5[Chapitres et sortie]
    end

    C1 --> D[EncodePanel]
    C2 -. sync .-> D

    subgraph ENCODE[EncodePanel]
        D --> D1[Codec vidéo]
        D --> D2[Codec audio par piste]
        D --> D3[Qualité, preset, HDR]
    end

    D1 --> E{Tout en copy\naucun HDR ?}
    D2 --> E
    D3 --> E

    E -->|Oui| F[mkvmerge\nordre, langues, flags, titre, chapitres, attachements]
    F --> G[mkvpropedit\ntags MKV + writing-app si nécessaire]
    G --> Z([Sortie MKV])

    E -->|Non| H[ffmpeg\nvideo + audio + sous-titres + attachements]
    H --> I{Copie DoVi / HDR10+ ?}
    I -->|Non| J[mkvpropedit\nchapitres, tags, langue/titre des pistes, writing-app]
    I -->|Oui| K[Extraction HEVC + metadata\nencodage video\ninjection HDR]
    K --> J
    J --> Z
```

### Fusion DoVi / HDR10+

```mermaid
flowchart TD
    A([Film 1 + Film 2]) --> B[Validation]
    B --> C[HEVC sur les deux fichiers]
    C --> D[HDR avance detecte dans Film 2]
    D --> E[Comparaison frame count\ntolerance <= 4]
    E --> F[Extractions paralleles]

    subgraph EXTRACT[Extractions]
        F --> F1[mkvextract Film 1 vers HEVC si MKV]
        F --> F2[dovi_tool extract-rpu]
        F --> F3[hdr10plus_tool extract si HDR10+]
    end

    F1 --> G{DoVi present ?}
    F2 --> G
    F3 --> H{HDR10+ present ?}

    G -->|Oui| G1[dovi_tool inject-rpu]
    G -->|Non| H
    G1 --> H

    H -->|Oui| H1[hdr10plus_tool inject]
    H -->|Non| I
    H1 --> I

    I[Verification RPU si DoVi] --> J[mkvmerge final\nvideo enrichie + audio/subs de Film 1]
    J --> K[Nettoyage]
    K --> L([Sortie MKV])
```

### Détection HDR d'un fichier

```mermaid
flowchart TD
    A([Fichier video]) --> B[ffprobe JSON]
    B --> C{side_data HDR ?}
    C -->|Dolby Vision| D1[Dolby Vision]
    C -->|HDR10+| D2[HDR10+]
    C -->|Aucun| E[Fallback mediainfo]

    E --> F{HDR_Format}
    F -->|Dolby Vision| D1
    F -->|SMPTE ST 2094| D2
    F -->|Autre ou vide| G{Color transfer}

    G -->|smpte2084| H[HDR10]
    G -->|arib-std-b67| I[HLG]
    G -->|autre| J[SDR]
```

## Outils externes

| Outil | Rôle principal |
|-------|----------------|
| `ffprobe` | analyse des flux, chapitres et métadonnées |
| `mediainfo` | frame count et informations HDR fines |
| `ffmpeg` | encodage, copie de flux, reconstruction finale |
| `mkvmerge` | remuxage pur et remuxage final de la fusion HDR |
| `mkvextract` | extraction du flux HEVC depuis un MKV |
| `mkvpropedit` | chapitres, tags MKV, langues/titres de pistes, writing-app |
| `dovi_tool` | extraction, injection et vérification Dolby Vision |
| `hdr10plus_tool` | extraction et injection HDR10+ |
| `nvidia-smi` | fallback de détection NVENC sous Linux |

---

*Mediarecode v1.2*
