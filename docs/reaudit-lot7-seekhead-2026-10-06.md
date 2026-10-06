# Réaudit du lot 7 et du correctif SeekHead — 2026-10-06

Les corrections en attente ont été relues, complétées et vérifiées sur la base du commit `6eadddf`. Les corrections déjà apportées ont été conservées. Les cinq points RV7 initiaux sont traités ; la reprise a également corrigé trois défauts dans les cas limites de l'éditeur Matroska.

| ID | Priorité | Point identifié | Symptômes et impacts | Correction vérifiée |
|---|---|---|---|---|
| RV7-01 | P1 | Échec après des écritures ou une troncature in-place | Fichier partiellement modifié ; éléments déplacés et SeekPosition incohérentes. Le premier journal proposé pouvait aussi relire une plage déjà tronquée, partager son état entre fichiers ou laisser passer une erreur au flush final. | Journal par fichier, copie par blocs de 1 Mio, mémoire du spool limitée à 32 Mio, restauration inverse et restitution de la taille initiale. Le flush reste dans la transaction. Tests de chevauchement, troncature puis ajout, annulation, erreurs différées et isolation entre fichiers. |
| RV7-02 | P2 | Déplacement du premier SeekHead derrière les métadonnées ou derrière un second SeekHead | Ordre des SeekHead modifié ; références devenues inaccessibles aux lecteurs stricts. | Le premier conserve son offset. Compaction ou agrandissement sur place, avec déplacement borné des métadonnées vers un Void avant les Clusters. Test d'un second SeekHead à EOF et d'un second réellement décalé, avec référence et CRC valides. |
| RV7-03 | P2 | Repli Annex B absent du chemin mono-piste | Le réencodage avec copie DoVi/HDR10+ échoue sur les anciens MKV dont Tracks est indexé par un SeekHead orphelin. | Détection également appliquée à `MetadataInjectRunner`. Intégration réelle x265 : source refusée par dovi_tool en lecture directe, sortie lisible avec 12 images, profil 8.1 et 12 RPU extractibles. Les chemins multi-piste, extraction, détection DV et Merge DoVi sont relus et conservés. |
| RV7-04 | P2 | Une entrée SeekID=Tracks était considérée suffisante | Une position pointant Info, un Void ou hors segment était annoncée lisible ; le repli FFmpeg n'était pas choisi. | Validation de l'ID réellement pointé, de la taille connue et des limites du segment ; parcours des SeekHead chaînés avec protection contre les cycles. Tests des références erronées. |
| RV7-05 | P2 | Protection Windows limitée à l'analyse argv | `&` et autres opérateurs interprétés par cmd ; guillemets, `%`, `!` et antislashs terminaux pouvaient modifier les arguments. Les commentaires POSIX étaient inexécutables sous cmd. | Échappement adapté à cmd et MSVCRT, fragments protégés, antislashs terminaux doublés, commentaires REM. Tests de formatage et d'analyse ; tests d'exécution cmd préparés pour Windows, encore non exécutés dans cet environnement Linux. |
| RV7-06 | P2 | Création d'un premier SeekHead partiel dans un Void quelconque | Sur un fichier initialement sans index, ajouter Tags pouvait créer un SeekHead ne listant que Tags et masquer Tracks aux lecteurs stricts. | Création uniquement dans un Void en tête, après un éventuel CRC, avec index de toutes les métadonnées level-1 connues. Sinon, le fichier reste sans SeekHead ; une relocalisation de Tracks après les Clusters sans index est refusée et annulée. Tests avec et sans Void en tête. |
| RV7-07 | P2 | Resynchronisation des entrées de Clusters par leur rang | Un second SeekHead ne référençant que le deuxième Cluster était réécrit vers le premier ; navigation incorrecte. | Les Clusters ne bougent pas pendant ces éditions : leurs SeekPosition sont conservées. Test d'un index partiel et comparaison des octets des Clusters. |
| RV7-08 | P2 | Reconstruction du premier SeekHead sans compaction exploitable ni conservation du CRC | Refus d'ajout malgré des SeekPosition paddées suffisamment compactables ; CRC retiré lors de l'agrandissement. | Compaction utilisable sans déplacement, absorption d'un reliquat d'un octet dans le VINT, conservation et recalcul du CRC. Tests des uint paddés et des CRC des deux SeekHead. |

