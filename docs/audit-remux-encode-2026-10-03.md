# Audit RemuxPanel, EncodePanel et workflows — 3 octobre 2026

Audit du worktree non commité, comprenant les corrections du précédent audit.
Lecture des entrées UI, de la construction des configurations, du routage,
des runners, de la validation et de leurs sorties : succès, rejet, annulation,
fermeture, nettoyage et génération NFO. Aucun commit créé.

## Corrections des quatre points initiaux

| Défaut | Correction | Preuve |
| --- | --- | --- |
| Un tag MediaInfo plausible était traité comme un compte exact dans Merge DoVi. | Film 1 et les sources conteneurisées sont comptés par paquets. Le flux final est également compté exactement ; un échec de comptage ne reprend pas une estimation. | E2E : vidéo réelle de 48 trames avec tag `NUMBER_OF_FRAMES=49`, métadonnées de 48 trames, politique exacte, succès. Régression : estimation finale à 48 mais compte réel à 47, rejet. |
| Un scan silencieux pouvait retarder l'annulation et la fermeture. | Les lecteurs de FrameCountGuard acceptent un runner annulable. Les scans et sondes RPU/ffprobe/MediaInfo de Merge sont enregistrés et surveillés pendant `communicate()`. | Processus réel silencieux : annulation, arrêt du processus et sortie du worker. Tests de fermeture et de propagation d'annulation. |
| Film 2 était extrait inutilement après abandon des métadonnées dynamiques, avec un pic disque sous-estimé. | Extraction de Film 2 et modèle de stockage utilisent tous deux les flags HDR effectifs. | Régression HDR10 seul avec Film 2 MP4 : aucune extraction ni allocation correspondante. Tests des phases de stockage. |
| Merge DoVi contournait l'exclusivité entre preview et opération. | Garde commune fournie aux panneaux Merge et Remux ; extraction, synchronisations audio/sous-titres et Sync Studio la consultent également. | Tests UI des lancements bloqués ; l'état de synchronisation des sous-titres est transmis au bandeau global. |

## Rejet final et passage forcé

La popup affiche le motif du rejet et propose de poursuivre, avec **Non** par
défaut. Le worker attend la réponse ; l'affichage reste sur le thread Qt.
L'annulation libère l'attente et ferme une popup déjà ouverte.

Le candidat reste sur disque pendant la décision. Une réponse positive
autorise la poursuite et est journalisée en avertissement. Un refus ou une
annulation supprime le candidat et préserve une sortie préexistante. Le commit
reste atomique et vient après les validations acceptées.

| Chemin | Point de décision |
| --- | --- |
| Encode, sortie FFmpeg, y compris assemblage après encodage | `MatroskaOutputTransaction.execute()` : contrat sémantique et ffprobe. |
| Remux FFmpeg | `RemuxRuntimeRunner` : contrat sémantique et ffprobe avant remplacement. |
| Encode et Remux natifs | Contrôle structurel du writer, puis contrat sémantique et ffprobe, avant commit. Chaque contrôle distinct reste soumis à une décision explicite. |
| Encode avec injection HDR, mono-vidéo ou multi-vidéo | Rejet de FrameCountGuard après encodage et avant injection ; validation du conteneur final ensuite. |
| Merge DoVi | Rejet de VERIFY avant remux ; validations du MKV candidat avant commit. |
| Preview et CLI sans gestionnaire interactif | Aucun passage forcé implicite. La preview désactive explicitement la popup et le NFO dans sa configuration. |

Les erreurs de configuration, les fichiers absents, les échecs d'outils et les
échecs d'écriture restent des erreurs : accepter un contrôle ne rend pas une
opération techniquement impossible exécutable. Le contrôle de comptage ne
prouve pas à lui seul l'alignement temporel des scènes HDR.

## Autres défauts trouvés et corrigés

- **Sortie Encode égale à une entrée secondaire** : la validation protège
  désormais toutes les sources vidéo/audio/sous-titres/attachments/tags et
  attachments externes, ainsi que les liens symboliques et hardlinks.
- **Options perdues dans Remux → Encode** : enrichissement par
  `dataclasses.replace()` ; conservation des options NFO, preview et exécution.
- **Métadonnées empruntées à une autre source** : le bridge ne recherche plus
  une piste par son seul index dans un autre fichier. La clé est source + index
  et les variantes continuent d'utiliser leur identifiant de piste.
- **Audit HDR absent du pipeline multi-vidéo** : ajout du même contrôle que le
  pipeline simple, avec nombre encodé fourni par le squelette de timing.
- **Audit d'une piste secondaire portant sur la première vidéo** : comptage
  explicite de l'index sélectionné quand les métadonnées ne suffisent pas.
