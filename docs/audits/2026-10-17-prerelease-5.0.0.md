# Audit prérelease Muxiveo 5.0.0 — 17 octobre 2026

## Décision

**Correctifs validés localement : tous les défauts confirmés de cet audit sont corrigés.** La suite finale donne **4 756 réussites, 110 cas ignorés, aucun échec ni erreur**. Le verdict initial était une publication déconseillée : une P1 sur les origines audio TS/M2TS, deux P2 sur le recalage individuel Hybrid et la compatibilité FEL avec FFmpeg 6.1, et une P3 sur la conservation du résultat dans la barre commune. Les constats historiques sont conservés ci-dessous ; leur traitement et les vérifications après modification sont consignés dans le suivi. La publication reste conditionnée à la CI du commit final et au contrôle de ses paquets.

L’audit initial porte sur la branche locale `devel-cli`, de `7ce4c925379d80d0da1589aedb8fdae64e88709a` exclu à `4f5b27ef62b6599974ad0b39164c6edc0aa6b696` inclus : **27 commits, 88 fichiers, 4 553 insertions et 677 suppressions**. L’arbre de travail était propre au départ. Les corrections qui suivent sont appliquées dans l’arbre de travail, sans commit ni publication.

Les constats ci-dessous reposent sur des reproductions. Les observations également présentes dans la base sont distinguées des régressions introduites par la branche.

## Suivi des corrections et des réaudits

Chaque modification doit être suivie d’une revue de ses appelants et des fonctions qu’elle utilise, puis de tests de comportement. Les écarts découverts pendant cette revue sont corrigés avant la validation globale.

| Sujet | État du travail |
| --- | --- |
| Origines TS/M2TS et pré-roll FFmpeg 6/8 | Corrigés ; 94 scénarios temporels et 13 intégrations associées réussissent sous chaque version |
| Calibration individuelle Hybrid | État occupé libéré avant sélection, rafraîchissement explicite, tests Qt ajoutés |
| Résultat de la barre commune | Signature du projet propriétaire mémorisée ; sondes et notifications sans changement ne l’effacent plus |
| Relance Hybrid après succès partiel | Reprise des seuls épisodes restants ; identité/configuration confirmées contrôlées, préflight de toutes les destinations |
| Migration des cadences cibles | Rôle distinct cadence/facteur dans la liste ; chargement des valeurs entières, décimales et invalides testé |
| FEL sur FFmpeg 6.1 et récent | Capacité `setparams` sondée dans le worker ; réduction SDR unique ; marquage du dernier encodeur RIFE vérifié |
| Test du transport direct ancien plugin | Précision rapide explicite pour tester le transport lorsque l’ancien GPU n’annonce pas la précision exacte |
| CI | Workflow média réel réutilisable ajouté ; publication conditionnée à son succès ; étapes média et RIFE exécutées localement |

### Correction des origines audio — première passe et réaudit

`audio_timestamps.py` centralise la lecture de l’origine commune et les arguments de compensation. La préparation retire cette origine avant `aresample`, en conservant le pré-roll négatif limité par `initial_padding`. Au mux FFmpeg final, chaque entrée reçoit sa propre compensation : origine démuxée de l’artefact audio et pré-roll des sources originales ; aucun `-copyts` global ne déplace les autres sources. Le mux natif se fonde sur les blocs Matroska et conserve le TrackEntry, indépendamment de la présentation de `CodecDelay` par ffprobe 6 ou 8.

Appelants revus : préparation physique des deux backends remux, réécriture simple/avancée et copie audio objet, remappage des pistes dans le runtime remux, assemblage encode FFmpeg et natif. Les aperçus restent sans sonde. Les sondes d’origine supplémentaires sont annulables ; la sonde de codec existante a également été rendue annulable pendant ce réaudit.

