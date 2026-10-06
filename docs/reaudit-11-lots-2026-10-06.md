# Relecture des onze lots — 6 octobre 2026

Périmètre : les dix commits de `47ec2a1` à `c627bca`, puis les changements du lot 11 présents dans l'arbre de travail. Application **4.2.2**, RIFE **1.2.3**. Référence : [plan amendé](plan-implementation-audit-complet-2026-10-06.md), [audit initial](audit-complet-projet-2026-10-06.md), [réaudit SeekHead et A04](reaudit-lot7-seekhead-2026-10-06.md).

La suite initiale était verte (**3 995 réussis, 83 ignorés**). La relecture a néanmoins identifié les défauts ci-dessous. Ils sont corrigés dans le dernier lot, avec des tests de comportement supplémentaires. Les limites de validation et du verrouillage externe figurent séparément.

## Défauts identifiés et corrections

P1 : préservation des données et concurrence. P2 : comportement ou robustesse. P3 : documentation et maintenance. Toutes les lignes R11 ci-dessous sont corrigées localement.

| ID | Point identifié | Symptômes | Impacts | Correction et vérification |
|---|---|---|---|---|
| R11-01 | **P1 · A01 — Réservation réentrante et nettoyage sur exception.** `core/output_commit.py`, `core/file_lock.py`. | Une seconde réservation dans le même processus, avec un autre dossier de verrous, appelle `release()` sous le mutex que cette méthode reprend. Une erreur pendant l'acquisition peut aussi retenir un verrou/descripteur. | Blocage du processus ; destination restant occupée après une erreur. | Libération hors mutex, rollback de toute acquisition partielle, fermeture du descripteur dans `finally`. Reproduction du blocage en processus séparé, puis test de reprise après erreur du second verrou. |
| R11-02 | **P1 · A01 — Réservation perdue après changement de lien symbolique.** `core/output_commit.py`. | La publication recherche la réservation en recalculant le chemin réel ; un lien repointé produit une autre clé et emprunte le remplacement sans protection. | Remplacement d'une destination modifiée pendant le traitement. | Index stable par chemin fourni, verrouillage des deux identités, contrôle de la cible et de l'entrée du répertoire. Test : lien repointé, destination et candidat préservés. |
| R11-03 | **P1 · A01 — Repli sans liens physiques non atomique.** `core/output_commit.py` et producteurs de candidats. | Un contrôle `lexists()` suivi de `os.replace()` laisse une fenêtre pour la création d'un fichier externe. | Écrasement malgré une sortie initialement absente. | Repli atomique sans remplacement (`renameat2` Linux, `renamex_np` macOS). Si indisponible, refus avec conservation du candidat par les trois producteurs. Tests : création concurrente, publication sur repli, conservation du résultat natif après refus. |
| R11-04 | **P1 · A06/A16 — Résolution du nom de profil et écriture non sérialisées.** `core/profile_store.py`. | Deux instances peuvent choisir le même fichier libre pour deux noms normalisés identiques, avant leurs écritures atomiques respectives. | Perte d'un des profils. | Verrou OS du dossier pendant la résolution, l'écriture et la suppression. L'autre modification est refusée sans toucher au profil ; la reprise choisit son fichier distinct. Test avec deux managers. |
| R11-05 | **P2 · A06 — Recherche CLI donnant priorité au nom de fichier historique.** `cli/profile.py`. | Un fichier `Film.json` contenant un profil nommé `film` est choisi avant le fichier correspondant au nom exact `Film`. | Mauvais profil appliqué, particulièrement sur les volumes insensibles à la casse. | Recherche du nom exact avant les chemins historiques du dossier de profils ; priorité des chemins explicites fournis par l'utilisateur conservée. Test sur des noms de fichier volontairement divergents. |
| R11-06 | **P2 · A08/A18 — Descendants conservant les pipes d'une sonde.** `core/subprocess_utils.py`, `mediamanager.py`. | Le délai seul utilise `subprocess.run`; MediaManager ne tue que le processus principal. Un descendant silencieux peut retenir stdout. | Attente dépassant le délai ou fermeture Qt retardée ; processus restant. | Capture commune pour toutes les sondes `run_probe`, groupe dédié POSIX, arrêt de l'arbre Windows ; MediaManager utilise ce helper. Tests réels : délai, annulation, parent déjà terminé, sortie volumineuse et fermeture Qt. Arrêt effectif du descendant vérifié sous Linux. Mocks Inspector/Blu-ray adaptés à la frontière `run_probe`. |
| R11-07 | **P2 · A07 — Changement d'outil pendant la session incomplet.** workflow et panneau Encodage. | Le FFprobe est fourni au constructeur mais n'est pas réappliqué après modification des paramètres. Un changement de MediaInfo conserve aussi les caches HDR ; la clé du détecteur de géométrie ne contient pas FFprobe. | Ancien outil ou ancien résultat utilisé après changement de configuration. | Setter FFprobe, rafraîchissement du panneau, invalidation après changement FFprobe/MediaInfo, clé du détecteur incluant FFprobe. Tests de changement explicite et retour à la dérivation depuis FFmpeg. |
| R11-08 | **P2 · A12/A14 — Non-fini JSON par débordement numérique.** `core/json_documents.py`, `core/profile_store.py`. | `1e999` est accepté par `json.loads` et devient infini malgré le refus de `NaN`/`Infinity`. Les profils utilisent encore un lecteur distinct. | Nombre non fini pouvant atteindre une validation ou un calcul ; incohérence entre lecteurs. | Conversion flottante vérifiant la finitude ; lecteur commun pour les profils, avec diagnostic et conservation des fichiers rejetés. Tests `1e999` et `-1e999`. |
| R11-09 | **P2 · A09/A10/A27 — Statistiques non finies du budget.** `planning/size_budget.py`. | Un débit infini est traité comme mesuré ; une résolution infinie peut produire un poids NaN ; une durée infinie passe le test `> 0`. | Cible déclarée impossible à tort ou erreur de calcul pendant l'aperçu. | Débits non finis classés inconnus, repli du poids invalide, durée non finie refusée et aperçu restant fini. Six tests dédiés. |
| R11-10 | **P2 · A19 — Plafonds arbitraires ajoutés aux options RIFE.** `main.cpp`, `engine.cpp`. | GPU limité à 255, threads à 1024, padding à 4096, seuil de scène à 1e9, intervalle à 86400 s. | Refus supplémentaire d'options valides, contraire à la contrainte du plan. | Limites liées aux types représentables ; calcul du padding en 64 bits et validation des dimensions réellement consommées par ncnn. Cinq tests de valeurs au-dessus des anciens plafonds, en facteur 1 ; parsing strict conservé. Aucun intermédiaire ajouté au pipeline direct. |
| R11-11 | **P2 · A24 — Comparaison de versions Python inexacte.** `core/python_requirements.py`. | `(6, 6)` est inférieur à `(6, 6, 0)` ; une préversion de la borne finale est considérée satisfaite. | Installation inutile ou acceptation d'une dépendance trop ancienne. | Compléter les composantes par zéro et distinguer préversion/finale, sans pénaliser les versions locales. Sept cas de comparaison. |
| R11-12 | **P2 · A21/A25 — Rapports CI ajoutés aux assets de release.** `release.yml`. | Le téléchargement de tous les artefacts inclut les nouveaux XML pytest ; ils entrent ensuite dans les sommes et la publication. | Assets publics parasites et manifeste de release pollué. | Sélection explicite des cinq artefacts livrables. Test d'exclusion des rapports et archives RIFE. |
| R11-13 | **P2 · A22 — Publication RIFE anticipant le gate applicatif.** `release.yml`. | L'appel publiant RIFE ne dépend que de son état de release. | Publication native possible alors que les tests applicatifs du même run échouent. | Dépendance du job RIFE au gate `unit-tests`. Test de la dépendance ; publication principale déjà conditionnée au gate. |
| R11-14 | **P3 · A26/A27 — Documentation et consolidation du lot 11.** README, CLAUDE et documents d'audit. | Anciens schémas exclusivement FFmpeg, tolérance Merge présentée comme implicite, deux passes limitées au chemin direct dans le texte. Budget encore concentré dans la façade. | Lecture erronée des parcours et de la politique de comptage ; maintenance plus fragile. | Schémas remplacés, règles de publication et anciennes versions documentées, extraction `planning/size_budget.py` terminée. API de compatibilité du workflow conservée. CLAUDE local actualisé sans forcer son ajout à Git. |
| R11-15 | **P2 · A23 — Mypy local différent du checkout CI.** `cadence_pitch.py`, `waveform_view.py`. | Le `mypy.ini` local est ignoré par Git et traite NumPy comme `Any`. Le contrôle sans ce fichier révèle deux affectations `None` à un nom inféré comme module. | Gate CI susceptible de bloquer malgré le succès local. | Imports optionnels annotés `ModuleType \| None`, contrôle sans configuration locale avec cible Python 3.12, puis contrôle local également vert. |

