# Proposition : une barre d’exécution commune

Maquette interactive : [index.html](index.html). Les données et traitements y sont simulés. Cette proposition a servi de référence à l’[intégration dans l’application native](../../execution-unifiee.md).

Aperçus : [choix de l’opération](apercu-choix.png) et [hybridation en cours](apercu-execution.png).

## Comportement recommandé

Trois opérations sont exposées : le projet média partagé entre remux et encodage, Hybrid Studio, et Merge DoVi / HDR10+. La barre existante conserve sa place au-dessus du journal. Les boutons de lancement et d’annulation propres à Hybrid et Merge sont remplacés par cette commande commune. Les actions de préparation, d’analyse, d’écoute et d’export restent dans leurs panneaux.

| Contexte | Action principale |
| --- | --- |
| Remux ou encodage, projet en copie | **Remux & Encode** |
| Remux ou encodage, traitement vidéo configuré | **Remux & Encode** |
| Hybrid Studio | **Hybrider 8 épisodes**, ou **Hybrider le fichier** selon le lot |
| Merge DoVi / HDR10+ | **Fusionner DoVi / HDR10+** ; adapter le texte aux métadonnées réellement sélectionnées |
| Accueil, paramètres ou extensions | **Choisir une opération** : ouvre le menu des opérations prêtes |
| Aucune opération prête | Commande désactivée et indication de configuration requise |
| Panneau actif incomplet | Action associée désactivée, avec la raison à gauche |
| Une opération en cours | Libellé de l’opération en cours, progression et **Annuler l’opération** |

Le clic principal lance toujours l’opération du panneau actif. Sur les deux panneaux du projet média, la décision encode/remux reste celle du code actuel ; ils ne deviennent pas deux lancements concurrents.

La petite flèche apparaît lorsqu’une autre opération est prête. Son menu montre le verbe d’action, le projet concerné et l’état « Prêt ». Cliquer une entrée affiche le panneau correspondant et lance l’action indiquée. Les validations et confirmations de remplacement existantes continuent de s’appliquer. Le choix n’est pas mémorisé comme remplacement du contexte actif.

Si le panneau actif est incomplet et qu’un autre projet est prêt, seule la partie principale est désactivée : la flèche reste utilisable. Avec une seule opération prête dans le panneau actif, la flèche disparaît.

Une seule opération s’exécute à la fois. La navigation reste possible ; la barre, le journal et l’annulation continuent de suivre l’opération démarrée. Changer de panneau n’en change pas la cible. Une annulation pendant Hybrid arrête le traitement en cours et empêche le lancement des épisodes suivants. L’annulation des outils Merge passe par le workflow qui les possède.

## Journal et progression

Le journal commun s’ouvre au démarrage. Il reçoit les messages d’analyse, de préparation, de synchronisation physique, d’extraction, d’injection et d’assemblage, puis le résultat final ou l’erreur. Chaque message est associé à l’opération et, pour Hybrid, à l’épisode. Changer de panneau préserve l’historique.

Le suivi automatique affiche les nouvelles lignes tant que l’utilisateur reste en bas. Remonter dans l’historique suspend ce suivi ; **Reprendre le défilement** le réactive. Le journal garde ses niveaux et sa limite de lignes. Les pourcentages répétitifs restent dans la progression, avec des points d’étape lisibles dans le journal ; les sorties complètes des outils alimentent le journal détaillé. La maquette montre également un filtre facultatif par opération.

Hybrid affiche le numéro d’épisode et l’étape courante. Un pourcentage d’outil est présenté comme pourcentage de cette étape, sans le faire passer pour le pourcentage du lot. Une phase sans métrique utilise une progression indéterminée. Merge affiche l’étape d’extraction, d’injection ou d’assemblage en cours.

## Points confirmés dans le code

- `ui/main_window.py` : `_on_run()` et `_on_ready_changed()` gèrent le projet média. Les signaux `log_message` de Hybrid et Merge sont déjà connectés au journal global ; cette connexion seule ne couvre pas tous les producteurs de messages.
- `ui/panels/hybrid_studio.py` : les appels de calibration créent un `cli.logging.Logger()` qui écrit sur stderr. `run_next()` crée un `RemuxWorkflow` sans relier son `log_message` au signal du panneau, puis relie seulement la progression à `status.setText`. Il manque également un cycle de vie global pour le lot.
- `ui/panels/merge_dovi_panel.py` : les événements d’étape sont déjà relayés ; certaines progressions d’assemblage sortent par `op_progress`, sans entrée dans le journal. Dans `core/workflows/merge_dovi.py`, `_run_raw()` utilise aussi `communicate()` pour certains outils, sans relayer leurs messages en direct. Il faut unifier le routage des messages utiles, en conservant la séparation avec les métriques fréquentes. Le démarrage Merge ne déplie actuellement pas le journal.
- `ui/main_window.py` : l’annulation globale s’appuie actuellement sur `_signals`. Elle doit appeler la fonction d’annulation du workflow propriétaire pour couvrir aussi Merge et le lot Hybrid.

## Intégration envisagée

Un contrôleur de la barre reçoit trois adaptateurs : projet média, Hybrid et Merge. Chacun expose l’état de préparation, le libellé, un résumé, une raison de blocage, le lancement et l’annulation. Les changements de page et les signaux d’état actualisent la barre sans lancer de validation lourde. La validation complète reste au clic.

Le contrôleur conserve le propriétaire de l’opération démarrée. Il centralise le verrou d’exécution, les transitions démarrage/fin/erreur/annulation et la progression. Un relai de journal conserve la source et transmet les événements des workers au thread Qt principal. Pour Hybrid, un adaptateur du logger de calibration et une connexion au `log_message` de chaque `RemuxWorkflow` ferment les deux ruptures identifiées.

Critères de vérification : libellé selon contexte ; projet média unique ; menu accessible au clavier ; action principale désactivée sans bloquer la flèche ; navigation pendant l’exécution ; verrou global ; fin et annulation propres ; logs des étapes Hybrid et Merge visibles ; lecture de l’historique sans saut forcé ; aucune connexion de signal doublée entre épisodes ou relancements.

## Scénarios de la maquette

Les commandes en haut permettent d’essayer plusieurs opérations prêtes, une seule opération prête, un panneau incomplet, une exécution, le journal en pause et un échec. La navigation latérale change l’action principale. Le réglage d’encodage montre le basculement du projet média entre copie et encodage. Le menu lance uniquement des simulations.