Les tests réels génèrent maintenant des TS/M2TS à origine de zéro ou de plusieurs minutes, des offsets négatif/nul/positif et des pistes volontairement tardives. Le test historique du mux final utilise le vrai résultat de `prepare_physical()` au lieu de reconstruire artificiellement un mode physique. Première validation FFmpeg 8 : les 56 scénarios d’intégration temporelle passent. Les tests unitaires qui produisaient des fichiers factices simulent désormais aussi la sonde de sortie ; la vérification des horodatages reste dans les tests sur médias réels.

Le réaudit des sources multiples a étendu la compensation au conteneur vidéo portant un audio non sélectionné, dans les deux workflows encode et remux. Deux réécritures partageant une source ne la corrigent qu’une fois. Le chemin physique utilise aussi le constructeur d’entrée Blu-ray commun : une playlist `.mpls` conserve son protocole et son numéro de playlist pendant la normalisation.

Une nouvelle passe sur les entrées auxiliaires a reproduit un écart de 5 ms avec une piste réécrite et une piste en mode offset sous FFmpeg 6. Chaque réouverture de source reçoit maintenant la même compensation de pré-roll. Le test des offsets négatifs a également exposé le rebasage global automatique du mux FFmpeg : des paquets audio précédant le seek déplaçaient toute la vidéo de 218 ms pour une coupe de 250 ms. Les constructeurs communs désactivent ce rebasage lorsqu’un offset négatif est demandé. La matrice de régression couvre sources communes/séparées, offsets ±250 ms, modes réécriture/offset, encode/remux et les deux muxers. Les 94 scénarios temporels et les 13 intégrations associées passent sous FFmpeg 6.1.4 et 8.1 récent. Les consommateurs des constructeurs d’offset ont aussi été relus : deux passes (première passe vidéo seule), reconstruction de métadonnées, NVEncC et assemblage multividéo ; la politique de timestamps est posée avant l’unique sortie finale.

### Corrections Hybrid et barre commune — réaudit des continuations

Le contrôle d’ajustement et la forme d’onde se rechargent après la fin de la calibration, même pour une ligne déjà sélectionnée. Les signaux de sélection sont suspendus pendant cette sélection explicite afin de ne lancer qu’un chargement. La revue du lancement suivant a révélé un second problème : une calibration individuelle n’était pas mémorisée comme préparation valide et pouvait être recalculée au clic sur Exécuter. La signature de préparation est désormais conservée ; un changement de paramètres invalide les anciennes préparations. Le test prolonge la calibration jusqu’au lancement et vérifie que le micro-ajustement de 126 ms est utilisé.

La même revue a trouvé des options de scan périmées réutilisées lors d’une calibration individuelle. Lot et calibration individuelle capturent maintenant leurs arguments à partir du même parseur et des widgets actuels, avec la même gestion d’erreur et le même événement d’annulation.

La reprise d’un lot conserve uniquement les jobs dont le succès a été reçu, dont la configuration est inchangée et dont le fichier est toujours identique (périphérique, inode, taille, date de modification). Les cas échec, annulation, fichier remplacé, supprimé, sortie étrangère et configuration éditée sont testés. Aucun fichier préexistant inconnu n’est ignoré.

Le réaudit de `run()` → `run_next()` a révélé que les collisions n’étaient contrôlées qu’au début de chaque épisode. Le lot vérifie désormais toutes les destinations avant de lancer le premier job, y compris les doublons via le validateur commun CLI. Le contrôle par épisode reste présent pour détecter une sortie apparue pendant le lot. Deux tests vérifient qu’une collision sur le deuxième épisode ou deux sorties identiques ne lancent aucun traitement.

Le résultat de la barre conserve une copie de la configuration de son propriétaire. Une fin de sonde d’encodeur ou une notification de disponibilité ne l’efface plus ; une modification effective du projet concerné l’invalide. Le rafraîchissement différé n’est plus repoussé indéfiniment par une succession de notifications.

### Compatibilité FEL — réaudit du pipeline complet