Les trois premiers défauts ont été reproduits par **trois tests échouant avant correction** : délai de trois secondes dépassé pour la seconde réservation ; absence du refus attendu pour le lien repointé ; écrasement dans le repli sans liens. La suite historique verte ne couvrait pas ces scénarios.

## Couverture de la relecture

| Lot | Commit relu | Périmètre et résultat |
|---|---|---|
| 1 | `47ec2a1` | Candidats du writer/transactions/runtime, réservations Encode/Remux/Merge, propriété et activité des workspaces, préplanification batch/profil/hybride. Renforcé par R11-01 à R11-03. |
| 2 | `02bf18a` | Stockage par nom exact, suffixes de collision, écritures atomiques et diagnostics UI. Renforcé par R11-04/R11-05/R11-08. |
| 3 | `d9d1c4a` | Résolution des chemins avant fusion, BOM, chapitres finis, énumérations partagées schéma/contrat. Renforcé par R11-08. |
| 4 | `b331401` | Propagation du FFprobe configuré dans les services d'encodage, durée des sondes, annulation et fermeture Qt. Renforcé par R11-06/R11-07. |
| 5 | `00169bb` | Cible globale, indépendance de l'ordre des pistes, provenance et partage UI. Renforcé par R11-09 ; extraction terminée au lot 11. |
| 6 | `1deafa7` | Formateur commun et références natives, round-trip POSIX. Tests cmd réels présents ; validation Windows reste à exécuter en CI. |
| 7 | `cb366cd` | Suppression partielle, arbre, compteurs et espace effectivement libéré. Aucun défaut supplémentaire identifié sur les parcours relus et testés. |
| 8 | `15a23da` | Parsing strict, produits et tailles, build natif et sanitizers. Renforcé par R11-10 ; version non publiée 1.2.3 conservée. |
| 9 | `849ac34` | Filtres, collecte, contrôles statiques et graphe de publication. Renforcé par R11-12/R11-13/R11-15. |
| 10 | `c627bca` | Source des dépendances, digests avant extraction, repli réseau, manifeste et outils préexistants. Renforcé par R11-11. |
| 11 | Changements locaux | Budget extrait, documentation du routage et suivi des constats consolidés ; corrections de relecture ci-dessus. |