## Vérification

- Suite complète dans `my-distrobox` : **3 837 réussis, 63 ignorés**, en 243,40 s.
- Complément final après ajout du test du second SeekHead décalé : éditeur et échappement, **46 réussis, 2 ignorés**.
- Intégration SeekHead/DoVi avec contrôles renforcés en sortie : **2 réussis**.
- Ruff sur tous les fichiers Python modifiés et les nouveaux tests : OK. `git diff --check` : OK.
- Analyse supplémentaire de l'échappement Windows avec mslex 1.3.0, installé uniquement dans `/tmp` pour la vérification : **2 010 arguments**, dont 2 000 aléatoires, sans échec de restitution dans les analyses MSVCRT/UCRT.

Les deux tests nécessitant un cmd.exe réel sont ignorés sous Linux. L'analyse des arguments ne remplace pas cette validation native, notamment pour les pipelines et l'expansion des variables. Le correctif Windows doit encore passer ces tests sur Windows.

Ces résultats décrivent la validation initiale du lot RV7, sur la base de `6eadddf`. Le dernier complément de ce lot modifiait les tests et la documentation ; aucune modification du code de production ne suivait alors la suite complète. Aucun autre défaut bloquant n'avait été identifié dans les changements RV7 relus. Lors du complément transversal ci-dessous, le code de ces corrections est intégré dans l'état `e1ed1c2`.

## Complément — vérification d’A01, A03 et A04 de l’audit transversal

À la demande de revue des constats de l’[audit complet](audit-complet-projet-2026-10-06.md), les chemins réellement employés par les jobs ont été retracés sur `e1ed1c2`. Le [plan d’implémentation complet](plan-implementation-audit-complet-2026-10-06.md) prend en compte ces conclusions et couvre les ID A01–A27.

| ID | Conclusion révisée | Preuve et périmètre réel | Correction / suite |
|---|---|---|---|
| A01 | **Confirmé pour le candidat final ; préparation déjà isolée.** | Chaque job crée bien son propre ProcessWorkDir avec `mkdtemp`. Une reproduction créant deux dossiers pour la même sortie absolue donne deux workspaces distincts. Le plan réel de RemuxWorkflow place pourtant le candidat dans `/exports/result.mkv.partial`, hors de ces dossiers. Encode et writer natif utilisent également `output + '.partial'`. | Conserver les workspaces actuels. Prévoir un candidat unique réservé dans le dossier de destination, avec propriété de nettoyage et réservation de la sortie. Le défaut concerne les jobs vers une même destination ou un candidat préexistant ; les jobs vers des destinations distinctes ne partagent pas ce fichier. |
| A03 | **Retiré des défauts du flux TMDB courant ; compatibilité ancienne facultative.** | Les nouvelles covers sont téléchargées sous `process_work_dir/attachments` en remux FFmpeg et encode ; le remux natif utilise un sous-dossier de son propre workspace. La reproduction initiale portait sur l’exception de nettoyage d’un dossier nommé `tmdb_covers` à la racine, pas sur ces téléchargements. | Aucun changement du stockage des nouvelles covers. Requalifier l’ancienne exception en amélioration P3 facultative : retirer ou borner le nettoyage par simple nom après revue de la compatibilité des caches historiques. |
| A04 | **Confirmé en usage fichiers du binaire, corrigé dans les sources. Pipeline direct conservé.** | Avant correction, `muxiveo-rife -i même.y4m -o même.y4m --factor 1` ramène une fixture de 46 à 0 octet et échoue en code 2. Le pipeline applicatif passe les images par stdin/stdout et n’emploie pas ces chemins fichiers ; il fonctionne normalement. | Refus en code 1 quand `std::filesystem::equivalent` reconnaît le même fichier, avant toute ouverture en écriture et avant Vulkan. Protection également vérifiée pour chemins relatifs, symlinks et hardlinks. Les quatre modes E/S directs sont conservés. |

### Chemins de fichiers vérifiés

