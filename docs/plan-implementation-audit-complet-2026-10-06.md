# Plan d’implémentation — audit complet Muxiveo du 6 octobre 2026

Référence : [audit complet](audit-complet-projet-2026-10-06.md), corrigé par le [complément de réaudit](reaudit-lot7-seekhead-2026-10-06.md). Les ID A01–A27 restent stables pour suivre les changements de qualification et les corrections.

**Version 2 (amendée), 2026-10-06 soir.** Base d’exécution : `cb26966` (application 4.2.2, branche `devel-cli`, poussée), qui contient le correctif A04 (`3f96613`). La version 1 décrivait les lots sans arbitrer plusieurs choix de conception ; la section « Audit du plan v1 » liste les amendements. Ce document est tenu à jour pendant l’exécution : colonne **État** du découpage, paragraphe **Suivi** de chaque lot et tableau de couverture.

## Audit du plan v1 — amendements

| Point du plan v1 | Constat | Amendement |
|---|---|---|
| Lot 0, validation | Le test natif ajouté (`tests/native/test_rife_file_safety.py`) n’était conditionné qu’à la présence d’un binaire : la suite complète échoue sur un poste dont le `muxiveo-rife` installé est antérieur à 1.2.2 (ici **1.2.0**, et non 1.2.1). | Test conditionné à `--version ≥ 1.2.2` (corrigé). Critère ajouté : la suite complète reste verte avec le binaire installé du poste. |
| Lot 1, A01 | Inventaire incomplet : le backend natif (`remux_backend.py`) calcule aussi `output + '.partial'` et le **supprime en cas d’échec ou d’annulation**, même s’il ne l’a jamais créé. `MatroskaWriter` sert aussi aux sorties finales d’Encode (mux natif) et de Merge DoVi, ainsi qu’aux intermédiaires (squelette HEVC, réécriture de payload) placés dans les workspaces. | Réserver le candidat **dans `MatroskaWriter` et dans `MatroskaOutputTransaction`** : tous les chemins finaux et intermédiaires sont couverts. Le backend natif ne supprime plus de chemin calculé. |
| Lot 1, A01 étapes 6–7 | « Verrouiller la destination » et « porter la politique d’écrasement jusqu’au commit » sans mécanisme : un drapeau `force` n’existe pas dans les configs GUI/Encode/Merge. | Verrou inter-processus par destination (fichier de verrou dans un dossier applicatif, clé = chemin canonique + identité du fichier existant), pris au lancement, avant toute préparation. Commit « sans écrasement » si la destination n’existait pas au départ (`link`/`rename` atomiques), remplacement seulement si l’identité relevée au départ est inchangée ; sinon refus, destination et candidat conservés. Aucun drapeau supplémentaire : la confirmation GUI / `--force` CLI restent les autorisations de départ. |
| Lot 1, A02 | Le classement « actif / abandonné / inconnu » ne disait pas quoi faire des dossiers process créés par les versions ≤ 4.2.2 (marqueur sans verrou). Les exclure rendrait tous les résidus actuels non nettoyables. | Verrou OS (`flock` / `msvcrt.locking`) détenu par le processus créateur jusqu’à la suppression. Dossier marqué sans fichier de verrou = ancienne version : nettoyable comme aujourd’hui (cohabitation simultanée de deux versions sur un même workdir hors périmètre, documentée). Dossier récent sans marqueur ni verrou (création en cours) protégé par un délai de grâce. |
| Lot 1, A03 | Facultatif et peu coûteux. | Réalisé dans le lot 1 : l’exception par nom `tmdb_covers` n’est plus appliquée hors racine marquée. Aucun changement des covers courantes. |
| Lot 2 | Un UUID imposerait de modifier l’UI (combos indexés par nom) et le CLI (`--profile <nom>`), alors que l’UI garantit déjà l’unicité du nom exact. | Identité = **nom exact** stocké dans le JSON. Nom de fichier = nom normalisé historique s’il est libre, sinon suffixe stable dérivé du nom exact. Recherche, écrasement et suppression par nom exact ; anciens fichiers relus sans renommage. |
| Lot 3 | La base de résolution des chemins d’un template de batch et des imports de chapitres n’était pas précisée. | Chemins d’un document JSON (sources, pièces jointes, `chapters.import`, `output`) relatifs au dossier de ce document, résolus **avant fusion** ; chemins fournis en argv relatifs au cwd. Changement de comportement CLI documenté. |
| Lot 4, A08 | « Runner annulable » pour toutes les sondes : refonte trop large (≈ 60 appels `subprocess`). | Helper `run_probe` (timeout + événement d’annulation + arrêt de l’arbre de processus) appliqué aux sondes non bornées identifiées (inspection, HDR, MediaManager) ; les sondes déjà bornées sont conservées. |
| Lot 5, A09 | « Présenter ce réglage au niveau du fichier » : déplacer le champ hors de l’onglet Video serait une restructuration d’UI non demandée. | Champ conservé à sa place, libellé « taille du fichier (Mio) » et partagé par toutes les pistes en mode taille ; `EncodeConfig.target_size_mb` global ; valeurs divergentes refusées. |
| Lot 9 | La gate de release dépendait d’une suite non encore définie. | Nouveau workflow réutilisable `ci-unit-all.yml` (`workflow_call` + PR/push), appelé par `release.yml` et requis par la publication. |
| Lot 10, A25 | Manifeste complet de tous les outils (licences, cache par digest, builds hors ligne) : chantier au-delà des constats. | Vérification commune pour RIFE (digest GitHub), FFmpeg (fichier `checksums.sha256` publié par BtbN), MediaInfo (provenance + SHA-256 consignés) et manifeste `tools-manifest.json` écrit dans chaque bundle. Cache par digest et builds hors ligne : hors périmètre. |
| Validation plateformes | Le PC Windows de test est accessible en SSH. | Les tests cmd réels (A13, RV7-05) et les verrous Windows (A01/A02) sont exécutés sur ce poste en plus de la CI. |

