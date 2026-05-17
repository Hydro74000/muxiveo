# Profils Muxiveo

## Vue rapide

Muxiveo distingue deux usages :

| Besoin | Format | Usage |
|---|---|---|
| Refaire exactement un traitement | `exact-job` | CLI, preview, run, batch strict |
| Réappliquer des décisions à d'autres sources | `decision-profile` | GUI low-code, CLI `--profile`, batch par dossier |

Un **exact job** contient sources, sortie et sélecteurs stricts.  
Un **decision profile** ne contient ni source ni sortie : il décrit comment retrouver, choisir, renommer et ordonner les pistes.

## Profils décisionnels

Un profil décisionnel `version: 1` peut couvrir :

- sélection vidéo au plus proche : résolution, HDR, HDR10, HDR10+, Dolby Vision, HLG ;
- sélection audio et sous-titres par type, langue, codec, canaux, titre, flags ;
- renommage des titres de pistes par keywords ;
- flags MKV : défaut, forcé, malentendant, malvoyant, original, commentaire ;
- ordre des pistes ;
- tags temporaires de pistes pour chaîner des règles ;
- création de variantes audio, par exemple une compatibilité AC3 depuis une piste TrueHD.

Les profils GUI sont enregistrés ici :

```text
<dossier de config Muxiveo>/profiles/decision/
```

## Éditeur low-code

Dans le panneau Remux, **Éditer profil** ouvre une fenêtre dédiée :

- panneau gauche : groupes et règles ;
- panneau central : critères, actions, priorité, mode d'écriture, tags, patterns ;
- panneau droit : preview piste par piste.
- bouton **Insérer keyword** : menu par catégories pour remplir le champ actif sans afficher toute la liste en permanence.
- clic droit dans un champ compatible : entrée **Insérer keyword** avec les mêmes catégories.
- les keywords apparaissent comme des badges lorsque le champ n'est pas en cours d'édition.
- les critères peuvent être obligatoires ou préférés : un préféré donne un bonus de priorité sans bloquer le fallback.
- un sélecteur permet de charger un profil existant pour l'éditer, ou de le supprimer après confirmation.

Deux départs sont possibles :

- **Profil vide** : construire les règles à la main ;
- **Capturer l'état courant** : partir de la table de pistes actuelle puis ajuster.

Les modèles disponibles couvrent les usages principaux : sélection vidéo, garder une langue, exclure commentaire, renommer, flags, ordre, variante audio et tag temporaire.

## Comprendre une règle

Une règle répond toujours à trois questions :

- **Quelles pistes regarder ?** Ce sont les critères : type, langue, codec, titre, flags, largeur, hauteur, HDR, Dolby Vision, etc.
- **Combien de pistes appliquer ?** C'est la **portée** : `best`, `first` ou `all`.
- **Que faire sur ces pistes ?** Ce sont les actions : activer, désactiver, renommer, changer la langue, écrire des flags, créer une variante audio, ou donner une priorité d'ordre.

Un critère peut être **obligatoire** ou **préféré**.

- **Obligatoire** : si la piste ne respecte pas ce critère, elle est exclue.
- **Préféré** : la piste peut quand même être choisie, mais celle qui respecte le critère marque plus de points.

Exemple : pour une piste audio française en EAC3, avec Atmos si disponible :

- langue `fr-FR` : obligatoire ;
- codec `EAC3` : obligatoire ;
- keyword préféré `{atmos}` : bonus si une piste Atmos existe.

Si aucune piste Atmos n'existe, Muxiveo peut quand même garder la meilleure piste française EAC3.

## Expressions de critères

Les champs texte de critères acceptent une mini-syntaxe commune au GUI et au CLI :

- `&` signifie **AND** ;
- `|` signifie **OR** ;
- les parenthèses fixent la priorité.

Exemples :

```text
VFQ | VFF
(VFQ | VFF) & Forced
EAC3 | AC3
{atmos} | {dtsx}
```

Les champs de critères restent combinés entre eux par AND : si vous renseignez `type = audio` et `codec = EAC3 | AC3`, la piste doit être audio et avoir l'un des deux codecs.

Pour chercher un caractère `&`, `|`, `(` ou `)` comme texte réel, utilisez des guillemets ou un échappement :

```text
"A | B"
Dolby \& DTS
\(VFQ\)
```

Les expressions invalides bloquent la sauvegarde du profil et s'affichent dans la preview au lieu de faire planter l'éditeur.

## Portée des règles

La portée détermine combien de pistes une règle va modifier.

| Portée | Effet | Usage typique |
|---|---|---|
| `best` | choisit la piste qui a le meilleur score | choisir une seule vidéo, une seule VF principale, une seule VO principale |
| `first` | choisit la première piste compatible dans l'ordre des sources | faire confiance à l'ordre du fichier source |
| `all` | applique la règle à toutes les pistes compatibles | renommer toutes les pistes audio, désactiver tous les commentaires, tagger tous les sous-titres forcés |