| Élément | Remux FFmpeg | Encode | Remux natif |
|---|---|---|---|
| Workspace de préparation | Dossier unique sous le workdir racine | Dossier unique sous le workdir racine | Dossier unique sous le workdir racine |
| Nouvelle cover TMDB | `<job>/attachments/<cover>` | `<job>/attachments/<cover>` | `<job>/<sous-dossier-canonique>/attachments/<cover>` |
| Candidat final pour `/exports/film.mkv` | `/exports/film.mkv.partial` | `/exports/film.mkv.partial` | `/exports/film.mkv.partial` |

Références : [plan remux](../core/workflows/remux_plan.py#L1011), [runtime remux](../core/workflows/remux_runtime.py#L111), [préparation encode](../core/workflows/encode/runtime/preparation.py#L147), [cover encode](../core/workflows/encode/runtime/preparation.py#L215), [cover native](../core/workflows/remux_backend.py#L316), [transaction encode](../core/workflows/common/matroska_finalize.py#L302), [writer natif](../core/matroska/writer.py#L507).

### Correctif A04 et vérifications

La modification de [main.cpp](../native/muxiveo-rife/src/main.cpp) ajoute seulement une validation de l’identité des fichiers : les entrées/sorties `-` gardent le chemin pipe, les fichiers distincts gardent l’ouverture et le traitement directs existants. Aucun fichier intermédiaire, copie de trames ou changement de routage n’est ajouté. `--version` et `--list-gpus` conservent leur comportement. Les versions de `CMakeLists.txt` et `core/version.py` sont portées ensemble à **1.2.2** pour la prochaine livraison du binaire.

| Vérification | Résultat |
|---|---|
| Compilation native CMake/Ninja depuis les sources du dépôt | **Réussie**, avec le checkout ncnn épinglé `20260526`. |
| Même fichier sous le même nom, par chemin relatif, hardlink ou symlink | **4 tests réussis** ; code 1 et octets d’entrée/sortie préservés. Pilote Vulkan volontairement absent : le refus précède bien l’initialisation GPU. |
| Fichier → fichier distinct, stdin → fichier, fichier → stdout, stdin → stdout | **4 tests réussis** ; cadence et trames originales conservées en facteur 1, source intacte. |
| Suite `tests/native`, avec le nouveau binaire explicitement sélectionné | **27 réussis**, en **15,01 s**, sans test ignoré. |
| Tests applicatifs ciblés : interpolation domaine/intégration, packaging, release, Homebrew, setup/config et NVEncC setup | **321 réussis**, en **7,87 s**, sans test ignoré. L’intégration d’interpolation ×2 passe avec les backends FFmpeg et natif. |
| Ruff sur la version et les tests natifs modifiés/ajoutés | **OK**. |
| `git diff --check` | **OK**. |

Commandes de tests, exécutées dans `my-distrobox` :

```bash
MUXIVEO_RIFE_BIN=/home/hydromel/.cache/muxiveo-rife-dev/build-repo/muxiveo-rife \
  python -m pytest -q -p no:cacheprovider --noconftest tests/native

QT_QPA_PLATFORM=offscreen \
MUXIVEO_RIFE_BIN=/home/hydromel/.cache/muxiveo-rife-dev/build-repo/muxiveo-rife \
  python -m pytest -q \
  tests/test_encode_interpolation.py \
  tests/integration/test_encode_interpolation.py \
  tests/test_package.py tests/test_release_workflows.py \
  tests/test_homebrew_formula.py tests/test_setup_and_config.py \
  tests/test_setup_nvencc.py
```

Journaux dans le `/tmp` du distrobox : `muxiveo-reaudit-a04-build.log`, `muxiveo-reaudit-a04-native-tests.log` et `muxiveo-reaudit-a04-app-tests.log`.

Le correctif et les tests restent locaux. Le binaire installé 1.2.1 n’a pas été remplacé et aucune release n’a été publiée. Les tests du complément utilisent le build 1.2.2 ; la suite complète de l’audit initial (**3 838 réussis, 63 ignorés**) reste un résultat antérieur à ce correctif, distinct des validations ciblées ci-dessus. Les cas fichiers doivent encore être exécutés nativement sur Windows/macOS via la CI du nouveau build.

## Contre-vérification du réaudit — 2026-10-06 soir

Relecture des affirmations ci-dessus sur l’état `cb26966` (application 4.2.2, A04 commité en `3f96613` et poussé sur `origin/devel-cli`).

| Point | Résultat | Suite donnée |
|---|---|---|
| RV7-01 à RV7-08 | **Confirmés par lecture.** Journal `_WriteJournal` par fichier (copie par blocs de 1 Mio, spool mémoire de 32 Mio, troncature restaurée), `strict_demuxer_reads_tracks` employé par Encode mono/multi-piste, injection, géométrie DoVi, détection DV et Merge DoVi. Tests présents et verts. | Aucune. |
| A01 | **Confirmé, périmètre plus large que décrit.** Outre les trois sites cités, le backend natif (`core/workflows/remux_backend.py`, variable `partial`) supprime `output + '.partial'` en cas d’échec ou d’annulation, même s’il ne l’a pas créé ; `MatroskaWriter` produit aussi les sorties finales d’Encode (mux natif) et de Merge DoVi. `MatroskaOutputTransaction.execute` supprime le candidat avant de tester l’annulation. | Réservation du candidat dans `MatroskaWriter` et `MatroskaOutputTransaction` (plan v2, lot 1). |
| A03 | Confirmé : aucun écrivain courant sous `<workdir>/tmdb_covers` ; seule l’exception de nettoyage par nom subsiste. | Bornée aux racines marquées (lot 1). |
| A04 | Correctif confirmé dans les sources et le build 1.2.2 (`~/.cache/muxiveo-rife-dev/build-repo/muxiveo-rife`) : 4 tests d’identité réussis. **Défaut de test :** `tests/native/test_rife_file_safety.py` s’exécutait avec n’importe quel binaire trouvé dans le PATH ; avec le binaire installé du poste (**1.2.0**, et non 1.2.1), la suite complète échouait (code 3, Vulkan indisponible, au lieu du refus attendu). | Test conditionné à `--version ≥ 1.2.2`. |
| « Le correctif et les tests restent locaux » | **Périmé** : commités (`3f96613`) et poussés avec le passage en 4.2.2 (`cb26966`). La release `muxiveo-rife-v1.2.2` n’existe pas encore : le setup ne peut pas installer la version épinglée avant le prochain `release.yml`. | Noté dans le plan (lot 0). |
| Compteurs de suite | 3 837 (validation RV7) puis 3 838 (audit initial, après ajout du test du second SeekHead décalé) : cohérents. | — |

Suite complète après conditionnement du test natif, binaire installé 1.2.0 : **3 842 réussis, 67 ignorés** en 251 s (dont les 4 tests d’identité RIFE ignorés faute de binaire ≥ 1.2.2 dans le PATH). Avec `MUXIVEO_RIFE_BIN` pointant le build 1.2.2 : 4 réussis.


## Clôture après les onze lots — 2026-10-06

La relecture des dix commits suivants et du lot 11 local est consignée dans [le rapport final](reaudit-11-lots-2026-10-06.md). Ce complément actualise les états précédents sans réécrire les résultats historiques.

| Point | État final local | Validation / limite |
|---|---|---|
| RV7-01 à RV7-08 | Logique SeekHead et journal de restauration conservés. | Régressions couvertes par la suite complète finale du rapport. |
| A01 | Candidats uniques, réservations par destination, publication renforcée lors de la relecture. | Tests de concurrence, lien repointé, repli sans liens, interruption et préservation du résultat. Verrou consultatif : limite externe entre relevé d’état et remplacement explicitée dans le rapport. |
| A02 / A03 | Dossiers actifs exclus du nettoyage ; exception legacy TMDB limitée aux racines possédées. | La coexistence simultanée avec une ancienne version dépourvue de verrous reste hors périmètre. |
| A04 | Protection entrée/sortie conservée ; transfert FFmpeg → RIFE → encodeur toujours direct. | Build local **1.2.3**, suite native **73 réussis**, sans test ignoré. Aucun fichier intermédiaire ajouté au pipe. |
| A19 | Parsing numérique et tailles sûrs ; plafonds arbitraires retirés au profit de contrôles de représentabilité et des dimensions réellement utilisées. | Lecteur UBSan/ASan et options au-dessus des anciens plafonds testés. Release 1.2.3 absente lors de la vérification GitHub, aucun push/publication ici. |
| A26 / A27 | Documentation et extraction du budget terminées. | README, CLAUDE local ignoré par Git, audit et plan consolidés. Résultats complets et réserves dans le rapport final. |
