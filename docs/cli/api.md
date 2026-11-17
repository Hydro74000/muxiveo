# API de Muxiveo : couche de service, `call`, worker `serve`, événements

Chaque fonction du CLI est une **opération** de la couche de service (`core/service/`) : une
requête JSON typée, des événements pendant l'exécution, un résultat JSON. Les commandes du CLI
en sont des adaptateurs ; trois entrées donnent accès au contrat brut : `call` (un appel par
processus), le worker `serve` (JSON-RPC sur stdio, plusieurs jobs) et l'import Python.

## Catalogue

56 opérations, par famille :

| Famille | Opérations |
|---|---|
| Média | `media.inspect`, `media.validate`, `media.preview`, `media.run`, `media.batch`, `media.extract`, `media.bluray_titles` |
| Encodage | `encode.preview_capture`, `encode_preset.list/show/save/import/export/delete` |
| HDR | `hdr.inspect`, `hdr.patch` |
| Synchro | `sync.scan`, `sync.shift_subtitles`, `sync.waveform`, `calibration.show/edit/compose/invert` |
| Hybrid | `hybrid.plan`, `hybrid.run` |
| Profils, TMDB | `profile.validate/preview/apply/batch`, `profile.list/show/save/delete/capture`, `tmdb.search`, `tmdb.details` |
| Système | `system.version`, `system.tools`, `system.capabilities`, `schema.get`, `config.path/show/get/set/unset/reset`, `workdir.list/clean`, `plugins.list/install/update/remove`, `nvencc.status/install/remove`, `app.update_install` |

```bash
muxiveo --cli operations                          # liste
muxiveo --cli operations --describe media.extract # requête, résultat, erreurs, événements, nature
muxiveo --cli schema --version api                # catalogue complet en JSON (base OpenAPI / JSON-RPC)
```

Chaque description publie le schéma de la requête et du résultat, les codes d'erreur, les événements
possibles (`progress` pour les opérations qui en publient, `artifact` pour celles qui écrivent) et :

- `long_running`, `mutating` (écrit des fichiers ou la configuration), `uses_gpu` ;
- `privileged` : modifie la configuration, les extensions, NVEncC, l'installation ou nettoie le
  dossier de travail — refusée sans autorisation explicite (voir plus bas) ;
- `queue` : classe d'ordonnancement — `job` (produit ou transforme des médias), `analysis` (inspection,
  aperçus, analyses) ou `exclusive` (opérations privilégiées et lecture de la configuration : exécutées
  seules).

Les documents portés par les requêtes sont décrits une fois (`$defs` : `job`, `encode_block`,
`decision_profile`) et reliés par `$ref` ; `job` est fermé comme l'admission (clé inconnue refusée,
clés internes `_…` / `$…` ignorées — sauf dans les objets `sync`, `flags` et le bloc `encode`, qui les
refusent —, noms de `variables` et tables libres) et l'admission d'une requête vérifie le job contre ce
schéma : le client et le service rendent le même verdict (seuls l'ordre croissant des segments d'une
calibration et ses champs non publiés relèvent de son propre contrat). Les motifs publiés marquent la fin
du texte de façon portable (`(?![\s\S])`) : même lecture en ECMA-262 et en Python. Les résultats sont typés : fiche
d'inspection et pistes (`hdr_type` publié par son libellé), sorties d'extraction, entrées de lots,
réglages, presets, profils, titres Blu-ray, capacités, extensions, verdicts FEL, TMDB. Restent des
objets décrits (contenu documenté ailleurs) : plans de `media.validate/preview`, rapports HDR, plans
Hybrid, calibrations. Chaque document publié (catalogue, `--describe`) est autonome : toute `$ref` s'y
résout.

## Appeler une opération (`call`)

```bash
muxiveo --cli call media.extract --request requete.json      # chemins relatifs au dossier du fichier
echo '{"inputs": ["/media/film.mkv"]}' | muxiveo --cli call media.inspect --request -
muxiveo --cli call config.set --allow-privileged --request reglage.json
```