`best` est le choix le plus courant pour une sélection intelligente. Le moteur additionne les points des critères compatibles : langue, codec, canaux, Atmos/DTS:X, flags, résolution, HDR, etc. S'il y a une égalité impossible à départager, le GUI le signale dans l'aperçu. Pour la vidéo, si plusieurs pistes ont exactement le même score, Muxiveo garde la première source/index.

`first` est plus simple : il prend la première piste compatible. C'est pratique quand les fichiers sont toujours construits pareil, par exemple “prendre la première audio française”.

`all` est fait pour les actions de masse. Exemple : “renommer toutes les pistes audio avec `{lang_name} {codec} {channels}`” ou “désactiver toutes les pistes dont le titre contient commentary”.

## Écriture et conflits

Si deux règles écrivent le même champ d'une même piste, Muxiveo utilise le **mode d'écriture** de la règle :

- **Priorité** : mode recommandé. La règle la plus prioritaire gagne pour le champ concerné. Les autres règles peuvent toujours écrire d'autres champs.
- **Remplacer** : la règle écrase la valeur déjà proposée, même si une règle plus prioritaire est passée avant.
- **Compléter** : la règle n'écrase pas la valeur déjà proposée. Pour un titre, elle ajoute le fragment rendu au titre déjà construit.

Une égalité de priorité avec deux valeurs différentes reste un conflit : le GUI le signale, et la CLI bloque avec un rapport JSON.

Dans l'éditeur, le nombre **Priorité** sert aussi à savoir quelle règle passe avant une autre. **Plus le nombre est grand, plus la règle est prioritaire** : `999` passe avant `1`.

Le moteur trie les règles du plus grand au plus petit. Si vous utilisez les groupes, la priorité du groupe compte d'abord, puis la priorité de la règle :

```text
priorité effective = priorité du groupe, puis priorité de la règle
```

Par exemple, une règle priorité `1` dans un groupe priorité `300` passe avant une règle priorité `999` dans un groupe priorité `200`, car le groupe est plus prioritaire.

En mode **Priorité**, le plus grand gagne aussi pour les conflits d'écriture. Une règle priorité `999` peut écrire un titre en premier ; si une règle priorité `1` passe ensuite et propose un autre titre en mode **Priorité**, elle ne remplace pas le titre déjà choisi. Les boutons de déplacement des règles ajustent cette priorité.

Le mode **Remplacer** est différent : c'est un choix volontaire pour forcer une exception. Une règle moins prioritaire en mode **Remplacer** peut donc écraser une valeur écrite par une règle plus prioritaire.

Exemple :

- règle A, priorité 100 : titre = `French DDP 5.1`;
- règle B, priorité 50 : titre = `VF`;
- en mode **Priorité**, le titre reste `French DDP 5.1`;
- en mode **Remplacer** sur la règle B, le titre devient `VF`;
- en mode **Compléter** sur la règle B, le titre devient `French DDP 5.1 VF`.

La priorité règle donc l'ordre d'évaluation et les écritures concurrentes. Elle ne déplace pas les pistes à elle seule.

## Ordonner les pistes

L'ordre de sortie se règle avec le modèle/action **Ordre**. Cette action donne une priorité d'ordre à la piste matchée :

- plus la valeur est haute, plus la piste remonte ;
- les pistes sans priorité d'ordre gardent leur ordre relatif ;
- si deux pistes ont la même priorité d'ordre, leur ordre relatif reste stable.

Une façon simple de construire un profil d'ordre :

| Règle | Portée | Critères | Action d'ordre |
|---|---|---|---|
| Vidéo principale | `best` | type vidéo, 2160p/HDR/Dolby Vision préférés | `1000` |
| Audio FR principale | `best` | langue `fr-FR`, codec voulu, Atmos préféré | `800` |
| Audio VO principale | `best` | langue `en-US`, codec voulu, Atmos préféré | `700` |
| Sous-titres FR forcés | `all` | langue `fr-FR`, flag forced | `400` |
| Sous-titres FR complets | `best` | langue `fr-FR`, non forced | `300` |

Pour ordonner proprement, séparez souvent les intentions :

- une règle choisit ou désactive les pistes ;
- une règle renomme les titres ;
- une règle donne les priorités d'ordre.

Ce découpage rend l'aperçu plus lisible et évite qu'une règle fasse trop de choses à la fois.

## Keywords

Les keywords servent à trois choses :

- renommer une piste avec un pattern de titre ;
- identifier une piste dans les critères du profil, par exemple `{flag_visual_impaired}` ou `{codec_atmos}` ;
- compléter certains champs d'action, par exemple garder la langue courante avec `{language}`.

## Variables

Un profil peut contenir des variables éditables. Le bouton **Aliases** de l'éditeur permet de définir des remplacements de rendu pour les keywords :

```text
EAC3=DDP
AC3=Dolby Digital
lang_name:French=Français
codec:TRUEHD=Dolby TrueHD
```

Ensuite :

- `EAC3=DDP` est un alias global, utilisable par tous les keywords rendus ;
- `lang_name:French=Français` ne s'applique qu'au keyword `{lang_name}` ;
- un alias ciblé gagne sur un alias global ;
- les aliases sont insensibles à la casse et aux espaces de bord ;
- les aliases changent le rendu des titres et templates de sortie, pas les critères de matching ;
- `{codec_raw}` garde toujours le nom technique, par exemple `EAC3` ;
- les anciens profils avec `variables.codec_names` restent lus pour compatibilité.