## Décisions retenues après réaudit

| Point | Décision | Conséquence pour l’implémentation |
|---|---|---|
| A01 | Les workspaces de préparation sont déjà uniques. Le candidat final est toutefois créé à côté de la sortie, sous un nom partagé. | Candidat unique réservé à côté de la destination ; verrou de destination ; commit sans écrasement implicite. Workspaces inchangés. |
| A03 | Les nouvelles covers TMDB sont déjà téléchargées dans les attachments du job. | Seule l’exception de nettoyage par nom est bornée aux racines marquées. |
| A04 | Défaut confirmé pour un appel direct où entrée et sortie désignent le même fichier. Le pipeline applicatif fonctionne avec stdin/stdout. | **Conserver le pipeline direct.** Refuser uniquement l’identité entrée/sortie avant écriture et avant Vulkan. |

## Contraintes communes

- Préserver le transfert direct FFmpeg → RIFE → encodeur, les flux en mémoire et l’alignement des métadonnées par trame.
- Conserver les workspaces créés par `mkdtemp` et leurs jetons ; distinguer préparation, candidat final et destination publiée.
- Garder le candidat final sur le même système de fichiers que la destination pour permettre son remplacement atomique. Un workdir en RAM ou sur un autre volume ne doit pas casser cette garantie.
- Ne nettoyer que les fichiers détenus par le job ; une erreur, une annulation ou une seconde instance ne doit pas supprimer le travail d’un autre job.
- Préserver la règle CLI « sortie existante refusée sans `--force` » jusqu’au commit final. `--force` autorise le remplacement d’une sortie, pas une collision interne au batch.
- Conserver les contrats Matroska, les validations avant commit et le journal de restauration des éditions in-place du lot SeekHead.
- Rendre les migrations explicites et déterministes ; les valeurs invalides ne doivent pas être remplacées silencieusement.
- Aucune option compatible avec l’outil ne doit être retirée, masquée ou bornée sans validation utilisateur (les refus ajoutés portent sur des contradictions ou des entrées invalides).
- Exécuter les commandes locales dans `my-distrobox`, utiliser `QT_QPA_PLATFORM=offscreen` pour Qt et préciser le binaire natif réellement testé.
- Livrer par lots cohérents et testables, un commit local par lot (identité utilisateur, sans push ni publication). A27 accompagne les corrections concrètes ; éviter une refonte globale préalable.

## Découpage, dépendances et état

| Lot | ID couverts | Livrable | Dépendances | État |
|---|---|---|---|---|
| 0 | A04 | Protection des chemins RIFE, pipeline direct conservé | Aucune | **Réalisé et distribué** : `3f96613`, release `muxiveo-rife-v1.2.2` publiée le 2026-10-06 ; test conditionné à la version (`26c1f41`) |
| 1 | A01, A02, A03, A05 | Candidats réservés, verrou de destination, exclusion des jobs actifs, réservation des sorties batch | Aucune | **Réalisé** (`47ec2a1`) ; validation Windows native en attente |
| 2 | A06, A16 | Identité par nom exact et persistance atomique des profils | Helper d’écriture atomique existant | **Réalisé** (`02bf18a`) |
| 3 | A11, A12, A14, A15 | Chargement de documents et contrat GUI/CLI cohérents | Aucune | **Réalisé** (`d9d1c4a`) |
| 4 | A07, A08, A18 | FFprobe configuré et sondes bornées/annulables | Contrats de runner existants | **Réalisé** (`b331401`) |
| 5 | A09, A10 | Taille globale et budget avec provenance | Lot 4 pour les sondes | **Réalisé** (`00169bb`) |
| 6 | A13 | Rendu commun des commandes | Formateur encode existant | **Réalisé** (`1deafa7`) ; tests cmd réels en attente (Windows) |
| 7 | A17 | Suppressions partielles MediaManager cohérentes | Lot 4 pour la fermeture du même outil | **Réalisé** (`cb366cd`) |
| 8 | A19 | Parsing Y4M et calculs numériques sûrs | Lot 0 ; tests natifs sans GPU | **Réalisé** (`15a23da`, muxiveo-rife 1.2.3) ; release à publier |
| 9 | A20, A21, A22, A23 | CI couvrant les régressions et conditionnant les releases | Gate de release après la suite générale | **Réalisé** (`849ac34`) ; premiers runs GitHub à observer |
| 10 | A24, A25 | Installation cohérente et manifeste des outils de build | Lot 9 | **Réalisé** (`c627bca`) ; premier build all-inclusive à observer |
| 11 | A26, A27 | Documentation actualisée et extractions ciblées | Au fil des lots, consolidation finale | **Terminé localement**, avec corrections de relecture ([rapport](reaudit-11-lots-2026-10-06.md)) |

Ordre d’exécution : **1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11**. Les extractions A27 sont faites dans les lots qui les motivent (transaction de sortie : lot 1 ; écriture atomique : lot 2 ; chargement de documents : lot 3 ; sondes : lot 4 ; budget : lot 5 ; rendu des commandes : lot 6).

## Lot 0 — protéger RIFE en conservant le pipeline direct

**Périmètre :** `native/muxiveo-rife/src/main.cpp`, versions CMake/Python, documentation native, `tests/native/`.

Correctif réalisé (`3f96613`) :

1. Après parsing, avant initialisation Vulkan et avant les ouvertures de fichiers, comparer les chemins d’entrée/sortie avec `std::filesystem::equivalent`.
2. Exclure les pipes `-` de cette comparaison. Préserver `--version` et `--list-gpus`.
3. Refuser le même fichier avec le code d’usage `1`, un message indiquant de choisir une sortie distincte, et zéro modification des octets.
4. Reconnaître les chemins relatifs/absolus, symlinks et hardlinks.
5. Garder l’ouverture et le traitement directs des fichiers distincts ainsi que les trois variantes avec stdin/stdout.
6. Porter ensemble la version native CMake et la version épinglée dans `core/version.py` à **1.2.2**.