- **Workspace Encode abandonné sur erreur de préparation** : capture du jeton
  de propriété et nettoyage sur erreur ou annulation avant le branchement des
  hooks normaux. Un dossier utilisateur non possédé est conservé.
- **Relocalisation des covers Remux hors du worker protégé** : déplacement dans
  le bloc qui garantit signal terminal et nettoyage, pour les deux backends.
- **Session sync non fermée après TaskCancelledError** : nettoyage aussi pour
  cette exception, qui hérite de BaseException, en simple passe et deux passes.
- **COPY avec transformation routé vers Remux** : passage par la validation
  Encode pour rendre l'incompatibilité visible au lieu d'ignorer le réglage.

## Chemins relus et vérifiés

| Famille | Appelants et dispatch | Exécution et sorties |
| --- | --- | --- |
| RemuxPanel | Inspection asynchrone, sélection/ordre/variantes de pistes, chapitres, tags, covers et attachments ; config builder, profils et restauration ; `MainWindow._on_run()`, validation et `RemuxWorkflow.run()`. | Plan partagé, FFmpeg / natif / auto, canonicalisation des conteneurs, variantes audio, conversion de sous-titres, offsets, sync live/fichier et réécriture physique ; patchs, contrat, commit, NFO, signaux terminaux et nettoyage. |
| EncodePanel | État par piste, source/index sélectionnés, COPY vs encode, HDR et géométrie, audio, transformations, preview ; configuration, validation, bridge avec Remux, `EncodeWorkflow.run()`. | Préparation asynchrone et synchrone, workspace/attachments, normalisation HDR, sélection du backend avant écriture, protection des entrées, progression et arrêt. |
| Encode direct FFmpeg | Pipeline `ffmpeg_direct`, mono-source et multi-source. | Simple passe et deux passes, offsets et sync/remap, transaction finale FFmpeg ; découpage vidéo et assemblage lorsque le natif est demandé. |
| Encode HDR | Pipeline `metadata_inject`. | Extraction de la piste sélectionnée, routage P5/P7/P8, expansion HDR pour interpolation, encodage, squelette de timing, comptage, injection RPU/HDR10+/SEI statiques, réécriture des payloads, reconstruction FFmpeg ou native. |
| Encode multi-vidéo | Pipeline `multi_video`, pistes COPY + encodées, ordonnanceur parallèle. | Préparation indépendante, contrôle HDR par piste, timing, ordre final, audio/sous-titres, offsets, métadonnées et assemblage FFmpeg ou natif. |
| NVEncC / interpolation | Pipeline `nvencc_direct` et étapes RIFE des pipelines FFmpeg/NVEncC. | Construction des commandes, pipes, progression, sélection de l'assembleur, matérialisation des artefacts et annulation ; validation finale commune. Tests d'exécution matérielle limités comme indiqué ci-dessous. |
| Sorties transversales | Contrats Matroska, callbacks UI/CLI, `TaskSignals`, cancel et fermeture. | Candidat conservé pendant décision, sortie ancienne préservée sur rejet, commit atomique, NFO après commit, arrêt des workers et nettoyage sur les chemins d'erreur. |

## Validation effectuée

- Suite complète hors `tests/integration` : **3 022 réussis, 5 ignorés**.
- Complément après durcissement du comptage final Merge : **72 réussis**
  (workflows/panneau Merge et sous-processus, dont une nouvelle régression).
- Intégration sur médias réels : **162 scénarios validés**, couvrant conteneurs,
  sous-titres, statistiques, pistes sélectionnées, assemblage, Merge DoVi et
  passage forcé. La batterie initiale en validait 148 ; 12 scénarios Remux/Encode
  de décision et 2 scénarios Merge ont été ajoutés.
- Vérification complémentaire finale : **204 réussis**, incluant les 6 E2E
  Merge, FrameCountGuard, unification du muxage et interpolation.
- `mypy` : **aucune erreur dans les 24 modules ciblés**, avec analyse de leurs
  imports. Compilation Python et `git diff --check` sans erreur.

Les nombres des suites ciblées se recoupent avec les suites complètes ; ils
ne s'additionnent pas comme des tests uniques.

Les 18 cas d'intégration ignorés concernent les parcours dépendants de GPU/RIFE
ou d'une activation matérielle explicite. Les chemins de commande sont relus
et couverts par les tests unitaires, mais l'exécution sur GPU réel, Windows et
macOS n'est pas validée par cette session Linux. Les échantillons synthétiques
et le corpus Matroska ne couvrent pas tous les fichiers endommagés possibles.