La présence réelle de l’option `setparams/chroma_location` est sondée pendant la préparation asynchrone, puis conservée dans `FelExecution`. Les constructeurs n’exécutent aucun outil. Sur un ancien FFmpeg, la conversion zscale et l’option de sortie `-chroma_sample_location` restent explicites ; le filtre non disponible est omis. Le tone mapping conserve une seule réduction chroma, que le marquage soit disponible ou non.

Appelants et sorties revus : images RGB du plugin ancien, formats finaux/crop du plugin récent, upload matériel, NVEncC direct NUT, transport Vulkan/CUDA et pipeline FEL → RIFE → encodeur. Cette dernière revue a révélé que le marquage devait aussi être reposé sur le dernier encodeur après le y4m RIFE. Ce chemin a été corrigé et ajouté aux assertions d’intégration, avec un test unitaire pour FFmpeg ancien et récent.

### Préparation de la CI

Le plugin récent `47643569` utilisé pendant l’audit n’est pas encore disponible sur GitHub (API : 422). Le job d’intégration ne doit donc pas dépendre de cette référence absente. La CI utilisera une référence publique épinglée pour la compatibilité avec le plugin existant ; une entrée explicite permettra de tester une nouvelle référence publiée. Les capacités récentes restent couvertes par les tests unitaires du gate et par les intégrations locales avec les bibliothèques reconstruites. Aucune publication du dépôt des extensions n’est effectuée implicitement.

Le workflow `ci-media-regressions.yml` construit et teste le plugin CPU public `855d6ca8092aa82752085ae888bc4a4700dff20f`, avec Rust 1.95.0 et les dépendances privées du plugin épinglées. Cette construction exacte a été exécutée localement : quatre tests natifs réussissent. Le plugin produit passe ensuite les 15 cas CPU FEL (HDR10, DV, SDR, crop, deux passes, chroma et PTS) sous FFmpeg 6.1 et 8.1. Le contrôle de version accepte le préfixe `n` réellement présent dans les builds BtbN.

Un job indépendant installe RIFE 1.9.0 et ses modèles, utilise Vulkan logiciel (llvmpipe) et vérifie réellement ×2 et ×2,5 avec un nombre impair d’images, la cadence, le nombre final d’images et le décodage complet, pour les muxers FFmpeg et natif : quatre tests réussis localement. Les outils téléchargés passent par la vérification SHA-256 GitHub commune ; le préflight exige les outils et périphériques requis pour éviter un succès constitué uniquement de tests ignorés.

Les nouveaux tests Qt sont inclus dans les workflows UI et Windows existants ; les nouveaux tests temporels unitaires sont collectés automatiquement sous Linux et ajoutés au gate Windows. `release.yml` exige explicitement la réussite de ce nouveau workflow média avant toute publication. Les quatre workflows modifiés passent `actionlint`. Les exécutions distantes ne peuvent avoir lieu qu’après un push.

## Régressions confirmées avant correction

Les emplacements, reproductions base/HEAD et tests manquants de cette section décrivent l’état initial au commit `4f5b27ef`. Leur résolution et les nouveaux tests sont détaillés dans le suivi ci-dessus.

### 1. P1 — L’origine absolue des TS/M2TS devient du silence audio