**Requête** : objet JSON **strict**, documents `job` (contrat `schema --version 1` / exact-job) et
blocs `encode` compris ; un profil décisionnel porté par `profile.save` est vérifié contre
`$defs/decision_profile_input` (`version` et `kind` complétés) puis par les règles des profils (identifiants de
règles uniques, syntaxe des expressions), ses autres clés conservées ; un preset d'encodage
(`encode_preset.save`, `encode_preset.import`) contre le schéma publié du preset. Les profils sont rendus tels
qu'enregistrés (`$defs/profile_document`) ; un preset enregistré aux champs non conformes (fichier écrit à la
main) est signalé dans `load_errors` (`encode_preset.show` : `validation`), jamais publié. Refusés dès la lecture (`invalid_request`, avec le
chemin de chaque champ en cause, ex. `job.sources[0].sycn`, `job.encode.video.default.crf`) : clé
inconnue, type incorrect, valeur hors bornes ou hors énumération, format HDR10 statique invalide
(`static_hdr.master_display`, `static_hdr.max_cll` ; texte vide = valeurs de la source), champ requis
absent ou `null` non autorisé ; un entier peut s'écrire `1.0` (sémantique JSON) ; nombres limités à la
plage des flottants double précision (RFC 8259 § 6) et imbrication à 64 niveaux. Ce qui dépend des sources et de la machine (pistes sélectionnées,
encodeurs et presets disponibles, géométrie Dolby Vision + NVENC) est refusé à la validation
(`validation`), comme au lancement. Les documents des commandes historiques (`--config`, `--template`,
`--batch`, exact-jobs) restent **tolérants** : clé inconnue signalée (`warning`, code `unknown_key`)
et ignorée — sauf dans les objets fermés du contrat (`sources[i].sync`, `tracks[i].sync`, `flags`), qui la
refusent (`invalid_request`, code retour 2). Un fichier désigné par une requête (`template`, `batch` de
`media.batch`) est un document : tolérant aussi, ses chemins contrôlés comme ceux de la requête.
Exemples : `docs/cli/examples/`.

**Réponse** (stdout, un objet JSON indenté) ; `status` = état terminal du job :

```json
{"api_version": 1, "operation": "media.extract", "job": "…", "status": "succeeded", "result": {"outputs": [ … ]}}
{"api_version": 1, "operation": "media.extract", "job": "…", "status": "failed",
 "error": {"code": "validation", "message": "…", "details": []}}
```

## Requêtes de l'API : chemins et redirections

Règles communes à `call`, `serve` et l'intégration Python (les commandes historiques gardent leur
comportement) :

- **Dossier de référence** : une valeur relative (sources, sorties, `work_dir`, calibrations, pièces
  jointes, chapitres importés, preset ou profil désigné par un fichier) est résolue depuis le dossier du
  fichier de requête (`call --request fichier.json` ; avec `--request -`, le dossier courant), le
  `base_dir` de la soumission (`serve`) ou celui du contexte (Python) ; sans dossier de référence, elle
  est **refusée** — jamais résolue depuis le dossier courant d'un worker. Un gabarit de sortie rendu
  relatif se place d'abord dans le dossier de sortie (`output` terminé par `/`, `output_dir` d'un lot),
  sinon dans le dossier de référence, sinon il est refusé. Chemins de documents et sorties utilisés tels
  qu'admis (blancs compris ; `~` = dossier personnel) ; une référence de profil (nom ou fichier) est
  lue sans ses blancs de début et de fin.
- **Chemins inutilisables** refusés : absolus pour un autre OS (`C:/film.mkv` sous Linux) ou ambigus
  sous Windows (`C:film.mkv`, `\film.mkv`).
- **Pas de redirection** : un document ne désigne jamais un exécutable (clés de job `ffprobe` /
  `mediainfo` hors contrat : refusées en requête, signalées et sans effet dans un document historique ;
  outils = configuration ou options de l'appel), `tmdb_cover` doit venir de `https://image.tmdb.org/`,
  les clés internes `_…` du premier niveau d'un document sont retirées (plus profondes : ignorées, sauf
  dans `sync`, `flags` et le bloc `encode`, qui les refusent).