Les tests des dix lots et leurs modifications ont également été relus, puis exécutés avec les nouvelles reproductions. Les usages du writer natif comme intermédiaire restent distincts des destinations finales réservées. Les garanties SeekHead et l'alignement des métadonnées restent exercés par la suite complète.

La configuration Mypy locale n'est pas versionnée. La vérification du checkout vierge a été effectuée avec `python -m mypy --config-file /dev/null --python-version 3.12 core cli ui workers main.py launcher.py` ; les deux diagnostics reproduits avant correction sont éliminés sans ajouter de configuration qui masquerait les imports NumPy en CI.

## Limites et validations restantes

| ID | Limite | Conséquence / suite |
|---|---|---|
| L11-01 | **Verrou consultatif, sortie préexistante.** Une modification externe précisément entre le dernier relevé d'état et `os.replace` n'est pas exclue par un verrou Muxiveo. | La protection couvre les jobs Muxiveo du même utilisateur et les changements détectés avant remplacement ; aucune garantie de comparaison-et-remplacement atomique face à un logiciel externe non coopératif. Cette limite est explicitée dans le README. Pour l'exclure, tous les écrivains doivent coopérer ou une politique supplémentaire de conservation/versionnement doit être définie. La publication d'une sortie initialement absente est, elle, sans écrasement atomique. |
| L11-02 | **Anciennes versions sur workdir commun.** Un marqueur sans verrou reste nettoyable pour permettre la récupération des résidus historiques. | Ne pas utiliser simultanément une ancienne et une nouvelle version sur le même workdir. Comportement volontaire du plan v2, documenté. |
| L11-03 | **Plateformes.** Validation locale Linux uniquement. | Exécuter les tests cmd.exe, msvcrt et repli macOS sur les runners natifs. Leur présence dans la CI ne prouve pas leur résultat sur ce changement. |
| L11-04 | **Publication et packaging réels.** RIFE `muxiveo-rife-v1.2.3` absent lors de la vérification GitHub ; téléchargements et build all-inclusive non exécutés ici. | Le setup ne peut obtenir cette release avant sa publication. Observer le prochain run de release et les digests/manifeste produits. Aucun push ni publication effectué pendant cette relecture. |
| L11-05 | **Versions Python.** Distrobox local : Python 3.14.6 ; CI : 3.12 et 3.10 informative ; packaging : 3.11. | Le succès local ne remplace pas les validations des versions cibles. |