**Amendement :** `test_rife_file_safety.py` interroge `--version` et s’ignore pour un binaire < 1.2.2 ; la suite complète ne dépend plus de la version installée.

**Livraison :** release `muxiveo-rife-v1.2.2` publiée le 2026-10-06 (17:17 UTC) par le workflow existant ; le binaire installé sur le poste de développement reste 1.2.0 tant que le setup n’est pas relancé.

## Lot 1 — propriété des fichiers et réservation des destinations

**Périmètre :** `core/workdir.py`, nouveau `core/workflows/common/output_commit.py`, `matroska_finalize.py`, `core/matroska/writer.py`, `remux_plan.py`, `remux_runtime.py`, `remux_backend.py`, Encode (transaction et mux natif), Merge DoVi, `cli/batch.py`, `cli/profile.py`, `cli/hybrid.py`, `main.py`.

### A01 — candidat final et destination

1. `reserve_candidate(output)` : `mkstemp` dans `output.parent`, nom `<stem>.<aléa><suffix>.partial` (terminaison `.mkv.partial` conservée pour les post-actions). Le fichier vide créé appartient à l’appelant ; FFmpeg l’écrase (`-y` déjà présent), le writer natif l’ouvre en écriture.
2. `MatroskaOutputTransaction` : candidat réservé à l’exécution, annulation contrôlée **avant** réservation, suppression limitée à ce candidat. `MatroskaWriter.write` : même réservation. Le backend natif et le remux FFmpeg ne calculent plus de chemin `.partial`.
3. Plan/aperçu : `candidate_output` devient un motif symbolique (`<stem>.<unique><suffix>.partial`) ; aucune création à la compilation du plan.
4. `OutputReservation` : verrou inter-processus non bloquant pris au lancement des jobs Remux (FFmpeg/natif), Encode et Merge DoVi. Fichier de verrou dans le dossier de verrous de l’application (`~/.cache/muxiveo/locks`, `%LOCALAPPDATA%\muxiveo\locks`, `~/Library/Caches/muxiveo/locks`), clé = SHA-256 du chemin canonique (`realpath` + `normcase`) et, si la destination existe, de son identité (périphérique, inode). Second job vers la même cible : échec immédiat avec diagnostic. Verrou libéré à la fin réelle du job.
5. Commit : identité de la destination relevée à la réservation. Destination absente au départ → publication sans écrasement (`os.link` puis retrait du candidat sous POSIX, `os.rename` sous Windows ; repli contrôlé si le système de fichiers ne gère pas les liens). Destination présente au départ (écrasement confirmé en GUI ou `--force`) → remplacement atomique seulement si son identité est inchangée. Destination apparue ou modifiée pendant le job → refus, destination intacte, candidat conservé et signalé.
6. Annulation/échec : suppression exclusive du candidat détenu ; un `.partial` étranger, même homonyme de l’ancien schéma, reste intact.

**Critères :** les deux workspaces de préparation restent uniques ; les candidats de deux transactions sont distincts ; un `.partial` préexistant reste intact ; une transaction annulée avant départ ne change aucun fichier ; une sortie finale existante reste intacte après échec de post-action/validation ; second job vers la même destination refusé avant préparation ; destination apparue pendant le job conservée.

### A02 — jobs actifs et nettoyage

1. `create_process_work_dir` crée `.muxiveo-process.lock`, le verrouille (`fcntl.flock` / `msvcrt.locking`), puis écrit le marqueur. Le verrou est détenu par le processus jusqu’à `ProcessWorkDir.remove()` / `remove_path()` (registre interne des verrous détenus, protégé par un `Lock`) ; un processus mort libère son verrou.
2. Classement des entrées : `active` (verrou détenu par un autre processus ou par ce processus), `abandoned` (verrou libre, ou dossier marqué d’une ancienne version), `unknown` (dossier sans marqueur créé il y a moins de 120 s dans une racine marquée).
3. Nettoyage : seules les entrées `abandoned` (et, en racine marquée, les autres entrées non actives) sont proposées ; chaque suppression revérifie le verrou en le prenant pendant la suppression, ce qui ferme la fenêtre entre dialogue et clic.
4. Le dialogue de démarrage annonce séparément les jobs actifs conservés.
5. Garder les protections existantes (liens/jonctions, marqueur restauré après un nettoyage incomplet).

**Tests :** deux processus réels partageant le workdir ; job actif d’un autre processus non proposé ni supprimé ; processus tué → dossier récupérable ; ancien marqueur sans verrou nettoyable ; dossier en création protégé ; verrou Windows exécuté sur le poste de test.

### A05 — batch

1. Deux phases : préparation de tous les jobs (fusion, contrat, configuration, sortie rendue), puis exécution. La préparation n’écrit rien (pas de création de dossier ni de cover).
2. Collisions détectées sur la clé de destination (chemin canonique, casse repliée sous Windows/macOS, identité des fichiers existants) pour toutes les sorties : explicites, héritées du template, générées, rendues par `output_template`. Même règle dans `profile batch` et `hybrid`.
3. Collision = erreur de planification (`EXIT_ARGS`) avant tout traitement, indépendante de `--force`, avec indices, entrées et chemin en conflit.
4. `--continue-on-error` reste valable pour les erreurs de préparation ou d’exécution propres à un job ; le résumé conserve l’ordre et les indices.
5. Dry-run inchangé : aucune écriture de média.

**Tests :** deux sorties héritées identiques, deux sorties explicites identiques, template non discriminant, alias par lien, casse, doublon avec `--force`, destinations distinctes, erreur indépendante avec `--continue-on-error`, collision hybride.

### A03 — compatibilité ancienne

Les covers actuelles restent dans `process_work_dir/attachments`. Hors racine marquée, un dossier `tmdb_covers` n’est plus proposé au nettoyage (aucune adoption par simple nom) ; dans une racine marquée, il reste nettoyable comme tout son contenu. `relocate_tmdb_covers_to_process_dir` est conservé pour les anciens jobs qui référencent encore ce dossier.

