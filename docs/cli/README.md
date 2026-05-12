# Mediarecode CLI

`mediarecode-cli` est le point d'entree headless de Mediarecode. Il ne lance pas l'interface graphique et reste strictement non interactif.

## Lancement

```bash
python3 mediarecode_cli.py --help
./mediarecode-cli --help
```

Dans les builds packages, le CLI utilise le même bundle que l'application GUI :

- Linux/AppImage : entrée `mediarecode-cli` à côté de `mediarecode`;
- Windows : entrée `mediarecode-cli.exe`;
- macOS : entrée `mediarecode-cli` dans `Mediarecode.app/Contents/MacOS/`;
- fallback commun : `mediarecode --cli ...`.

## Commandes

| Commande | Role |
|---|---|
| `inspect` | inspecte une source et sort du JSON |
| `inspect --config-template` | genere un template JSON de depart |
| `inspect --rules-preview` | compatibilite historique rules |
| `schema` | affiche le schéma JSON public du contrat CLI |
| `validate` | valide un job/template sans executer ffmpeg |
| `preview` | affiche la commande ffmpeg prevue |
| `remux` | execute un remux headless |
| `batch` | applique un template a plusieurs jobs |
| `validate --profile` | valide un `decision-profile`, ou son application si `-i` est fourni |
| `preview --profile` | applique un profil en dry preview JSON/commande |
| `run/remux --profile` | applique un profil et remuxe |
| `batch --profile` | applique un profil a un dossier |

Exemples :

```bash
mediarecode-cli inspect source.mkv
mediarecode-cli inspect source.mkv --config-template --output sortie.mkv
mediarecode-cli inspect source.mkv --config template.json --rules-preview
mediarecode-cli schema --output mediarecode-cli.schema.json
mediarecode-cli schema --version decision-profile
mediarecode-cli preview --config docs/cli/middle.json
mediarecode-cli preview --config docs/cli/middle.json --json
mediarecode-cli validate --config docs/cli/middle.json --json
mediarecode-cli remux -i source.mkv -o sortie.mkv --languages fr-FR,en-US
mediarecode-cli remux --config docs/cli/middle.json --dry-run
mediarecode-cli batch --template docs/cli/complexe-toutes-options-template.json --batch docs/cli/complexe-toutes-options-batch.json --force
mediarecode-cli batch --template template.json --batch batch.json --dry-run --log-format jsonl
mediarecode-cli batch --template template.json --batch batch.json --summary summary.json
mediarecode-cli batch --template exact-job.json --input-dir "Serie/Saison 01" --output-dir "out/Saison 01" --dry-run
mediarecode-cli batch --template exact-job.json --input-dir "Serie" --recursive --include "*.mkv" --exclude "*sample*" --output-dir "out"
mediarecode-cli preview --profile profil.json -i source.mkv --json
mediarecode-cli run --profile profil.json -i source.mkv -o sortie.mkv
mediarecode-cli batch --profile profil.json --input-dir "Serie" --recursive --output-dir "out" --dry-run
```

## Contrat JSON

Les deux contrats publics principaux sont :

- `kind: "exact-job", version: 1` pour un traitement strict;
- `kind: "decision-profile", version: 1` pour l'automapping low-code.

Le schéma peut être exporté avec `mediarecode-cli schema`.

Job minimal :

```json
{
  "version": 1,
  "kind": "exact-job",
  "sources": [
    {"path": "source.mkv"}
  ],
  "output": "sortie.mkv"
}
```

### `sources`

Chaque source peut etre une chaine ou un objet :

```json
{
  "path": "source.mkv",
  "attachments": "none",
  "copy_tags": false
}
```

`attachments` accepte :

- `"none"` ou `false`;
- `"all"` ou `true`;
- une liste de noms ou indices.

### `decision-profile`

Un profil décisionnel ne contient ni source ni sortie. Il décrit des règles
réutilisables :