- **Paramètres libres de l'encodeur** (`extra_params`, presets compris) : limités aux options de
  l'encodeur — options privées et options d'encodage vidéo pour FFmpeg (sans option de la ligne de
  commande homonyme : `-stats`…), options d'encodage de `--help` pour NVEncC (sans entrées, sorties,
  journaux, filtres `--vpp-*`, chargeurs `--option-file` / `--avsdll`) — contrôlés tels que la commande
  les transmettra ; une option réservée au workflow (codec, mapping, filtres déjà posés) est refusée.
  Chaînes `-x265-params` / `-svtav1-params` : syntaxe seule. Les options d'encodeur qui nomment un
  fichier (`-passlogfile`, `csv=`) restent permises.
- **Champs de filtres bornés** (pour tous les appelants, interface comprise) : YADIF, algorithme de
  tone mapping… limités à leurs valeurs ; une valeur libre ne peut plus ajouter un filtre.

## Opérations privilégiées et files

Les opérations `privileged` (`config.set/unset/reset`, `workdir.clean`, `nvencc.install/remove`,
`plugins.install/update/remove`, `app.update_install`) sont refusées (`forbidden`, code retour 3) sans
autorisation explicite : `call --allow-privileged`, `serve --allow-privileged`,
`ExecutionContext.create(allow_privileged=True)` ; les commandes historiques (`config set`,
`plugins install`…) restent autorisées. `app.update_install` renvoie `restart_required` (vrai si la
version est installée ou programmée : le processus doit se terminer).

Le gestionnaire de jobs (`serve`, Python) ordonnance par `queue`, en FIFO par classe : au plus
`max_jobs` jobs (défaut 1) et `max_analyses` analyses (défaut 2) en parallèle — un client reste réactif
pendant un long encodage ; une `exclusive` attend que tout soit terminé, s'exécute seule et fait
barrière (rien de soumis après elle ne démarre avant elle) ; la configuration est relue après elle.

## Événements

Une ligne JSON par événement (stderr avec `call`, notifications `job.event` avec `serve`) :

```json
{"v": 1, "job": "j1", "parent": null, "seq": 4, "ts": "2026-11-14T08:00:00.000Z", "type": "progress", "pct": 42.5, "stage": "…"}
```