**Suivi (2026-10-06, `47ec2a1`) :**

- Modules communs : `core/file_lock.py` (verrou OS non bloquant) et `core/output_commit.py` (`reserve_candidate`, `OutputReservation`, `publish_candidate`). Réservation appelée par `RemuxWorkflow.run`, `EncodeWorkflow.run` et `MergeDoviWorkflow.start`, libérée au signal terminal ; refus = `RemuxError` / `EncodeError` / `workflow_failed(VALIDATION)`, déjà affichés par la GUI et la CLI.
- Candidats réservés dans `MatroskaWriter.write` (toutes sorties natives, intermédiaires compris), `MatroskaOutputTransaction.execute` et le remux FFmpeg ; le backend natif ne supprime plus de chemin calculé. Le plan expose `candidate_output` sous forme de motif (`film.<unique>.mkv.partial`).
- Écart assumé : le candidat est créé avec les droits par défaut (`0o666` filtré par l’umask), pas en `0600` comme `mkstemp`, pour qu’un serveur média sous un autre compte lise la sortie publiée.
- A02 : verrou `.muxiveo-process.lock` ; délai de grâce limité aux dossiers sans marqueur de forme `mkdtemp` dans une racine marquée. Dossiers marqués sans verrou (≤ 4.2.2) : nettoyables.
- A05 : `PlannedBatchJob` + `assert_unique_batch_outputs` partagés par `batch`, `profile batch` et `hybrid`. Sans `--continue-on-error`, une erreur de préparation arrête le batch après les jobs précédents, comme avant.
- Tests : `tests/test_output_commit.py`, `tests/test_cli_batch_collisions.py`, compléments `test_workdir_ownership.py` (deux processus réels), réservation Remux/Encode/Merge. Suite complète : **3 876 réussis, 67 ignorés** (après correction du test de détection `tmdb_covers`, ajusté à A03).
- **Non fait :** exécution des verrous sur Windows réel (copie de l’arbre vers le poste de test refusée par la politique de permissions de la session). À couvrir par la CI Windows (lot 9) ou sur autorisation.

## Lot 2 — profils identifiés et sauvegardés atomiquement

**Périmètre :** `core/workflows/encode/profiles.py`, `core/profiles/decision.py`, `cli/profile.py`, `ui/panels/encode_panel/panel.py`, `ui/panels/remux_panel/`, nouveau `core/atomic_io.py`.

1. Identité = nom exact (après `strip`) stocké dans le JSON ; l’UI reste indexée par ce nom.
2. Fichier : nom normalisé historique s’il est libre ou déjà détenu par ce nom exact (comparaison sans casse) ; sinon `<normalisé>-<8 hex du SHA-1 du nom exact>.json`. Aucun renommage des fichiers existants.
3. `save` réécrit le fichier du profil de même nom exact ; `delete` et `load` ciblent ce fichier ; un homonyme normalisé n’est jamais touché. `DecisionProfileManager.path_for_name` et la recherche CLI `--profile <nom>` passent par cette résolution.
4. `atomic_write_text` déplacé dans `core/atomic_io.py` (réexporté par `workflow_store`) : candidat unique, `fsync`, `os.replace`.
5. Profils illisibles : conservés sur disque, listés dans `load_errors` (chemin + cause) ; l’UI les signale dans le journal sans bloquer les profils valides.

**Tests :** `Film/4K` et `Film:4K` coexistants et supprimables séparément ; casse ; sauvegarde répétée du même profil ; anciens fichiers relus et réécrits en place ; échec simulé avant remplacement préservant l’ancienne version ; JSON malformé diagnostiqué ; résolution CLI par nom.

**Suivi (`02bf18a`) :** `core/profile_store.py` (`ProfileStore`, `ProfileLoadError`) partagé par `ProfileManager` et `DecisionProfileManager` ; `core/atomic_io.py` réexporté par `workflow_store`. Diagnostics affichés par EncodePanel (une fois par fichier) et par l’application d’un profil décisionnel dans le panneau Conteneur. Tests : `tests/test_profile_store.py` (9 cas).

## Lot 3 — documents, chemins et contrat public

**Périmètre :** `cli/json_io.py`, `jobs.py`, `batch.py`, `chapters.py`, `contract.py`, `schema.py`, `core/workflows/workflow_store.py`, `core/workflows/remux_validation.py`.

1. `load_json` : UTF-8 avec ou sans BOM ; constantes `NaN`/`Infinity` refusées.
2. `resolve_document_paths(job, base_dir)` partagé : sources, `extra_attachments`, `chapters.import` et `output` relatifs au dossier du document. Appliqué à `--config`, `--template`, `--batch` (items) et `load_workflow`, chacun **avant fusion**. Les chemins argv (`-i`, `-o`) restent relatifs au cwd. `output_template` n’est pas un chemin.
3. `load_workflow` garde sa relocalisation interactive (basename voisin, callback) au-dessus de ce helper.
4. Chapitres : `isfinite` et `>= 0` exigés au parsing (`parse_timecode`, ffmetadata, OGM) et dans `validate_remux_config`.
5. Contrat : constantes partagées entre `contract.py` et `schema.py` (`mux_backend`, `sync_rewrite_mode`, `sync_mode`, `sync_subtitles`) ; version booléenne refusée ; test de concordance des énumérations et d’un corpus valide/invalide.
6. Codes de retour CLI inchangés ; `docs/cli/README.md` documente la règle des chemins.

**Migration :** un JSON qui comptait sur le cwd pour ses chemins relatifs doit être lancé depuis son dossier ou passer des chemins absolus/argv. Pas de réécriture automatique.

**Tests :** lancement depuis un autre cwd ; template et job dans deux dossiers ; BOM ; sources/attachments/chapitres relatifs ; NaN/infini en chaîne et en nombre ; concordance schéma/validateur ; codes de sortie.