```json
{
  "version": 1,
  "kind": "decision-profile",
  "name": "VF + VO",
  "variables": {
    "codec_names": {
      "EAC3": "DDP",
      "AC3": "Dolby Digital"
    }
  },
  "groups": [
    {"id": "audio", "label": "Audio", "enabled": true, "priority": 200}
  ],
  "rules": [
    {
      "id": "rename_fr",
      "label": "Renommer VF",
      "group_id": "audio",
      "priority": 100,
      "write_mode": "priority",
      "scope": "all",
      "match": {
        "all": [
          {"field": "type", "op": "is", "value": "audio"},
          {"field": "language", "op": "is", "value": "fr-FR"}
        ]
      },
      "actions": [
        {"type": "set_enabled", "value": true},
        {"type": "set_title", "pattern": "{lang_name} {codec} {channels} {audio_object}"}
      ]
    }
  ]
}
```

`write_mode` définit ce qui se passe si plusieurs règles écrivent le même champ :

- `"priority"` : la plus forte priorité gagne; une priorité égale reste un conflit;
- `"override"` : cette règle remplace une valeur déjà proposée;
- `"add"` : cette règle complète sans écraser; pour un titre, le fragment est ajouté.

Commandes :

```bash
mediarecode-cli validate --profile profil.json
mediarecode-cli preview --profile profil.json -i source.mkv --json
mediarecode-cli run --profile profil.json -i source.mkv -o sortie.mkv
mediarecode-cli batch --profile profil.json --input-dir "Serie" --recursive --output-dir "out"
```

Keywords de renommage disponibles :

```text
{type} {source_index} {track_index}
{language} {lang} {lang_name}
{title} {source_title}
{codec} {codec_raw} {codec_name} {channels} {channel_layout} {audio_object} {atmos} {dtsx}
{resolution} {hdr} {video_flags_hex}
{flags} {flag_default} {flag_forced}
{flag_hearing_impaired} {flag_visual_impaired}
{flag_original} {flag_commentary}
```

Dans les patterns de titre, `variables.codec_names` s'applique à `{codec}` et
`{codec_name}`. Pour forcer le codec technique brut, utilisez `{codec_raw}`.
Dans les critères décisionnels, le champ `codec` reste technique.

### `rules` legacy

Le bloc `rules` reste documente ici comme compatibilite historique. Pour les
nouveaux usages, preferer `decision-profile`.

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
      },
      "subtitle": {
        "include": true,
        "languages": ["fr-FR"],
        "rename_pattern": "{LangName} {tag_forced} {tag_malentendant}"
      }
    }
  }
}
```

Tokens de pattern :

| Token | Valeur |
|---|---|
| `{lang}` / `<lang>` | code langue normalise |
| `{LangName}` / `<LangName>` | nom lisible de langue |
| `{codec}` / `<codec>` | codec |
| `{channels}` / `<channels>` | canaux/layout detecte |
| `{atmos}` / `<atmos>` | `Atmos`, `DTS:X` ou vide |
| `{tag_default}` | tag default |
| `{tag_forced}` | tag forced |
| `{tag_malentendant}` | tag hearing impaired |
| `{tag_malvoyant}` | tag visual impaired |
| `{tag_original}` | tag original |
| `{tag_commentary}` | tag commentary |
| `{flags}` | libelle compact des flags |
| `{title}` / `{source_title}` | titre source |
| `{type}` | type de piste |

Rules avancees :

```json
{
  "rules": {
    "presets": {
      "series-fr-en": {
        "tracks": {
          "audio": {
            "languages": ["fr-FR"],
            "fallback_languages": ["en-US"]
          }
        }
      }
    },
    "use_presets": ["series-fr-en"],
    "tracks": {
      "audio": {
        "priority": [
          {"languages": ["fr-FR"], "codec": "EAC3", "channels": "5.1"},
          {"languages": ["en-US"]}
        ],
        "conditions": {
          "not": {"flags": {"commentary": true}}
        },
        "limit_per_language": 1,
        "default": "first"
      }
    }
  }
}
```

Champs avances v1 :

- `presets` + `use_presets` fusionnent des blocs de rules nommes avant les rules locales;
- `conditions` accepte `all`, `any`, `not`, `language(s)`, `codec(s)`, `channels`, `flags`, `title_contains`, `atmos`;
- `priority` trie les pistes d'un même type avant construction de l'ordre de sortie;
- `fallback_languages` reactive une langue de secours si aucune piste du type n'est retenue;
- `limit_per_language` garde au plus N pistes activees par langue et par type;
- `default: "first"` marque la premiere piste activee du type comme default;
- `default: "first_per_language"` marque la premiere piste activee de chaque langue.

### `tracks`

Les edits explicites sont appliques apres les rules :

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

### `track_order`

`track_order` permet de fixer l'ordre de sortie apres filtrage. Chaque entree
peut etre un objet ou un tableau court :

```json
{
  "track_order": [
    {"source": 0, "id": 0},
    [0, 2]
  ]
}
```

Les erreurs de forme sont reportees avec le chemin exact, par exemple
`track_order[1][1]`.

### `chapters`

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

Imports acceptes :

- JSON avec `chapters` ou `entries`;
- ffmetadata;
- OGM simple.

### `tmdb`

TMDB est opt-in. Aucun appel reseau n'est fait si `tmdb` est absent ou faux.

```json
{
  "tmdb": {
    "enabled": true,
    "kind": "tv",
    "query": "Series",
    "season": "1",
    "episode": "1",
    "language": "fr-FR"
  }
}
```

Si `id` ou `tmdb_id` est present, cet ID est utilise. Sinon, le premier resultat de recherche est retenu.

### `batch`

Le batch fusionne chaque job avec le template :

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

Le fichier batch est valide avant le premier job. Les entrees de `jobs` ou
`inputs` doivent etre des objets job ou des chemins string.

Le batch peut aussi créer les jobs depuis un dossier :

```bash
mediarecode-cli batch \
  --template exact-job.json \
  --input-dir "Serie" \
  --recursive \
  --output-dir "out" \
  --dry-run