## Validation finale

Toutes les commandes locales passent par `distrobox enter my-distrobox --`. Qt utilise `QT_QPA_PLATFORM=offscreen`. Le binaire testé est `/home/hydromel/.cache/muxiveo-rife-dev/build-repo/muxiveo-rife`, construit depuis les sources présentes ; `--version` : **1.2.3**, GPU principal **NVIDIA GeForce RTX 4070 Ti SUPER**.

| Contrôle | Résultat |
|---|---|
| Suite initiale avant relecture | 3 995 réussis, 83 ignorés, 270,61 s. |
| Suite native après reconstruction | **73 réussis**, aucun ignoré, 17,33 s ; lecteur sous UBSan/ASan inclus. |
| Régressions Inspector/Blu-ray et descendant | **34 réussis**, 6,29 s. |
| Suite complète, nouveau binaire explicitement sélectionné ; dernière correction d’annotations couverte ensuite par les 81 tests ci-dessous | **4 049 réussis, 63 ignorés**, 268,66 s ; ignorés : 56 cas matériels désactivés et 7 cas Windows. |
| Ruff, périmètre identique au gate CI et nouveaux tests | **Aucun diagnostic**. |
| Mypy, configuration locale puis sans celle-ci, cible Python 3.12 | **Aucune erreur**, 230 fichiers dans les deux modes ; notes existantes sur fonctions non annotées. |
| Actionlint sur les quatre workflows concernés | **Aucun diagnostic**. |
| Derniers tests ciblés après ajustement de la comparaison des versions locales | **59 réussis**, 6,26 s. |
| Cadence, synchronisation hybride et matrice après correction des annotations NumPy | **81 réussis**, 2,85 s. |
| Padding RIFE maximal (`2147483647`), test fonctionnel réel | Refus contrôlé du moteur (code 3), sans allocation géante. |
| `git diff --check` | **OK**. |

Commande de suite complète :

```bash
distrobox enter my-distrobox -- env QT_QPA_PLATFORM=offscreen \
  MUXIVEO_RIFE_BIN=/home/hydromel/.cache/muxiveo-rife-dev/build-repo/muxiveo-rife \
  python -m pytest -q -rs tests
```

Journaux locaux : `/tmp/muxiveo-reaudit-11lots-pytest.log` (avant relecture), `/tmp/muxiveo-review-native-build.log`, `/tmp/muxiveo-review-native-tests.log`, `/tmp/muxiveo-review-validated-pytest.log`, `/tmp/muxiveo-review-mypy-final.log`, `/tmp/muxiveo-review-mypy-clean-final.log`.

Références des primitives de repli : [manuel Linux renameat2 / RENAME_NOREPLACE](https://man7.org/linux/man-pages/man2/rename.2.html), [déclarations Apple renamex_np / RENAME_EXCL](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/stdio.h). Le filtrage des artefacts utilise le paramètre `pattern` de [download-artifact v4](https://github.com/actions/download-artifact/tree/v4).