| `type` | Contenu |
|---|---|
| `state` | état du job : `queued` (gestionnaire), `running`, puis `succeeded`, `partial`, `failed` ou `cancelled` |
| `log` | message (`level` : `debug`, `info`, `ok`, `error` ; `message`, `origin`) |
| `progress` | `pct`, `stage` (identifiant stable : `download`, `assembly`, `metadata`, couches d'extraction, étapes de `hdr.patch`…), `label` (texte), encodage : `fps`, `frame`, `eta_s`, `track` / `track_count`, `pass_index` / `pass_count` |
| `warning` | `message`, `code` toujours présent (`unknown_key`, `extract_skipped`, `cancel_too_late`, `sync_unverified`, `sync_uncertain`, `nvencc_install`, `hdr_patch_plan`, `event_too_large`, `unspecified`) ; synchro : `kind`, `zones`, `windows`, `spread_ms`, `shift_ms` |
| `artifact` | fichier produit (`kind` : `output`, `exact-job`, `preview-image`, `calibration`…, `path`) |
| `result` / `error` | fin du job |

`seq` croît sans trou par job (depuis `queued` avec le gestionnaire) ; `parent` relie les jobs enfants
à leur lot. Le schéma (`event` du catalogue) décrit chaque variante.

## Lots, jobs enfants, résultats partiels

`media.batch`, `profile.batch`, `hybrid.run` : un job enfant par élément (événements `parent` = lot).
Le résultat du lot liste chaque élément (état, sortie, erreur, `job` = identifiant de son job enfant,
`null` quand l'élément n'en a pas, ex. épisode Hybrid seulement exporté : aucun événement enfant) avec
`status`, repris par l'état terminal du lot, l'enveloppe de `call` et le code retour ; `job` n'existe que
dans le résultat de service (résumé de lot et rapport Hybrid écrits sur disque inchangés) :

- `succeeded` : tous réussis (code retour 0) ;
- `partial` : au moins un réussi et au moins un en échec ou annulé ;
- `failed` / `cancelled` : aucun réussi.

Lot non entièrement réussi : code retour 7, comme la commande `batch`. Une sortie publiée reste
publiée quel que soit le sort des suivants. Toutes les sorties sont préparées puis toute collision est
refusée **avant** le premier job.

## Annulation

Ctrl+C (CLI), `job.cancel` (`serve`) ou jeton (`CancelToken`, Python) : processus arrêtés, fichier
`.partial` et dossier de travail supprimés, état terminal `cancelled` émis après le nettoyage complet.
Un job en file est retiré aussitôt (`error` puis `state: cancelled`). Remux et encodage : une annulation
arrivée après la publication de la sortie laisse l'opération réussie avec l'avertissement
`cancel_too_late`. Extraction de plusieurs éléments : les sorties déjà publiées restent (événements
`artifact`), l'opération se termine `cancelled`. Dans un lot : le job en cours est annulé puis le lot
s'arrête.

## Erreurs et codes retour

| `error.code` | Sens | Code retour du CLI |
|---|---|---:|
| `invalid_request`, `unknown_operation` | requête mal formée, opération inconnue | 2 |
| `validation`, `forbidden` | requête refusée par les règles métier ; opération privilégiée non autorisée | 3 |
| `tool_missing` | outil absent (FFmpeg, dovi_tool, extension…) | 4 |
| `output_exists` | sortie existante sans `force` | 5 |
| `workflow_failed`, `busy`, `cancelled` | échec, ressource occupée (ou file pleine), annulation | 6 |
| `partial` (ou `status` d'un lot autre que `succeeded`) | lot partiel, en échec ou interrompu | 7 |

Avec `call`, une opération réussie renvoie 0 : son état métier (mise à jour disponible, synchro à
relire…) est dans `result`.

## Worker `serve`

```bash
muxiveo --cli serve [--allow-privileged] [--max-jobs N] [--max-analyses N]
```

JSON-RPC 2.0 sur stdio, **un objet JSON par ligne** UTF-8 (trame ≤ 16 Mio, JSON standard : `NaN` et
`Infinity` refusés ; `id` : texte d'au plus 256 caractères, entier 64 bits ou `null`), pour **un** client :

| Méthode | Paramètres | Résultat |
|---|---|---|
| `server.info` | — | `protocol`, `api_version`, `event_version`, `app_version`, `privileged`, `max_jobs`, `max_analyses` |
| `job.submit` | `operation`, `request` (objet), `base_dir` (absolu, facultatif) | `{"job": "…"}` |
| `job.cancel` | `job` | `{"cancelled": true \| false}` (faux : déjà terminé) |
| `job.get` | `job` | état, horodatages, dernière progression, `result` ou `error` |
| `job.list` | — | `{"jobs": [ … ]}` (en file, en cours, 100 derniers terminés) |
| `server.shutdown` | — | `{}` puis arrêt |

Notifications du worker : `job.event` (événement tel quel) et `server.exiting` (`reason` : `shutdown`,
`update`, `client_gone`). Erreurs : codes JSON-RPC standard (`-32700` JSON illisible, `-32600` requête
invalide ou trame trop volumineuse, `-32601` méthode inconnue, `-32602` paramètres, `-32603` défaut
interne : le worker continue) ; erreur de l'API →
`-32000`, `data` = erreur publique (`code` : `forbidden`, `invalid_request`, `busy`…). Le catalogue
s'obtient par `job.submit` de `schema.get` (`{"version": "api"}`).

```json
{"jsonrpc":"2.0","id":1,"method":"job.submit","params":{"operation":"media.inspect","request":{"inputs":["film.mkv"]},"base_dir":"/media"}}
{"jsonrpc":"2.0","id":1,"result":{"job":"9f…"}}
{"jsonrpc":"2.0","method":"job.event","params":{"v":1,"job":"9f…","seq":1,"type":"state","state":"queued",…}}
```

Garanties : la réponse de `job.submit` précède tout événement du job ; les descripteurs du protocole
sont protégés (toute impression parasite et les sous-processus écrivent sur stderr, stdin des
sous-processus = `os.devnull`) ; écrire ne bloque jamais le worker — un client qui ne lit plus (64 Mio
en attente) ou un pipe fermé est considéré parti : le worker s'arrête. Aucune trame sortante ne dépasse 16 Mio : une réponse trop volumineuse
devient une erreur `-32000` (`response_too_large`), une erreur répétant une très longue valeur du client
est abrégée (même code), un événement trop volumineux devient un avertissement `event_too_large`. Arrêt (`server.shutdown`, fin de stdin, SIGTERM, SIGINT, client parti) : jobs en
file et en cours annulés puis attendus, vidage final borné (5 s). Après une mise à jour installée
(`restart_required`), le worker annonce `server.exiting` (`update`) et s'arrête : le client le relance.

## Appeler Muxiveo depuis un outil local

| Entrée | Quand la choisir |
|---|---|
| `call` | un processus par appel : script de post-traitement, appel ponctuel |
| `serve` en processus enfant | plusieurs jobs, annulation par identifiant, progression en direct ; un worker = un client |
| import Python (`core.service`) | outil Python lancé depuis les sources de Muxiveo |

Partager un même worker entre plusieurs outils ou l'appeler par le réseau relève d'une extension.
Client minimal sans dépendance : [`docs/cli/examples/serve_client.py`](examples/serve_client.py).

### Intégrer en Python

```python
from pathlib import Path

from core.config import AppConfig
from core.service import CallbackSink, ExecutionContext, JobManager, call
from core.service.catalog import CATALOG

# Un appel dans le fil courant (chemins relatifs depuis base_dir).
ctx = ExecutionContext.create(AppConfig(), CallbackSink(print), base_dir=Path("/media"))
result = call(CATALOG, "media.inspect", {"inputs": ["film.mkv"]}, ctx)

# Jobs concurrents : admission, files, annulation, suivi.
manager = JobManager(CATALOG, AppConfig, CallbackSink(lambda event: print(event.to_dict())))
job = manager.submit("media.inspect", {"inputs": ["/media/film.mkv"]})
print(manager.get(job).state)
manager.cancel(job)
manager.shutdown()
```

`ExecutionContext.create` : configuration (instantané), destination des événements (`EventSink` :
`ListSink`, `CallbackSink`…), `cancel`, `overrides` (outils de l'appel), `ports` (services externes
remplaçables : TMDB, réseau), `base_dir`, `allow_privileged`. Une application Qt doit exister
(`QCoreApplication`) ; avec `JobManager`, sa boucle tourne dans le fil principal pendant que les jobs
s'exécutent dans leurs fils (certaines tâches y livrent leur fin), comme dans `serve`.

La destination est appelée depuis le fil qui publie (jamais sous un verrou du cœur) : elle peut rappeler
`get`, `list` ou `cancel` (y compris d'un autre job). Les événements d'un job lui parviennent dans l'ordre
de `seq`, un seul à la fois ; un événement publié pendant la livraison d'un autre événement du même job
est livré juste après, par le fil qui livre. Un job reçu terminé (`state` final, `result`, `error`) est
déjà terminé pour `get`, résultat ou erreur compris ; il reste consultable au moins jusqu'à la
publication de ses événements terminaux.

## Modèle de sécurité

Le cœur ne confine pas les chemins : une requête lit et écrit partout où l'OS le permet au processus
(comme les sorties du CLI). Il refuse en revanche les redirections (exécutables, réseau, ajouts à la
commande hors de l'encodeur) et les opérations privilégiées non autorisées. **Toute exposition au-delà
d'un client local de confiance** (réseau, utilisateurs multiples) exige d'isoler le worker par l'OS :
utilisateur dédié, conteneur, dossiers montés — rôle d'une extension, avec authentification et TLS.

## Versions et compatibilité

`api_version` (catalogue), `event_version` (événements) et `protocol` (`serve`) : un ajout compatible
(opération, champ, code d'avertissement ou d'erreur) ne change pas la version ; un retrait ou un
changement de sens l'incrémente. Les clients ignorent les champs et codes inconnus.

## Limites

Hors périmètre du cœur, prévu pour une extension : transport réseau, authentification, TLS,
transfert de fichiers, persistance des jobs, clients multiples, OpenAPI généré depuis le catalogue.
`app.update_install` en mode `scheduled` (installeur, échange de dossiers) attend la fin du processus.
Les opérations sans interface n'ouvrent aucune fenêtre ; les choix interactifs de l'interface sont des
champs explicites des requêtes, refusés par défaut (ex. `accept_dovi_nvenc_crop`, `confirm`).