**Suivi (`d9d1c4a`) :** `core/json_documents.py` (`read_json_document`, `resolve_document_paths`) ; chemins Windows absolus (lecteur, UNC) laissés tels quels sur POSIX pour la relocalisation GUI. `parse_timecode` refuse aussi les valeurs négatives et booléennes (erreur `Chapitres invalides`, code 2). Énumérations dans `cli/constants.py`. Documentation : `docs/cli/README.md` (règles de lecture, collisions de batch). Tests : `tests/test_json_documents.py` (31 cas).

## Lot 4 — outils cohérents, sondes bornées et fermeture

**Périmètre :** `core/subprocess_utils.py`, `core/inspector.py`, EncodeWorkflow et services HDR/metadata/multi-vidéo/NVEncC, EncodePanel, `mediamanager.py`.

### A07 — FFprobe configuré

1. `EncodeWorkflow(..., ffprobe_bin=None)` : paramètre nommé optionnel, transmis par EncodePanel (`config.tool_ffprobe`) et le script de matrice d’intégration.
2. Un accesseur unique (`_ffprobe_path()` / `bins["ffprobe"]`) remplace les dérivations `ffprobe_beside`, `_ffprobe_bin_from_ffmpeg` et `bins.get("ffprobe", …)` des services d’encodage. La dérivation depuis FFmpeg ne sert plus qu’en l’absence de valeur explicite.
3. `set_ffmpeg` (FFmpeg système pour le matériel) ne change plus un FFprobe explicite.
4. Caches de sondes indexés aussi par le binaire FFprobe.

**Tests :** FFmpeg et FFprobe dans des dossiers distincts ; `set_ffmpeg` après création ; toutes les commandes FFprobe capturées utilisent le chemin configuré ; ancien appel sans paramètre inchangé.

### A08/A18 — durée de vie des sondes

1. `run_probe(cmd, *, timeout, cancel_event=None)` : `Popen`, `communicate` par tranches, arrêt de l’arbre de processus à l’expiration ou à l’annulation, exceptions distinctes `ProbeTimeout` / `ProbeCancelled` (outil absent = `FileNotFoundError` inchangé).
2. Appliqué à l’inspection (FFprobe, MediaInfo), aux sondes HDR/metadata sans timeout et à la sonde MediaManager. Délais par classe : inspection 120 s, sondes ciblées 60 s ; les sondes déjà bornées sont conservées.
3. `FileInspector(cancel_event=…)` et `HdrMetadataProbeService` acceptent un événement d’annulation ; la fermeture de la fenêtre le déclenche.
4. MediaManager : `MediaInfoThread` utilise `run_probe` avec l’événement d’interruption ; `closeEvent` diffère la fermeture tant que les threads tournent (même schéma `defer_close` que l’application principale).

**Tests :** faux outil suspendu (timeout), annulation pendant l’attente, sortie volumineuse sans blocage, aucun processus restant, fermeture MediaManager pendant une sonde.

**Suivi (`b331401`) :**

- A07 : `EncodeWorkflow._ffprobe_path()` et `bins["ffprobe"]` remplacent les dérivations `ffprobe_beside` / `_ffprobe_bin_from_ffmpeg` des services d’encodage ; `HdrMetadataProbeService(ffprobe_bin=…)` et `clear_probe_caches()` (invalidation plutôt que clé de cache par binaire). Deux sondes HDR utilisaient `ffprobe` du PATH (`_tool_bin("ffprobe")`) : corrigées.
- A08 : `run_probe` (`core/subprocess_utils.py`) s’appuie sur `run_cancellable_capture` quand un événement d’annulation est fourni, sinon sur `subprocess.run(timeout=…)` (comportement existant des tests et des appels CLI). `FileInspector(cancel_event=…, probe_timeout_s=120)`, exception `InspectionCancelled`. Sondes HDR : MediaInfo JSON borné à 120 s ; expirations traitées comme une absence de résultat sans mise en cache. Annulation branchée sur la fermeture de `FileInspectorWidget` et du panneau Conteneur ; les sondes HDR de l’Encodage restent bornées par leur délai (20–120 s), sans annulation dédiée.
- A18 : sonde MediaInfo interruptible locale (MediaManager reste autonome) et `closeEvent` différé par `QTimer`.
- Tests : `tests/test_encode_ffprobe_config.py`, `tests/test_probe_lifecycle.py` ; suite complète **3 931 réussis, 67 ignorés**.

## Lot 5 — taille globale et budget justifié

**Périmètre :** `EncodeConfig`, `EncodePreset`, EncodePanel, `_size_budget`, validation et résumé.

1. `EncodeConfig.target_size_mb: int | None` (Mio) : taille du fichier complet. Quand elle est absente, les pistes en mode taille doivent porter la même valeur ; des valeurs divergentes sont refusées (plus de choix silencieux de la première piste).
2. UI : le champ taille reste dans l’onglet Video, libellé taille du fichier en Mio, et sa valeur est partagée par toutes les pistes en mode taille ; la config porte la valeur globale.
3. Budget détaillé : chaque poste porte débit, durée et provenance (`mesuré`, `estimé`, `inconnu`). Copie sans débit → `inconnu` + avertissement ; FLAC → débit source `estimé` ; qualité constante → `estimé`.
4. Blocage uniquement si les coûts **mesurés** dépassent la cible ; avertissement pour les estimations et les inconnues.
5. Messages en Mio ; la permutation des pistes ne change pas la cible.

**Tests :** cibles divergentes refusées ; permutation ; copie sans BPS ; PCM→FLAC non bloquant ; audio avec perte mesuré ; profils anciens.

