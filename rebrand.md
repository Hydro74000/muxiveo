# Rebrand Mediarecode vers Muxiveo

## Summary
- Créer `rebrand` depuis `origin/syncrewrite`, préserver les changements locaux actuels hors rebrand, puis pousser la branche sur le remote GitHub `git@github.com:Hydro74000/Muxiveo.git`.
- Faire une rupture nette : aucun alias public `Mediarecode`, `mediarecode`, `MEDIARECODE`, `mediarecode-cli` n'est conservé dans le code, l'UI, les docs, les artefacts ou la CI.
- Utiliser `Muxiveo` avec majuscule partout où c'est techniquement possible ; utiliser une forme minuscule uniquement quand une plateforme l'impose réellement.
- Créer `rebrand.md` à la racine et le maintenir comme fichier de suivi/source de vérité du plan.

## Interfaces Publiques À Changer
- CLI : remplacer `mediarecode-cli` par `Muxiveo-cli`, `mediarecode_cli.py` par l'entrée Muxiveo correspondante, et le fallback packagé par `Muxiveo --cli`.
- Config/runtime : passer à des chemins `Muxiveo`, fichiers de logs `Muxiveo-verbose-*.log`, logger `Muxiveo.tmdb`, dossier temporaire `Muxiveo_work`.
- Variables d'environnement : remplacer tout préfixe `MEDIARECODE_` par `MUXIVEO_`.
- Artefacts : `Muxiveo-x86_64_allinc-<version>.AppImage`, `Muxiveo-Setup-<version>.exe`, `Muxiveo.app`, `Muxiveo-<version>.dmg`, `Muxiveo-<version>.msix`, `Muxiveo-MSIX-*`.
- Identifiants : repo `Hydro74000/Muxiveo`, site `https://muxiveo.fr/`, bundle macOS `com.hydro74000.muxiveo`, AppStream `fr.aotr.muxiveo`, MSIX par défaut `AOTR.Muxiveo` / `AOTRMuxiveo`.

## Changements Clés
- Centraliser le branding dans les constantes applicatives : nom affiché, slug technique, CLI, repo URL, site, user-agent, schema URL, env prefix, dossier config, noms de logs et noms d'artefacts.
- Mettre à jour l'UI Qt : titre fenêtre, sidebar/logo, tooltips, messages de démarrage/erreur, `locales.json`, panneaux de settings, textes setup/launcher et boîtes Windows.
- Mettre à jour packaging Linux/Windows/macOS : PyInstaller names, AppDir, desktop file, AppRun, AppStream, icônes, NSIS, registre Windows, Controlled Folder Access, MSIX manifest, Info.plist, DMG, Homebrew formula.
- Mettre à jour documentation et website refs : README, docs CLI/profils, exemples JSON, commandes d'installation, Homebrew, liens GitHub vers `Hydro74000/Muxiveo`.
- Mettre à jour `rebrand.md` à chaque ajustement du plan ou décision de rebrand, avant de modifier l'implémentation correspondante.

## CI Et Artefacts
- Tous les workflows doivent se déclencher automatiquement au push sur `rebrand`.
- Les workflows de build publient uniquement des artifacts GitHub Actions sur `rebrand`.
- Les étapes `gh release create/upload`, publication Homebrew tap et publication Store restent désactivées quand `github.ref_name == 'rebrand'`.
- Nettoyer les noms locaux générés : remplacer les patterns `.gitignore` par les noms Muxiveo ou des patterns génériques, puis supprimer/régénérer les anciens outputs ignorés.

## Test Plan
- Vérifier l'absence totale de l'ancien nom dans les fichiers suivis, hors ce fichier de suivi : `git grep -n -i -E 'mediarecode|media recode|media-recode|media_recode|MEDIARECODE' -- ':!rebrand.md'`.
- Vérifier que `rebrand.md` existe, reflète le plan courant, et apparaît dans `git status`.
- Lancer les tests ciblés : package, launcher, startup paths, setup/config, logs, UI startup/progress, CLI headless, Homebrew formula, i18n, Matroska header/remux.
- Lancer `pytest` complet si possible, sinon valider via la matrice GitHub Actions au push de `rebrand`.
- Contrôler les artefacts CI : noms `Muxiveo*`, aucune création de GitHub Release, aucun upload vers release/tap/store depuis `rebrand`.

## Assumptions
- La branche de base est `origin/syncrewrite`.
- Le repo visible est `Hydro74000/Muxiveo` avec M majuscule.
- `Muxiveo` prend la majuscule partout sauf contrainte réelle de plateforme.
- Aucune migration automatique des anciennes configs/utilisateurs n'est prévue.
