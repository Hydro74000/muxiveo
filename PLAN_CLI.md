# Plan CLI Mediarecode `devel-cli`

## Statut

- Branche `devel-cli` créée depuis `devel` et suivie par `origin/devel-cli`.
- Point d'entrée CLI v1 ajouté :
  - `python3 mediarecode_cli.py ...`
  - `./mediarecode-cli ...`
- Sous-commandes v1 présentes : `inspect`, `validate`, `preview`, `remux`, `batch`.
- Exemples JSON présents dans `docs/cli/` :
  - `simple.json`
  - `middle.json`
  - `complexe-toutes-options-template.json`
  - `complexe-toutes-options-batch.json`
- Tests dédiés présents dans `tests/test_cli.py`.
- Reste à durcir :
  - contrat JSON public;
  - validation de schéma plus explicite;
  - packaging distribué complet;
  - tests d'intégration CLI sur vrais médias synthétiques;
  - documentation utilisateur plus exhaustive.

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

## Commandes CLI

### `inspect`

Objectif : inspecter une ou plusieurs sources avec `ffprobe` + `mediainfo`, sans exécution de workflow.

Formes prévues :

```bash
mediarecode-cli inspect source.mkv
mediarecode-cli inspect source.mkv --config-template
mediarecode-cli inspect source.mkv --config-template --output sortie.mkv
```

Sorties :

- JSON d'inspection complet par défaut;
- template JSON remux si `--config-template`.

### `validate`

Objectif : charger arguments/config/template, inspecter les sources, appliquer règles, construire `RemuxConfig`, puis exécuter uniquement la validation.

```bash
mediarecode-cli validate --config job.json
mediarecode-cli validate -i source.mkv -o sortie.mkv --languages fr-FR,en-US
```

Code retour attendu :

- `0` si valide;
- `3` si validation métier échouée;
- `2` si arguments ou JSON invalides.

### `preview`

Objectif : produire la commande ffmpeg prévue sans lancer le remux.

```bash
mediarecode-cli preview --config job.json
```

Sortie :

- commande shell lisible sur stdout;
- erreurs/logs sur stderr.

### `remux`

Objectif : exécuter un remux headless strictement non interactif.

```bash
mediarecode-cli remux -i source.mkv -o sortie.mkv
mediarecode-cli remux --config job.json --force
mediarecode-cli remux -i source.mkv -o sortie.mkv --languages fr-FR,en-US --save template.json
```

Règles :

- sortie existante refusée sauf `--force`;
- NFO généré selon `AppConfig.generate_nfo`, sauf `--no-nfo`;
- TMDB jamais appelé sauf option ou config explicite.

### `batch`

Objectif : appliquer un template JSON à plusieurs jobs.

```bash
mediarecode-cli batch --template template.json --batch batch.json
mediarecode-cli batch --template template.json -i S01E01.mkv -i S01E02.mkv --output-dir out/
```

Règles :

- chaque job est isolé;
- `--continue-on-error` permet de continuer après un échec;
- code retour `7` si au moins un job échoue.

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

## Contrat JSON v1

Le format racine est un objet JSON. Les champs inconnus doivent être ignorés tant qu'ils ne créent pas d'ambiguïté, afin de préserver une compatibilité ascendante.

### Job remux minimal

```json
{
  "version": 1,
  "sources": [
    {"path": "source.mkv"}
  ],
  "output": "sortie.mkv"
}
```

Comportement :

- toutes les pistes détectées sont incluses;
- les chapitres source sont conservés selon le workflow remux existant;
- pas de TMDB;
- pas d'attachments externes;
- `.nfo` selon config globale.

### Sources

```json
{
  "path": "source.mkv",
  "attachments": "none",
  "copy_tags": false
}
```

Champs :

- `path` : chemin source requis;
- `attachments` :
  - `"none"` ou `false` : aucun attachment sélectionné;
  - `"all"` ou `true` : tous les attachments;
  - liste d'indices ou noms : sélection ciblée;
- `copy_tags` : copie les tags globaux de cette source si le workflow l'utilise.

### Rules

```json
{
  "rules": {
    "normalize_languages": true,
    "tracks": {
      "audio": {
        "include": true,
        "languages": ["fr-FR", "en-US"],
        "flags": {"commentary": false},
        "rename_pattern": "{LangName} {codec} {channels} {atmos}"
      }
    }
  }
}
```

Sémantique v1 :

- `tracks.video`, `tracks.audio`, `tracks.subtitle` configurent chaque type;
- `include` définit le défaut d'inclusion du type;
- `languages` filtre les pistes sur langues normalisées BCP-47/RFC5646;
- `flags` filtre sur les flags d'origine;
- `rename_pattern` renomme les pistes retenues ou exclues, utile pour preview/template.

Tokens v1 :

- `{lang}` / `<lang>` : code langue normalisé;
- `{LangName}` / `<LangName>` : nom lisible anglais issu du registre;
- `{codec}` / `<codec>`;
- `{channels}` / `<channels>`;
- `{atmos}` / `<atmos>` : `Atmos`, `DTS:X` ou vide;
- `{tag_default}`, `{tag_forced}`, `{tag_malentendant}`, `{tag_malvoyant}`, `{tag_original}`, `{tag_commentary}`;
- `{flags}`;
- `{title}` / `{source_title}`;
- `{type}`.

### Track edits explicites

```json
{
  "tracks": [
    {
      "source": 0,
      "id": 2,
      "enabled": true,
      "language": "fr-FR",
      "title": "French EAC3 5.1 Atmos",
      "flags": {"default": true},
      "time_shift_ms": 0
    }
  ]
}
```