**Suivi (`00169bb`) :** `_SizeBudget.measured_bps` / `estimated_bps` / `unknown` ; blocage limité aux postes mesurés ; FLAC compté au débit source comme estimation haute (taille finale plutôt inférieure à la cible). Divergence sans valeur globale : refus à la validation, aperçu calculé sur la plus petite valeur (indépendant de l’ordre). UI : unité affichée « Mio » (les calculs étaient déjà en 1024²) ; un profil chargé applique sa taille à toutes les pistes. Tests : 6 cas `test_audit_a09_*` / `test_audit_a10_*`, partage UI dans `test_encode_panel_widgets.py` ; suite complète **3 938 réussis, 67 ignorés**.

## Lot 6 — commandes copiables communes

**Périmètre :** nouveau `core/command_preview.py`, `core/workflows/encode/planning/preview.py` (réexport), `core/workflows/remux_command.py`, `core/workflows/remux.py`.

1. Déplacer le formateur POSIX/cmd vers `core/command_preview.py` ; `planning/preview.py` le réexporte.
2. `preview_remux_command` et les préparations natives utilisent ce formateur et `preview_comment` (commentaires `REM` sous Windows).
3. Les placeholders (`<chapitres.ffmetadata>`) restent signalés par un commentaire comme non exécutables.

**Tests :** chemins avec espaces, apostrophes, `%`, `!`, `&`, guillemets et antislashs ; round-trip POSIX `/bin/sh` ; tests cmd réels sur le poste Windows ; aperçu natif et FFmpeg.

**Suivi (`1deafa7`) :** `core/command_preview.py` ; `preview_remux_command` et `RemuxWorkflow.preview_command` (backend natif) l’utilisent. L’export argv existait déjà côté CLI (`execution_preview` : champ `command`). Tests : 5 cas ajoutés à `tests/test_encode_preview_quoting.py`, dont un round-trip `/bin/sh`. **Non fait :** exécution des deux tests cmd.exe réels (ignorés sous Linux ; copie vers le poste Windows refusée dans cette session) — à couvrir par la CI Windows (lot 9).

## Lot 7 — MediaManager après suppression partielle

**Périmètre :** `confirm_delete_item`, `full_data`, `tree_items`, compteurs et statut.

1. Résultat par fichier : supprimé, déjà absent, échec.
2. Retirer de l’arbre et de `full_data` seulement les chemins supprimés ou absents ; garder visibles les échecs.
3. Recalculer agrégats et espace libéré à partir des suppressions effectives.
4. Retirer un groupe seulement s’il est vide ; signaler les échecs dans le statut.

**Tests :** suppression partielle d’un groupe de deux fichiers (100 et 200 octets, échec sur le second) : 100 octets annoncés, fichier de 200 octets visible ; fichier absent ; groupe entièrement supprimé.

**Suivi (`cb366cd`) :** logique « fichier unique » extraite (`_remove_file_item`) et appliquée fichier par fichier en cas d’échec ; chemin sans erreur inchangé. Tests : 3 cas ajoutés à `tests/test_mediamanager.py` (film, saison, fichier déjà absent).

## Lot 8 — lecteur Y4M et calculs natifs sûrs

**Périmètre :** `y4m.cpp`, `y4m.h`, options numériques de `main.cpp`, tests natifs.

1. Parsing strict des jetons `W`, `H`, `F` (consommation complète, plage, finitude) ; bornes : dimensions 1–32768, surface ≤ 2²⁸ échantillons de luma, cadence numérateur/dénominateur > 0 et ≤ 2³¹.
2. Tailles chroma et octets calculés en 64 bits après validation des bornes.
3. Options numériques de `main.cpp` (`--factor`, `--fps`, GPU, TTA…) parsées strictement.
4. Test C++ du lecteur compilé par pytest avec `g++ -fsanitize=undefined,address` (sans ncnn ni Vulkan), ignoré sans compilateur.

**Tests :** `W2147483647 H2`, zéros/négatifs, suffixes, hors plage, cadence invalide, en-tête tronqué, formats 420/422/444 8/10 bits ; suite native complète avec le nouveau build.

**Suivi (`15a23da`) :** `src/numparse.h` (entiers/réels stricts, produit vérifié) ; bornes `Y4M_MAX_*` dans `y4m.h` ; rapport de cadence borné à 31 bits (sinon `EXIT_USAGE`). `--threads 0` reste accepté (comportement antérieur). La release 1.2.2 étant déjà publiée, la modification du code natif impose **1.2.3** (CMake + `MUXIVEO_RIFE_VERSION`) : la release sera produite par `release.yml` au prochain push, non effectué ici ; d’ici là, le setup ne trouvera pas `muxiveo-rife-v1.2.3`. Le binaire vérifie le chargeur Vulkan avant de lire le flux : les tests de refus du binaire exigent un chargeur (lavapipe en CI). Validation : build 1.2.3 depuis les sources, `tests/native` **68 réussis** (dont 26 cas du lecteur sous UBSan/ASan), tests applicatifs liés (interpolation ×2 réelle, packaging, release, setup) **322 réussis**.

## Lot 9 — CI et gate de publication

**Périmètre :** `.github/workflows/`, configuration Ruff/Mypy, tests de cohérence des workflows.

1. Filtres Matroska vers `core/matroska/**` et `core/workflows/common/matroska_*.py` ; push sur les branches actives (`devel-cli`, `main`).
2. `ci-unit-all.yml` : suite unitaire générale Linux (`pytest tests`, outils médias installés), déclenchée sur PR/push et appelable (`workflow_call`).
3. `windows-encode-tests.yml` : ajout de `tests/test_encode_preview_quoting.py` (tests cmd réels) et des tests de propriété de fichiers.
4. `release.yml` : la publication dépend de `ci-unit-all` sur le même SHA.
5. Ruff : diagnostics corrigés ou annotés (imports différés intentionnels) puis check bloquant. Mypy : erreurs corrigées puis check bloquant sur le périmètre configuré.
6. Test de cohérence : un changement de `core/matroska/reader.py` déclenche la matrice native ; tout test unitaire est collecté par la suite générale.

**Suivi (`849ac34`) :**

