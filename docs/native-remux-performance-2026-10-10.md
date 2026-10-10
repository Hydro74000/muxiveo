# Latence du remux natif partagé Remux/Encode — 10 octobre 2026

Deux défauts distincts ont été reproduits et corrigés : une extraction TrueHD
imposée par l'interface et un parcours exhaustif des blocs avant l'écriture.
Le second concerne tous les codecs. Les validations finales ne sont pas la
cause principale mesurée.

## Extraction audio

`_AudioTable.current_audio_settings()` activait `extract_truehd_core` dès
qu'une piste TrueHD Atmos était en mode `copy`. L'assemblage natif exécutait
alors FFmpeg pour créer `native_audio_0.mkv`, même sans demande de conversion.
Le filtre `truehd_core` supprime les données Atmos :
[documentation FFmpeg](https://ffmpeg.org/ffmpeg-bitstream-filters.html#truehd_005fcore).

La copie issue de l'interface conserve désormais le bitstream complet. Une
extraction du core explicitement demandée dans `AudioTrackSettings` reste
fonctionnelle, de même que la préparation des pistes réellement réencodées.

Le remux MKV simple ne matérialise pas systématiquement l'audio : les tests
du plan et l'exécution réelle confirment l'absence de préparation FFmpeg en
copie. Pour une source MP4/MOV/TS, la canonicalisation vers un artefact MKV
reste nécessaire au lecteur natif actuel. Les synchronisations physiques,
conversions audio et extractions explicitement demandées peuvent également
produire un artefact ; ce sont des opérations distinctes.

## Attente commune à tous les fichiers

`compile_assembly_plan()` appelait `MatroskaReader.blocks(read_payload=False)`
sur chaque artefact média pour régénérer BPS, DURATION, NUMBER_OF_FRAMES et
NUMBER_OF_BYTES. Le writer relisait ensuite les mêmes sources avec les
payloads. La première passe n'était accompagnée ni de progression ni de
vérification d'annulation. Les sondes et lectures tamponnées touchaient aussi
des octets de média, malgré l'absence de matérialisation des frames.

Ce coût dépend du nombre de blocs et des accès disque. L'extrait réel de
60 secondes contient 72 000 paquets TrueHD ; les tags du film original
annoncent 12 884 450 frames TrueHD. Le parcours supplémentaire est donc
particulièrement coûteux sur cette source, sans être spécifique à TrueHD.
L'intervalle de 8 min 12 s du journal englobe préparation et écriture : il
ne permet pas d'attribuer toute cette durée à la seule préparation.

Les statistiques sont maintenant accumulées pendant l'unique passe
d'écriture. Les Tags correspondants sont écrits en fin de Segment et
indexés dans le SeekHead. La durée exacte est finalisée à partir des blocs
écrits ; la durée du conteneur source sert seulement à estimer la progression.
La première progression précède la lecture du premier paquet. La publication
atomique, les Cues, la validation sémantique et ffprobe restent actifs.

Le comptage tient compte des frames de chaque lace et de ses octets média,
sans compter la table de lacing. Les offsets et les paquets écartés avant
zéro sont pris en compte. Le cache d'identité Encode évite aussi de relire
et hacher jusqu'à 2 Mio de la même source pour chacune de ses pistes.

## Mesures avant/après

Python 3.14.7, FFmpeg système, Linux, fichiers temporaires dans `/tmp`, cache
système chaud. Une chauffe exclue, trois passages alternés par backend,
médianes sans cProfile. Le profilage constitue un passage séparé. Ces mesures
portent sur le contrat, la compilation, l'assemblage, les validations et la
publication ; elles excluent l'extraction TrueHD indésirable du workflow
avant correction. Elles ne mesurent pas un remux complet du film de 34 Go.

| Source | Paquets | Préparation avant | Après | Total natif avant | Après | Gain total |
|---|---:|---:|---:|---:|---:|---:|
| HEVC + AAC, 1 800 s, 96,6 Mio | 138 600 | 1,141 s | 0,001 s | 2,852 s | 1,738 s | 39 % |
| Extrait HEVC + TrueHD + EAC3, 60 s, 70,7 Mio | 75 317 | 0,636 s | 0,0016 s | 1,664 s | 1,063 s | 36 % |

La première progression arrive respectivement après environ 4 et 5 ms.
La compilation seule du plan à 16 pistes du film original de 34 238 614 475
octets prend 8,8 ms ; contrat et signature de source compris, 33,9 ms.
Aucun parcours des paquets ni écriture du film complet pour cette mesure.

Après ce premier correctif, le mux Python restait plus lent que FFmpeg :
environ 1,738 contre 0,495 s sur le premier jeu, 1,063 contre 0,378 s sur le
second. L'optimisation complémentaire ci-dessous réduit aussi le coût de
la passe média. Sur une source non Matroska, l'artefact de canonicalisation
ajoute encore une passe de copie.

## Vérifications

- Comparaison SHA-256 par piste des payloads et de la séquence complète des
  horodatages/durées : source = assemblage avant = assemblage après sur les
  deux jeux. 75 317 et 138 600 paquets, aucune perte ni modification.
- Statistiques identiques avant/après hors date de génération ; durée de
  Segment identique. ffprobe et la bibliothèque MediaInfo lisent les sorties
  et leurs compteurs, y compris les Tags en fin de Segment.
- Exécutions réelles des workflows Remux et Encode en copie avec backend
  `native` : sorties valides, paquets identiques, aucune extraction audio.
- Tests de régression : aucune lecture de média pendant la compilation,
  une seule passe ensuite, copie AAC/AC3/EAC3/TrueHD/FLAC, core TrueHD
  explicite, cache d'identité, lacing Xiph/fixed/EBML, offsets positifs et
  négatifs, Tags indexés, durée finale et nettoyage après annulation.
- Suite Matroska/Remux/UI et intégration conteneurs : 591 réussites.
  Suite élargie Encode/Remux : 1 697 réussites, 15 exclusions ; deux tests
  matériels NVEncC échouent à l'initialisation CUDA (`CUDA_ERROR_NO_DEVICE`).
- Intégrations Encode (copie/réencodage/statistiques), concaténation et CLI :
  29 réussites, 3 exclusions sur la sélection, puis 12 réussites sur la relance
  de l'ensemble du fichier CLI. Le premier passage CLI a rencontré un verrou dans le cache
  utilisateur en lecture seule ; la relance avec cache temporaire passe.
- Ruff et Mypy passent sur les fichiers de code concernés.

Mesures brutes : `/tmp/native-remux-before.json`,
`/tmp/native-remux-after.json`, `/tmp/native-remux-truehd-before.json`,
`/tmp/native-remux-truehd-after.json`. Comparaison des paquets et journaux des
workflows : `/tmp/native-remux-audit/packet-verification.json`.

Reproduction du benchmark :

```bash
python3 -m scripts.benchmark_native_mux source.mkv \
  --work-dir /tmp/native-remux-audit --report /tmp/resultats.json --runs 3
```

## Contrôle complémentaire de préservation Atmos

Un extrait réel de 2 secondes contient 2 400 paquets TrueHD 7.1, avec le
profil ffprobe `Dolby TrueHD + Dolby Atmos`. Huit exécutions réelles ont été
vérifiées : Remux, Encode en copie, Encode avec réencodage vidéo seul et
Encode multi-vidéo (copie + réencodage), chacune avec les backends `ffmpeg`
et `native`. Dans les huit cas : même SHA-256 des payloads audio avec leurs
longueurs, même nombre de paquets, huit canaux, profil Atmos conservé,
décodage FFmpeg `-xerror` réussi, aucune extraction `truehd_core`.

Les chemins NVEncC et injection HDR partagent le mapping audio et
`audio_codec_args` des chemins FFmpeg vérifiés ; leur contrôle complémentaire
porte sur le code et les tests de commandes, sans nouvelle exécution GPU.
Le passage natif final est commun aux pipelines. Ce contrôle ne constitue
pas un essai sur récepteur home cinéma ni une matrice exhaustive de médias.

La synchronisation avancée avec une avance de 137 ms a également été
exécutée sur le TrueHD Atmos réel : copie/coupe sans réencodage, profil
Atmos maintenu et décodage réussi. Les retards positifs Atmos restent des
offsets de conteneur ; les calibrations multi-segments incompatibles sont
refusées. Les tests vérifient également la détection d'Atmos/JOC depuis le
profil sondé lorsque les titres ne l'indiquent pas.

Un défaut supplémentaire a été reproduit : en synchronisation physique,
un codec `EAC3-JOC` sans le mot « Atmos » dans le titre ou la description
pouvait produire une commande de réencodage EAC3. Ce chemin utilise maintenant
le détecteur commun de métadonnées objet, sur le codec, le titre et la
description. Des tests couvrent les variantes JOC et les offsets des deux
signes. Les transformations physiques détectées comme incompatibles sont
refusées au lieu de réencoder implicitement.

La garantie de préservation concerne la copie audio. Une conversion audio
explicitement demandée ou `extract_truehd_core=True` sort de cette garantie.
Résultats des huit chemins réels :
`/tmp/native-remux-audit/atmos-paths/report.json`. Tests complémentaires dans
`tests/test_atmos_preservation.py` ; suites de synchronisation/CLI/cadence :
168 réussites, puis 72 réussites pour la sélection comprenant les nouveaux
tests Atmos. La sélection audio des tests de workflow et d'interface donne
189 réussites. Ruff et Mypy passent sur le correctif complémentaire.

## Optimisation complémentaire de la passe média

Le profilage après suppression de la pré-passe montrait surtout le lecteur :
lectures/seek/tell répétés pour chaque élément et deux constructions de
`MatroskaBlock` par frame, dont la seconde servait seulement à convertir les
horodatages. Le writer copiait aussi chaque payload dans son Block puis dans
plusieurs buffers de Cluster.

- Le lecteur utilise maintenant une projection glissante de 32 Mio. Seules
  les frames sélectionnées sont copiées ; les bytes émis restent indépendants
  des fenêtres fermées. Une frame dépassant la fenêtre reste prise en charge.
  Le pic mémoire résidente mesuré sur la source de 438 Mio est d'environ
  70 Mio. Une première version projetant tout le fichier atteignait 475 Mio
  et n'a pas été retenue.
- Le repli sur lecture tamponnée est conservé si la projection initiale est
  impossible. Une erreur de parsing ou de remapping après émission de paquets
  remonte comme erreur ; elle ne rejoue jamais les paquets déjà consommés.
- Les clusters de taille inconnue sont parcourus sans pré-scan de leur frontière.
- Un seul objet est construit par frame. Le mux demande seulement la première
  frame de chaque lace, tout en conservant le payload lacé original complet.
  L'API publique continue par défaut à exposer toutes les frames.
- Le writer sépare en-têtes, payloads et métadonnées de BlockGroup, puis compose
  chaque Cluster en une seule copie des payloads. Les positions des Cues sont
  calculées à partir des tailles exactes des mêmes éléments.
- Les VINT usuelles et les compteurs sont calculés avec moins d'allocations.
  Les bornes de Cluster, l'ordre de décodage, les contrôles de représentabilité
  temporelle, les validations et la publication atomique sont conservés.

Nouvelle comparaison sur la même machine : une chauffe puis trois passages
alternés, avec les validations. « Avant » inclut déjà le premier correctif.
Les fichiers et le cache disque sont chauds ; ce ne sont pas des garanties de
débit sur tout stockage.

| Source | Paquets | Natif avant optimisation | Natif final | Réduction du temps | FFmpeg final |
|---|---:|---:|---:|---:|---:|
| HEVC + AAC, 1 800 s, 96,6 Mio | 138 600 | 1,756 s | 0,941 s | 46,4 % | 0,472 s |
| HEVC + TrueHD + EAC3, 60 s, 70,7 Mio | 75 317 | 0,991 s | 0,568 s | 42,6 % | 0,348 s |
| H.264 + AAC, 180 s, 437,6 Mio | 13 860 | 0,500 s | 0,363 s | 27,3 % | 0,451 s |

Le natif dépasse ici FFmpeg sur la source à gros débit. Il reste plus lent
sur les petits paquets très nombreux, où les objets Python coûtent encore.
La première progression arrive en 4 à 5 ms sur ces trois jeux.

### Contrôles croisés de cette optimisation

Le lecteur d'origine (`git show HEAD:core/matroska/reader.py` avant le commit)
a servi d'oracle indépendant : égalité de tous les champs et payloads, dans
leur ordre source, sur les trois fichiers de benchmark et les trois MKV du
corpus (227 877 frames). Le mode destiné au mux est également comparé aux
premières frames lacées du lecteur d'origine. Les assemblages avant/après
ont les mêmes signatures par piste, horodatages, durées, statistiques et
durée de Segment sur les deux jeux HEVC/AAC et HEVC/TrueHD/EAC3.

Les huit workflows Atmos réels ont été rejoués après les derniers changements.
Ils conservent les 2 400 payloads TrueHD 7.1 à l'identique, leur profil Atmos
et leur décodabilité FFmpeg. Les sorties sont aussi inspectées par ffprobe
et MediaInfo. Le corpus couvre AVC/AAC/SRT, HEVC/FLAC/ASS/HDR10 et AV1/Opus/WebVTT,
avec chapitres, tags et attachments.

Les nouveaux tests comparent les parcours projeté et tamponné : trois échelles
de temps, numéros de pistes multi-octets, filtrage, timestamps relatifs négatifs,
les trois modes de lacing, ReferenceBlock, BlockDuration, DiscardPadding,
CodecState et BlockAdditions. Ils couvrent les frontières de fenêtre, les
paquets plus grands que la fenêtre, la fermeture du mapping, les clusters
inconnus, les fichiers tronqués et l'absence de rejeu après erreur.

Les six séries de mesures sont conservées dans
[`benchmarks/2026-10-10-native-remux.json`](benchmarks/2026-10-10-native-remux.json).
Les profils cProfile complémentaires restent dans
`/tmp/native-remux-optimization*-before.json` et
`/tmp/native-remux-optimization*-after.json`. Contrôle du lecteur :
`/tmp/native-remux-audit/reader-optimization-verification.json`.

Le gate Linux complet, avec les avertissements de threads pytest traités comme
erreurs, passe : **4 428 tests réussis, 104 ignorés** pour outils, opt-in matériel
ou plateforme. Ruff passe sur le périmètre CI ainsi que sur les tests/scripts
modifiés ; Mypy passe sur les 245 fichiers de `core`, `cli`, `ui`, `workers` et
les points d'entrée. Les contrôles complémentaires de concaténation, SeekHead
et roundtrip corpus donnent 8 réussites, avec le MediaInfo CLI compilé localement.
Les encodages HEVC 10 bits sur la RTX 4070 Ti SUPER passent aussi : NVENC et
NVEncC, chacun avec les backends `native` et `ffmpeg` (4 réussites). Les six
cas GPU DoVi/HDR10+ restent ignorés faute de corpus combiné désigné par
`MUXIVEO_TEST_HDR_SOURCE` ; ils ne sont pas comptés comme validés.

Le premier gate complet avait des erreurs d'environnement : pytest-qt absent
et le NVEncC système 9.16 dont libplacebo annonçait être compilé sans Vulkan.
Le gate final utilise pytest-qt 4.5.0 installé uniquement dans le dossier de
test temporaire et le NVEncC 9.36 déjà présent dans le cache local. Le test
réel de conversion P5 passe avec ce dernier ; aucun test n'a été neutralisé
pour obtenir le résultat vert. Rapports :
`/tmp/native-remux-audit/pytest-full-final.xml` et
`/tmp/native-remux-audit/pytest-full-final.log`.
