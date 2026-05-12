# Profils Mediarecode

## Vue rapide

Mediarecode distingue deux usages :

| Besoin | Format | Usage |
|---|---|---|
| Refaire exactement un traitement | `exact-job` | CLI, preview, run, batch strict |
| Réappliquer des décisions à d'autres sources | `decision-profile` | GUI low-code, CLI `profile`, batch par dossier |

Un **exact job** contient sources, sortie et sélecteurs stricts.  
Un **decision profile** ne contient ni source ni sortie : il décrit comment retrouver, choisir, renommer et ordonner les pistes.

## Profils décisionnels

Un profil décisionnel `version: 1` peut couvrir :

- sélection vidéo au plus proche : résolution, HDR, HDR10, HDR10+, Dolby Vision, HLG, profondeur si disponible ;
- sélection audio et sous-titres par type, langue, codec, canaux, titre, flags ;
- renommage des titres de pistes par keywords ;
- flags MKV : défaut, forcé, malentendant, malvoyant, original, commentaire ;
- ordre des pistes ;
- tags temporaires de pistes pour chaîner des règles ;
- création de variantes audio, par exemple une compatibilité AC3 depuis une piste TrueHD.

Les profils GUI sont enregistrés ici :

```text
<dossier de config Mediarecode>/profiles/decision/
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

## Priorité et conflits

Si deux règles écrivent le même champ d'une même piste, Mediarecode utilise le **mode d'écriture** de la règle :

- **Priorité** : mode recommandé. La règle la plus prioritaire garde sa valeur, les règles moins prioritaires complètent les autres champs mais n'écrasent pas ce champ.
- **Remplacer** : la règle écrase la valeur déjà proposée, même si une règle plus prioritaire est passée avant.
- **Compléter** : la règle n'écrase pas les champs déjà proposés. Pour un titre, elle ajoute le fragment rendu au titre déjà construit.

Une égalité de priorité avec deux valeurs différentes reste un conflit : le GUI le signale, et la CLI bloque avec un rapport JSON.

## Keywords

Les keywords servent à deux choses :

- renommer une piste avec un pattern de titre ;
- identifier une piste dans les critères du profil, par exemple `{flag_visual_impaired}` ou `{codec_atmos}`.
- compléter certains champs d'action, par exemple garder la langue courante avec `{language}`.

## Variables

Un profil peut contenir des variables éditables. Le premier usage disponible dans l'éditeur est **Aliases codecs**, accessible par un bouton dans l'éditeur :

```text
EAC3=DDP
AC3=Dolby Digital
TRUEHD=Dolby TrueHD
```

Ensuite :

- dans les patterns de titre, `{codec}` et `{codec_name}` affichent l'alias friendly, par exemple `DDP`;
- `{codec_raw}` garde toujours le nom technique, par exemple `EAC3`;
- si aucun alias n'existe, `{codec}` et `{codec_name}` retombent sur le codec technique.

Exemple de pattern :

```text
{lang_name} {codec} {channels}
```

Avec `EAC3=DDP`, le titre devient `French DDP 5.1`.

Une action de titre peut utiliser un pattern :

```text
{lang_name} {codec} {channels} {audio_object}
```

Exemples :

- `VF {codec} {channels}` -> `VF DDP 5.1` si `EAC3=DDP`, sinon `VF EAC3 5.1`
- `{lang_name} {codec} {channels} {flag_forced}` -> `French PGS Forced`
- `{lang_name} {codec_raw} {channels} {audio_object}` -> `French EAC3 5.1 Atmos`

Si une valeur manque, Mediarecode l'omet et nettoie les espaces ou séparateurs inutiles.

Keywords disponibles au premier lot :

```text
{type} {source_index} {track_index}
{language} {lang} {lang_name}
{title} {source_title}
{codec} {codec_raw} {codec_name} {channels} {channel_layout} {audio_object}
{atmos} {dtsx} {codec_atmos} {codec_dtsx}
{resolution} {width} {height} {hdr} {video_flags_hex}
{video_hdr} {video_hdr10} {video_hdr10plus}
{video_dolby_vision} {video_hlg} {video_sdr}
{flags} {flag_enabled} {flag_default} {flag_forced}
{flag_hearing_impaired} {flag_visual_impaired}
{flag_original} {flag_commentary}
```

Pour la vidéo, `width` et `height` sont indépendants : vous pouvez renseigner seulement la largeur, seulement la hauteur, les deux, ou aucun des deux.

Exemple : pour garder une piste audio française en EAC3 et préférer Atmos si elle existe, indiquez `fr-FR` en langue, `EAC3` en codec avec **Obligatoire**, puis `{atmos}` dans **Keywords préférés**.

## Application

Depuis le GUI, **Appliquer profil** charge un profil enregistré, affiche un aperçu, puis applique après confirmation.

En CLI :

```bash
mediarecode-cli validate --profile profil.json
mediarecode-cli preview --profile profil.json -i source.mkv --json
mediarecode-cli run --profile profil.json -i source.mkv -o sortie.mkv
```

En batch dossier :

```bash
mediarecode-cli batch \
  --profile profil.json \
  --input-dir "Serie" \
  --recursive \
  --output-dir "out" \
  --dry-run
```

La CLI n'ouvre pas de dialogue interactif. Si un conflit ou une ambiguïté ne peut pas être résolu automatiquement, elle retourne un rapport JSON et bloque l'application.

## Exact jobs

**Exporter JSON CLI** produit un job exact destiné aux traitements stricts.

Il est adapté aux épisodes ou fichiers construits de la même façon :

```bash
mediarecode-cli validate --config exact-job.json
mediarecode-cli preview --config exact-job.json
mediarecode-cli run --config exact-job.json
mediarecode-cli batch --template exact-job.json --input-dir "Serie" --output-dir "out"
```

Utilisez un exact job quand la structure des sources est stable. Utilisez un profil décisionnel quand les sources varient mais que vos décisions restent les mêmes.