- `ci-unit-all.yml` (déclenché sur PR, manuel et par `release.yml`, pas sur push pour éviter un double run) : job `lint` (Ruff sur le périmètre de l’audit, Mypy `core cli ui workers main.py launcher.py`), job `linux` (`pytest -q -rs tests`, ffmpeg/mediainfo/g++ installés, rapport JUnit), job `windows` (Qt réel, 7 fichiers de contrats plateforme dont les tests cmd.exe). La jambe Python 3.10 est **informative** (`continue-on-error`) : aucun interpréteur 3.10 n’était disponible localement pour valider le minimum annoncé.
- `release.yml` : job `unit-tests` (`uses: ./.github/workflows/ci-unit-all.yml`), requis par `release` (et donc par la publication Homebrew). La publication `muxiveo-rife` reste conditionnée à ses propres tests natifs (`muxiveo-rife.yml`).
- `windows-encode-tests.yml` inchangé : les tests cmd réels sont exécutés par le job Windows de la suite générale (Qt réel plutôt que le bootstrap simulé).
- Mypy : **0 erreur** (227 fichiers) ; Ruff : **0 diagnostic** sur le périmètre de l’audit. `actionlint` : aucun diagnostic sur les trois workflows modifiés.
- `tests/test_ci_workflows.py` (6 cas) ; suite complète locale **3 977 réussis, 83 ignorés** (tous avec raison : matériel, binaire RIFE ≥ 1.2.2/1.2.3 absent du PATH, Windows natif).
- **À observer :** ces workflows n’ont pas encore tourné sur GitHub (aucun push effectué) ; le job Windows exécutera pour la première fois les verrous `msvcrt` et les tests cmd.exe.

## Lot 10 — installation et builds reproductibles

**Périmètre :** `requirements.txt`, `setup.py`, `setup_brew.py`, `package.py`, `package_appimage.py`, `core/github_release.py`.

### A24 — dépendances Python

1. `requirements.txt` = source unique : le setup lit nom de distribution, contrainte et module importable (`PySide6` → `PySide6`, `pymediainfo`, `numpy`, `certifi`).
2. Vérification par `importlib.metadata` (version installée vs borne minimale) ; installation seulement de ce qui manque ou est trop ancien.

**Tests :** PySide6 détecté avec sa casse ; version trop ancienne ; certifi absent ; seconde exécution idempotente ; dry-run.

### A25 — provenance et intégrité

1. RIFE (AppImage, Windows, macOS) : asset de la release épinglée vérifié par le digest GitHub (`verify_download`), refus si différent.
2. FFmpeg BtbN : archive comparée au digest SHA-256 publié par l’API GitHub pour son asset ; refus si absent ou différent. Le repli Gyan utilise sa somme `.sha256` publiée.
3. MediaInfo : URL, version et SHA-256 consignés (pas de somme publiée).
4. `tools-manifest.json` écrit dans chaque bundle : outil, version/tag, URL, SHA-256, méthode de vérification.

**Tests :** archives valides/corrompues, somme absente, manifeste produit (téléchargements simulés).

**Suivi (`c627bca`) :**

- A24 : `core/python_requirements.py` (`read_requirements`, `unsatisfied_requirements`) ; `setup.PYTHON_PACKAGES` dérivé de `requirements.txt`. Sans `--force`, `pip install <spec>` limité aux dépendances absentes ou trop anciennes (pip ne met à niveau que ce qui ne satisfait pas la contrainte) ; application figée : aucun appel pip. `setup_brew.py` n’installe pas de paquets Python (formule Homebrew) : inchangé.
- A25 : `core/github_release.py` (`fetch_release_by_repo`, `github_release_asset`, `published_checksum`, `verify_sha256`) et `core/tool_manifest.py`. BtbN est traité comme une release GitHub (tag `latest`) : son SHA-256 publié est vérifié. Outils déjà présents dans un dossier `tools/` réutilisé (Windows) : consignés `preexisting` avec leurs sommes. La règle « erreur réseau = muxiveo-rife absent de ce build » est conservée ; une somme absente ou différente fait échouer le build. Hors périmètre (documenté) : cache indexé par digest, builds hors ligne, épinglage des versions Python de release.
- Tests : `tests/test_python_requirements.py` (8 cas), `tests/test_tool_provenance.py` (9 cas) ; Ruff/Mypy verts ; suite complète **3 995 réussis, 83 ignorés**.
- **À observer :** premier build all-inclusive en CI (aucun téléchargement réel effectué dans cette session).

## Lot 11 — documentation et extractions ciblées

1. README : préparation FFmpeg, RIFE direct et assemblage final natif de Merge DoVi.
2. CLAUDE.md : modules `core/matroska`, priorité de configuration unique, nombre de tests remplacé par la collecte, nouvelles règles (candidats, verrous, chemins JSON, taille globale).
3. Audit, réaudit et ce plan mis à jour avec l’état final.

**Suivi final :** extraction du budget vers `core/workflows/encode/planning/size_budget.py` achevée ; façade conservant ses méthodes de validation et ses helpers de flux. README : routage Remux/Encode, pipe RIFE direct, deux passes dans les préparations vidéo, assemblage natif Merge, politique de comptage explicite, candidats et limites du verrou consultatif. CLAUDE.md local actualisé (fichier ignoré par Git, non ajouté de force). Audit et réaudit reliés au [rapport de relecture des onze lots](reaudit-11-lots-2026-10-06.md).

La relecture complète a ajouté les corrections R11-01 à R11-15 : réservation sans blocage avec rollback, identité stable des liens, publication atomique sans écrasement et conservation du candidat sur repli impossible, mutation des profils verrouillée et recherche CLI par nom exact, arrêt des descendants des sondes, changement FFprobe/MediaInfo en cours de session, JSON et budget finis, options RIFE sans plafonds arbitraires, comparaison des versions Python, filtrage des artefacts, gate applicatif avant publication native et typage des imports NumPy vérifié sans le mypy.ini local ignoré par Git. Voir les reproductions, résultats de tests et limites dans le rapport. Le code natif reste en **1.2.3**, release absente lors de la vérification et non publiée ici.

