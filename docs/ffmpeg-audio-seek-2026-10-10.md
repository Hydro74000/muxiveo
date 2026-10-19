# Audio après seek : assemblage FFmpeg de vidéo préparée

## Défaut

Les assemblages après interpolation, NVEncC et injection HDR ouvrent la vidéo
préparée puis les sources originales pour l'audio et les sous-titres. Quand une
entrée fournit à la fois l'audio et des sous-titres rares, FFmpeg peut écrire des
minutes d'audio avant la vidéo correspondante. Un seek utilisant les Cues vidéo
dépasse ces paquets audio et coupe le son.

L'analyse de synchronisation se fonde sur les sources originales. Elle reste
désactivée pour une seule source logique, même si la vidéo préparée constitue
une entrée FFmpeg distincte. Ajouter `max_interleave_delta=5000000` ne suffit
pas ; mettre cette valeur à zéro peut retenir des minutes de vidéo en mémoire.

## Correction

`planning/subtitle_inputs.py` examine les entrées audio effectivement mappées
après synchronisation. Lorsqu'une source fournit encore audio et sous-titres,
elle est rouverte sur une entrée réservée aux sous-titres. Les flux restent
copiés directement ; aucune extraction audio ni transformation TrueHD n'est
ajoutée.

Ce plan s'applique indépendamment de l'analyse des sources logiques dans les
trois assemblages runtime et dans l'aperçu de l'assemblage multi-vidéo.
Il conserve les remappages de synchronisation existants, prend en charge les
sous-titres explicites/automatiques et le repli `:s?`, puis laisse les décalages
par piste s'appliquer. Les entrées de métadonnées sont indexées après les
nouvelles entrées sous-titres. Les sources de chapitres et d'attachments restent
inchangées. Une source sans audio sélectionné ou sans sous-titres n'est pas
rouverte pour cette protection.

## Validation sans encodage

- 467 tests réussis : workflow encode, planification, services runtime,
  interpolation unitaire et nouveau test d'intégration audio/seek.
- Le test `tests/integration/test_encode_audio_seek.py` étend le corpus existant
  à 160 secondes par répétition de paquets, ajoute des sous-titres rares et
  exécute uniquement des remux en copie. Il vérifie le décodage audio après
  trois seeks vidéo et l'identité des payloads.
- Avec l'isolation désactivée en mémoire, les deux variantes de ce test
  échouent au seek de 30 secondes ; avec la correction, elles passent.
- Les tests croisés couvrent les trois routes et l'aperçu, les sélections
  explicites/automatiques/inconnues/vides, les offsets positifs/négatifs,
  chapitres, tags, attachments et remappages multi-sources existants.
- Sur les deux samples utilisateur de cinq minutes, FFmpeg embarqué
  `N-127271-gba987fe24d-20261010` / libavformat `63.8.100` retrouve les deux
  pistes audio aux seeks 30, 120 et 240 secondes. Les suites de payloads des
  cinq pistes ont les mêmes SHA-256 que les remux FFmpeg de référence, avec
  TrueHD Atmos intact. Chapitres et cover sont conservés. CodecPrivate,
  cadence, paramètres vidéo/audio et signalisation Dolby Vision sont identiques
  entre les sorties de référence et les sorties corrigées.
- Ruff, mypy sur les cinq modules modifiés/ajoutés et `git diff --check` passent.

Commande de vérification :

```sh
QT_QPA_PLATFORM=offscreen python3 -m pytest -q \
  tests/test_encode_workflow.py tests/test_encode_planning.py \
  tests/test_encode_runtime_services.py tests/test_encode_interpolation.py \
  tests/integration/test_encode_audio_seek.py
```

Les fichiers déjà mal entrelacés nécessitent un remux de réparation. Ce
correctif prévient le défaut lors des prochains assemblages ; il ne modifie
pas les sorties existantes.