Exemple de pattern :

```text
{lang_name} {codec} {channels}
```

Avec `EAC3=DDP`, le titre devient `French DDP 5.1`.

Le keyword `{lang_name}` affiche le nom de langue sans région quand il s'agit de la variante d'origine, et garde la région entre parenthèses pour les variantes non standard :

| Langue | `{lang_name}` |
|---|---|
| `fr-FR` | `French` |
| `fr-CA` | `French (Canada)` |
| `pt-PT` | `Portuguese` |
| `pt-BR` | `Portuguese (Brazil)` |

Une action de titre peut utiliser un pattern :

```text
{lang_name} {codec} {channels} {audio_object}
```

Exemples :

- `VF {codec} {channels}` -> `VF DDP 5.1` si `EAC3=DDP`, sinon `VF EAC3 5.1`
- `{lang_name} {codec} {channels} {flag_forced}` -> `French PGS Forced`
- `{lang_name} {codec_raw} {channels} {audio_object}` -> `French EAC3 5.1 Atmos`

Si une valeur manque, Muxiveo l'omet et nettoie les espaces ou séparateurs inutiles.

Keywords disponibles au premier lot :

```text
{type} {source_index} {track_index}
{language} {lang} {lang_name} {source_language}
{title} {source_title}
{codec} {codec_raw} {codec_name} {channels} {channel_layout} {audio_object}
{atmos} {dtsx} {codec_atmos} {codec_dtsx}
{resolution} {width} {height} {hdr} {video_flags_hex}
{video_hdr} {video_hdr10} {video_hdr10plus}
{video_dolby_vision} {video_hlg} {video_sdr}
{flags} {flag_enabled} {flag_default} {flag_forced}
{flag_hearing_impaired} {flag_visual_impaired}
{flag_original} {flag_commentary}
{track_tags}
```

Pour la vidéo, `width` et `height` sont indépendants : vous pouvez renseigner seulement la largeur, seulement la hauteur, les deux, ou aucun des deux. La profondeur couleur, quand elle est détectée, est prise en compte dans les caractéristiques techniques internes (`video_flags_hex`) mais n'a pas de champ dédié dans l'éditeur simple.

Exemple : pour garder une piste audio française en EAC3 et préférer Atmos si elle existe, indiquez `fr-FR` en langue, `EAC3` en codec avec **Obligatoire**, puis `{atmos}` dans **Keywords préférés**.

Dans `--output-template`, les keywords partagés existent aussi avec préfixe de
groupe pour lever les ambiguïtés, par exemple `{audio-lang:all}`,
`{sub-lang:all}`, `{audio-codec-release:best}` ou `{video-resolution:best}`.
Les décisions de nommage utilisent les pistes finales activées; `--output-all`
force les keywords pistes à lister toutes les valeurs trouvées. Les aliases du
profil s'appliquent aussi aux keywords rendus dans `--output-template`.

## Application

Depuis le GUI, **Appliquer profil** charge un profil enregistré, affiche un aperçu, puis applique après confirmation.

En CLI :

```bash
Muxiveo-cli validate --profile profil.json
Muxiveo-cli preview --profile profil.json -i source.mkv --json
Muxiveo-cli run --profile profil.json -i source.mkv -o sortie.mkv
```

Vous pouvez donner un chemin complet ou simplement le nom d'un profil sauvegardé
dans le dossier utilisateur. L'extension `.json` est optionnelle : `--profile BestOfAll`
cherchera aussi `BestOfAll.json` dans `<dossier de config Muxiveo>/profiles/decision/`.

En batch dossier :

```bash
Muxiveo-cli batch \
  --profile profil.json \
  --input-dir "Serie" \
  --recursive \
  --output-dir "out" \
  --auto-tmdb \
  --dry-run
```

`--auto-tmdb` peut être ajouté aux traitements CLI pour récupérer les tags TMDB
et la cover. La CLI déduit automatiquement saison/épisode depuis les noms de
fichiers du type `S01E02` ou `01x02`. Utilisez `--tmdb-id` pour forcer une fiche,
`--no-cover` pour garder les tags sans cover, et `--no-attach` pour ne sortir
aucun attachment.

La CLI n'ouvre pas de dialogue interactif. Si un conflit ou une ambiguïté ne peut pas être résolu automatiquement, elle retourne un rapport JSON et bloque l'application.

## Exact jobs

**Exporter JSON CLI** produit un job exact destiné aux traitements stricts.

Il est adapté aux épisodes ou fichiers construits de la même façon :

```bash
Muxiveo-cli validate --config exact-job.json
Muxiveo-cli preview --config exact-job.json
Muxiveo-cli run --config exact-job.json
Muxiveo-cli batch --template exact-job.json --input-dir "Serie" --output-dir "out"
```

Utilisez un exact job quand la structure des sources est stable. Utilisez un profil décisionnel quand les sources varient mais que vos décisions restent les mêmes.