## Stratégie de validation et livraison

| Niveau | Quand | Contrôles |
|---|---|---|
| Régression ciblée | À chaque correction | Reproduction avant/après et vérification des données préservées ; tests de comportements adjacents. |
| Domaine et UI | À chaque lot concerné | Modules impactés, Qt offscreen, migrations des anciens payloads et diagnostics visibles. |
| Concurrence | Lot 1 | Deux processus réels ; workdir partagé, même destination, destinations différentes ; annulations/échecs avant et après écriture. |
| Native | Lots 0 et 8 | C++17 compilé ; tests fichiers sans GPU ; suite Vulkan ; parsing sous sanitizers. |
| Plateformes | Lots 1, 6 | cmd et verrous réels sur le poste Windows de test ; skips matériels explicités. |
| Suite complète | Après chaque lot | `python -m pytest -q tests`, Ruff sur les fichiers modifiés, `git diff --check`. |

Une correction ne ferme son ID qu’après validation du comportement attendu. Pour A04, distinguer **sources corrigées**, **build vérifié** et **binaire distribué**. Ce plan n’autorise ni ne réalise de publication.

## Couverture de tous les constats

| ID | Lot | Critère de clôture | État |
|---|---|---|---|
| A01 | 1 | Candidat unique détenu, destination réservée, échec/annulation préservant les fichiers externes. | Réalisé (`47ec2a1`) |
| A02 | 1 | Job actif exclu du nettoyage entre deux processus ; abandon récupérable. | Réalisé (`47ec2a1`) |
| A03 | 1 | Aucune suppression par simple nom hors racine marquée. | Réalisé (`47ec2a1`) |
| A04 | 0 | Même fichier refusé sans troncature ; fichiers distincts et pipes directs préservés ; build 1.2.2 distribué lors de la release. | Réalisé et distribué (`muxiveo-rife-v1.2.2`) |
| A05 | 1 | Toutes les collisions internes au batch détectées avant exécution, même avec force. | Réalisé (`47ec2a1`) |
| A06 | 2 | Profils de noms normalisés identiques coexistants et supprimables indépendamment. | Réalisé (`02bf18a`) |
| A07 | 4 | Toutes les sondes encode respectent FFprobe configuré. | Réalisé (`b331401`) |
| A08 | 4 | Sondes bornées/annulables, sans processus restant ni fermeture bloquée. | Réalisé (`b331401`) |
| A09 | 5 | Taille globale indépendante de l’ordre et contradictions diagnostiquées. | Réalisé (`00169bb`) |
| A10 | 5 | Incertitudes visibles ; blocage limité aux coûts mesurés. | Réalisé (`00169bb`) |
| A11 | 3 | Même résolution des chemins de document GUI/CLI, provenance conservée pendant les fusions. | Réalisé (`d9d1c4a`) |
| A12 | 3 | BOM et UTF-8 simple acceptés partout pour les documents utilisateur. | Réalisé (`d9d1c4a`) |
| A13 | 6 | Restitution réelle argv sous POSIX/cmd pour remux et préparations natives. | Réalisé (`1deafa7`), cmd réel en attente |
| A14 | 3 | NaN/infini refusés avant exécution et avant conversion en timestamps. | Réalisé (`d9d1c4a`) |
| A15 | 3 | Contraintes de champs connus concordantes entre schéma et validation runtime. | Réalisé (`d9d1c4a`) |
| A16 | 2 | Écriture interrompue préservant l’ancien profil ; fichiers rejetés diagnostiqués. | Réalisé (`02bf18a`) |
| A17 | 7 | Arbre, compteurs et statut concordant avec les suppressions effectives. | Réalisé (`cb366cd`) |
| A18 | 4 | MediaManager attend l’arrêt réel sans détruire un QThread actif. | Réalisé (`b331401`) |
| A19 | 8 | Parsing et tailles sûrs, cas limite reproduit rejeté sous UBSan/ASan. | Réalisé (`15a23da`), release 1.2.3 à publier |
| A20 | 9 | Les modules Matroska actuels déclenchent la matrice native. | Réalisé (`849ac34`), premier run CI à observer |
| A21 | 9 | Suite générale et régressions Windows effectivement exécutées. | Réalisé (`849ac34`), premier run CI à observer |
| A22 | 9 | Publication conditionnée aux checks applicatifs du même SHA. | Réalisé (`849ac34`), premier run CI à observer |
| A23 | 9 | Périmètre de contrôles statiques vert et exécuté en CI. | Réalisé (`849ac34`), premier run CI à observer |
| A24 | 10 | Dépendances et bornes cohérentes ; setup idempotent. | Réalisé (`c627bca`) |
| A25 | 10 | Digests vérifiés pour RIFE et FFmpeg, manifeste des outils dans chaque bundle. | Réalisé (`c627bca`) |
| A26 | 11 | Flux, chemins, configuration et exemples documentés conformes au code. | Terminé localement ; voir le rapport final |
| A27 | 1–6, 11 | Politiques communes extraites et consommées par les différentes façades, avec tests de comportement. | Terminé localement ; budget extrait et suite de régression |

## Relecture de clôture

Le suivi historique des lots ci-dessus décrit leurs commits initiaux. Les garanties renforcées, les résultats finaux et les réserves de validation sont consignés dans [reaudit-11-lots-2026-10-06.md](reaudit-11-lots-2026-10-06.md). Une sortie absente est publiée atomiquement sans écrasement ; pour une sortie préexistante, le dernier contrôle d’état puis le remplacement n’excluent pas une modification externe dans cet intervalle. Le verrou protège les jobs Muxiveo du même utilisateur et reste consultatif face aux autres logiciels. Windows/macOS et le premier build all-inclusive restent à valider en CI ; aucune publication effectuée ici.
