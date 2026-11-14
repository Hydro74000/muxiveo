# API de Muxiveo : couche de service, `call`, événements

Chaque fonction du CLI est une **opération** de la couche de service (`core/service/`) : une
requête JSON typée, des événements pendant l'exécution, un résultat JSON. Les commandes du CLI
en sont des adaptateurs ; `call` donne accès au contrat brut, celui d'un futur service (daemon).

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

Chaque description indique `long_running` (progression publiée), `mutating` (écrit des fichiers
ou la configuration) et `uses_gpu` : un ordonnanceur peut s'en servir.

## Appeler une opération

```bash
muxiveo --cli call media.extract --request requete.json      # chemins relatifs au dossier du fichier
echo '{"inputs": ["film.mkv"]}' | muxiveo --cli call media.inspect --request -
muxiveo --cli call media.run --request job.json --ffmpeg /opt/ffmpeg/bin/ffmpeg
```

**Requête** : objet JSON **strict**. Clé inconnue, type incorrect, champ requis absent ou `null`
non autorisé : refus `invalid_request` avec le chemin de chaque champ en cause. Chemins relatifs
résolus depuis le dossier du fichier de requête (dossier courant pour stdin). Les documents
historiques (`--config`, `--template`, `--batch`, exact-jobs) restent **tolérants** : clés inconnues
signalées par un avertissement, jamais refusées. Exemples de requêtes : `docs/cli/examples/`.

**Réponse** (stdout, un objet JSON indenté) ; `status` = état terminal du job (pour un lot : celui de son
résultat, voir plus bas) :

```json
{"api_version": 1, "operation": "media.extract", "job": "…", "status": "succeeded", "result": {"outputs": [ … ]}}
{"api_version": 1, "operation": "media.extract", "job": "…", "status": "failed",
 "error": {"code": "validation", "message": "…", "details": []}}
```

## Événements

Sur stderr, une ligne JSON par événement (toujours avec `call`, sur demande avec
`--log-format events` pour les autres commandes, qui publient alors aussi leur réponse sur stdout) :

```json
{"v": 1, "job": "j1", "parent": null, "seq": 4, "ts": "2026-11-14T08:00:00.000Z", "type": "progress", "pct": 42.5, "stage": "…"}
```

| `type` | Contenu |
|---|---|
| `state` | état du job : `running`, puis `succeeded`, `partial`, `failed` ou `cancelled` |
| `log` | message (`level`, `message`, `origin`) |
| `progress` | avancement (`pct`, `stage`, `fps`, `eta_s`…) — opérations longues |
| `warning` | avertissement (`message`, `code`) |
| `artifact` | fichier produit (`kind` : `output`, `exact-job`, `preview-image`, `calibration`…, `path`) |
| `result` / `error` | fin du job |

`seq` croît sans trou par job ; `parent` relie les jobs enfants à leur lot.

## Lots, jobs enfants, résultats partiels

`media.batch`, `profile.batch`, `hybrid.run` : un job enfant par élément (événements `parent` = lot).
Le résultat du lot liste chaque enfant (état, sortie, erreur) avec `status`, repris par l'état
terminal du lot, l'enveloppe de `call` et le code retour :

- `succeeded` : tous réussis (code retour 0) ;
- `partial` : au moins un réussi et au moins un en échec ou annulé ;
- `failed` / `cancelled` : aucun réussi.

Lot non entièrement réussi (`partial`, `failed`, `cancelled`) : code retour 7, comme la commande
`batch`. `hybrid.plan` suit la même règle d'après les épisodes dont la préparation a échoué.

Une sortie publiée reste publiée (et listée) quel que soit le sort des suivants. Toutes les sorties
sont préparées puis toute collision est refusée **avant** le premier job.

## Annulation

Ctrl+C (CLI) ou annulation du jeton (service) : processus arrêtés, fichier `.partial` et dossier de
travail supprimés, état terminal `cancelled` émis après le nettoyage complet. Une annulation arrivée
après la publication de la sortie laisse l'opération réussie avec l'avertissement `cancel_too_late`.
Dans un lot : le job en cours est annulé puis le lot s'arrête.

## Erreurs et codes retour

| `error.code` | Sens | Code retour du CLI |
|---|---|---:|
| `invalid_request`, `unknown_operation` | requête mal formée, opération inconnue | 2 |
| `validation` | requête bien formée refusée par les règles métier | 3 |
| `tool_missing` | outil absent (FFmpeg, dovi_tool, extension…) | 4 |
| `output_exists` | sortie existante sans `force` | 5 |
| `workflow_failed`, `busy`, `cancelled` | échec, ressource occupée, annulation | 6 |
| `partial` (ou `status` d'un lot autre que `succeeded`) | lot partiel, en échec ou interrompu | 7 |

Avec `call`, une opération réussie renvoie 0 : son état métier (mise à jour disponible, synchro à
relire…) est dans `result`.

## Limites (plan du service)

Hors périmètre actuel, prévu pour le service (daemon) : transport réseau et authentification,
racines de fichiers autorisées, file d'attente et ordonnancement GPU / disque, persistance des jobs,
clients multiples. `app.update_install` en mode `scheduled` (installeur, échange de dossiers) attend
la fin du processus : un service permanent devra s'arrêter pour que l'installation aboutisse.
Les opérations sans interface n'ouvrent aucune fenêtre ; les choix interactifs de l'interface sont
des champs explicites des requêtes, refusés par défaut (ex. `accept_dovi_nvenc_crop`, `confirm`).