Emplacements : [sync_rewrite.py:805](../../core/workflows/common/sync_rewrite.py#L805), application du filtre à la ligne 823, variante avancée aux lignes 920/927 ; même combinaison dans [physical_sync.py:100](../../core/workflows/physical_sync.py#L100) et son graphe aux lignes 122–124.

Le nouveau `-copyts` conserve les PTS absolus d’entrée. Le nouveau `aresample=async=1:first_pts=0` complète ensuite par du silence tout l’intervalle entre zéro et le début de la piste. Les scanners et la vidéo assemblée utilisent une chronologie relative. Une origine technique du conteneur devient ainsi un délai audible supplémentaire.

Reproduction avec FFmpeg réel et comparaison des implémentations exactes de la base et de HEAD :

| Cas | Base | HEAD |
| --- | ---: | ---: |
| TS de 2,005 s, origine 1,434667 s, correction +125 ms : début du signal | 0,135458 s | **1,565125 s** |
| Même cas : durée de l’artefact réécrit | 2,146 s | **3,576 s** |
| M2TS de 2,005 s, origine 602,834667 s : durée de l’artefact | durée relative | **604,976 s** |

La base présente un petit écart de pré-roll ; l’ajout de plus d’une seconde, ou de dix minutes dans le second exemple, est bien nouveau. Les décalages positifs et négatifs sont concernés. En calibration physique nulle, la branche de copie AC3 conserve aussi un premier bloc à 1 435 ms au lieu d’une origine normalisée, ce qui affecte l’assemblage natif.

**Correction attendue :** retirer l’origine commune positive du conteneur avant le recalage, tout en conservant le pré-roll négatif des codecs et les retards intrinsèques entre pistes. Une normalisation de l’origine positive a été essayée hors dépôt : elle préserve le retard audio volontaire de 500 ms et le `CodecDelay` de 256 échantillons. Une remise à zéro aveugle de chaque piste ferait perdre ces retards intrinsèques.

**Test manquant :** sources TS et M2TS avec origine positive, dont une origine de plusieurs minutes ; vérifier le repère acoustique, la durée et les PTS finaux pour offsets négatif, nul et positif, avec les deux moteurs d’assemblage. Les nouveaux tests de synchronisation ne génèrent que des MKV commençant à zéro.

### 2. P2 — Le micro-ajustement Hybrid peut écraser la calibration calculée

Emplacement : [hybrid_studio.py:1556](../../ui/panels/hybrid_studio.py#L1556), sélection effectuée avant la fin de l’opération à la ligne 1557. Le nouvel état occupé est activé à la ligne 1472 ; `select_waveform()` abandonne lorsque `busy=True` à la ligne 1690.

Après une calibration individuelle à **125 ms**, la ligne est sélectionnée pendant que le panneau est encore occupé. Le chargement de la forme d’onde et l’actualisation du contrôle « Ajustement manuel » sont ignorés. Le contrôle reste à **0 ms**. Un clic sur « + » remplace alors effectivement la calibration de 125 ms par **1 ms**. Avec un seul épisode, sélectionner à nouveau la même ligne ne déclenche pas de rafraîchissement.

La reproduction a été exécutée avec les widgets Qt et le callback de calibration réels. La préparation média est remplacée par une calibration déterministe ; le code de sélection et de modification reste celui de l’application. Le même scénario avec le fichier de la base affiche correctement 125 ms.

**Correction attendue :** terminer l’état occupé avant le rafraîchissement, puis actualiser explicitement les valeurs de la ligne courante, même si elle était déjà sélectionnée. Bloquer les signaux du contrôle pendant son chargement.

**Test manquant :** calibration individuelle réussie sur un seul épisode, puis micro-ajustement ; vérifier la valeur affichée, le démarrage du chargement de la forme d’onde et la calibration réellement enregistrée après une variation de 1 ms.

### 3. P2 — Le nouveau marquage chroma casse le chemin FEL avec FFmpeg 6.1

Emplacement principal : [pipeline.py:195](../../core/fel/pipeline.py#L195), en particulier `setparams=chroma_location=…` à la ligne 197. Le même marquage intervient dans la branche d’images finales du plugin aux lignes 166–167 et dans [codecs.py:1239](../../core/workflows/encode/domain/codecs.py#L1239) pour le tone mapping.

Le filtre `setparams` de FFmpeg **6.1.4** n’accepte pas l’option `chroma_location`. Le nouveau graphe FEL échoue avec :

```text
Error applying option 'chroma_location' to filter 'setparams': Option not found
```

Reproduction sur les images du vrai plugin FEL installé et la séquence synthétique du dépôt des extensions, avec un FFmpeg 6.1.4 construit avec les filtres nécessaires. La commande échoue avec le nouveau graphe et réussit avec la conversion de la base. Ce défaut est une incompatibilité du filtre aval, indépendante du GPU. Aucun plancher de version FFmpeg ni contrôle préalable de cette option n’a été trouvé dans l’application.

**Correction attendue :** détecter les capacités de `setparams` et adapter le marquage, ou imposer explicitement un FFmpeg compatible lors de la validation avec un diagnostic avant le lancement. Conserver la signalisation chroma correcte sur les versions récentes.

**Test manquant :** compatibilité d’un vrai pipeline FEL avec une version FFmpeg ancienne acceptée par l’application ; couvrir les sorties HDR10, DV et SDR. Les tests avec le FFmpeg 8.1 du conteneur ne peuvent pas détecter cette incompatibilité.

### 4. P3 — Une sonde matérielle efface le résultat d’une autre opération

Emplacement : [main_window.py:2116](../../ui/main_window.py#L2116).

`_request_operation_refresh()` supprime toujours `_last_operation_result`. Après un échec Merge, la fin asynchrone de détection des encodeurs dans le panneau Encodage déclenche `_rebuild_preview()` puis `operation_changed`. Le bouton « Relancer la fusion » et son état d’échec disparaissent alors, sans changement du projet Merge. La reproduction est déterministe en déclenchant ce callback après l’échec ; elle explique également un échec intermittent observé dans la suite hôte.

**Correction attendue :** associer l’invalidation au propriétaire du résultat et à un changement effectif de sa configuration. Un rafraîchissement de disponibilité ou d’apparence doit préserver le résultat.

## Observations historiques, également traitées

- **Synchronisation avec FFmpeg 6 : correction incomplète.** `prepare_physical()` renvoie le mode `container`. Le nouveau `-copyts` conditionné au mode `physical` dans la commande de mux finale n’est donc pas appliqué au chemin d’exécution réel. La vidéo commence à 5 ms dans la base comme dans HEAD. Le nouveau test contourne ce comportement en reconstruisant manuellement un `RemuxConfig` en mode physique.
- **Origine du mux natif avec ffprobe 6 : correction amont annulée en partie.** Le nouveau helper compense le `start_time` négatif de pré-roll. Comparer ce helper seul à l’ancien suggère une régression, mais la comparaison complète de la réécriture et de l’assemblage donne le même repère acoustique dans la base et dans HEAD : 0,630458 s. Ce point n’est donc pas compté comme une nouvelle régression.
- **Relance d’un lot Hybrid partiellement réussi.** Le nouveau bouton propose une relance, mais le moteur repart sur le premier épisode déjà produit et refuse sa sortie existante. Le comportement moteur existait dans la base ; la nouvelle présentation le rend plus visible. Une reprise à partir du premier épisode en échec serait utile.
- **Anciennes cadences cibles non canoniques.** Un profil personnalisé avec `target_fps="60"` est désormais chargé comme facteur 60 ; seules les valeurs contenant `/` sont reconnues comme cadences. Cette représentation n’était déjà pas correctement restaurée dans la base. Préférer une donnée typée ou normaliser la cadence en fraction lors du chargement.
- **Test direct avec l’ancien plugin FEL.** Le cas d’intégration `direct_nvenc` exige le transport CUDA, alors que le nouveau défaut `fel_precision="exact"` fait volontairement choisir le CPU lorsque l’ancien plugin n’annonce pas la précision exacte GPU. Ce cas échoue dans cette configuration compatible. Avec une précision `fast` explicitement choisie, le transport direct réel et ses contrôles de sortie passent. Le test doit distinguer les capacités du plugin.

## Vérifications et périmètre

### Environnement

Validations principales dans `my-distrobox`, conformément à `CLAUDE.md` : Linux, Python **3.14.6**, PySide6 **6.11.1**, FFmpeg/ffprobe **8.1**, NVEncC et dovi_tool réels. GPU disponible : NVIDIA RTX 4070 Ti SUPER ; Vulkan expose également l’iGPU AMD et llvmpipe. Une première exécution complémentaire sur l’hôte a précédé la lecture de la consigne distrobox ; elle ne sert pas de référence pour les capacités CUDA.

Les réglages de test et les dépendances supplémentaires ont été isolés sous `/tmp`. Les suites principales utilisent aussi un cache applicatif temporaire. `pytest-qt` 4.5.0 a été ajouté uniquement dans `/tmp/muxiveo-prerelease-audit-test-deps`, car il manquait dans l’environnement. Les outils de construction peuvent avoir utilisé leur cache utilisateur habituel.

### Contrôles automatisés de l’audit initial, avant corrections

| Contrôle | Résultat |
| --- | --- |
| Ruff, périmètre identique au gate CI | Réussi |
| Mypy, `core cli ui workers main.py launcher.py` | Réussi, 250 fichiers sources |
| `git diff --check` depuis la base | Réussi |
| Suite complète avec les dépendances du gate | **4 646 réussites, 109 cas ignorés, 0 échec, 0 erreur ; 302,79 s** |
| Workflows ciblés : encode/NVEncC/P5/interpolation/sync/audio natif/Merge | 415 tests réussis |
| Interface ciblée : barre, menu, annulation, journal, panels, i18n | 228 tests réussis |
| FEL unitaire ciblé | 160 tests réussis |
| FEL réel avec plugin installé 0.1.0 | 23 cas réussis ; direct vérifié séparément avec précision explicite |
| FEL réel avec plugin HEAD construit localement, CPU | 23 cas réussis, plus recadrage DV x265/NVEncC, seek, arrêt du consommateur et annulation au démarrage |
| FEL réel avec plugin HEAD construit localement, GPU | 23 cas réussis, plus FEL GPU → RIFE réel |
| FEL final recadré en précision exacte, CPU contre RTX 4070 Ti SUPER | Pixels décompressés 10 bits identiques sur la fixture synthétique |
| Interpolation ×2,5 réelle : x264/NVEncC × assemblage FFmpeg/natif | 4 parcours réussis : 11 images source → 28 images à 60 i/s, décodage complet |
| Releases épinglées RIFE 1.9.0 / TRT 1.2.0 | Archives et SHA-256 présents pour toutes les plateformes attendues |
| Rendu de la documentation française et anglaise | Réussi avec les widgets natifs, dans `/tmp` |
| Catalogue des captures françaises | 18 empreintes vérifiées, aucune différence |
| Liens locaux des Markdown modifiés | Aucun lien manquant |

Les nombres des suites ciblées se recouvrent avec la suite générale et ne doivent pas être additionnés. La première suite complète du conteneur avait donné 4 642 réussites, 109 cas ignorés, un échec et trois erreurs : les quatre problèmes étaient dus à l’absence de `pytest-qt` (fixture `qtbot`), corrigée dans l’environnement temporaire avant relance.

### Contrôles après correction

| Contrôle | Résultat |
| --- | --- |
| Suite complète sur l’arbre corrigé final | **4 756 réussites, 110 cas ignorés, aucun échec ni erreur ; 339,85 s** |
| Horodatages réels + sync native + lot Hybrid, FFmpeg 6.1.4 | **107 réussites**, aucun cas ignoré |
| Même matrice, FFmpeg BtbN n8.1.3 | **107 réussites**, aucun cas ignoré |
| FEL CPU, plugin public épinglé, FFmpeg 6.1.4 | **15 réussites**, 12 cas GPU/RIFE exclus explicitement |
| FEL CPU, même plugin, FFmpeg BtbN n8.1.3 | **15 réussites**, 12 cas GPU/RIFE exclus explicitement |
| RIFE 1.9.0 réellement installé, llvmpipe, ×2/×2,5, deux muxers | **4 réussites**, 9 autres cas exclus explicitement |
| Interface commune et Hybrid, dont nouveau préflight des destinations | **36 réussites** |
| Réaudit final UI/Hybrid/Blu-ray avec le préflight | **118 réussites** |
| Ruff, périmètre du gate + nouveau script CI | Réussi |
| Mypy, périmètre du gate | Réussi, **251 fichiers sources** |
| Actionlint, quatre workflows modifiés/ajoutés | Réussi |
| Export réel des trois schémas CLI | Réussi |
| Liens locaux du rapport et `git diff --check` depuis la base | Réussis |

La matrice FFmpeg 6 a été rejouée avec les deux exécutables anciens en tête du PATH, y compris pour les fixtures et les outils appelés indirectement. Les premières erreurs de cette passe venaient de décodeurs/démuxeurs et de l’alias d’encodeur SRT omis dans la construction minimale locale ; ils ont été ajoutés avant la passe verte. Cette construction reste sous `/tmp/muxiveo-fixes-ffmpeg6/`, les en-têtes x264/x265 ayant été installés dans le distrobox. La construction CI du plugin utilise également une chaîne Rust isolée sous `/tmp`.

Les régressions sont conservées dans [les intégrations temporelles](../../tests/integration/test_physical_sync_timestamps.py), [les scénarios Qt](../../tests/test_unified_operations.py), [les profils d’interpolation](../../tests/test_encode_panel_widgets.py), [les contrats FEL](../../tests/test_fel.py), [les intégrations FEL réelles](../../tests/integration/test_encode_fel.py) et [les intégrations RIFE](../../tests/integration/test_encode_interpolation.py). Les sondes annulables et les aperçus sans exécution sont couverts dans [test_audio_timestamps.py](../../tests/test_audio_timestamps.py).

La revue a couvert les six fichiers `core/fel`, les changements de workflows et d’inspection/version, tous les changements `ui`, les traductions, les tests ajoutés/modifiés, les scripts de benchmark/capture et la documentation. Les vérifications portent notamment sur les contrats de capacités du plugin, la fidélité des PTS, la chroma, le RPU L5, les formats finaux, les chemins sans FFmpeg intermédiaire, la précision, la gestion des pipes, l’annulation et les transitions de la barre commune. Le test réel d’arrêt du consommateur n’a pas vérifié explicitement l’erreur native EPIPE ; celui d’annulation FEL positionne l’événement avant le démarrage.

### Limites de validation

- Les nouveaux chemins FEL ont nécessité la construction du plugin du dépôt voisin ; ils ne sont pas annoncés par le plugin 0.1.0 installé. Le plugin de validation est construit depuis `476435694818b5f16e54c5ff3b3c7cf40d937d3e`, avec le FFmpeg privé, libplacebo et glslang épinglés, et libdovi système 3.3.2. Il ne constitue pas un paquet publié à destination des utilisateurs.
- Le nouveau transport direct P010 n’a pas été exercé, faute de FFmpeg direct reconstruit correspondant. Le transport direct de l’ancien plugin a été vérifié ; les nouveaux chemins NUT/descripteur natif/PTS relatifs/recadrage et le rendu GPU exact ont été exercés. Vulkan Video n’était pas activé dans le plugin temporaire construit pour cet audit.
- La validation de précision exacte CPU/GPU porte sur les pixels de sortie finale 10 bits de la séquence synthétique disponible ; elle ne remplace pas une validation de qualité sur un corpus de contenus commerciaux FEL. Empreinte SHA-256 commune : `930a70b9100981c06b868b4a043327232b56db2f84fffac80c9aa8385ba67329`.
- Les paquets de prérelease n’ont pas été construits ni installés pendant cet audit. Les tests natifs Windows/macOS et la matrice Python 3.10/3.12 de CI n’ont pas été exécutés ici.
- Les tests longs d’interpolation et certains scénarios dépendant de médias/GPU explicitement fournis restent hors de la suite générale par défaut. Les résultats ignorés sont conservés dans le journal et le rapport JUnit.

## Reproductions et livrables locaux

Les reproductions sont dans `/tmp`, sans modification des sources applicatives :

- `/tmp/audit_workflow_timestamps.py` : comparaison exacte base/HEAD ; résultats dans `/tmp/workflow-timestamps-audit/results.json`, commandes dans `commands.json`.
- `/tmp/test_muxiveo_ui_audit.py` : calibration individuelle et perte du résultat après sonde matérielle.
- `/tmp/muxiveo-audit-baseline-calibration.py` : contrôle du comportement de calibration dans la base.
- `/tmp/muxiveo-prerelease-audit-fractional.py` et les journaux `muxiveo-prerelease-audit-fractional*.log` : interpolation réelle ×2,5.
- `/tmp/muxiveo-prerelease-audit-gate.log` et `/tmp/muxiveo-prerelease-audit-gate.xml` : suite complète de référence.
- `/tmp/muxiveo-prerelease-audit-screenshots/` : rendus de documentation de contrôle.
- `/tmp/muxiveo-audit-fel-current/` : plugins CPU/GPU temporaires et constructions de validation ; `/tmp/muxiveo-audit-fel-compat-lvgxk9ap/` : comparaison du filtre FEL avec FFmpeg 6.1.4.
- `/tmp/audit_fel_rpu_offsets.py` : conversion RPU de la base et réutilisation de HEAD identiques ; validation de l’interdiction des offsets vidéo négatifs avec copie DV.

Livrables de validation des corrections :

- `/tmp/muxiveo-corrections-verified.log` et `.xml` : dernière suite complète.
- `/tmp/muxiveo-fixes-media6-final.{log,xml}` et `/tmp/muxiveo-fixes-media8-final.{log,xml}` : matrice média du nouveau workflow, exécutée localement.
- `/tmp/muxiveo-fixes-ci-fel6-final.{log,xml}` et `/tmp/muxiveo-fixes-ci-fel8-final.{log,xml}` : plugin public CPU avec les deux versions de FFmpeg.
- `/tmp/muxiveo-fixes-rife-cpu.log` : validation RIFE sur Vulkan logiciel.
- `/tmp/muxiveo-ci-fel-public-build.log` : construction et tests natifs de la référence publique du plugin.
- `/tmp/muxiveo-fixes-last-callers-final.{log,xml}` : réaudit Qt/Hybrid/Blu-ray après le dernier préflight.

Exemples de commandes :

```bash
distrobox enter my-distrobox -- env QT_QPA_PLATFORM=offscreen \
  python3 /tmp/audit_workflow_timestamps.py \
  --repo /home/hydromel/dev/mediarecode

distrobox enter my-distrobox -- env QT_QPA_PLATFORM=offscreen \
  PYTHONPATH=/tmp/muxiveo-prerelease-audit-test-deps \
  python3 -m pytest -q -s /tmp/test_muxiveo_ui_audit.py

distrobox enter my-distrobox -- env QT_QPA_PLATFORM=offscreen \
  PYTHONPATH=/tmp/muxiveo-prerelease-audit-test-deps \
  XDG_CACHE_HOME=/tmp/muxiveo-prerelease-audit-gate-cache \
  python3 -m pytest -q -rs tests \
  -W error::pytest.PytestUnhandledThreadExceptionWarning
```

Les régressions historiques décrites dans ce rapport disposent désormais de corrections et de tests permanents dans le dépôt. Avant publication, obtenir la CI verte sur le commit final et contrôler les paquets construits depuis ce même commit, en particulier leur démarrage et leurs outils embarqués sur chaque plateforme.