Ces edits doivent être appliqués après les règles automatiques.

### Chapitres

```json
{
  "chapters": {
    "source_index": 0,
    "include_source": true,
    "import": "chapters.ffmetadata",
    "add": [
      {"timestamp": "00:00:00.000", "chaptername": "Opening"}
    ]
  }
}
```

Formats d'import v1 :

- JSON avec `chapters` ou `entries`;
- ffmetadata;
- OGM simple `CHAPTER01=...` / `CHAPTER01NAME=...`.

### TMDB

```json
{
  "tmdb": {
    "enabled": true,
    "kind": "tv",
    "query": "Nom Serie",
    "season": "1",
    "episode": "2",
    "language": "fr-FR"
  }
}
```

Règles :

- si `id` ou `tmdb_id` est présent, l'ID est utilisé;
- si TMDB est activé sans ID, le premier résultat de recherche est retenu;
- aucun appel réseau si `tmdb` est absent ou faux.

### Batch

```json
{
  "jobs": [
    {
      "sources": [{"path": "S01E01.mkv"}],
      "output": "S01E01.remux.mkv",
      "tmdb": {"episode": "1"}
    }
  ]
}
```

Chaque job est fusionné avec le template passé à `--template`.

## Codes Retour

- `0` : succès.
- `2` : arguments, JSON ou contrat d'entrée invalides.
- `3` : validation métier échouée.
- `4` : outil externe requis introuvable.
- `5` : sortie existante sans `--force`.
- `6` : échec workflow.
- `7` : batch partiellement échoué.

## Roadmap D'implémentation

### Phase 1 - V1 utilisable localement

- [x] Créer branche `devel-cli` et upstream `origin/devel-cli`.
- [x] Ajouter `mediarecode_cli.py` et wrapper `mediarecode-cli`.
- [x] Ajouter sous-commandes `inspect`, `validate`, `preview`, `remux`, `batch`.
- [x] Réutiliser `AppConfig`, `FileInspector`, `RemuxWorkflow`.
- [x] Ajouter rules simples : langues, flags, normalisation, patterns.
- [x] Ajouter import/ajout chapitres.
- [x] Ajouter TMDB opt-in.
- [x] Ajouter exemples JSON simple/middle/complexe.
- [x] Ajouter tests unitaires CLI de base.

### Phase 2 - Durcissement du contrat JSON

- [ ] Ajouter une validation structurée du JSON avant inspection des sources.
- [ ] Produire des erreurs avec chemins de champs lisibles, par exemple `rules.tracks.audio.languages[0]`.
- [ ] Documenter tous les champs supportés dans README ou `docs/cli/README.md`.
- [ ] Ajouter tests de champs invalides et types incorrects.
- [ ] Stabiliser la compatibilité ascendante : `version: 1` obligatoire ou warning clair.

### Phase 3 - Intégration runtime

- [ ] Ajouter tests CLI avec médias synthétiques pour `inspect`, `preview`, `validate`.
- [ ] Ajouter un test remux headless court si les outils externes sont disponibles.
- [ ] Ajouter vérification que `--force` correspond bien à la politique d'écrasement effective.
- [ ] Ajouter un mode `--dry-run` alias ou complément de `preview` si utile pour batch.
- [ ] Ajouter résumé batch JSON Lines plus précis : job index, input, output, status.

### Phase 4 - Packaging

- [ ] Exposer `mediarecode-cli` dans les artefacts distribués Linux/AppImage.
- [ ] Prévoir un binaire/entrypoint Windows, par exemple `mediarecode-cli.exe`.
- [ ] Prévoir un entrypoint macOS accessible depuis le bundle ou le package.
- [ ] Vérifier que le CLI ne lance jamais `QApplication` ni fenêtre GUI.
- [ ] Documenter les commandes installées par plateforme.

### Phase 5 - Rules Engine avancé

- [ ] Ajouter priorités et fallback par type/langue/codec/canaux.
- [ ] Ajouter conditions combinées `all` / `any` / `not`.
- [ ] Ajouter limites par type, par exemple garder seulement la première audio par langue.
- [ ] Ajouter règles de default/forced automatique après filtrage.
- [ ] Ajouter presets nommés réutilisables dans un template.

## Test Plan

- Parser CLI, JSON, templates, batch, `--save`, `--force`.
- Rules : sélection langue/type, flags, normalisation, patterns.
- Chapitres : import + ajouts unitaires + combinaison source.
- TMDB : ID explicite, premier résultat demandé, aucun réseau implicite.
- Preview/validate/remux : succès, erreurs, sortie existante, outil manquant.
- Batch : succès multi-épisodes, échec partiel, résumé final.
- Documentation : les 3 exemples JSON doivent rester syntaxiquement valides.

## Critères D'acceptation V1

- `mediarecode-cli --help` liste les sous-commandes sans lancer l'interface.
- `inspect source.mkv` retourne un JSON exploitable.
- `inspect source.mkv --config-template` produit un template réutilisable.
- `preview --config job.json` affiche une commande ffmpeg sans exécuter.
- `validate --config job.json` retourne un code stable.
- `remux --config job.json` exécute le remux et refuse l'écrasement sans `--force`.
- `batch --template template.json --batch batch.json` traite plusieurs jobs avec résumé final.
- Les exemples JSON fournis restent valides.
- Les modifications locales utilisateur non liées, comme `config.ini`, ne sont jamais embarquées par erreur.