```

Dans ce mode, chaque fichier vidéo/conteneur compatible devient un job. Les
fichiers audio seuls et sous-titres seuls sont ignorés. Le parcours des
sous-dossiers est activé uniquement avec `--recursive`.

Filtres disponibles :

| Option | Effet |
|--------|-------|
| `--input-dir DIR` | ajoute un dossier à scanner ; option répétable |
| `--recursive` | inclut les sous-dossiers |
| `--include GLOB` | limite les fichiers retenus ; option répétable |
| `--exclude GLOB` | ignore les fichiers correspondants ; option répétable |
| `--output-dir DIR` | génère les sorties en `.mkv` sous ce dossier |

Avec `--recursive` et `--output-dir`, l'arborescence relative est conservée :

```text
Serie/Saison 01/E01.mkv  ->  out/Saison 01/E01.mkv
Serie/Saison 02/E01.mkv  ->  out/Saison 02/E01.mkv
```

`--batch FILE` ne se mélange pas avec `-i/--input` ou `--input-dir` : utilisez
soit le batch JSON, soit le mode direct.

Avec `--summary FILE`, le batch ecrit un rapport JSON final :

```json
{
  "total": 1,
  "successes": 1,
  "failures": 0,
  "exit_code": 0,
  "jobs": [
    {
      "job_index": 0,
      "input": "S01E01.mkv",
      "output": "S01E01.remux.mkv",
      "status": "success",
      "exit_code": 0
    }
  ]
}
```

### Sorties JSON de validation et preview

`validate --json` et `preview --json` gardent les mêmes codes retour que les
commandes texte, mais ecrivent un objet JSON sur stdout.

Exemple `preview --json` :

```json
{
  "valid": true,
  "errors": [],
  "output": "sortie.mkv",
  "sources": [{"index": 0, "path": "source.mkv"}],
  "track_order": [{"source": 0, "id": 0}],
  "command": ["ffmpeg", "-hide_banner", "..."],
  "command_text": "ffmpeg \\\n    -hide_banner \\\n    ..."
}
```

## Codes retour

| Code | Signification |
|---:|---|
| 0 | succes |
| 2 | arguments, JSON ou contrat invalide |
| 3 | validation metier echouee |
| 4 | outil externe introuvable |
| 5 | sortie existante sans `--force` |
| 6 | echec workflow |
| 7 | batch partiellement echoue |

## Batch JSON Lines

Avec `--log-format jsonl`, le batch emet des evenements structurés :

```json
{"level":"info","message":"Batch job 1 demarre","event":"batch_job","job_index":0,"input":"S01E01.mkv","output":"S01E01.remux.mkv","status":"started"}
{"level":"info","message":"Batch job 1 termine","event":"batch_job","job_index":0,"input":"S01E01.mkv","output":"S01E01.remux.mkv","status":"success"}
{"level":"info","message":"Batch termine : 1/1 succes.","event":"batch_summary","total":1,"failures":0}
```
