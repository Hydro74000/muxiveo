# Mediarecode v1.2

Date: 2026-04-08

Depuis `v1.1` (commit `aee1c107586806719bd5775f563d30037e3e5760`), cette version regroupe **22 commits** (dont 1 merge), avec **49 fichiers modifiés** (`+13 191 / -706`).

## Points forts

- Interface localisée (FR/EN) avec gestion centralisée des traductions (`locales.json`) et détection initiale de langue.
- Nouveau panneau **Paramètres** pour éditer `config.ini` dans l’application (UI, chemins, outils externes, encodage, TMDB).
- Ajout des thèmes **dark/light** appliqués dynamiquement sans redémarrage.
- Workflow enrichi avec intégration **TMDB/IMDb** (recherche, tags, titre conteneur, synopsis, cover auto).
- Gros renforcement du packaging Windows/Linux (PyInstaller, AppImage, NSIS, cross-build Wine).

## Nouvelles fonctionnalités

- **Localisation complète de l’UI**
- **Panneau de démarrage configurable** (dashboard/remux/encode/dovi/settings).
- **Recherche TMDB/IMDb intégrée** dans le remux:
  - pré-remplissage de requête depuis le nom de fichier,
  - détection saison/épisode,
  - injection de métadonnées dans les tags MKV,
  - remplacement automatique du titre conteneur selon le résultat TMDB.
- **Gestion automatique des pochettes** via TMDB (`auto-cover.jpg`) dans le workflow remux.
- **Sélecteur de bitrate audio amélioré** (valeurs par défaut + disponibles selon codec).
- **Threading FFmpeg** configurable avec nouvelle valeur par défaut basée sur les CPU logiques.
- **Métadonnées de version centralisées** (`core/version.py`) pour harmoniser version label/user-agent/writing-app.

## Améliorations techniques

- Détection matérielle encodeurs accélérée (probes runtime optimisées).
- Normalisation des langues renforcée (RFC 5646 / ISO 639, gestion des codes 2 caractères et variantes).
- Normalisation des requêtes TMDB améliorée pour mieux extraire titre/année/saison/épisode.
- `setup.py` renforcé:
  - meilleur support multi-plateforme,
  - auto-remplissage des chemins outils,
  - options `--dry-run` / `--force`,
  - traitement charset/IO Windows,
  - gestion des chemins `config.ini` cross-platform.
- `package.py` / `package_appimage.py` refondus:
  - packaging `.exe` natif,
  - installateur NSIS,
  - cross-build Windows depuis Linux via Wine,
  - récupération des DLL ICU manquantes en environnement Wine.

## Correctifs principaux

- Correction des labels/boutons localisés.
- Correction de la création parasite d’une piste MJPEG lors de l’attachement `cover.jpg` en encode.
- Correction de la conversion audio TrueHD (downmix/cas de conversion ciblés).
- Correction de la normalisation TMDB et des cas synopsis d’épisode absent selon locale (fallback plus robuste).
- Correction du tag de version pour la release 1.2.
- Correction des doubles backslashes lors d’éditions successives de `config.ini` sous Windows.
- Correctifs packaging Windows (sécurité, encodage des sorties, robustesse install/build).

## Tests et fiabilité

Extension importante de la couverture avec nouveaux tests ciblant notamment:

- i18n,
- settings/setup/config,
- launcher,
- package / packaging cross-platform,
- media_info_fetcher (TMDB),
- encode workflow (audio bitrate, progression, matériel),
- remux.

## Notes de migration

- Le chemin de `config.ini` est désormais strictement résolu selon plateforme/contexte (XDG Linux/macOS, `%APPDATA%` en Windows packagé).
- Pour Windows, une configuration de sécurité peut être requise (Controlled Folder Access) pour autoriser certains exports.

## Remerciements

Merci à toutes les personnes ayant contribué aux retours, tests et validations de cette itération `v1.2`.
