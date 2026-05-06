# Plan CLI Mediarecode `devel-cli`

## Summary

Créer une branche `devel-cli` basée sur `devel`, suivie par `origin/devel-cli`, puis y développer `mediarecode-cli`, un point d'entrée headless strictement non interactif centré sur le remux.

La v1 réutilise `config.ini` / `AppConfig`, accepte arguments directs, JSON, ou JSON avec overrides CLI, et ajoute une première couche de règles/templates pour automatiser des traitements répétables, notamment sur des épisodes de séries.

## Key Changes

- Commande CLI dédiée : `mediarecode-cli` ou `python3 mediarecode_cli.py`.
- Sous-commandes : `inspect`, `validate`, `preview`, `remux`, `batch`.
- Format canonique : JSON.
- `--save FILE` sauvegarde les options/règles choisies en template réutilisable, sans figer les chemins source/sortie.
- Politique sortie : refus d'écrasement sauf `--force`.
- Logs : texte par défaut, JSON Lines via `--log-format jsonl`.
- TMDB : ID explicite supporté; premier résultat automatique seulement si TMDB est explicitement demandé sans ID.
- Trois exemples JSON sont fournis dans `docs/cli/`.

## Rules And Templates

- V1 rules simple :
  - sélection par type de piste;
  - sélection par langues autorisées par type;
  - filtrage par flags d'origine;
  - normalisation BCP-47/RFC5646;
  - renommage automatique via patterns configurables.
- Tokens de renommage : langue, nom langue, codec, canaux/layout, Atmos/DTS:X, flags, titre source.
- Chapitres :
  - import JSON, ffmetadata ou OGM simple;
  - ajouts unitaires `timestamp + chaptername`;
  - combinaison possible avec les chapitres d'une source choisie.
- Batch :
  - template JSON + inputs/jobs JSON;
  - chaque job fournit sources/sortie ou une règle de sortie via `--output-dir`.

## Test Plan

- Parser CLI, JSON, templates, batch, `--save`, `--force`.
- Rules : sélection langue/type, flags, normalisation, patterns.
- Chapitres : import + ajouts unitaires + combinaison source.
- TMDB : ID explicite, premier résultat demandé, aucun réseau implicite.
- Preview/validate/remux : succès, erreurs, sortie existante, outil manquant.
- Batch : succès multi-épisodes, échec partiel, résumé final.
- Documentation : les 3 exemples JSON doivent rester syntaxiquement valides.

