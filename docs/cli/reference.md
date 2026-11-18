# Référence du CLI Muxiveo

Document généré par `muxiveo --cli help --format md` : ne pas modifier à la main.

## Média

### `inspect`

Inspecter une ou plusieurs sources : pistes, HDR, chapitres, pièces jointes (JSON). --config-template écrit un exact-job de départ ; --hdr ouvre l'inspection du HDR Studio.

```text
muxiveo --cli inspect [-h] [--sync-mode {physical,container}] [--sync-subtitles {mirror,none}]
                      [--clean-nfo | --no-clean-nfo] [--remove-closed-captions | --no-remove-closed-captions]
                      [--crossfade-ms CROSSFADE_MS] [--auto-forced-subs] [--forced-threshold FORCED_THRESHOLD]
                      [--auto-sdh] [--auto-sync] [--auto-sync-all] [--calibration CALIBRATION] [--detect-cuts]
                      [--quick-sync-check | --no-quick-sync-check] [--reject-coherent-sync]
                      [--drift-threshold-ms DRIFT_THRESHOLD_MS] [--cadence-auto | --no-cadence-auto]
                      [--cadence-method {auto,atempo,asetrate}] [--export-workflow EXPORT_WORKFLOW]
                      [--config CONFIG] [--ffmpeg FFMPEG] [--ffprobe FFPROBE] [--mediainfo MEDIAINFO]
                      [--work-dir WORK_DIR] [--threads THREADS] [--mux-backend {auto,native,ffmpeg}]
                      [--output-template OUTPUT_TEMPLATE] [--output-all] [--log-format {text,jsonl,events}]
                      [--verbose] [--config-template] [--with-encode] [--output OUTPUT]
                      [--hdr {static,dynamic,measure}] [--hdr-track HDR_TRACK] [--hdr-report HDR_REPORT]
                      [--hdr-compare] [--hdr-offset HDR_OFFSET] [--hdr-fail-on {warn,error}] [--hdr-fast]
                      [--hdr-no-cache] [--hdr-fel] [--hdr-fel-interval SECONDES]
                      [--hdr-fel-survival ENCODEUR:KBPS] [--hdr-fel-min {moderate,useful}]
                      [--hdr-fel-moments DOSSIER] [--hdr-fel-exposure EV] [--hdr-advice]
                      [--hdr-display-peak NITS] [--hdr-tv-model {maxcll,mdcv,dynamic}] input [input ...]
```

| Option | Effet |
|---|---|
| `--sync-mode` | Synchro des sources : physical (pistes réécrites, coupures et cadence) ou container (décalage du conteneur). — `physical`, `container` |
| `--sync-subtitles` | Sous-titres : mirror (suivent la synchro de leur source) ou none. — `mirror`, `none` |
| `--clean-nfo`, `--no-clean-nfo` | Nettoyer le NFO (balises techniques retirées). |
| `--remove-closed-captions`, `--no-remove-closed-captions` | Retirer les sous-titres embarqués EIA-608/708 détectés, sans réencodage |
| `--crossfade-ms` | Fondu aux raccords de synchro (ms, 0–1000). |
| `--auto-forced-subs` | Repérer les sous-titres forcés (peu d'événements) et les marquer. |
| `--forced-threshold` | Nombre d'événements sous lequel une piste de sous-titres est jugée forcée. (défaut : `50`) |
| `--auto-sdh` | Repérer les sous-titres SDH (malentendants) et les marquer. |
| `--auto-sync` | Analyser et recalibrer automatiquement les sources dynamiquement. |
| `--auto-sync-all` | Synchroniser chaque source ≥ 1 face à la source 0 (sources sans bloc `sync` du job). |
| `--calibration` | Fichier JSON de calibration explicite (outrepasse l'analyse dynamique). |
| `--detect-cuts` | Détecter les coupures et ruptures temporelles (multi-segments). |
| `--quick-sync-check`, `--no-quick-sync-check` | Avec --detect-cuts : pas d'analyse continue si les mesures ponctuelles concordent (confiance ≥ 0,90). Défaut : [sync] quick_audio_check. |
| `--reject-coherent-sync` | Avec --detect-cuts : refuser une synchro cohérente en partie non vérifiée (zones sans correspondance entourées du même décalage). Défaut : acceptée avec un avertissement. |
| `--drift-threshold-ms` | Seuil de dérive en ms pour détecter une coupure. (défaut : `25`) |
| `--cadence-auto`, `--no-cadence-auto` | Détection et conversion automatique de cadence PAL <-> Cinéma. (défaut : `True`) |
| `--cadence-method` | Méthode de conversion audio pour cadence : auto (analyse acoustique du pitch, défaut), atempo (tonalité préservée) ou asetrate (hauteur naturelle / pitch shift). — `auto`, `atempo`, `asetrate` |
| `--export-workflow` | Sauvegarder le workflow exact sans exécuter. |
| `--config` | Fichier JSON job/template. |
| `--ffmpeg` | Chemin ffmpeg override. |
| `--ffprobe` | Chemin ffprobe override. |
| `--mediainfo` | Chemin mediainfo override. |
| `--work-dir` | Répertoire de travail override. |
| `--threads` | Nombre de threads ffmpeg. |
| `--mux-backend` | Backend MKV : auto (défaut), native ou ffmpeg. Prioritaire sur le job. — `auto`, `native`, `ffmpeg` |
| `--output-template` | Template du nom de sortie. Placeholders disponibles : {source_name},{title},{year},{season},{episode},{episode_title},{season_episode},{group}, keywords pistes comme {audio-lang:best} (+ {season_num},{episode_num} pour formats numériques). Extension .mkv ajoutée si absente. |
| `--output-all` | Force les keywords pistes de --output-template en mode all (toutes les valeurs finales trouvées). |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Affiche la sortie ffmpeg en direct (progression, codecs, timing). Par défaut, seule la progression des étapes de workflow est affichée. |
| `input` | Sources à inspecter. |
| `--config-template` | Écrire un exact-job minimal de la source au lieu de la fiche. |
| `--with-encode` | Avec --config-template : bloc `encode` de départ (copie, HDR de la source). |
| `--output` | Fichier de sortie. |
| `--hdr` | Inspection HDR : en-têtes (static), + RPU / HDR10+ (dynamic), + mesure de chaque image (measure, extension mvo-fel ≥ 0.4.0). Sortie JSON. — `static`, `dynamic`, `measure` |
| `--hdr-track` | Index ffprobe de la piste vidéo (défaut : première piste vidéo). |
| `--hdr-report` | Rapport écrit dans ce fichier : HTML (.html) ou JSON (autre extension). |
| `--hdr-compare` | Compare la seconde source à la première (deux entrées). |
| `--hdr-offset` | Décalage imposé de B en images (défaut : alignement automatique). |
| `--hdr-fail-on` | Code de sortie 3 si un contrôle atteint ce niveau. — `warn`, `error` |
| `--hdr-fast` | Avec --hdr measure : mesure rapide (images clés, extension mvo-fel ≥ 0.5.0) ; estimation. |
| `--hdr-no-cache` | Relit le fichier au lieu du résultat en cache (RPU, HDR10+, mesure). |
| `--hdr-fel` | FEL Analyzer : apport de la couche FEL d'une source Dolby Vision profil 7 (lit les métadonnées dynamiques ; extension mvo-fel ≥ 0.4.0). |
| `--hdr-fel-interval` | Avec --hdr-fel : une image mesurée toutes les SECONDES (défaut 30 ; 60 rapide, 10 fin). |
| `--hdr-fel-survival` | Avec --hdr-fel : test de survie à l'encodage (ex. libx265:35000). |
| `--hdr-fel-min` | Avec --hdr-fel : code de sortie 3 si le verdict est inférieur (moderate, useful). — `moderate`, `useful` |
| `--hdr-fel-moments` | Avec --hdr-fel : moments les plus marqués en PNG pleine définition (sans FEL, avec FEL, carte de l'écart). |
| `--hdr-fel-exposure` | Avec --hdr-fel-moments : exposition des PNG (EV, -6 à 6 ; défaut 0). |
| `--hdr-advice` | Recommandations (clé `advice`) : MaxCLL/MaxFALL et MDCV proposés, simulation du tone mapping d'une TV, réalignement d'après la mesure de l'image. |
| `--hdr-display-peak` | Avec --hdr-advice : pic de l'écran simulé (défaut : dernier pic choisi dans HDR Studio, sinon 1000 nits). |
| `--hdr-tv-model` | Avec --hdr-advice : TV suivant la MaxCLL (défaut), le MDCV seul, ou dynamique (référence). — `maxcll`, `mdcv`, `dynamic` |

**Exemples**

```bash
muxiveo --cli inspect film.mkv
muxiveo --cli inspect film.mkv --config-template --with-encode > job.json
muxiveo --cli inspect film.mkv --hdr measure --hdr-advice --hdr-report rapport.html
```

### `validate`

Valider un job (ou un profil) sans rien exécuter ; --json publie le plan résolu (pistes, synchro, encodage).

```text
muxiveo --cli validate [-h] [--sync-mode {physical,container}] [--sync-subtitles {mirror,none}]
                       [--clean-nfo | --no-clean-nfo] [--remove-closed-captions | --no-remove-closed-captions]
                       [--crossfade-ms CROSSFADE_MS] [--auto-forced-subs]
                       [--forced-threshold FORCED_THRESHOLD] [--auto-sdh] [--auto-sync] [--auto-sync-all]
                       [--calibration CALIBRATION] [--detect-cuts]
                       [--quick-sync-check | --no-quick-sync-check] [--reject-coherent-sync]
                       [--drift-threshold-ms DRIFT_THRESHOLD_MS] [--cadence-auto | --no-cadence-auto]
                       [--cadence-method {auto,atempo,asetrate}] [--export-workflow EXPORT_WORKFLOW]
                       [--config CONFIG] [--ffmpeg FFMPEG] [--ffprobe FFPROBE] [--mediainfo MEDIAINFO]
                       [--work-dir WORK_DIR] [--threads THREADS] [--mux-backend {auto,native,ffmpeg}]
                       [--output-template OUTPUT_TEMPLATE] [--output-all] [--log-format {text,jsonl,events}]
                       [--verbose] [--profile PROFILE] [-i INPUT] [-o OUTPUT] [--auto-tmdb] [--tmdb]
                       [--tmdb-id TMDB_ID] [--tmdb-apikey TMDB_APIKEY] [--no-cover] [--no-attach] [--force]
                       [--dry-run] [--nfo] [--no-nfo] [--writing-application WRITING_APPLICATION]
                       [--encode-preset NOM|FICHIER] [--video-codec CODEC] [--rate-control MODE] [--crf CRF]
                       [--cq CQ] [--bitrate KBPS] [--target-size MIO] [--encoder-preset PRESET]
                       [--bit-depth {auto,8,10}] [--crop auto|HAUT,BAS,GAUCHE,DROITE] [--resize PRESET|LxH|N%]
                       [--deinterlace] [--denoise mvo-r2[:NIVEAU]|nlmeans[:FORCE]|off]
                       [--interpolate xN|CADENCE] [--interpolation-preset {fast,balanced,quality,ultra,light}]
                       [--tonemap [ALGO]] [--dv {copy,off}] [--hdr10plus {copy,off}] [--bake-fel]
                       [--audio-codec SOURCE:FLUX=CODEC[:KBPS]] [--video-track SOURCE:FLUX]
                       [--accept-dovi-nvenc-crop] [--keep-failed-work-dir] [--video-set CLÉ.POINTÉE=VALEUR]
                       [--json]
```

| Option | Effet |
|---|---|
| `--sync-mode` | Synchro des sources : physical (pistes réécrites, coupures et cadence) ou container (décalage du conteneur). — `physical`, `container` |
| `--sync-subtitles` | Sous-titres : mirror (suivent la synchro de leur source) ou none. — `mirror`, `none` |
| `--clean-nfo`, `--no-clean-nfo` | Nettoyer le NFO (balises techniques retirées). |
| `--remove-closed-captions`, `--no-remove-closed-captions` | Retirer les sous-titres embarqués EIA-608/708 détectés, sans réencodage |
| `--crossfade-ms` | Fondu aux raccords de synchro (ms, 0–1000). |
| `--auto-forced-subs` | Repérer les sous-titres forcés (peu d'événements) et les marquer. |
| `--forced-threshold` | Nombre d'événements sous lequel une piste de sous-titres est jugée forcée. (défaut : `50`) |
| `--auto-sdh` | Repérer les sous-titres SDH (malentendants) et les marquer. |
| `--auto-sync` | Analyser et recalibrer automatiquement les sources dynamiquement. |
| `--auto-sync-all` | Synchroniser chaque source ≥ 1 face à la source 0 (sources sans bloc `sync` du job). |
| `--calibration` | Fichier JSON de calibration explicite (outrepasse l'analyse dynamique). |
| `--detect-cuts` | Détecter les coupures et ruptures temporelles (multi-segments). |
| `--quick-sync-check`, `--no-quick-sync-check` | Avec --detect-cuts : pas d'analyse continue si les mesures ponctuelles concordent (confiance ≥ 0,90). Défaut : [sync] quick_audio_check. |
| `--reject-coherent-sync` | Avec --detect-cuts : refuser une synchro cohérente en partie non vérifiée (zones sans correspondance entourées du même décalage). Défaut : acceptée avec un avertissement. |
| `--drift-threshold-ms` | Seuil de dérive en ms pour détecter une coupure. (défaut : `25`) |
| `--cadence-auto`, `--no-cadence-auto` | Détection et conversion automatique de cadence PAL <-> Cinéma. (défaut : `True`) |
| `--cadence-method` | Méthode de conversion audio pour cadence : auto (analyse acoustique du pitch, défaut), atempo (tonalité préservée) ou asetrate (hauteur naturelle / pitch shift). — `auto`, `atempo`, `asetrate` |
| `--export-workflow` | Sauvegarder le workflow exact sans exécuter. |
| `--config` | Fichier JSON job/template. |
| `--ffmpeg` | Chemin ffmpeg override. |
| `--ffprobe` | Chemin ffprobe override. |
| `--mediainfo` | Chemin mediainfo override. |
| `--work-dir` | Répertoire de travail override. |
| `--threads` | Nombre de threads ffmpeg. |
| `--mux-backend` | Backend MKV : auto (défaut), native ou ffmpeg. Prioritaire sur le job. — `auto`, `native`, `ffmpeg` |
| `--output-template` | Template du nom de sortie. Placeholders disponibles : {source_name},{title},{year},{season},{episode},{episode_title},{season_episode},{group}, keywords pistes comme {audio-lang:best} (+ {season_num},{episode_num} pour formats numériques). Extension .mkv ajoutée si absente. |
| `--output-all` | Force les keywords pistes de --output-template en mode all (toutes les valeurs finales trouvées). |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Affiche la sortie ffmpeg en direct (progression, codecs, timing). Par défaut, seule la progression des étapes de workflow est affichée. |
| `--profile` | Profil décisionnel à appliquer aux entrées. |
| `-i`, `--input` | Source (répétable). |
| `-o`, `--output` | Sortie : fichier, ou dossier (avec / final). |
| `--auto-tmdb` | Chercher TMDB automatiquement et prendre le premier résultat. |
| `--tmdb` | Alias historique de --auto-tmdb. |
| `--tmdb-id` | ID TMDB explicite. |
| `--tmdb-apikey` | Clé API TMDB v3 (surcharge la config Muxiveo et le JSON job). |
| `--no-cover` | Ne pas ajouter la cover TMDB. |
| `--no-attach` | Ne pas inclure d'attachments ni de cover TMDB. |
| `--force` | Autoriser l'écrasement de la sortie. |
| `--dry-run` | Valider et afficher la commande sans exécuter. |
| `--nfo` | Écrire le NFO à côté de la sortie. |
| `--no-nfo` | Ne pas écrire de NFO. (défaut : `True`) |
| `--writing-application` | Balise WritingApp de la sortie. |
| `--encode-preset` | Preset d'encodage enregistré ou fichier JSON. |
| `--video-codec` | Codec vidéo (copy, libx265, hevc_nvenc…). |
| `--rate-control` | Mode de débit du codec (crf, vbr_cq, cbr…). |
| `--crf` | Qualité constante des codecs logiciels. |
| `--cq` | Qualité constante des codecs matériels. |
| `--bitrate` | Débit vidéo (kbit/s). |
| `--target-size` | Taille du fichier visée (mode taille). |
| `--encoder-preset` | Préréglage de l'encodeur (slow, p5…). |
| `--bit-depth` | Profondeur de sortie. — `auto`, `8`, `10` |
| `--crop` | Recadrage : auto (bandes noires mesurées) ou valeurs en pixels. |
| `--resize` | Redimensionnement (1080p, 1920x1080, 50%%). |
| `--deinterlace` | Désentrelacement YADIF. |
| `--denoise` | Débruitage : MVO-R2 (auto, leger, moyen, fort) ou NLMeans ; off le désactive. |
| `--interpolate` | Interpolation MVO-RIFE : facteur (x2, x2.5…) ou cadence cible (60, 60000/1001). |
| `--interpolation-preset` | Préréglage d'interpolation : fast, balanced, quality, ultra ou light. — `fast`, `balanced`, `quality`, `ultra`, `light` |
| `--tonemap` | Tone mapping vers SDR. |
| `--dv` | Dolby Vision de la source : copié ou retiré. — `copy`, `off` |
| `--hdr10plus` | HDR10+ de la source : copié ou retiré. — `copy`, `off` |
| `--bake-fel` | Intégrer la couche Dolby Vision FEL (mvo-fel). |
| `--audio-codec` | Réencoder une piste audio (ex. 0:1=eac3:640) ; répétable. |
| `--video-track` | Options vidéo ci-dessus appliquées à cette piste seulement (défaut : toutes). |
| `--accept-dovi-nvenc-crop` | Accepter le recadrage aligné sur 32 imposé par Dolby Vision + NVENC. |
| `--keep-failed-work-dir` | Encodage en échec : conserver le dossier de travail (fichiers produits). |
| `--video-set` | Champ vidéo du bloc encode (ex. restoration.mode=fast) ; valeur JSON ou texte. |
| `--json` | Sortie JSON structurée. |

**Exemples**

```bash
muxiveo --cli validate --config job.json --json
muxiveo --cli validate --profile Films -i film.mkv
```

### `preview`

Afficher les commandes prévues (FFmpeg, encodeur) sans les exécuter.

```text
muxiveo --cli preview [-h] [--sync-mode {physical,container}] [--sync-subtitles {mirror,none}]
                      [--clean-nfo | --no-clean-nfo] [--remove-closed-captions | --no-remove-closed-captions]
                      [--crossfade-ms CROSSFADE_MS] [--auto-forced-subs] [--forced-threshold FORCED_THRESHOLD]
                      [--auto-sdh] [--auto-sync] [--auto-sync-all] [--calibration CALIBRATION] [--detect-cuts]
                      [--quick-sync-check | --no-quick-sync-check] [--reject-coherent-sync]
                      [--drift-threshold-ms DRIFT_THRESHOLD_MS] [--cadence-auto | --no-cadence-auto]
                      [--cadence-method {auto,atempo,asetrate}] [--export-workflow EXPORT_WORKFLOW]
                      [--config CONFIG] [--ffmpeg FFMPEG] [--ffprobe FFPROBE] [--mediainfo MEDIAINFO]
                      [--work-dir WORK_DIR] [--threads THREADS] [--mux-backend {auto,native,ffmpeg}]
                      [--output-template OUTPUT_TEMPLATE] [--output-all] [--log-format {text,jsonl,events}]
                      [--verbose] [--profile PROFILE] [-i INPUT] [-o OUTPUT] [--auto-tmdb] [--tmdb]
                      [--tmdb-id TMDB_ID] [--tmdb-apikey TMDB_APIKEY] [--no-cover] [--no-attach] [--force]
                      [--dry-run] [--nfo] [--no-nfo] [--writing-application WRITING_APPLICATION]
                      [--encode-preset NOM|FICHIER] [--video-codec CODEC] [--rate-control MODE] [--crf CRF]
                      [--cq CQ] [--bitrate KBPS] [--target-size MIO] [--encoder-preset PRESET]
                      [--bit-depth {auto,8,10}] [--crop auto|HAUT,BAS,GAUCHE,DROITE] [--resize PRESET|LxH|N%]
                      [--deinterlace] [--denoise mvo-r2[:NIVEAU]|nlmeans[:FORCE]|off]
                      [--interpolate xN|CADENCE] [--interpolation-preset {fast,balanced,quality,ultra,light}]
                      [--tonemap [ALGO]] [--dv {copy,off}] [--hdr10plus {copy,off}] [--bake-fel]
                      [--audio-codec SOURCE:FLUX=CODEC[:KBPS]] [--video-track SOURCE:FLUX]
                      [--accept-dovi-nvenc-crop] [--keep-failed-work-dir] [--video-set CLÉ.POINTÉE=VALEUR]
                      [--json]
```

| Option | Effet |
|---|---|
| `--sync-mode` | Synchro des sources : physical (pistes réécrites, coupures et cadence) ou container (décalage du conteneur). — `physical`, `container` |
| `--sync-subtitles` | Sous-titres : mirror (suivent la synchro de leur source) ou none. — `mirror`, `none` |
| `--clean-nfo`, `--no-clean-nfo` | Nettoyer le NFO (balises techniques retirées). |
| `--remove-closed-captions`, `--no-remove-closed-captions` | Retirer les sous-titres embarqués EIA-608/708 détectés, sans réencodage |
| `--crossfade-ms` | Fondu aux raccords de synchro (ms, 0–1000). |
| `--auto-forced-subs` | Repérer les sous-titres forcés (peu d'événements) et les marquer. |
| `--forced-threshold` | Nombre d'événements sous lequel une piste de sous-titres est jugée forcée. (défaut : `50`) |
| `--auto-sdh` | Repérer les sous-titres SDH (malentendants) et les marquer. |
| `--auto-sync` | Analyser et recalibrer automatiquement les sources dynamiquement. |
| `--auto-sync-all` | Synchroniser chaque source ≥ 1 face à la source 0 (sources sans bloc `sync` du job). |
| `--calibration` | Fichier JSON de calibration explicite (outrepasse l'analyse dynamique). |
| `--detect-cuts` | Détecter les coupures et ruptures temporelles (multi-segments). |
| `--quick-sync-check`, `--no-quick-sync-check` | Avec --detect-cuts : pas d'analyse continue si les mesures ponctuelles concordent (confiance ≥ 0,90). Défaut : [sync] quick_audio_check. |
| `--reject-coherent-sync` | Avec --detect-cuts : refuser une synchro cohérente en partie non vérifiée (zones sans correspondance entourées du même décalage). Défaut : acceptée avec un avertissement. |
| `--drift-threshold-ms` | Seuil de dérive en ms pour détecter une coupure. (défaut : `25`) |
| `--cadence-auto`, `--no-cadence-auto` | Détection et conversion automatique de cadence PAL <-> Cinéma. (défaut : `True`) |
| `--cadence-method` | Méthode de conversion audio pour cadence : auto (analyse acoustique du pitch, défaut), atempo (tonalité préservée) ou asetrate (hauteur naturelle / pitch shift). — `auto`, `atempo`, `asetrate` |
| `--export-workflow` | Sauvegarder le workflow exact sans exécuter. |
| `--config` | Fichier JSON job/template. |
| `--ffmpeg` | Chemin ffmpeg override. |
| `--ffprobe` | Chemin ffprobe override. |
| `--mediainfo` | Chemin mediainfo override. |
| `--work-dir` | Répertoire de travail override. |
| `--threads` | Nombre de threads ffmpeg. |
| `--mux-backend` | Backend MKV : auto (défaut), native ou ffmpeg. Prioritaire sur le job. — `auto`, `native`, `ffmpeg` |
| `--output-template` | Template du nom de sortie. Placeholders disponibles : {source_name},{title},{year},{season},{episode},{episode_title},{season_episode},{group}, keywords pistes comme {audio-lang:best} (+ {season_num},{episode_num} pour formats numériques). Extension .mkv ajoutée si absente. |
| `--output-all` | Force les keywords pistes de --output-template en mode all (toutes les valeurs finales trouvées). |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Affiche la sortie ffmpeg en direct (progression, codecs, timing). Par défaut, seule la progression des étapes de workflow est affichée. |
| `--profile` | Profil décisionnel à appliquer aux entrées. |
| `-i`, `--input` | Source (répétable). |
| `-o`, `--output` | Sortie : fichier, ou dossier (avec / final). |
| `--auto-tmdb` | Chercher TMDB automatiquement et prendre le premier résultat. |
| `--tmdb` | Alias historique de --auto-tmdb. |
| `--tmdb-id` | ID TMDB explicite. |
| `--tmdb-apikey` | Clé API TMDB v3 (surcharge la config Muxiveo et le JSON job). |
| `--no-cover` | Ne pas ajouter la cover TMDB. |
| `--no-attach` | Ne pas inclure d'attachments ni de cover TMDB. |
| `--force` | Autoriser l'écrasement de la sortie. |
| `--dry-run` | Valider et afficher la commande sans exécuter. |
| `--nfo` | Écrire le NFO à côté de la sortie. |
| `--no-nfo` | Ne pas écrire de NFO. (défaut : `True`) |
| `--writing-application` | Balise WritingApp de la sortie. |
| `--encode-preset` | Preset d'encodage enregistré ou fichier JSON. |
| `--video-codec` | Codec vidéo (copy, libx265, hevc_nvenc…). |
| `--rate-control` | Mode de débit du codec (crf, vbr_cq, cbr…). |
| `--crf` | Qualité constante des codecs logiciels. |
| `--cq` | Qualité constante des codecs matériels. |
| `--bitrate` | Débit vidéo (kbit/s). |
| `--target-size` | Taille du fichier visée (mode taille). |
| `--encoder-preset` | Préréglage de l'encodeur (slow, p5…). |
| `--bit-depth` | Profondeur de sortie. — `auto`, `8`, `10` |
| `--crop` | Recadrage : auto (bandes noires mesurées) ou valeurs en pixels. |
| `--resize` | Redimensionnement (1080p, 1920x1080, 50%%). |
| `--deinterlace` | Désentrelacement YADIF. |
| `--denoise` | Débruitage : MVO-R2 (auto, leger, moyen, fort) ou NLMeans ; off le désactive. |
| `--interpolate` | Interpolation MVO-RIFE : facteur (x2, x2.5…) ou cadence cible (60, 60000/1001). |
| `--interpolation-preset` | Préréglage d'interpolation : fast, balanced, quality, ultra ou light. — `fast`, `balanced`, `quality`, `ultra`, `light` |
| `--tonemap` | Tone mapping vers SDR. |
| `--dv` | Dolby Vision de la source : copié ou retiré. — `copy`, `off` |
| `--hdr10plus` | HDR10+ de la source : copié ou retiré. — `copy`, `off` |
| `--bake-fel` | Intégrer la couche Dolby Vision FEL (mvo-fel). |
| `--audio-codec` | Réencoder une piste audio (ex. 0:1=eac3:640) ; répétable. |
| `--video-track` | Options vidéo ci-dessus appliquées à cette piste seulement (défaut : toutes). |
| `--accept-dovi-nvenc-crop` | Accepter le recadrage aligné sur 32 imposé par Dolby Vision + NVENC. |
| `--keep-failed-work-dir` | Encodage en échec : conserver le dossier de travail (fichiers produits). |
| `--video-set` | Champ vidéo du bloc encode (ex. restoration.mode=fast) ; valeur JSON ou texte. |
| `--json` | Sortie JSON structurée. |

**Exemples**

```bash
muxiveo --cli preview --config job.json
muxiveo --cli preview -i film.mkv -o sortie.mkv --video-codec libx265 --crf 18
```

### `remux`

Remuxer des sources selon le job ou les options (sélection des pistes, synchro, métadonnées).

```text
muxiveo --cli remux [-h] [--sync-mode {physical,container}] [--sync-subtitles {mirror,none}]
                    [--clean-nfo | --no-clean-nfo] [--remove-closed-captions | --no-remove-closed-captions]
                    [--crossfade-ms CROSSFADE_MS] [--auto-forced-subs] [--forced-threshold FORCED_THRESHOLD]
                    [--auto-sdh] [--auto-sync] [--auto-sync-all] [--calibration CALIBRATION] [--detect-cuts]
                    [--quick-sync-check | --no-quick-sync-check] [--reject-coherent-sync]
                    [--drift-threshold-ms DRIFT_THRESHOLD_MS] [--cadence-auto | --no-cadence-auto]
                    [--cadence-method {auto,atempo,asetrate}] [--export-workflow EXPORT_WORKFLOW]
                    [--config CONFIG] [--ffmpeg FFMPEG] [--ffprobe FFPROBE] [--mediainfo MEDIAINFO]
                    [--work-dir WORK_DIR] [--threads THREADS] [--mux-backend {auto,native,ffmpeg}]
                    [--output-template OUTPUT_TEMPLATE] [--output-all] [--log-format {text,jsonl,events}]
                    [--verbose] [--profile PROFILE] [-i INPUT] [-o OUTPUT] [--auto-tmdb] [--tmdb]
                    [--tmdb-id TMDB_ID] [--tmdb-apikey TMDB_APIKEY] [--no-cover] [--no-attach] [--force]
                    [--dry-run] [--nfo] [--no-nfo] [--writing-application WRITING_APPLICATION]
                    [--encode-preset NOM|FICHIER] [--video-codec CODEC] [--rate-control MODE] [--crf CRF]
                    [--cq CQ] [--bitrate KBPS] [--target-size MIO] [--encoder-preset PRESET]
                    [--bit-depth {auto,8,10}] [--crop auto|HAUT,BAS,GAUCHE,DROITE] [--resize PRESET|LxH|N%]
                    [--deinterlace] [--denoise mvo-r2[:NIVEAU]|nlmeans[:FORCE]|off] [--interpolate xN|CADENCE]
                    [--interpolation-preset {fast,balanced,quality,ultra,light}] [--tonemap [ALGO]]
                    [--dv {copy,off}] [--hdr10plus {copy,off}] [--bake-fel]
                    [--audio-codec SOURCE:FLUX=CODEC[:KBPS]] [--video-track SOURCE:FLUX]
                    [--accept-dovi-nvenc-crop] [--keep-failed-work-dir] [--video-set CLÉ.POINTÉE=VALEUR]
                    [--save SAVE]
```

| Option | Effet |
|---|---|
| `--sync-mode` | Synchro des sources : physical (pistes réécrites, coupures et cadence) ou container (décalage du conteneur). — `physical`, `container` |
| `--sync-subtitles` | Sous-titres : mirror (suivent la synchro de leur source) ou none. — `mirror`, `none` |
| `--clean-nfo`, `--no-clean-nfo` | Nettoyer le NFO (balises techniques retirées). |
| `--remove-closed-captions`, `--no-remove-closed-captions` | Retirer les sous-titres embarqués EIA-608/708 détectés, sans réencodage |
| `--crossfade-ms` | Fondu aux raccords de synchro (ms, 0–1000). |
| `--auto-forced-subs` | Repérer les sous-titres forcés (peu d'événements) et les marquer. |
| `--forced-threshold` | Nombre d'événements sous lequel une piste de sous-titres est jugée forcée. (défaut : `50`) |
| `--auto-sdh` | Repérer les sous-titres SDH (malentendants) et les marquer. |
| `--auto-sync` | Analyser et recalibrer automatiquement les sources dynamiquement. |
| `--auto-sync-all` | Synchroniser chaque source ≥ 1 face à la source 0 (sources sans bloc `sync` du job). |
| `--calibration` | Fichier JSON de calibration explicite (outrepasse l'analyse dynamique). |
| `--detect-cuts` | Détecter les coupures et ruptures temporelles (multi-segments). |
| `--quick-sync-check`, `--no-quick-sync-check` | Avec --detect-cuts : pas d'analyse continue si les mesures ponctuelles concordent (confiance ≥ 0,90). Défaut : [sync] quick_audio_check. |
| `--reject-coherent-sync` | Avec --detect-cuts : refuser une synchro cohérente en partie non vérifiée (zones sans correspondance entourées du même décalage). Défaut : acceptée avec un avertissement. |
| `--drift-threshold-ms` | Seuil de dérive en ms pour détecter une coupure. (défaut : `25`) |
| `--cadence-auto`, `--no-cadence-auto` | Détection et conversion automatique de cadence PAL <-> Cinéma. (défaut : `True`) |
| `--cadence-method` | Méthode de conversion audio pour cadence : auto (analyse acoustique du pitch, défaut), atempo (tonalité préservée) ou asetrate (hauteur naturelle / pitch shift). — `auto`, `atempo`, `asetrate` |
| `--export-workflow` | Sauvegarder le workflow exact sans exécuter. |
| `--config` | Fichier JSON job/template. |
| `--ffmpeg` | Chemin ffmpeg override. |
| `--ffprobe` | Chemin ffprobe override. |
| `--mediainfo` | Chemin mediainfo override. |
| `--work-dir` | Répertoire de travail override. |
| `--threads` | Nombre de threads ffmpeg. |
| `--mux-backend` | Backend MKV : auto (défaut), native ou ffmpeg. Prioritaire sur le job. — `auto`, `native`, `ffmpeg` |
| `--output-template` | Template du nom de sortie. Placeholders disponibles : {source_name},{title},{year},{season},{episode},{episode_title},{season_episode},{group}, keywords pistes comme {audio-lang:best} (+ {season_num},{episode_num} pour formats numériques). Extension .mkv ajoutée si absente. |
| `--output-all` | Force les keywords pistes de --output-template en mode all (toutes les valeurs finales trouvées). |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Affiche la sortie ffmpeg en direct (progression, codecs, timing). Par défaut, seule la progression des étapes de workflow est affichée. |
| `--profile` | Profil décisionnel à appliquer aux entrées. |
| `-i`, `--input` | Source (répétable). |
| `-o`, `--output` | Sortie : fichier, ou dossier (avec / final). |
| `--auto-tmdb` | Chercher TMDB automatiquement et prendre le premier résultat. |
| `--tmdb` | Alias historique de --auto-tmdb. |
| `--tmdb-id` | ID TMDB explicite. |
| `--tmdb-apikey` | Clé API TMDB v3 (surcharge la config Muxiveo et le JSON job). |
| `--no-cover` | Ne pas ajouter la cover TMDB. |
| `--no-attach` | Ne pas inclure d'attachments ni de cover TMDB. |
| `--force` | Autoriser l'écrasement de la sortie. |
| `--dry-run` | Valider et afficher la commande sans exécuter. |
| `--nfo` | Écrire le NFO à côté de la sortie. |
| `--no-nfo` | Ne pas écrire de NFO. (défaut : `True`) |
| `--writing-application` | Balise WritingApp de la sortie. |
| `--encode-preset` | Preset d'encodage enregistré ou fichier JSON. |
| `--video-codec` | Codec vidéo (copy, libx265, hevc_nvenc…). |
| `--rate-control` | Mode de débit du codec (crf, vbr_cq, cbr…). |
| `--crf` | Qualité constante des codecs logiciels. |
| `--cq` | Qualité constante des codecs matériels. |
| `--bitrate` | Débit vidéo (kbit/s). |
| `--target-size` | Taille du fichier visée (mode taille). |
| `--encoder-preset` | Préréglage de l'encodeur (slow, p5…). |
| `--bit-depth` | Profondeur de sortie. — `auto`, `8`, `10` |
| `--crop` | Recadrage : auto (bandes noires mesurées) ou valeurs en pixels. |
| `--resize` | Redimensionnement (1080p, 1920x1080, 50%%). |
| `--deinterlace` | Désentrelacement YADIF. |
| `--denoise` | Débruitage : MVO-R2 (auto, leger, moyen, fort) ou NLMeans ; off le désactive. |
| `--interpolate` | Interpolation MVO-RIFE : facteur (x2, x2.5…) ou cadence cible (60, 60000/1001). |
| `--interpolation-preset` | Préréglage d'interpolation : fast, balanced, quality, ultra ou light. — `fast`, `balanced`, `quality`, `ultra`, `light` |
| `--tonemap` | Tone mapping vers SDR. |
| `--dv` | Dolby Vision de la source : copié ou retiré. — `copy`, `off` |
| `--hdr10plus` | HDR10+ de la source : copié ou retiré. — `copy`, `off` |
| `--bake-fel` | Intégrer la couche Dolby Vision FEL (mvo-fel). |
| `--audio-codec` | Réencoder une piste audio (ex. 0:1=eac3:640) ; répétable. |
| `--video-track` | Options vidéo ci-dessus appliquées à cette piste seulement (défaut : toutes). |
| `--accept-dovi-nvenc-crop` | Accepter le recadrage aligné sur 32 imposé par Dolby Vision + NVENC. |
| `--keep-failed-work-dir` | Encodage en échec : conserver le dossier de travail (fichiers produits). |
| `--video-set` | Champ vidéo du bloc encode (ex. restoration.mode=fast) ; valeur JSON ou texte. |
| `--save` | Sauvegarder les options/règles en template réutilisable. |

**Exemples**

```bash
muxiveo --cli remux -i film.mkv -o sortie.mkv
muxiveo --cli remux --config job.json --save gabarit.json
```

### `run`

Exécuter un job JSON (exact-job ou contrat v1), remux ou encodage selon son bloc `encode`.

```text
muxiveo --cli run [-h] [--sync-mode {physical,container}] [--sync-subtitles {mirror,none}]
                  [--clean-nfo | --no-clean-nfo] [--remove-closed-captions | --no-remove-closed-captions]
                  [--crossfade-ms CROSSFADE_MS] [--auto-forced-subs] [--forced-threshold FORCED_THRESHOLD]
                  [--auto-sdh] [--auto-sync] [--auto-sync-all] [--calibration CALIBRATION] [--detect-cuts]
                  [--quick-sync-check | --no-quick-sync-check] [--reject-coherent-sync]
                  [--drift-threshold-ms DRIFT_THRESHOLD_MS] [--cadence-auto | --no-cadence-auto]
                  [--cadence-method {auto,atempo,asetrate}] [--export-workflow EXPORT_WORKFLOW]
                  [--config CONFIG] [--ffmpeg FFMPEG] [--ffprobe FFPROBE] [--mediainfo MEDIAINFO]
                  [--work-dir WORK_DIR] [--threads THREADS] [--mux-backend {auto,native,ffmpeg}]
                  [--output-template OUTPUT_TEMPLATE] [--output-all] [--log-format {text,jsonl,events}]
                  [--verbose] [--profile PROFILE] [-i INPUT] [-o OUTPUT] [--auto-tmdb] [--tmdb]
                  [--tmdb-id TMDB_ID] [--tmdb-apikey TMDB_APIKEY] [--no-cover] [--no-attach] [--force]
                  [--dry-run] [--nfo] [--no-nfo] [--writing-application WRITING_APPLICATION]
                  [--encode-preset NOM|FICHIER] [--video-codec CODEC] [--rate-control MODE] [--crf CRF]
                  [--cq CQ] [--bitrate KBPS] [--target-size MIO] [--encoder-preset PRESET]
                  [--bit-depth {auto,8,10}] [--crop auto|HAUT,BAS,GAUCHE,DROITE] [--resize PRESET|LxH|N%]
                  [--deinterlace] [--denoise mvo-r2[:NIVEAU]|nlmeans[:FORCE]|off] [--interpolate xN|CADENCE]
                  [--interpolation-preset {fast,balanced,quality,ultra,light}] [--tonemap [ALGO]]
                  [--dv {copy,off}] [--hdr10plus {copy,off}] [--bake-fel]
                  [--audio-codec SOURCE:FLUX=CODEC[:KBPS]] [--video-track SOURCE:FLUX]
                  [--accept-dovi-nvenc-crop] [--keep-failed-work-dir] [--video-set CLÉ.POINTÉE=VALEUR]
```

| Option | Effet |
|---|---|
| `--sync-mode` | Synchro des sources : physical (pistes réécrites, coupures et cadence) ou container (décalage du conteneur). — `physical`, `container` |
| `--sync-subtitles` | Sous-titres : mirror (suivent la synchro de leur source) ou none. — `mirror`, `none` |
| `--clean-nfo`, `--no-clean-nfo` | Nettoyer le NFO (balises techniques retirées). |
| `--remove-closed-captions`, `--no-remove-closed-captions` | Retirer les sous-titres embarqués EIA-608/708 détectés, sans réencodage |
| `--crossfade-ms` | Fondu aux raccords de synchro (ms, 0–1000). |
| `--auto-forced-subs` | Repérer les sous-titres forcés (peu d'événements) et les marquer. |
| `--forced-threshold` | Nombre d'événements sous lequel une piste de sous-titres est jugée forcée. (défaut : `50`) |
| `--auto-sdh` | Repérer les sous-titres SDH (malentendants) et les marquer. |
| `--auto-sync` | Analyser et recalibrer automatiquement les sources dynamiquement. |
| `--auto-sync-all` | Synchroniser chaque source ≥ 1 face à la source 0 (sources sans bloc `sync` du job). |
| `--calibration` | Fichier JSON de calibration explicite (outrepasse l'analyse dynamique). |
| `--detect-cuts` | Détecter les coupures et ruptures temporelles (multi-segments). |
| `--quick-sync-check`, `--no-quick-sync-check` | Avec --detect-cuts : pas d'analyse continue si les mesures ponctuelles concordent (confiance ≥ 0,90). Défaut : [sync] quick_audio_check. |
| `--reject-coherent-sync` | Avec --detect-cuts : refuser une synchro cohérente en partie non vérifiée (zones sans correspondance entourées du même décalage). Défaut : acceptée avec un avertissement. |
| `--drift-threshold-ms` | Seuil de dérive en ms pour détecter une coupure. (défaut : `25`) |
| `--cadence-auto`, `--no-cadence-auto` | Détection et conversion automatique de cadence PAL <-> Cinéma. (défaut : `True`) |
| `--cadence-method` | Méthode de conversion audio pour cadence : auto (analyse acoustique du pitch, défaut), atempo (tonalité préservée) ou asetrate (hauteur naturelle / pitch shift). — `auto`, `atempo`, `asetrate` |
| `--export-workflow` | Sauvegarder le workflow exact sans exécuter. |
| `--config` | Fichier JSON job/template. |
| `--ffmpeg` | Chemin ffmpeg override. |
| `--ffprobe` | Chemin ffprobe override. |
| `--mediainfo` | Chemin mediainfo override. |
| `--work-dir` | Répertoire de travail override. |
| `--threads` | Nombre de threads ffmpeg. |
| `--mux-backend` | Backend MKV : auto (défaut), native ou ffmpeg. Prioritaire sur le job. — `auto`, `native`, `ffmpeg` |
| `--output-template` | Template du nom de sortie. Placeholders disponibles : {source_name},{title},{year},{season},{episode},{episode_title},{season_episode},{group}, keywords pistes comme {audio-lang:best} (+ {season_num},{episode_num} pour formats numériques). Extension .mkv ajoutée si absente. |
| `--output-all` | Force les keywords pistes de --output-template en mode all (toutes les valeurs finales trouvées). |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Affiche la sortie ffmpeg en direct (progression, codecs, timing). Par défaut, seule la progression des étapes de workflow est affichée. |
| `--profile` | Profil décisionnel à appliquer aux entrées. |
| `-i`, `--input` | Source (répétable). |
| `-o`, `--output` | Sortie : fichier, ou dossier (avec / final). |
| `--auto-tmdb` | Chercher TMDB automatiquement et prendre le premier résultat. |
| `--tmdb` | Alias historique de --auto-tmdb. |
| `--tmdb-id` | ID TMDB explicite. |
| `--tmdb-apikey` | Clé API TMDB v3 (surcharge la config Muxiveo et le JSON job). |
| `--no-cover` | Ne pas ajouter la cover TMDB. |
| `--no-attach` | Ne pas inclure d'attachments ni de cover TMDB. |
| `--force` | Autoriser l'écrasement de la sortie. |
| `--dry-run` | Valider et afficher la commande sans exécuter. |
| `--nfo` | Écrire le NFO à côté de la sortie. |
| `--no-nfo` | Ne pas écrire de NFO. (défaut : `True`) |
| `--writing-application` | Balise WritingApp de la sortie. |
| `--encode-preset` | Preset d'encodage enregistré ou fichier JSON. |
| `--video-codec` | Codec vidéo (copy, libx265, hevc_nvenc…). |
| `--rate-control` | Mode de débit du codec (crf, vbr_cq, cbr…). |
| `--crf` | Qualité constante des codecs logiciels. |
| `--cq` | Qualité constante des codecs matériels. |
| `--bitrate` | Débit vidéo (kbit/s). |
| `--target-size` | Taille du fichier visée (mode taille). |
| `--encoder-preset` | Préréglage de l'encodeur (slow, p5…). |
| `--bit-depth` | Profondeur de sortie. — `auto`, `8`, `10` |
| `--crop` | Recadrage : auto (bandes noires mesurées) ou valeurs en pixels. |
| `--resize` | Redimensionnement (1080p, 1920x1080, 50%%). |
| `--deinterlace` | Désentrelacement YADIF. |
| `--denoise` | Débruitage : MVO-R2 (auto, leger, moyen, fort) ou NLMeans ; off le désactive. |
| `--interpolate` | Interpolation MVO-RIFE : facteur (x2, x2.5…) ou cadence cible (60, 60000/1001). |
| `--interpolation-preset` | Préréglage d'interpolation : fast, balanced, quality, ultra ou light. — `fast`, `balanced`, `quality`, `ultra`, `light` |
| `--tonemap` | Tone mapping vers SDR. |
| `--dv` | Dolby Vision de la source : copié ou retiré. — `copy`, `off` |
| `--hdr10plus` | HDR10+ de la source : copié ou retiré. — `copy`, `off` |
| `--bake-fel` | Intégrer la couche Dolby Vision FEL (mvo-fel). |
| `--audio-codec` | Réencoder une piste audio (ex. 0:1=eac3:640) ; répétable. |
| `--video-track` | Options vidéo ci-dessus appliquées à cette piste seulement (défaut : toutes). |
| `--accept-dovi-nvenc-crop` | Accepter le recadrage aligné sur 32 imposé par Dolby Vision + NVENC. |
| `--keep-failed-work-dir` | Encodage en échec : conserver le dossier de travail (fichiers produits). |
| `--video-set` | Champ vidéo du bloc encode (ex. restoration.mode=fast) ; valeur JSON ou texte. |

**Exemples**

```bash
muxiveo --cli run --config job.json
muxiveo --cli run --config job.json --dry-run
```

### `batch`

Appliquer un gabarit (ou un profil) à un lot de jobs ou de fichiers ; collisions refusées avant le premier job.

```text
muxiveo --cli batch [-h] [--sync-mode {physical,container}] [--sync-subtitles {mirror,none}]
                    [--clean-nfo | --no-clean-nfo] [--remove-closed-captions | --no-remove-closed-captions]
                    [--crossfade-ms CROSSFADE_MS] [--auto-forced-subs] [--forced-threshold FORCED_THRESHOLD]
                    [--auto-sdh] [--auto-sync] [--auto-sync-all] [--calibration CALIBRATION] [--detect-cuts]
                    [--quick-sync-check | --no-quick-sync-check] [--reject-coherent-sync]
                    [--drift-threshold-ms DRIFT_THRESHOLD_MS] [--cadence-auto | --no-cadence-auto]
                    [--cadence-method {auto,atempo,asetrate}] [--export-workflow EXPORT_WORKFLOW]
                    [--config CONFIG] [--ffmpeg FFMPEG] [--ffprobe FFPROBE] [--mediainfo MEDIAINFO]
                    [--work-dir WORK_DIR] [--threads THREADS] [--mux-backend {auto,native,ffmpeg}]
                    [--output-template OUTPUT_TEMPLATE] [--output-all] [--log-format {text,jsonl,events}]
                    [--verbose] [--template TEMPLATE] [--profile PROFILE] [--batch BATCH] [-i INPUT]
                    [--input-dir INPUT_DIR] [--recursive] [--include INCLUDE] [--exclude EXCLUDE]
                    [--output-dir OUTPUT_DIR] [--auto-tmdb] [--tmdb] [--tmdb-id TMDB_ID]
                    [--tmdb-apikey TMDB_APIKEY] [--no-cover] [--no-attach] [--force] [--dry-run]
                    [--continue-on-error] [--summary SUMMARY] [--nfo] [--no-nfo]
                    [--writing-application WRITING_APPLICATION] [--encode-preset NOM|FICHIER]
                    [--video-codec CODEC] [--rate-control MODE] [--crf CRF] [--cq CQ] [--bitrate KBPS]
                    [--target-size MIO] [--encoder-preset PRESET] [--bit-depth {auto,8,10}]
                    [--crop auto|HAUT,BAS,GAUCHE,DROITE] [--resize PRESET|LxH|N%] [--deinterlace]
                    [--denoise mvo-r2[:NIVEAU]|nlmeans[:FORCE]|off] [--interpolate xN|CADENCE]
                    [--interpolation-preset {fast,balanced,quality,ultra,light}] [--tonemap [ALGO]]
                    [--dv {copy,off}] [--hdr10plus {copy,off}] [--bake-fel]
                    [--audio-codec SOURCE:FLUX=CODEC[:KBPS]] [--video-track SOURCE:FLUX]
                    [--accept-dovi-nvenc-crop] [--keep-failed-work-dir] [--video-set CLÉ.POINTÉE=VALEUR]
```

| Option | Effet |
|---|---|
| `--sync-mode` | Synchro des sources : physical (pistes réécrites, coupures et cadence) ou container (décalage du conteneur). — `physical`, `container` |
| `--sync-subtitles` | Sous-titres : mirror (suivent la synchro de leur source) ou none. — `mirror`, `none` |
| `--clean-nfo`, `--no-clean-nfo` | Nettoyer le NFO (balises techniques retirées). |
| `--remove-closed-captions`, `--no-remove-closed-captions` | Retirer les sous-titres embarqués EIA-608/708 détectés, sans réencodage |
| `--crossfade-ms` | Fondu aux raccords de synchro (ms, 0–1000). |
| `--auto-forced-subs` | Repérer les sous-titres forcés (peu d'événements) et les marquer. |
| `--forced-threshold` | Nombre d'événements sous lequel une piste de sous-titres est jugée forcée. (défaut : `50`) |
| `--auto-sdh` | Repérer les sous-titres SDH (malentendants) et les marquer. |
| `--auto-sync` | Analyser et recalibrer automatiquement les sources dynamiquement. |
| `--auto-sync-all` | Synchroniser chaque source ≥ 1 face à la source 0 (sources sans bloc `sync` du job). |
| `--calibration` | Fichier JSON de calibration explicite (outrepasse l'analyse dynamique). |
| `--detect-cuts` | Détecter les coupures et ruptures temporelles (multi-segments). |
| `--quick-sync-check`, `--no-quick-sync-check` | Avec --detect-cuts : pas d'analyse continue si les mesures ponctuelles concordent (confiance ≥ 0,90). Défaut : [sync] quick_audio_check. |
| `--reject-coherent-sync` | Avec --detect-cuts : refuser une synchro cohérente en partie non vérifiée (zones sans correspondance entourées du même décalage). Défaut : acceptée avec un avertissement. |
| `--drift-threshold-ms` | Seuil de dérive en ms pour détecter une coupure. (défaut : `25`) |
| `--cadence-auto`, `--no-cadence-auto` | Détection et conversion automatique de cadence PAL <-> Cinéma. (défaut : `True`) |
| `--cadence-method` | Méthode de conversion audio pour cadence : auto (analyse acoustique du pitch, défaut), atempo (tonalité préservée) ou asetrate (hauteur naturelle / pitch shift). — `auto`, `atempo`, `asetrate` |
| `--export-workflow` | Sauvegarder le workflow exact sans exécuter. |
| `--config` | Fichier JSON job/template. |
| `--ffmpeg` | Chemin ffmpeg override. |
| `--ffprobe` | Chemin ffprobe override. |
| `--mediainfo` | Chemin mediainfo override. |
| `--work-dir` | Répertoire de travail override. |
| `--threads` | Nombre de threads ffmpeg. |
| `--mux-backend` | Backend MKV : auto (défaut), native ou ffmpeg. Prioritaire sur le job. — `auto`, `native`, `ffmpeg` |
| `--output-template` | Template du nom de sortie. Placeholders disponibles : {source_name},{title},{year},{season},{episode},{episode_title},{season_episode},{group}, keywords pistes comme {audio-lang:best} (+ {season_num},{episode_num} pour formats numériques). Extension .mkv ajoutée si absente. |
| `--output-all` | Force les keywords pistes de --output-template en mode all (toutes les valeurs finales trouvées). |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Affiche la sortie ffmpeg en direct (progression, codecs, timing). Par défaut, seule la progression des étapes de workflow est affichée. |
| `--template` | Gabarit JSON appliqué à chaque job du lot. |
| `--profile` | Profil décisionnel à appliquer au lot. |
| `--batch` | JSON contenant `jobs` ou `inputs`. |
| `-i`, `--input` | Entrée batch simple; répétable. |
| `--input-dir` | Dossier à scanner pour créer un job par vidéo; répétable. |
| `--recursive` | Scanner récursivement les dossiers fournis avec --input-dir. |
| `--include` | Glob de fichiers à inclure lors du scan de dossiers; répétable. |
| `--exclude` | Glob de fichiers à exclure lors du scan de dossiers; répétable. |
| `--output-dir` | Dossier des sorties. |
| `--auto-tmdb` | Chercher TMDB automatiquement et prendre le premier résultat. |
| `--tmdb` | Alias historique de --auto-tmdb. |
| `--tmdb-id` | ID TMDB explicite. |
| `--tmdb-apikey` | Clé API TMDB v3 (surcharge la config Muxiveo et le JSON job). |
| `--no-cover` | Ne pas ajouter la cover TMDB. |
| `--no-attach` | Ne pas inclure d'attachments ni de cover TMDB. |
| `--force` | Remplacer une sortie existante. |
| `--dry-run` | Valider et afficher les commandes sans exécuter. |
| `--continue-on-error` | Continuer le lot après un échec. |
| `--summary` | Ecrire un rapport JSON final. |
| `--nfo` | Écrire le NFO à côté de la sortie. |
| `--no-nfo` | Ne pas écrire de NFO. (défaut : `True`) |
| `--writing-application` | Balise WritingApp de la sortie. |
| `--encode-preset` | Preset d'encodage enregistré ou fichier JSON. |
| `--video-codec` | Codec vidéo (copy, libx265, hevc_nvenc…). |
| `--rate-control` | Mode de débit du codec (crf, vbr_cq, cbr…). |
| `--crf` | Qualité constante des codecs logiciels. |
| `--cq` | Qualité constante des codecs matériels. |
| `--bitrate` | Débit vidéo (kbit/s). |
| `--target-size` | Taille du fichier visée (mode taille). |
| `--encoder-preset` | Préréglage de l'encodeur (slow, p5…). |
| `--bit-depth` | Profondeur de sortie. — `auto`, `8`, `10` |
| `--crop` | Recadrage : auto (bandes noires mesurées) ou valeurs en pixels. |
| `--resize` | Redimensionnement (1080p, 1920x1080, 50%%). |
| `--deinterlace` | Désentrelacement YADIF. |
| `--denoise` | Débruitage : MVO-R2 (auto, leger, moyen, fort) ou NLMeans ; off le désactive. |
| `--interpolate` | Interpolation MVO-RIFE : facteur (x2, x2.5…) ou cadence cible (60, 60000/1001). |
| `--interpolation-preset` | Préréglage d'interpolation : fast, balanced, quality, ultra ou light. — `fast`, `balanced`, `quality`, `ultra`, `light` |
| `--tonemap` | Tone mapping vers SDR. |
| `--dv` | Dolby Vision de la source : copié ou retiré. — `copy`, `off` |
| `--hdr10plus` | HDR10+ de la source : copié ou retiré. — `copy`, `off` |
| `--bake-fel` | Intégrer la couche Dolby Vision FEL (mvo-fel). |
| `--audio-codec` | Réencoder une piste audio (ex. 0:1=eac3:640) ; répétable. |
| `--video-track` | Options vidéo ci-dessus appliquées à cette piste seulement (défaut : toutes). |
| `--accept-dovi-nvenc-crop` | Accepter le recadrage aligné sur 32 imposé par Dolby Vision + NVENC. |
| `--keep-failed-work-dir` | Encodage en échec : conserver le dossier de travail (fichiers produits). |
| `--video-set` | Champ vidéo du bloc encode (ex. restoration.mode=fast) ; valeur JSON ou texte. |

**Exemples**

```bash
muxiveo --cli batch --template gabarit.json --input-dir /films --output-dir /sorties
muxiveo --cli batch --template gabarit.json --batch jobs.json --continue-on-error --summary bilan.json
```

### `extract`

Extrait des éléments d'une source vers des fichiers autonomes : piste en flux élémentaire (ou Matroska avec --format mkv), couche EL / BL d'un Dolby Vision profil 7, RPU, HDR10+, pièce jointe. Chaque sortie est écrite dans un fichier .partial puis publiée.

```text
muxiveo --cli extract [-h] -i FICHIER [--track SEL] [--layer {stream,el,bl,rpu,hdr10plus}]
                      [--attachment N|NOM] [--all] [-o SORTIE] [--output-dir DOSSIER] [--format {raw,mkv}]
                      [--force] [--dry-run] [--json] [--ffmpeg FFMPEG] [--ffprobe FFPROBE]
                      [--mediainfo MEDIAINFO] [--work-dir WORK_DIR] [--log-format {text,jsonl,events}]
                      [--verbose]
```

| Option | Effet |
|---|---|
| `-i`, `--input` | Fichier source. |
| `--track` | Piste : index ffprobe ou nature et rang (v:0, a:1, s:0) ; répétable. |
| `--layer` | Couche de la piste --track qui précède (défaut : stream) : flux, EL / BL (Dolby Vision profil 7), RPU, HDR10+. — `stream`, `el`, `bl`, `rpu`, `hdr10plus` |
| `--attachment` | Pièce jointe : rang affiché par inspect ou nom de fichier ; répétable. |
| `--all` | Toutes les pistes extractibles et toutes les pièces jointes. |
| `-o`, `--output` | Fichier de sortie (un seul élément). |
| `--output-dir` | Dossier des sorties (défaut : celui de la source). |
| `--format` | raw : flux élémentaire (défaut) ; mkv : piste dans un conteneur Matroska. — `raw`, `mkv` |
| `--force` | Remplacer les sorties existantes. |
| `--dry-run` | Lister les sorties prévues sans rien extraire. |
| `--json` | Résultat JSON sur stdout. |
| `--ffmpeg` | Chemin ffmpeg override. |
| `--ffprobe` | Chemin ffprobe override. |
| `--mediainfo` | Chemin mediainfo override. |
| `--work-dir` | Répertoire de travail override. |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Journal détaillé. |

**Exemples**

```bash
extract -i film.mkv --track a:0 --output-dir pistes/
extract -i film.mkv --track v:0 --layer el -o film.el.hevc
extract -i film.mkv --track v:0 --layer rpu --track v:0 --layer hdr10plus
extract -i film.mkv --all --output-dir film/
```

### `bluray-titles`

Lister les titres (playlists) d'un Blu-ray, du plus long au plus court.

```text
muxiveo --cli bluray-titles [-h] [--min-duration S] [--json] [--log-format {text,jsonl,events}] [--verbose]
                            DISQUE
```

| Option | Effet |
|---|---|
| `path` | Racine du disque, dossier BDMV ou fichier .mpls / .m2ts. |
| `--min-duration` | Durée minimale (s). |
| `--json` | Résultat JSON sur stdout. |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Journal détaillé (sortie brute des outils). |

**Exemples**

```bash
muxiveo --cli bluray-titles /disques/FILM --min-duration 600
```

## Encodage

### `encode`

Encoder : job + bloc `encode` ou options d'encodage ; aperçus réels avec --preview-image / --preview-video.

```text
muxiveo --cli encode [-h] [--sync-mode {physical,container}] [--sync-subtitles {mirror,none}]
                     [--clean-nfo | --no-clean-nfo] [--remove-closed-captions | --no-remove-closed-captions]
                     [--crossfade-ms CROSSFADE_MS] [--auto-forced-subs] [--forced-threshold FORCED_THRESHOLD]
                     [--auto-sdh] [--auto-sync] [--auto-sync-all] [--calibration CALIBRATION] [--detect-cuts]
                     [--quick-sync-check | --no-quick-sync-check] [--reject-coherent-sync]
                     [--drift-threshold-ms DRIFT_THRESHOLD_MS] [--cadence-auto | --no-cadence-auto]
                     [--cadence-method {auto,atempo,asetrate}] [--export-workflow EXPORT_WORKFLOW]
                     [--config CONFIG] [--ffmpeg FFMPEG] [--ffprobe FFPROBE] [--mediainfo MEDIAINFO]
                     [--work-dir WORK_DIR] [--threads THREADS] [--mux-backend {auto,native,ffmpeg}]
                     [--output-template OUTPUT_TEMPLATE] [--output-all] [--log-format {text,jsonl,events}]
                     [--verbose] [--profile PROFILE] [-i INPUT] [-o OUTPUT] [--auto-tmdb] [--tmdb]
                     [--tmdb-id TMDB_ID] [--tmdb-apikey TMDB_APIKEY] [--no-cover] [--no-attach] [--force]
                     [--dry-run] [--nfo] [--no-nfo] [--writing-application WRITING_APPLICATION]
                     [--encode-preset NOM|FICHIER] [--video-codec CODEC] [--rate-control MODE] [--crf CRF]
                     [--cq CQ] [--bitrate KBPS] [--target-size MIO] [--encoder-preset PRESET]
                     [--bit-depth {auto,8,10}] [--crop auto|HAUT,BAS,GAUCHE,DROITE] [--resize PRESET|LxH|N%]
                     [--deinterlace] [--denoise mvo-r2[:NIVEAU]|nlmeans[:FORCE]|off]
                     [--interpolate xN|CADENCE] [--interpolation-preset {fast,balanced,quality,ultra,light}]
                     [--tonemap [ALGO]] [--dv {copy,off}] [--hdr10plus {copy,off}] [--bake-fel]
                     [--audio-codec SOURCE:FLUX=CODEC[:KBPS]] [--video-track SOURCE:FLUX]
                     [--accept-dovi-nvenc-crop] [--keep-failed-work-dir] [--video-set CLÉ.POINTÉE=VALEUR]
                     [--preview-image SECONDES] [--preview-video SECONDES:DURÉE] [--random-scene]
                     [--preview-dir DOSSIER]
```

| Option | Effet |
|---|---|
| `--sync-mode` | Synchro des sources : physical (pistes réécrites, coupures et cadence) ou container (décalage du conteneur). — `physical`, `container` |
| `--sync-subtitles` | Sous-titres : mirror (suivent la synchro de leur source) ou none. — `mirror`, `none` |
| `--clean-nfo`, `--no-clean-nfo` | Nettoyer le NFO (balises techniques retirées). |
| `--remove-closed-captions`, `--no-remove-closed-captions` | Retirer les sous-titres embarqués EIA-608/708 détectés, sans réencodage |
| `--crossfade-ms` | Fondu aux raccords de synchro (ms, 0–1000). |
| `--auto-forced-subs` | Repérer les sous-titres forcés (peu d'événements) et les marquer. |
| `--forced-threshold` | Nombre d'événements sous lequel une piste de sous-titres est jugée forcée. (défaut : `50`) |
| `--auto-sdh` | Repérer les sous-titres SDH (malentendants) et les marquer. |
| `--auto-sync` | Analyser et recalibrer automatiquement les sources dynamiquement. |
| `--auto-sync-all` | Synchroniser chaque source ≥ 1 face à la source 0 (sources sans bloc `sync` du job). |
| `--calibration` | Fichier JSON de calibration explicite (outrepasse l'analyse dynamique). |
| `--detect-cuts` | Détecter les coupures et ruptures temporelles (multi-segments). |
| `--quick-sync-check`, `--no-quick-sync-check` | Avec --detect-cuts : pas d'analyse continue si les mesures ponctuelles concordent (confiance ≥ 0,90). Défaut : [sync] quick_audio_check. |
| `--reject-coherent-sync` | Avec --detect-cuts : refuser une synchro cohérente en partie non vérifiée (zones sans correspondance entourées du même décalage). Défaut : acceptée avec un avertissement. |
| `--drift-threshold-ms` | Seuil de dérive en ms pour détecter une coupure. (défaut : `25`) |
| `--cadence-auto`, `--no-cadence-auto` | Détection et conversion automatique de cadence PAL <-> Cinéma. (défaut : `True`) |
| `--cadence-method` | Méthode de conversion audio pour cadence : auto (analyse acoustique du pitch, défaut), atempo (tonalité préservée) ou asetrate (hauteur naturelle / pitch shift). — `auto`, `atempo`, `asetrate` |
| `--export-workflow` | Sauvegarder le workflow exact sans exécuter. |
| `--config` | Fichier JSON job/template. |
| `--ffmpeg` | Chemin ffmpeg override. |
| `--ffprobe` | Chemin ffprobe override. |
| `--mediainfo` | Chemin mediainfo override. |
| `--work-dir` | Répertoire de travail override. |
| `--threads` | Nombre de threads ffmpeg. |
| `--mux-backend` | Backend MKV : auto (défaut), native ou ffmpeg. Prioritaire sur le job. — `auto`, `native`, `ffmpeg` |
| `--output-template` | Template du nom de sortie. Placeholders disponibles : {source_name},{title},{year},{season},{episode},{episode_title},{season_episode},{group}, keywords pistes comme {audio-lang:best} (+ {season_num},{episode_num} pour formats numériques). Extension .mkv ajoutée si absente. |
| `--output-all` | Force les keywords pistes de --output-template en mode all (toutes les valeurs finales trouvées). |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Affiche la sortie ffmpeg en direct (progression, codecs, timing). Par défaut, seule la progression des étapes de workflow est affichée. |
| `--profile` | Profil décisionnel à appliquer aux entrées. |
| `-i`, `--input` | Source (répétable). |
| `-o`, `--output` | Sortie : fichier, ou dossier (avec / final). |
| `--auto-tmdb` | Chercher TMDB automatiquement et prendre le premier résultat. |
| `--tmdb` | Alias historique de --auto-tmdb. |
| `--tmdb-id` | ID TMDB explicite. |
| `--tmdb-apikey` | Clé API TMDB v3 (surcharge la config Muxiveo et le JSON job). |
| `--no-cover` | Ne pas ajouter la cover TMDB. |
| `--no-attach` | Ne pas inclure d'attachments ni de cover TMDB. |
| `--force` | Autoriser l'écrasement de la sortie. |
| `--dry-run` | Valider et afficher la commande sans exécuter. |
| `--nfo` | Écrire le NFO à côté de la sortie. |
| `--no-nfo` | Ne pas écrire de NFO. (défaut : `True`) |
| `--writing-application` | Balise WritingApp de la sortie. |
| `--encode-preset` | Preset d'encodage enregistré ou fichier JSON. |
| `--video-codec` | Codec vidéo (copy, libx265, hevc_nvenc…). |
| `--rate-control` | Mode de débit du codec (crf, vbr_cq, cbr…). |
| `--crf` | Qualité constante des codecs logiciels. |
| `--cq` | Qualité constante des codecs matériels. |
| `--bitrate` | Débit vidéo (kbit/s). |
| `--target-size` | Taille du fichier visée (mode taille). |
| `--encoder-preset` | Préréglage de l'encodeur (slow, p5…). |
| `--bit-depth` | Profondeur de sortie. — `auto`, `8`, `10` |
| `--crop` | Recadrage : auto (bandes noires mesurées) ou valeurs en pixels. |
| `--resize` | Redimensionnement (1080p, 1920x1080, 50%%). |
| `--deinterlace` | Désentrelacement YADIF. |
| `--denoise` | Débruitage : MVO-R2 (auto, leger, moyen, fort) ou NLMeans ; off le désactive. |
| `--interpolate` | Interpolation MVO-RIFE : facteur (x2, x2.5…) ou cadence cible (60, 60000/1001). |
| `--interpolation-preset` | Préréglage d'interpolation : fast, balanced, quality, ultra ou light. — `fast`, `balanced`, `quality`, `ultra`, `light` |
| `--tonemap` | Tone mapping vers SDR. |
| `--dv` | Dolby Vision de la source : copié ou retiré. — `copy`, `off` |
| `--hdr10plus` | HDR10+ de la source : copié ou retiré. — `copy`, `off` |
| `--bake-fel` | Intégrer la couche Dolby Vision FEL (mvo-fel). |
| `--audio-codec` | Réencoder une piste audio (ex. 0:1=eac3:640) ; répétable. |
| `--video-track` | Options vidéo ci-dessus appliquées à cette piste seulement (défaut : toutes). |
| `--accept-dovi-nvenc-crop` | Accepter le recadrage aligné sur 32 imposé par Dolby Vision + NVENC. |
| `--keep-failed-work-dir` | Encodage en échec : conserver le dossier de travail (fichiers produits). |
| `--video-set` | Champ vidéo du bloc encode (ex. restoration.mode=fast) ; valeur JSON ou texte. |
| `--preview-image` | Image(s) encodée(s) à cet instant ; répétable. |
| `--preview-video` | Extrait vidéo encodé (ex. 600:10 ; durée 5–30 s). |
| `--random-scene` | Instant d'aperçu tiré au hasard. |
| `--preview-dir` | Dossier des aperçus (défaut : celui de la sortie). |

**Exemples**

```bash
muxiveo --cli encode -i film.mkv -o film.x265.mkv --video-codec libx265 --crf 18
muxiveo --cli encode -i film.mkv -o sortie.mkv --encode-preset "Films 4K" --preview-image 600
```

### `encode-preset`

Presets d'encodage enregistrés (communs avec l'interface).

```text
muxiveo --cli encode-preset [-h] {list,show,save,import,export,delete} ...
```

**Exemples**

```bash
muxiveo --cli encode-preset list
muxiveo --cli encode-preset save "x265 rapide" --video-codec libx265 --crf 22
```

### `encode-preset list`

Lister les presets d'encodage.

```text
muxiveo --cli encode-preset list [-h] [--log-format {text,jsonl,events}]
```

| Option | Effet |
|---|---|
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |

**Exemples**

```bash
muxiveo --cli encode-preset list
```

### `encode-preset show`

Afficher un preset d'encodage (JSON).

```text
muxiveo --cli encode-preset show [-h] [--log-format {text,jsonl,events}] NOM
```

| Option | Effet |
|---|---|
| `name` | Nom du preset. |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |

**Exemples**

```bash
muxiveo --cli encode-preset show "x265 rapide"
```

### `encode-preset save`

Enregistrer un preset d'après les options d'encodage (réglages d'encodeur seulement).

```text
muxiveo --cli encode-preset save [-h] [--description DESCRIPTION] [--encode-preset NOM|FICHIER]
                                 [--video-codec CODEC] [--rate-control MODE] [--crf CRF] [--cq CQ]
                                 [--bitrate KBPS] [--target-size MIO] [--encoder-preset PRESET]
                                 [--bit-depth {auto,8,10}] [--crop auto|HAUT,BAS,GAUCHE,DROITE]
                                 [--resize PRESET|LxH|N%] [--deinterlace]
                                 [--denoise mvo-r2[:NIVEAU]|nlmeans[:FORCE]|off] [--interpolate xN|CADENCE]
                                 [--interpolation-preset {fast,balanced,quality,ultra,light}]
                                 [--tonemap [ALGO]] [--dv {copy,off}] [--hdr10plus {copy,off}] [--bake-fel]
                                 [--audio-codec SOURCE:FLUX=CODEC[:KBPS]] [--video-track SOURCE:FLUX]
                                 [--accept-dovi-nvenc-crop] [--keep-failed-work-dir]
                                 [--video-set CLÉ.POINTÉE=VALEUR] [--force] [--log-format {text,jsonl,events}]
                                 NOM
```

| Option | Effet |
|---|---|
| `name` | Nom du preset. |
| `--description` | Description enregistrée. |
| `--encode-preset` | Preset d'encodage enregistré ou fichier JSON. |
| `--video-codec` | Codec vidéo (copy, libx265, hevc_nvenc…). |
| `--rate-control` | Mode de débit du codec (crf, vbr_cq, cbr…). |
| `--crf` | Qualité constante des codecs logiciels. |
| `--cq` | Qualité constante des codecs matériels. |
| `--bitrate` | Débit vidéo (kbit/s). |
| `--target-size` | Taille du fichier visée (mode taille). |
| `--encoder-preset` | Préréglage de l'encodeur (slow, p5…). |
| `--bit-depth` | Profondeur de sortie. — `auto`, `8`, `10` |
| `--crop` | Recadrage : auto (bandes noires mesurées) ou valeurs en pixels. |
| `--resize` | Redimensionnement (1080p, 1920x1080, 50%%). |
| `--deinterlace` | Désentrelacement YADIF. |
| `--denoise` | Débruitage : MVO-R2 (auto, leger, moyen, fort) ou NLMeans ; off le désactive. |
| `--interpolate` | Interpolation MVO-RIFE : facteur (x2, x2.5…) ou cadence cible (60, 60000/1001). |
| `--interpolation-preset` | Préréglage d'interpolation : fast, balanced, quality, ultra ou light. — `fast`, `balanced`, `quality`, `ultra`, `light` |
| `--tonemap` | Tone mapping vers SDR. |
| `--dv` | Dolby Vision de la source : copié ou retiré. — `copy`, `off` |
| `--hdr10plus` | HDR10+ de la source : copié ou retiré. — `copy`, `off` |
| `--bake-fel` | Intégrer la couche Dolby Vision FEL (mvo-fel). |
| `--audio-codec` | Réencoder une piste audio (ex. 0:1=eac3:640) ; répétable. |
| `--video-track` | Options vidéo ci-dessus appliquées à cette piste seulement (défaut : toutes). |
| `--accept-dovi-nvenc-crop` | Accepter le recadrage aligné sur 32 imposé par Dolby Vision + NVENC. |
| `--keep-failed-work-dir` | Encodage en échec : conserver le dossier de travail (fichiers produits). |
| `--video-set` | Champ vidéo du bloc encode (ex. restoration.mode=fast) ; valeur JSON ou texte. |
| `--force` | Remplacer l'existant. |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |

**Exemples**

```bash
muxiveo --cli encode-preset save "x265 rapide" --video-codec libx265 --crf 22 --encoder-preset fast
```

### `encode-preset import`

Importer un preset depuis un fichier JSON.

```text
muxiveo --cli encode-preset import [-h] [--name IMPORT_NAME] [--force] [--log-format {text,jsonl,events}]
                                   FICHIER
```

| Option | Effet |
|---|---|
| `file` | Fichier JSON du preset. |
| `--name` | Nom enregistré (défaut : celui du fichier). |
| `--force` | Remplacer l'existant. |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |

**Exemples**

```bash
muxiveo --cli encode-preset import preset.json
```

### `encode-preset export`

Exporter un preset dans un fichier JSON.

```text
muxiveo --cli encode-preset export [-h] [--force] [--log-format {text,jsonl,events}] NOM FICHIER
```

| Option | Effet |
|---|---|
| `name` | Nom du preset. |
| `file` | Fichier JSON écrit. |
| `--force` | Remplacer l'existant. |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |

**Exemples**

```bash
muxiveo --cli encode-preset export "x265 rapide" preset.json
```

### `encode-preset delete`

Supprimer un preset d'encodage.

```text
muxiveo --cli encode-preset delete [-h] [--log-format {text,jsonl,events}] NOM
```

| Option | Effet |
|---|---|
| `name` | Nom du preset. |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |

**Exemples**

```bash
muxiveo --cli encode-preset delete "x265 rapide"
```

### `capabilities`

Capacités de la machine : encodeurs, préréglages, Vulkan, NVOF, TensorRT, moteur vidéo MVO.

```text
muxiveo --cli capabilities [-h] [--section {encoders,presets,vulkan,gpu,rife}] [--ffmpeg FFMPEG]
                           [--log-format {text,jsonl,events}]
```

| Option | Effet |
|---|---|
| `--section` | Section à sonder ; répétable (défaut : toutes). — `encoders`, `presets`, `vulkan`, `gpu`, `rife` |
| `--ffmpeg` | Chemin ffmpeg pour cette sonde. |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |

**Exemples**

```bash
muxiveo --cli capabilities
muxiveo --cli capabilities --section encoders
```

## HDR

### `hdr-patch`

Écrit une copie corrigée d'un MKV ou d'un HEVC brut : HDR10 statique (MaxCLL/MaxFALL, MDCV), RPU Dolby Vision (L6, L5, L11, écran du RPU, retrait CM v4.0), HDR10+ (retrait, TSDML, profil A, plafond, recalcul depuis la mesure), ou propositions de réalignement d'après la mesure de l'image. Les autres pistes, chapitres, pièces jointes et balises sont conservés.

```text
muxiveo --cli hdr-patch [-h] -i FICHIER -o SORTIE [--hdr-track N] [--max-cll NITS] [--max-fall NITS]
                        [--light-levels {suggested}] [--mdcv suggested|PRIMAIRES:MIN:MAX]
                        [--l6 MAX:MIN:CLL:FALL] [--l5 G,D,H,B] [--l11 TYPE[:POINT_BLANC[:ref]]]
                        [--rpu-display MIN:MAX] [--remove-cmv4] [--no-mastering-sync] [--hdr10plus-remove]
                        [--hdr10plus-target NITS] [--hdr10plus-profile-a] [--hdr10plus-cap NITS]
                        [--hdr10plus-recompute] [--realign all|static,mdcv,l6,hdr10plus]
                        [--measure {cached,fast,full}] [--display-peak NITS] [--force] [--dry-run] [--json]
                        [--ffmpeg FFMPEG] [--ffprobe FFPROBE] [--mediainfo MEDIAINFO] [--work-dir WORK_DIR]
                        [--threads THREADS] [--log-format {text,jsonl,events}] [--verbose]
```

| Option | Effet |
|---|---|
| `-i`, `--input` | Source (MKV ou HEVC brut). |
| `-o`, `--output` | Fichier corrigé (autre que la source). |
| `--hdr-track` | Index ffprobe de la piste vidéo (défaut : première). |
| `--max-cll` | MaxCLL écrite (avec --max-fall). |
| `--max-fall` | MaxFALL écrite (avec --max-cll). |
| `--light-levels` | MaxCLL/MaxFALL proposés : mesure de l'image > HDR10+ > L1 (--max-cll/--max-fall les remplacent un à un). — `suggested` |
| `--mdcv` | Écran de mastering : « suggested » (L6 > écran du RPU > écran usuel) ou « Display P3\|BT.2020\|BT.709:MIN:MAX » en nits. |
| `--l6` | L6 : mastering max. et min. (nits), MaxCLL, MaxFALL. |
| `--l5` | Zone active L5 de toutes les images (pixels). |
| `--l11` | L11 : type de contenu 0–4 (défaut, cinéma, jeu, sport, utilisateur), point blanc 0–15, « ref » : mode de référence. |
| `--rpu-display` | Écran de mastering du RPU (nits). |
| `--remove-cmv4` | Retirer les métadonnées CM v4.0. |
| `--no-mastering-sync` | Ne pas garder identiques MDCV, mastering du L6 et écran du RPU (recopie par défaut). |
| `--hdr10plus-remove` | Retirer le HDR10+. |
| `--hdr10plus-target` | TargetedSystemDisplayMaximumLuminance de toutes les images. |
| `--hdr10plus-profile-a` | Retirer la courbe de Bézier (profil A). |
| `--hdr10plus-cap` | Plafond des MaxScl, AverageRGB et percentiles. |
| `--hdr10plus-recompute` | Recalculer le HDR10+ depuis la mesure complète de l'image (profil A). |
| `--realign` | Retenir les propositions de réalignement d'après la mesure (valeurs explicites prioritaires). |
| `--measure` | Mesure de l'image : en cache seulement (défaut), rapide (images clés) ou complète. — `cached`, `fast`, `full` |
| `--display-peak` | Écran simulé du réalignement (défaut : dernier pic choisi dans HDR Studio, sinon 1000 nits). |
| `--force` | Remplacer la sortie si elle existe. |
| `--dry-run` | Afficher le plan résolu sans écrire. |
| `--json` | Résultat JSON sur stdout. |
| `--ffmpeg` | Chemin ffmpeg override. |
| `--ffprobe` | Chemin ffprobe override. |
| `--mediainfo` | Chemin mediainfo override. |
| `--work-dir` | Répertoire de travail override. |
| `--threads` | Fils de la mesure de l'image. |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Journal détaillé. |

**Exemples**

```bash
hdr-patch -i film.mkv -o film.hdrfix.mkv --max-cll 1000 --max-fall 400
hdr-patch -i film.mkv -o fix.mkv --light-levels suggested --mdcv suggested --dry-run
hdr-patch -i film.mkv -o fix.mkv --mdcv 'Display P3:0.0001:1000' --l6 1000:0.0001:1000:400
hdr-patch -i film.mkv -o fix.mkv --realign all --measure fast
hdr-patch -i film.mkv -o fix.mkv --hdr10plus-recompute --measure full
```

## Synchro

### `sync-scan`

Mesurer le calage d'une piste (audio ou sous-titres) face à une référence ; calibration JSON.

```text
muxiveo --cli sync-scan [-h] [--sync-mode {physical,container}] [--sync-subtitles {mirror,none}]
                        [--clean-nfo | --no-clean-nfo]
                        [--remove-closed-captions | --no-remove-closed-captions] [--crossfade-ms CROSSFADE_MS]
                        [--auto-forced-subs] [--forced-threshold FORCED_THRESHOLD] [--auto-sdh] [--auto-sync]
                        [--auto-sync-all] [--calibration CALIBRATION] [--detect-cuts]
                        [--quick-sync-check | --no-quick-sync-check] [--reject-coherent-sync]
                        [--drift-threshold-ms DRIFT_THRESHOLD_MS] [--cadence-auto | --no-cadence-auto]
                        [--cadence-method {auto,atempo,asetrate}] [--export-workflow EXPORT_WORKFLOW]
                        [--config CONFIG] [--ffmpeg FFMPEG] [--ffprobe FFPROBE] [--mediainfo MEDIAINFO]
                        [--work-dir WORK_DIR] [--threads THREADS] [--mux-backend {auto,native,ffmpeg}]
                        [--output-template OUTPUT_TEMPLATE] [--output-all] [--log-format {text,jsonl,events}]
                        [--verbose] --ref REF --target TARGET [--stream-ref STREAM_REF]
                        [--stream-target STREAM_TARGET] [--type {auto,audio,subtitle}]
                        [--output-json OUTPUT_JSON]
```

| Option | Effet |
|---|---|
| `--sync-mode` | Synchro des sources : physical (pistes réécrites, coupures et cadence) ou container (décalage du conteneur). — `physical`, `container` |
| `--sync-subtitles` | Sous-titres : mirror (suivent la synchro de leur source) ou none. — `mirror`, `none` |
| `--clean-nfo`, `--no-clean-nfo` | Nettoyer le NFO (balises techniques retirées). |
| `--remove-closed-captions`, `--no-remove-closed-captions` | Retirer les sous-titres embarqués EIA-608/708 détectés, sans réencodage |
| `--crossfade-ms` | Fondu aux raccords de synchro (ms, 0–1000). |
| `--auto-forced-subs` | Repérer les sous-titres forcés (peu d'événements) et les marquer. |
| `--forced-threshold` | Nombre d'événements sous lequel une piste de sous-titres est jugée forcée. (défaut : `50`) |
| `--auto-sdh` | Repérer les sous-titres SDH (malentendants) et les marquer. |
| `--auto-sync` | Analyser et recalibrer automatiquement les sources dynamiquement. |
| `--auto-sync-all` | Synchroniser chaque source ≥ 1 face à la source 0 (sources sans bloc `sync` du job). |
| `--calibration` | Fichier JSON de calibration explicite (outrepasse l'analyse dynamique). |
| `--detect-cuts` | Détecter les coupures et ruptures temporelles (multi-segments). |
| `--quick-sync-check`, `--no-quick-sync-check` | Avec --detect-cuts : pas d'analyse continue si les mesures ponctuelles concordent (confiance ≥ 0,90). Défaut : [sync] quick_audio_check. |
| `--reject-coherent-sync` | Avec --detect-cuts : refuser une synchro cohérente en partie non vérifiée (zones sans correspondance entourées du même décalage). Défaut : acceptée avec un avertissement. |
| `--drift-threshold-ms` | Seuil de dérive en ms pour détecter une coupure. (défaut : `25`) |
| `--cadence-auto`, `--no-cadence-auto` | Détection et conversion automatique de cadence PAL <-> Cinéma. (défaut : `True`) |
| `--cadence-method` | Méthode de conversion audio pour cadence : auto (analyse acoustique du pitch, défaut), atempo (tonalité préservée) ou asetrate (hauteur naturelle / pitch shift). — `auto`, `atempo`, `asetrate` |
| `--export-workflow` | Sauvegarder le workflow exact sans exécuter. |
| `--config` | Fichier JSON job/template. |
| `--ffmpeg` | Chemin ffmpeg override. |
| `--ffprobe` | Chemin ffprobe override. |
| `--mediainfo` | Chemin mediainfo override. |
| `--work-dir` | Répertoire de travail override. |
| `--threads` | Nombre de threads ffmpeg. |
| `--mux-backend` | Backend MKV : auto (défaut), native ou ffmpeg. Prioritaire sur le job. — `auto`, `native`, `ffmpeg` |
| `--output-template` | Template du nom de sortie. Placeholders disponibles : {source_name},{title},{year},{season},{episode},{episode_title},{season_episode},{group}, keywords pistes comme {audio-lang:best} (+ {season_num},{episode_num} pour formats numériques). Extension .mkv ajoutée si absente. |
| `--output-all` | Force les keywords pistes de --output-template en mode all (toutes les valeurs finales trouvées). |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Affiche la sortie ffmpeg en direct (progression, codecs, timing). Par défaut, seule la progression des étapes de workflow est affichée. |
| `--ref` | Fichier de référence (master : vidéo, chapitres). |
| `--target` | Fichier à caler. |
| `--stream-ref` | Flux de référence (spécificateur FFmpeg, ex. 0:a:0). (défaut : `0:a:0`) |
| `--stream-target` | Flux à caler (spécificateur FFmpeg). (défaut : `0:a:0`) |
| `--type` | Type d'analyse : auto (détection par extension/flux), audio ou subtitle. (défaut : `auto`) — `auto`, `audio`, `subtitle` |
| `--output-json` | Calibration écrite dans ce fichier JSON. |

**Exemples**

```bash
muxiveo --cli sync-scan --ref uhd.mkv --target vf.mkv --output-json calib.json
muxiveo --cli sync-scan --ref uhd.mkv --target vf.mkv --detect-cuts
```

### `shift-subs`

Recaler un fichier de sous-titres texte (SRT, ASS) : décalage fixe ou calibration.

```text
muxiveo --cli shift-subs [-h] [--config CONFIG] [--ffmpeg FFMPEG] [--ffprobe FFPROBE] [--mediainfo MEDIAINFO]
                         [--work-dir WORK_DIR] [--threads THREADS] [--mux-backend {auto,native,ffmpeg}]
                         [--output-template OUTPUT_TEMPLATE] [--output-all] [--log-format {text,jsonl,events}]
                         [--verbose] -i INPUT -o OUTPUT (--offset-ms OFFSET_MS | --calibration CALIBRATION)
                         [--force]
```

| Option | Effet |
|---|---|
| `--config` | Fichier JSON job/template. |
| `--ffmpeg` | Chemin ffmpeg override. |
| `--ffprobe` | Chemin ffprobe override. |
| `--mediainfo` | Chemin mediainfo override. |
| `--work-dir` | Répertoire de travail override. |
| `--threads` | Nombre de threads ffmpeg. |
| `--mux-backend` | Backend MKV : auto (défaut), native ou ffmpeg. Prioritaire sur le job. — `auto`, `native`, `ffmpeg` |
| `--output-template` | Template du nom de sortie. Placeholders disponibles : {source_name},{title},{year},{season},{episode},{episode_title},{season_episode},{group}, keywords pistes comme {audio-lang:best} (+ {season_num},{episode_num} pour formats numériques). Extension .mkv ajoutée si absente. |
| `--output-all` | Force les keywords pistes de --output-template en mode all (toutes les valeurs finales trouvées). |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Affiche la sortie ffmpeg en direct (progression, codecs, timing). Par défaut, seule la progression des étapes de workflow est affichée. |
| `-i`, `--input` | Source (répétable). |
| `-o`, `--output` | Sortie : fichier, ou dossier (avec / final). |
| `--offset-ms` | Décalage à appliquer (ms). |
| `--calibration` | Fichier JSON de calibration explicite (remplace l'analyse). |
| `--force` | Remplacer une sortie existante. |

**Exemples**

```bash
muxiveo --cli shift-subs -i vf.srt -o vf.cale.srt --offset-ms 1250
muxiveo --cli shift-subs -i vf.srt -o vf.cale.srt --calibration calib.json
```

### `calibration`

Calibrations de synchro (fichiers JSON de sync-scan, du Synchro Studio ou des exact-jobs) : mêmes éditions que le Synchro Studio.

```text
muxiveo --cli calibration [-h] {show,edit,compose,invert} ...
```

**Exemples**

```bash
muxiveo --cli calibration show calib.json
muxiveo --cli calibration edit calib.json --cut 600000-602500 -o calib2.json
```

### `calibration show`

Décrire une calibration : segments, coupes, plages de sortie, cadence.

```text
muxiveo --cli calibration show [-h] [--log-format {text,jsonl,events}] [--verbose] FICHIER
```

| Option | Effet |
|---|---|
| `file` | Fichier JSON de calibration. |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Journal détaillé (sortie brute des outils). |

**Exemples**

```bash
muxiveo --cli calibration show calib.json
```

### `calibration edit`

Éditions appliquées dans l'ordre de la ligne de commande. Instants de SORTIE en ms ou HH:MM:SS.mmm.

```text
muxiveo --cli calibration edit [-h] [--cut DÉBUT-FIN] [--silence DÉBUT-FIN] [--remove-junction N]
                               [--shift-ms MS] [--shift-from-segment N:MS]
                               [--cadence-method {atempo,asetrate,auto}]
                               [--resolve-cadence REF:INDEX CIBLE:INDEX] [-o SORTIE] [--force]
                               [--log-format {text,jsonl,events}] [--verbose] FICHIER
```

| Option | Effet |
|---|---|
| `file` | Fichier JSON de calibration. |
| `--cut` | Supprimer cette section de la sortie. |
| `--silence` | Insérer un silence de la longueur de cette section. |
| `--remove-junction` | Retirer la jonction du segment N. |
| `--shift-ms` | Départ fixé à ce décalage (la suite suit). |
| `--shift-from-segment` | Décalage du segment N fixé à MS ; l'écart est reporté sur les segments suivants. |
| `--cadence-method` | Méthode de conversion de cadence (calibration avec cadence). — `atempo`, `asetrate`, `auto` |
| `--resolve-cadence` | Trancher la méthode « auto » par l'analyse de hauteur (fichiers et index ffprobe). |
| `-o`, `--output` | Calibration écrite (sinon JSON sur stdout). |
| `--force` | Remplacer le fichier de sortie. |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Journal détaillé (sortie brute des outils). |

**Exemples**

```bash
calibration edit calib.json --cut 600000-602500 -o calib2.json
calibration edit calib.json --silence 1:00:00-1:00:04 --shift-from-segment 2:-120 -o calib2.json
calibration edit calib.json --cadence-method auto --resolve-cadence ref.mkv:1 cible.mkv:2
```

### `calibration compose`

Composer deux calibrations : mesure face à une référence elle-même synchronisée.

```text
muxiveo --cli calibration compose [-h] [-o SORTIE] [--force] [--log-format {text,jsonl,events}] [--verbose]
                                  MESURE POSITION
```

| Option | Effet |
|---|---|
| `inner` | Cible face à l'audio d'origine de la référence. |
| `outer` | Position de la référence dans la sortie. |
| `-o`, `--output` | Calibration écrite (sinon JSON sur stdout). |
| `--force` | Remplacer le fichier de sortie. |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Journal détaillé (sortie brute des outils). |

**Exemples**

```bash
muxiveo --cli calibration compose mesure.json position.json -o synchro.json
```

### `calibration invert`

Inverser une calibration (référence face à la cible).

```text
muxiveo --cli calibration invert [-h] [-o SORTIE] [--force] [--log-format {text,jsonl,events}] [--verbose]
                                 FICHIER
```

| Option | Effet |
|---|---|
| `file` | Fichier JSON de calibration. |
| `-o`, `--output` | Calibration écrite (sinon JSON sur stdout). |
| `--force` | Remplacer le fichier de sortie. |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Journal détaillé (sortie brute des outils). |

**Exemples**

```bash
muxiveo --cli calibration invert synchro.json -o inverse.json
```

### `sync-waveform`

Forme d'onde d'une piste audio sur une plage (pics par tranche, JSON).

```text
muxiveo --cli sync-waveform [-h] -i FICHIER [--stream STREAM] [--start INSTANT] [--duration S]
                            [--resolution-ms MS] [--ffmpeg FFMPEG] [--log-format {text,jsonl,events}]
                            [--verbose]
```

| Option | Effet |
|---|---|
| `-i`, `--input` | Source (répétable). |
| `--stream` | Flux : index ffprobe ou spécificateur (défaut 0:a:0). |
| `--start` | Début (s ou HH:MM:SS.mmm). (défaut : `0`) |
| `--duration` | Durée en secondes (défaut 60). |
| `--resolution-ms` | Largeur d'une tranche (ms). (défaut : `10`) |
| `--ffmpeg` | Chemin ffmpeg override. |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Journal détaillé (sortie brute des outils). |

**Exemples**

```bash
muxiveo --cli sync-waveform -i film.mkv --stream 0:a:0 --start 1:00:00 --duration 30
```

## Hybrid

### `hybrid`

Hybrider une paire ou une saison sur le moteur du Hybrid Studio : vidéo du master, audio et sous-titres des donneurs calés ; plan, exact-jobs, recette, encodage par épisode.

```text
muxiveo --cli hybrid [-h] [--sync-mode {physical,container}] [--sync-subtitles {mirror,none}]
                     [--clean-nfo | --no-clean-nfo] [--remove-closed-captions | --no-remove-closed-captions]
                     [--crossfade-ms CROSSFADE_MS] [--auto-forced-subs] [--forced-threshold FORCED_THRESHOLD]
                     [--auto-sdh] [--auto-sync] [--auto-sync-all] [--calibration CALIBRATION] [--detect-cuts]
                     [--quick-sync-check | --no-quick-sync-check] [--reject-coherent-sync]
                     [--drift-threshold-ms DRIFT_THRESHOLD_MS] [--cadence-auto | --no-cadence-auto]
                     [--cadence-method {auto,atempo,asetrate}] [--export-workflow EXPORT_WORKFLOW]
                     [--config CONFIG] [--ffmpeg FFMPEG] [--ffprobe FFPROBE] [--mediainfo MEDIAINFO]
                     [--work-dir WORK_DIR] [--threads THREADS] [--mux-backend {auto,native,ffmpeg}]
                     [--output-template OUTPUT_TEMPLATE] [--output-all] [--log-format {text,jsonl,events}]
                     [--verbose] [--ref REF] [--donor DONOR] [--ref-dir REF_DIR] [--donor-dir DONOR_DIR]
                     [--profile PROFILE] [--report-json REPORT_JSON] -o OUTPUT_DIR [--auto-tmdb [AUTO_TMDB]]
                     [--tmdb-apikey TMDB_APIKEY] [--no-cover] [--tag TAG] [--purge-temp] [--dry-run] [--force]
                     [--continue-on-error] [--source RÔLE=CHEMIN] [--matching {episode,fuzzy,order}]
                     [--pair MASTER::DONNEUR] [--recipe FICHIER] [--keep-master-audio LANGUES]
                     [--donor-audio-langs LANGUES] [--donor-sub-langs LANGUES] [--exclude-langs LANGUES]
                     [--sync {auto,manual,none}] [--shift-ms SHIFT_MS] [--plan] [--export-jobs DOSSIER]
                     [--save-recipe FICHIER] [--encode-preset NOM|FICHIER] [--video-codec CODEC]
                     [--rate-control MODE] [--crf CRF] [--cq CQ] [--bitrate KBPS] [--target-size MIO]
                     [--encoder-preset PRESET] [--bit-depth {auto,8,10}] [--crop auto|HAUT,BAS,GAUCHE,DROITE]
                     [--resize PRESET|LxH|N%] [--deinterlace] [--denoise mvo-r2[:NIVEAU]|nlmeans[:FORCE]|off]
                     [--interpolate xN|CADENCE] [--interpolation-preset {fast,balanced,quality,ultra,light}]
                     [--tonemap [ALGO]] [--dv {copy,off}] [--hdr10plus {copy,off}] [--bake-fel]
                     [--audio-codec SOURCE:FLUX=CODEC[:KBPS]] [--video-track SOURCE:FLUX]
                     [--accept-dovi-nvenc-crop] [--keep-failed-work-dir] [--video-set CLÉ.POINTÉE=VALEUR]
```

| Option | Effet |
|---|---|
| `--sync-mode` | Synchro des sources : physical (pistes réécrites, coupures et cadence) ou container (décalage du conteneur). (défaut : `physical`) — `physical`, `container` |
| `--sync-subtitles` | Sous-titres : mirror (suivent la synchro de leur source) ou none. (défaut : `mirror`) — `mirror`, `none` |
| `--clean-nfo`, `--no-clean-nfo` | Nettoyer le NFO (balises techniques retirées). (défaut : `True`) |
| `--remove-closed-captions`, `--no-remove-closed-captions` | Retirer les sous-titres embarqués EIA-608/708 détectés, sans réencodage |
| `--crossfade-ms` | Fondu aux raccords de synchro (ms, 0–1000). (défaut : `80`) |
| `--auto-forced-subs` | Repérer les sous-titres forcés (peu d'événements) et les marquer. |
| `--forced-threshold` | Nombre d'événements sous lequel une piste de sous-titres est jugée forcée. (défaut : `50`) |
| `--auto-sdh` | Repérer les sous-titres SDH (malentendants) et les marquer. |
| `--auto-sync` | Analyser et recalibrer automatiquement les sources dynamiquement. |
| `--auto-sync-all` | Synchroniser chaque source ≥ 1 face à la source 0 (sources sans bloc `sync` du job). |
| `--calibration` | Fichier JSON de calibration explicite (outrepasse l'analyse dynamique). |
| `--detect-cuts` | Détecter les coupures et ruptures temporelles (multi-segments). |
| `--quick-sync-check`, `--no-quick-sync-check` | Avec --detect-cuts : pas d'analyse continue si les mesures ponctuelles concordent (confiance ≥ 0,90). Défaut : [sync] quick_audio_check. |
| `--reject-coherent-sync` | Avec --detect-cuts : refuser une synchro cohérente en partie non vérifiée (zones sans correspondance entourées du même décalage). Défaut : acceptée avec un avertissement. |
| `--drift-threshold-ms` | Seuil de dérive en ms pour détecter une coupure. (défaut : `25`) |
| `--cadence-auto`, `--no-cadence-auto` | Détection et conversion automatique de cadence PAL <-> Cinéma. (défaut : `True`) |
| `--cadence-method` | Méthode de conversion audio pour cadence : auto (analyse acoustique du pitch, défaut), atempo (tonalité préservée) ou asetrate (hauteur naturelle / pitch shift). — `auto`, `atempo`, `asetrate` |
| `--export-workflow` | Sauvegarder le workflow exact sans exécuter. |
| `--config` | Fichier JSON job/template. |
| `--ffmpeg` | Chemin ffmpeg override. |
| `--ffprobe` | Chemin ffprobe override. |
| `--mediainfo` | Chemin mediainfo override. |
| `--work-dir` | Répertoire de travail override. |
| `--threads` | Nombre de threads ffmpeg. |
| `--mux-backend` | Backend MKV : auto (défaut), native ou ffmpeg. Prioritaire sur le job. — `auto`, `native`, `ffmpeg` |
| `--output-template` | Template du nom de sortie. Placeholders disponibles : {source_name},{title},{year},{season},{episode},{episode_title},{season_episode},{group}, keywords pistes comme {audio-lang:best} (+ {season_num},{episode_num} pour formats numériques). Extension .mkv ajoutée si absente. |
| `--output-all` | Force les keywords pistes de --output-template en mode all (toutes les valeurs finales trouvées). |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Affiche la sortie ffmpeg en direct (progression, codecs, timing). Par défaut, seule la progression des étapes de workflow est affichée. |
| `--ref` | Fichier de référence (master : vidéo, chapitres). |
| `--donor` | Fichier donneur (audio, sous-titres). |
| `--ref-dir` | Dossier de référence (épisodes). |
| `--donor-dir` | Dossier donneur (épisodes). |
| `--profile` | Profil décisionnel : chemin JSON ou nom d'un profil enregistré. |
| `--report-json` | Rapport JSON des épisodes écrit dans ce fichier. |
| `-o`, `--output-dir` | Sortie : fichier, ou dossier (avec / final). |
| `--auto-tmdb` | Fiche TMDB : sans valeur, recherche d'après le nom ; avec un ID, cette fiche. |
| `--tmdb-apikey` | Clé API TMDB (défaut : réglage [metadata]). |
| `--no-cover` | Sans couverture TMDB. |
| `--tag` | Groupe de release ({group} du gabarit de nom). (défaut : `MVO`) |
| `--purge-temp` | Compatibilité : nettoyage toujours activé. |
| `--dry-run` | Vérifier et afficher le plan sans exécuter. |
| `--force` | Remplacer une sortie existante. |
| `--continue-on-error` | Continuer le lot après un échec. |
| `--source` | Source et son rôle : master, donor, donor-audio, donor-sub ; fichier ou dossier ; répétable (--ref/--donor/--ref-dir/--donor-dir en sont des alias). |
| `--matching` | Appariement des épisodes : numéros SxxEyy (défaut), similarité des titres, ou ordre. — `episode`, `fuzzy`, `order` |
| `--pair` | Paire manuelle (MASTER::DONNEUR[::DONNEUR…]) ; répétable, exclut --source. |
| `--recipe` | Recette exportée par le Hybrid Studio (JSON). |
| `--keep-master-audio` | Langues audio du master gardées (fre,eng…). |
| `--donor-audio-langs` | Langues audio prises aux donneurs. |
| `--donor-sub-langs` | Langues de sous-titres prises aux donneurs. |
| `--exclude-langs` | Langues jamais retenues. |
| `--sync` | Synchro des donneurs : analyse (défaut), décalage manuel (--shift-ms) ou aucune. — `auto`, `manual`, `none` |
| `--shift-ms` | Décalage manuel des donneurs (avec --sync manual). |
| `--plan` | Préparer sans exécuter : appariement, pistes, calibrations et sorties en JSON. |
| `--export-jobs` | Écrire les exact-jobs finalisés (à corriger puis lancer par run / batch). |
| `--save-recipe` | Écrire la recette effective (JSON). |
| `--encode-preset` | Preset d'encodage enregistré ou fichier JSON. |
| `--video-codec` | Codec vidéo (copy, libx265, hevc_nvenc…). |
| `--rate-control` | Mode de débit du codec (crf, vbr_cq, cbr…). |
| `--crf` | Qualité constante des codecs logiciels. |
| `--cq` | Qualité constante des codecs matériels. |
| `--bitrate` | Débit vidéo (kbit/s). |
| `--target-size` | Taille du fichier visée (mode taille). |
| `--encoder-preset` | Préréglage de l'encodeur (slow, p5…). |
| `--bit-depth` | Profondeur de sortie. — `auto`, `8`, `10` |
| `--crop` | Recadrage : auto (bandes noires mesurées) ou valeurs en pixels. |
| `--resize` | Redimensionnement (1080p, 1920x1080, 50%%). |
| `--deinterlace` | Désentrelacement YADIF. |
| `--denoise` | Débruitage : MVO-R2 (auto, leger, moyen, fort) ou NLMeans ; off le désactive. |
| `--interpolate` | Interpolation MVO-RIFE : facteur (x2, x2.5…) ou cadence cible (60, 60000/1001). |
| `--interpolation-preset` | Préréglage d'interpolation : fast, balanced, quality, ultra ou light. — `fast`, `balanced`, `quality`, `ultra`, `light` |
| `--tonemap` | Tone mapping vers SDR. |
| `--dv` | Dolby Vision de la source : copié ou retiré. — `copy`, `off` |
| `--hdr10plus` | HDR10+ de la source : copié ou retiré. — `copy`, `off` |
| `--bake-fel` | Intégrer la couche Dolby Vision FEL (mvo-fel). |
| `--audio-codec` | Réencoder une piste audio (ex. 0:1=eac3:640) ; répétable. |
| `--video-track` | Options vidéo ci-dessus appliquées à cette piste seulement (défaut : toutes). |
| `--accept-dovi-nvenc-crop` | Accepter le recadrage aligné sur 32 imposé par Dolby Vision + NVENC. |
| `--keep-failed-work-dir` | Encodage en échec : conserver le dossier de travail (fichiers produits). |
| `--video-set` | Champ vidéo du bloc encode (ex. restoration.mode=fast) ; valeur JSON ou texte. |

**Exemples**

```bash
muxiveo --cli hybrid --ref-dir /uhd --donor-dir /vf -o /sorties
muxiveo --cli hybrid --source master=/uhd --source donor-audio=/vf -o /sorties --plan --export-jobs /jobs
```

## Profils et métadonnées

### `profile`

Profils décisionnels : valider, prévisualiser, appliquer, lots ; profils enregistrés.

```text
muxiveo --cli profile [-h] {validate,preview,apply,batch,list,show,save,delete,capture} ...
```

**Exemples**

```bash
muxiveo --cli profile list
muxiveo --cli profile apply --profile Films -i film.mkv -o sortie.mkv
```

### `profile validate`

Valider un profil décisionnel v1.

```text
muxiveo --cli profile validate [-h] [--sync-mode {physical,container}] [--sync-subtitles {mirror,none}]
                               [--clean-nfo | --no-clean-nfo]
                               [--remove-closed-captions | --no-remove-closed-captions]
                               [--crossfade-ms CROSSFADE_MS] [--auto-forced-subs]
                               [--forced-threshold FORCED_THRESHOLD] [--auto-sdh] [--auto-sync]
                               [--auto-sync-all] [--calibration CALIBRATION] [--detect-cuts]
                               [--quick-sync-check | --no-quick-sync-check] [--reject-coherent-sync]
                               [--drift-threshold-ms DRIFT_THRESHOLD_MS] [--cadence-auto | --no-cadence-auto]
                               [--cadence-method {auto,atempo,asetrate}] [--export-workflow EXPORT_WORKFLOW]
                               [--config CONFIG] [--ffmpeg FFMPEG] [--ffprobe FFPROBE] [--mediainfo MEDIAINFO]
                               [--work-dir WORK_DIR] [--threads THREADS] [--mux-backend {auto,native,ffmpeg}]
                               [--output-template OUTPUT_TEMPLATE] [--output-all]
                               [--log-format {text,jsonl,events}] [--verbose] --profile PROFILE [--json]
```

| Option | Effet |
|---|---|
| `--sync-mode` | Synchro des sources : physical (pistes réécrites, coupures et cadence) ou container (décalage du conteneur). — `physical`, `container` |
| `--sync-subtitles` | Sous-titres : mirror (suivent la synchro de leur source) ou none. — `mirror`, `none` |
| `--clean-nfo`, `--no-clean-nfo` | Nettoyer le NFO (balises techniques retirées). |
| `--remove-closed-captions`, `--no-remove-closed-captions` | Retirer les sous-titres embarqués EIA-608/708 détectés, sans réencodage |
| `--crossfade-ms` | Fondu aux raccords de synchro (ms, 0–1000). |
| `--auto-forced-subs` | Repérer les sous-titres forcés (peu d'événements) et les marquer. |
| `--forced-threshold` | Nombre d'événements sous lequel une piste de sous-titres est jugée forcée. (défaut : `50`) |
| `--auto-sdh` | Repérer les sous-titres SDH (malentendants) et les marquer. |
| `--auto-sync` | Analyser et recalibrer automatiquement les sources dynamiquement. |
| `--auto-sync-all` | Synchroniser chaque source ≥ 1 face à la source 0 (sources sans bloc `sync` du job). |
| `--calibration` | Fichier JSON de calibration explicite (outrepasse l'analyse dynamique). |
| `--detect-cuts` | Détecter les coupures et ruptures temporelles (multi-segments). |
| `--quick-sync-check`, `--no-quick-sync-check` | Avec --detect-cuts : pas d'analyse continue si les mesures ponctuelles concordent (confiance ≥ 0,90). Défaut : [sync] quick_audio_check. |
| `--reject-coherent-sync` | Avec --detect-cuts : refuser une synchro cohérente en partie non vérifiée (zones sans correspondance entourées du même décalage). Défaut : acceptée avec un avertissement. |
| `--drift-threshold-ms` | Seuil de dérive en ms pour détecter une coupure. (défaut : `25`) |
| `--cadence-auto`, `--no-cadence-auto` | Détection et conversion automatique de cadence PAL <-> Cinéma. (défaut : `True`) |
| `--cadence-method` | Méthode de conversion audio pour cadence : auto (analyse acoustique du pitch, défaut), atempo (tonalité préservée) ou asetrate (hauteur naturelle / pitch shift). — `auto`, `atempo`, `asetrate` |
| `--export-workflow` | Sauvegarder le workflow exact sans exécuter. |
| `--config` | Fichier JSON job/template. |
| `--ffmpeg` | Chemin ffmpeg override. |
| `--ffprobe` | Chemin ffprobe override. |
| `--mediainfo` | Chemin mediainfo override. |
| `--work-dir` | Répertoire de travail override. |
| `--threads` | Nombre de threads ffmpeg. |
| `--mux-backend` | Backend MKV : auto (défaut), native ou ffmpeg. Prioritaire sur le job. — `auto`, `native`, `ffmpeg` |
| `--output-template` | Template du nom de sortie. Placeholders disponibles : {source_name},{title},{year},{season},{episode},{episode_title},{season_episode},{group}, keywords pistes comme {audio-lang:best} (+ {season_num},{episode_num} pour formats numériques). Extension .mkv ajoutée si absente. |
| `--output-all` | Force les keywords pistes de --output-template en mode all (toutes les valeurs finales trouvées). |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Affiche la sortie ffmpeg en direct (progression, codecs, timing). Par défaut, seule la progression des étapes de workflow est affichée. |
| `--profile` | Profil décisionnel : chemin JSON ou nom d'un profil enregistré. |
| `--json` | Résultat JSON sur stdout. |

**Exemples**

```bash
muxiveo --cli profile validate --profile profil.json
```

### `profile preview`

Prévisualiser un profil sur des sources (pistes retenues, nom, commandes).

```text
muxiveo --cli profile preview [-h] [--sync-mode {physical,container}] [--sync-subtitles {mirror,none}]
                              [--clean-nfo | --no-clean-nfo]
                              [--remove-closed-captions | --no-remove-closed-captions]
                              [--crossfade-ms CROSSFADE_MS] [--auto-forced-subs]
                              [--forced-threshold FORCED_THRESHOLD] [--auto-sdh] [--auto-sync]
                              [--auto-sync-all] [--calibration CALIBRATION] [--detect-cuts]
                              [--quick-sync-check | --no-quick-sync-check] [--reject-coherent-sync]
                              [--drift-threshold-ms DRIFT_THRESHOLD_MS] [--cadence-auto | --no-cadence-auto]
                              [--cadence-method {auto,atempo,asetrate}] [--export-workflow EXPORT_WORKFLOW]
                              [--config CONFIG] [--ffmpeg FFMPEG] [--ffprobe FFPROBE] [--mediainfo MEDIAINFO]
                              [--work-dir WORK_DIR] [--threads THREADS] [--mux-backend {auto,native,ffmpeg}]
                              [--output-template OUTPUT_TEMPLATE] [--output-all]
                              [--log-format {text,jsonl,events}] [--verbose] --profile PROFILE -i INPUT
                              [-o OUTPUT] [--auto-tmdb] [--tmdb] [--tmdb-id TMDB_ID]
                              [--tmdb-apikey TMDB_APIKEY] [--no-cover] [--no-attach] [--json]
                              [--encode-preset NOM|FICHIER] [--video-codec CODEC] [--rate-control MODE]
                              [--crf CRF] [--cq CQ] [--bitrate KBPS] [--target-size MIO]
                              [--encoder-preset PRESET] [--bit-depth {auto,8,10}]
                              [--crop auto|HAUT,BAS,GAUCHE,DROITE] [--resize PRESET|LxH|N%] [--deinterlace]
                              [--denoise mvo-r2[:NIVEAU]|nlmeans[:FORCE]|off] [--interpolate xN|CADENCE]
                              [--interpolation-preset {fast,balanced,quality,ultra,light}] [--tonemap [ALGO]]
                              [--dv {copy,off}] [--hdr10plus {copy,off}] [--bake-fel]
                              [--audio-codec SOURCE:FLUX=CODEC[:KBPS]] [--video-track SOURCE:FLUX]
                              [--accept-dovi-nvenc-crop] [--keep-failed-work-dir]
                              [--video-set CLÉ.POINTÉE=VALEUR]
```

| Option | Effet |
|---|---|
| `--sync-mode` | Synchro des sources : physical (pistes réécrites, coupures et cadence) ou container (décalage du conteneur). — `physical`, `container` |
| `--sync-subtitles` | Sous-titres : mirror (suivent la synchro de leur source) ou none. — `mirror`, `none` |
| `--clean-nfo`, `--no-clean-nfo` | Nettoyer le NFO (balises techniques retirées). |
| `--remove-closed-captions`, `--no-remove-closed-captions` | Retirer les sous-titres embarqués EIA-608/708 détectés, sans réencodage |
| `--crossfade-ms` | Fondu aux raccords de synchro (ms, 0–1000). |
| `--auto-forced-subs` | Repérer les sous-titres forcés (peu d'événements) et les marquer. |
| `--forced-threshold` | Nombre d'événements sous lequel une piste de sous-titres est jugée forcée. (défaut : `50`) |
| `--auto-sdh` | Repérer les sous-titres SDH (malentendants) et les marquer. |
| `--auto-sync` | Analyser et recalibrer automatiquement les sources dynamiquement. |
| `--auto-sync-all` | Synchroniser chaque source ≥ 1 face à la source 0 (sources sans bloc `sync` du job). |
| `--calibration` | Fichier JSON de calibration explicite (outrepasse l'analyse dynamique). |
| `--detect-cuts` | Détecter les coupures et ruptures temporelles (multi-segments). |
| `--quick-sync-check`, `--no-quick-sync-check` | Avec --detect-cuts : pas d'analyse continue si les mesures ponctuelles concordent (confiance ≥ 0,90). Défaut : [sync] quick_audio_check. |
| `--reject-coherent-sync` | Avec --detect-cuts : refuser une synchro cohérente en partie non vérifiée (zones sans correspondance entourées du même décalage). Défaut : acceptée avec un avertissement. |
| `--drift-threshold-ms` | Seuil de dérive en ms pour détecter une coupure. (défaut : `25`) |
| `--cadence-auto`, `--no-cadence-auto` | Détection et conversion automatique de cadence PAL <-> Cinéma. (défaut : `True`) |
| `--cadence-method` | Méthode de conversion audio pour cadence : auto (analyse acoustique du pitch, défaut), atempo (tonalité préservée) ou asetrate (hauteur naturelle / pitch shift). — `auto`, `atempo`, `asetrate` |
| `--export-workflow` | Sauvegarder le workflow exact sans exécuter. |
| `--config` | Fichier JSON job/template. |
| `--ffmpeg` | Chemin ffmpeg override. |
| `--ffprobe` | Chemin ffprobe override. |
| `--mediainfo` | Chemin mediainfo override. |
| `--work-dir` | Répertoire de travail override. |
| `--threads` | Nombre de threads ffmpeg. |
| `--mux-backend` | Backend MKV : auto (défaut), native ou ffmpeg. Prioritaire sur le job. — `auto`, `native`, `ffmpeg` |
| `--output-template` | Template du nom de sortie. Placeholders disponibles : {source_name},{title},{year},{season},{episode},{episode_title},{season_episode},{group}, keywords pistes comme {audio-lang:best} (+ {season_num},{episode_num} pour formats numériques). Extension .mkv ajoutée si absente. |
| `--output-all` | Force les keywords pistes de --output-template en mode all (toutes les valeurs finales trouvées). |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Affiche la sortie ffmpeg en direct (progression, codecs, timing). Par défaut, seule la progression des étapes de workflow est affichée. |
| `--profile` | Profil décisionnel : chemin JSON ou nom d'un profil enregistré. |
| `-i`, `--input` | Source (répétable). |
| `-o`, `--output` | Sortie : fichier, ou dossier (avec / final). |
| `--auto-tmdb` | Chercher TMDB automatiquement et prendre le premier résultat. |
| `--tmdb` | Alias historique de --auto-tmdb. |
| `--tmdb-id` | ID TMDB explicite. |
| `--tmdb-apikey` | Clé API TMDB v3 (surcharge la config Muxiveo et le JSON job). |
| `--no-cover` | Ne pas ajouter la cover TMDB. |
| `--no-attach` | Ne pas inclure d'attachments ni de cover TMDB. |
| `--json` | Résultat JSON sur stdout. |
| `--encode-preset` | Preset d'encodage enregistré ou fichier JSON. |
| `--video-codec` | Codec vidéo (copy, libx265, hevc_nvenc…). |
| `--rate-control` | Mode de débit du codec (crf, vbr_cq, cbr…). |
| `--crf` | Qualité constante des codecs logiciels. |
| `--cq` | Qualité constante des codecs matériels. |
| `--bitrate` | Débit vidéo (kbit/s). |
| `--target-size` | Taille du fichier visée (mode taille). |
| `--encoder-preset` | Préréglage de l'encodeur (slow, p5…). |
| `--bit-depth` | Profondeur de sortie. — `auto`, `8`, `10` |
| `--crop` | Recadrage : auto (bandes noires mesurées) ou valeurs en pixels. |
| `--resize` | Redimensionnement (1080p, 1920x1080, 50%%). |
| `--deinterlace` | Désentrelacement YADIF. |
| `--denoise` | Débruitage : MVO-R2 (auto, leger, moyen, fort) ou NLMeans ; off le désactive. |
| `--interpolate` | Interpolation MVO-RIFE : facteur (x2, x2.5…) ou cadence cible (60, 60000/1001). |
| `--interpolation-preset` | Préréglage d'interpolation : fast, balanced, quality, ultra ou light. — `fast`, `balanced`, `quality`, `ultra`, `light` |
| `--tonemap` | Tone mapping vers SDR. |
| `--dv` | Dolby Vision de la source : copié ou retiré. — `copy`, `off` |
| `--hdr10plus` | HDR10+ de la source : copié ou retiré. — `copy`, `off` |
| `--bake-fel` | Intégrer la couche Dolby Vision FEL (mvo-fel). |
| `--audio-codec` | Réencoder une piste audio (ex. 0:1=eac3:640) ; répétable. |
| `--video-track` | Options vidéo ci-dessus appliquées à cette piste seulement (défaut : toutes). |
| `--accept-dovi-nvenc-crop` | Accepter le recadrage aligné sur 32 imposé par Dolby Vision + NVENC. |
| `--keep-failed-work-dir` | Encodage en échec : conserver le dossier de travail (fichiers produits). |
| `--video-set` | Champ vidéo du bloc encode (ex. restoration.mode=fast) ; valeur JSON ou texte. |

**Exemples**

```bash
muxiveo --cli profile preview --profile Films -i film.mkv --json
```

### `profile apply`

Appliquer un profil et remuxer (ou encoder avec les options d'encodage).

```text
muxiveo --cli profile apply [-h] [--sync-mode {physical,container}] [--sync-subtitles {mirror,none}]
                            [--clean-nfo | --no-clean-nfo]
                            [--remove-closed-captions | --no-remove-closed-captions]
                            [--crossfade-ms CROSSFADE_MS] [--auto-forced-subs]
                            [--forced-threshold FORCED_THRESHOLD] [--auto-sdh] [--auto-sync] [--auto-sync-all]
                            [--calibration CALIBRATION] [--detect-cuts]
                            [--quick-sync-check | --no-quick-sync-check] [--reject-coherent-sync]
                            [--drift-threshold-ms DRIFT_THRESHOLD_MS] [--cadence-auto | --no-cadence-auto]
                            [--cadence-method {auto,atempo,asetrate}] [--export-workflow EXPORT_WORKFLOW]
                            [--config CONFIG] [--ffmpeg FFMPEG] [--ffprobe FFPROBE] [--mediainfo MEDIAINFO]
                            [--work-dir WORK_DIR] [--threads THREADS] [--mux-backend {auto,native,ffmpeg}]
                            [--output-template OUTPUT_TEMPLATE] [--output-all]
                            [--log-format {text,jsonl,events}] [--verbose] --profile PROFILE -i INPUT -o
                            OUTPUT [--auto-tmdb] [--tmdb] [--tmdb-id TMDB_ID] [--tmdb-apikey TMDB_APIKEY]
                            [--no-cover] [--no-attach] [--force] [--dry-run] [--encode-preset NOM|FICHIER]
                            [--video-codec CODEC] [--rate-control MODE] [--crf CRF] [--cq CQ] [--bitrate KBPS]
                            [--target-size MIO] [--encoder-preset PRESET] [--bit-depth {auto,8,10}]
                            [--crop auto|HAUT,BAS,GAUCHE,DROITE] [--resize PRESET|LxH|N%] [--deinterlace]
                            [--denoise mvo-r2[:NIVEAU]|nlmeans[:FORCE]|off] [--interpolate xN|CADENCE]
                            [--interpolation-preset {fast,balanced,quality,ultra,light}] [--tonemap [ALGO]]
                            [--dv {copy,off}] [--hdr10plus {copy,off}] [--bake-fel]
                            [--audio-codec SOURCE:FLUX=CODEC[:KBPS]] [--video-track SOURCE:FLUX]
                            [--accept-dovi-nvenc-crop] [--keep-failed-work-dir]
                            [--video-set CLÉ.POINTÉE=VALEUR]
```

| Option | Effet |
|---|---|
| `--sync-mode` | Synchro des sources : physical (pistes réécrites, coupures et cadence) ou container (décalage du conteneur). — `physical`, `container` |
| `--sync-subtitles` | Sous-titres : mirror (suivent la synchro de leur source) ou none. — `mirror`, `none` |
| `--clean-nfo`, `--no-clean-nfo` | Nettoyer le NFO (balises techniques retirées). |
| `--remove-closed-captions`, `--no-remove-closed-captions` | Retirer les sous-titres embarqués EIA-608/708 détectés, sans réencodage |
| `--crossfade-ms` | Fondu aux raccords de synchro (ms, 0–1000). |
| `--auto-forced-subs` | Repérer les sous-titres forcés (peu d'événements) et les marquer. |
| `--forced-threshold` | Nombre d'événements sous lequel une piste de sous-titres est jugée forcée. (défaut : `50`) |
| `--auto-sdh` | Repérer les sous-titres SDH (malentendants) et les marquer. |
| `--auto-sync` | Analyser et recalibrer automatiquement les sources dynamiquement. |
| `--auto-sync-all` | Synchroniser chaque source ≥ 1 face à la source 0 (sources sans bloc `sync` du job). |
| `--calibration` | Fichier JSON de calibration explicite (outrepasse l'analyse dynamique). |
| `--detect-cuts` | Détecter les coupures et ruptures temporelles (multi-segments). |
| `--quick-sync-check`, `--no-quick-sync-check` | Avec --detect-cuts : pas d'analyse continue si les mesures ponctuelles concordent (confiance ≥ 0,90). Défaut : [sync] quick_audio_check. |
| `--reject-coherent-sync` | Avec --detect-cuts : refuser une synchro cohérente en partie non vérifiée (zones sans correspondance entourées du même décalage). Défaut : acceptée avec un avertissement. |
| `--drift-threshold-ms` | Seuil de dérive en ms pour détecter une coupure. (défaut : `25`) |
| `--cadence-auto`, `--no-cadence-auto` | Détection et conversion automatique de cadence PAL <-> Cinéma. (défaut : `True`) |
| `--cadence-method` | Méthode de conversion audio pour cadence : auto (analyse acoustique du pitch, défaut), atempo (tonalité préservée) ou asetrate (hauteur naturelle / pitch shift). — `auto`, `atempo`, `asetrate` |
| `--export-workflow` | Sauvegarder le workflow exact sans exécuter. |
| `--config` | Fichier JSON job/template. |
| `--ffmpeg` | Chemin ffmpeg override. |
| `--ffprobe` | Chemin ffprobe override. |
| `--mediainfo` | Chemin mediainfo override. |
| `--work-dir` | Répertoire de travail override. |
| `--threads` | Nombre de threads ffmpeg. |
| `--mux-backend` | Backend MKV : auto (défaut), native ou ffmpeg. Prioritaire sur le job. — `auto`, `native`, `ffmpeg` |
| `--output-template` | Template du nom de sortie. Placeholders disponibles : {source_name},{title},{year},{season},{episode},{episode_title},{season_episode},{group}, keywords pistes comme {audio-lang:best} (+ {season_num},{episode_num} pour formats numériques). Extension .mkv ajoutée si absente. |
| `--output-all` | Force les keywords pistes de --output-template en mode all (toutes les valeurs finales trouvées). |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Affiche la sortie ffmpeg en direct (progression, codecs, timing). Par défaut, seule la progression des étapes de workflow est affichée. |
| `--profile` | Profil décisionnel : chemin JSON ou nom d'un profil enregistré. |
| `-i`, `--input` | Source (répétable). |
| `-o`, `--output` | Sortie : fichier, ou dossier (avec / final). |
| `--auto-tmdb` | Chercher TMDB automatiquement et prendre le premier résultat. |
| `--tmdb` | Alias historique de --auto-tmdb. |
| `--tmdb-id` | ID TMDB explicite. |
| `--tmdb-apikey` | Clé API TMDB v3 (surcharge la config Muxiveo et le JSON job). |
| `--no-cover` | Ne pas ajouter la cover TMDB. |
| `--no-attach` | Ne pas inclure d'attachments ni de cover TMDB. |
| `--force` | Remplacer une sortie existante. |
| `--dry-run` | Vérifier et afficher le plan sans exécuter. |
| `--encode-preset` | Preset d'encodage enregistré ou fichier JSON. |
| `--video-codec` | Codec vidéo (copy, libx265, hevc_nvenc…). |
| `--rate-control` | Mode de débit du codec (crf, vbr_cq, cbr…). |
| `--crf` | Qualité constante des codecs logiciels. |
| `--cq` | Qualité constante des codecs matériels. |
| `--bitrate` | Débit vidéo (kbit/s). |
| `--target-size` | Taille du fichier visée (mode taille). |
| `--encoder-preset` | Préréglage de l'encodeur (slow, p5…). |
| `--bit-depth` | Profondeur de sortie. — `auto`, `8`, `10` |
| `--crop` | Recadrage : auto (bandes noires mesurées) ou valeurs en pixels. |
| `--resize` | Redimensionnement (1080p, 1920x1080, 50%%). |
| `--deinterlace` | Désentrelacement YADIF. |
| `--denoise` | Débruitage : MVO-R2 (auto, leger, moyen, fort) ou NLMeans ; off le désactive. |
| `--interpolate` | Interpolation MVO-RIFE : facteur (x2, x2.5…) ou cadence cible (60, 60000/1001). |
| `--interpolation-preset` | Préréglage d'interpolation : fast, balanced, quality, ultra ou light. — `fast`, `balanced`, `quality`, `ultra`, `light` |
| `--tonemap` | Tone mapping vers SDR. |
| `--dv` | Dolby Vision de la source : copié ou retiré. — `copy`, `off` |
| `--hdr10plus` | HDR10+ de la source : copié ou retiré. — `copy`, `off` |
| `--bake-fel` | Intégrer la couche Dolby Vision FEL (mvo-fel). |
| `--audio-codec` | Réencoder une piste audio (ex. 0:1=eac3:640) ; répétable. |
| `--video-track` | Options vidéo ci-dessus appliquées à cette piste seulement (défaut : toutes). |
| `--accept-dovi-nvenc-crop` | Accepter le recadrage aligné sur 32 imposé par Dolby Vision + NVENC. |
| `--keep-failed-work-dir` | Encodage en échec : conserver le dossier de travail (fichiers produits). |
| `--video-set` | Champ vidéo du bloc encode (ex. restoration.mode=fast) ; valeur JSON ou texte. |

**Exemples**

```bash
muxiveo --cli profile apply --profile Films -i film.mkv -o sortie.mkv
```

### `profile batch`

Appliquer un profil à un lot de fichiers.

```text
muxiveo --cli profile batch [-h] [--sync-mode {physical,container}] [--sync-subtitles {mirror,none}]
                            [--clean-nfo | --no-clean-nfo]
                            [--remove-closed-captions | --no-remove-closed-captions]
                            [--crossfade-ms CROSSFADE_MS] [--auto-forced-subs]
                            [--forced-threshold FORCED_THRESHOLD] [--auto-sdh] [--auto-sync] [--auto-sync-all]
                            [--calibration CALIBRATION] [--detect-cuts]
                            [--quick-sync-check | --no-quick-sync-check] [--reject-coherent-sync]
                            [--drift-threshold-ms DRIFT_THRESHOLD_MS] [--cadence-auto | --no-cadence-auto]
                            [--cadence-method {auto,atempo,asetrate}] [--export-workflow EXPORT_WORKFLOW]
                            [--config CONFIG] [--ffmpeg FFMPEG] [--ffprobe FFPROBE] [--mediainfo MEDIAINFO]
                            [--work-dir WORK_DIR] [--threads THREADS] [--mux-backend {auto,native,ffmpeg}]
                            [--output-template OUTPUT_TEMPLATE] [--output-all]
                            [--log-format {text,jsonl,events}] [--verbose] --profile PROFILE [-i INPUT]
                            [--input-dir INPUT_DIR] [--recursive] [--include INCLUDE] [--exclude EXCLUDE]
                            --output-dir OUTPUT_DIR [--auto-tmdb] [--tmdb] [--tmdb-id TMDB_ID]
                            [--tmdb-apikey TMDB_APIKEY] [--no-cover] [--no-attach] [--dry-run] [--force]
                            [--continue-on-error] [--summary SUMMARY] [--encode-preset NOM|FICHIER]
                            [--video-codec CODEC] [--rate-control MODE] [--crf CRF] [--cq CQ] [--bitrate KBPS]
                            [--target-size MIO] [--encoder-preset PRESET] [--bit-depth {auto,8,10}]
                            [--crop auto|HAUT,BAS,GAUCHE,DROITE] [--resize PRESET|LxH|N%] [--deinterlace]
                            [--denoise mvo-r2[:NIVEAU]|nlmeans[:FORCE]|off] [--interpolate xN|CADENCE]
                            [--interpolation-preset {fast,balanced,quality,ultra,light}] [--tonemap [ALGO]]
                            [--dv {copy,off}] [--hdr10plus {copy,off}] [--bake-fel]
                            [--audio-codec SOURCE:FLUX=CODEC[:KBPS]] [--video-track SOURCE:FLUX]
                            [--accept-dovi-nvenc-crop] [--keep-failed-work-dir]
                            [--video-set CLÉ.POINTÉE=VALEUR]
```

| Option | Effet |
|---|---|
| `--sync-mode` | Synchro des sources : physical (pistes réécrites, coupures et cadence) ou container (décalage du conteneur). — `physical`, `container` |
| `--sync-subtitles` | Sous-titres : mirror (suivent la synchro de leur source) ou none. — `mirror`, `none` |
| `--clean-nfo`, `--no-clean-nfo` | Nettoyer le NFO (balises techniques retirées). |
| `--remove-closed-captions`, `--no-remove-closed-captions` | Retirer les sous-titres embarqués EIA-608/708 détectés, sans réencodage |
| `--crossfade-ms` | Fondu aux raccords de synchro (ms, 0–1000). |
| `--auto-forced-subs` | Repérer les sous-titres forcés (peu d'événements) et les marquer. |
| `--forced-threshold` | Nombre d'événements sous lequel une piste de sous-titres est jugée forcée. (défaut : `50`) |
| `--auto-sdh` | Repérer les sous-titres SDH (malentendants) et les marquer. |
| `--auto-sync` | Analyser et recalibrer automatiquement les sources dynamiquement. |
| `--auto-sync-all` | Synchroniser chaque source ≥ 1 face à la source 0 (sources sans bloc `sync` du job). |
| `--calibration` | Fichier JSON de calibration explicite (outrepasse l'analyse dynamique). |
| `--detect-cuts` | Détecter les coupures et ruptures temporelles (multi-segments). |
| `--quick-sync-check`, `--no-quick-sync-check` | Avec --detect-cuts : pas d'analyse continue si les mesures ponctuelles concordent (confiance ≥ 0,90). Défaut : [sync] quick_audio_check. |
| `--reject-coherent-sync` | Avec --detect-cuts : refuser une synchro cohérente en partie non vérifiée (zones sans correspondance entourées du même décalage). Défaut : acceptée avec un avertissement. |
| `--drift-threshold-ms` | Seuil de dérive en ms pour détecter une coupure. (défaut : `25`) |
| `--cadence-auto`, `--no-cadence-auto` | Détection et conversion automatique de cadence PAL <-> Cinéma. (défaut : `True`) |
| `--cadence-method` | Méthode de conversion audio pour cadence : auto (analyse acoustique du pitch, défaut), atempo (tonalité préservée) ou asetrate (hauteur naturelle / pitch shift). — `auto`, `atempo`, `asetrate` |
| `--export-workflow` | Sauvegarder le workflow exact sans exécuter. |
| `--config` | Fichier JSON job/template. |
| `--ffmpeg` | Chemin ffmpeg override. |
| `--ffprobe` | Chemin ffprobe override. |
| `--mediainfo` | Chemin mediainfo override. |
| `--work-dir` | Répertoire de travail override. |
| `--threads` | Nombre de threads ffmpeg. |
| `--mux-backend` | Backend MKV : auto (défaut), native ou ffmpeg. Prioritaire sur le job. — `auto`, `native`, `ffmpeg` |
| `--output-template` | Template du nom de sortie. Placeholders disponibles : {source_name},{title},{year},{season},{episode},{episode_title},{season_episode},{group}, keywords pistes comme {audio-lang:best} (+ {season_num},{episode_num} pour formats numériques). Extension .mkv ajoutée si absente. |
| `--output-all` | Force les keywords pistes de --output-template en mode all (toutes les valeurs finales trouvées). |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Affiche la sortie ffmpeg en direct (progression, codecs, timing). Par défaut, seule la progression des étapes de workflow est affichée. |
| `--profile` | Profil décisionnel : chemin JSON ou nom d'un profil enregistré. |
| `-i`, `--input` | Source (répétable). |
| `--input-dir` | Dossier de sources (répétable). |
| `--recursive` | Parcourir aussi les sous-dossiers. |
| `--include` | Motif de fichiers retenus (glob, répétable). |
| `--exclude` | Motif de fichiers écartés (glob, répétable). |
| `--output-dir` | Dossier des sorties. |
| `--auto-tmdb` | Chercher TMDB automatiquement et prendre le premier résultat. |
| `--tmdb` | Alias historique de --auto-tmdb. |
| `--tmdb-id` | ID TMDB explicite. |
| `--tmdb-apikey` | Clé API TMDB v3 (surcharge la config Muxiveo et le JSON job). |
| `--no-cover` | Ne pas ajouter la cover TMDB. |
| `--no-attach` | Ne pas inclure d'attachments ni de cover TMDB. |
| `--dry-run` | Vérifier et afficher le plan sans exécuter. |
| `--force` | Remplacer une sortie existante. |
| `--continue-on-error` | Continuer le lot après un échec. |
| `--summary` | Résumé JSON du lot écrit dans ce fichier. |
| `--encode-preset` | Preset d'encodage enregistré ou fichier JSON. |
| `--video-codec` | Codec vidéo (copy, libx265, hevc_nvenc…). |
| `--rate-control` | Mode de débit du codec (crf, vbr_cq, cbr…). |
| `--crf` | Qualité constante des codecs logiciels. |
| `--cq` | Qualité constante des codecs matériels. |
| `--bitrate` | Débit vidéo (kbit/s). |
| `--target-size` | Taille du fichier visée (mode taille). |
| `--encoder-preset` | Préréglage de l'encodeur (slow, p5…). |
| `--bit-depth` | Profondeur de sortie. — `auto`, `8`, `10` |
| `--crop` | Recadrage : auto (bandes noires mesurées) ou valeurs en pixels. |
| `--resize` | Redimensionnement (1080p, 1920x1080, 50%%). |
| `--deinterlace` | Désentrelacement YADIF. |
| `--denoise` | Débruitage : MVO-R2 (auto, leger, moyen, fort) ou NLMeans ; off le désactive. |
| `--interpolate` | Interpolation MVO-RIFE : facteur (x2, x2.5…) ou cadence cible (60, 60000/1001). |
| `--interpolation-preset` | Préréglage d'interpolation : fast, balanced, quality, ultra ou light. — `fast`, `balanced`, `quality`, `ultra`, `light` |
| `--tonemap` | Tone mapping vers SDR. |
| `--dv` | Dolby Vision de la source : copié ou retiré. — `copy`, `off` |
| `--hdr10plus` | HDR10+ de la source : copié ou retiré. — `copy`, `off` |
| `--bake-fel` | Intégrer la couche Dolby Vision FEL (mvo-fel). |
| `--audio-codec` | Réencoder une piste audio (ex. 0:1=eac3:640) ; répétable. |
| `--video-track` | Options vidéo ci-dessus appliquées à cette piste seulement (défaut : toutes). |
| `--accept-dovi-nvenc-crop` | Accepter le recadrage aligné sur 32 imposé par Dolby Vision + NVENC. |
| `--keep-failed-work-dir` | Encodage en échec : conserver le dossier de travail (fichiers produits). |
| `--video-set` | Champ vidéo du bloc encode (ex. restoration.mode=fast) ; valeur JSON ou texte. |

**Exemples**

```bash
muxiveo --cli profile batch --profile Films --input-dir /films --output-dir /sorties
```

### `profile list`

Lister les profils enregistrés (ceux de l'interface).

```text
muxiveo --cli profile list [-h] [--json] [--log-format {text,jsonl,events}] [--verbose]
```

| Option | Effet |
|---|---|
| `--json` | Résultat JSON sur stdout. |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Journal détaillé (sortie brute des outils). |

**Exemples**

```bash
muxiveo --cli profile list
```

### `profile show`

Afficher un profil enregistré (JSON).

```text
muxiveo --cli profile show [-h] [--json] [--log-format {text,jsonl,events}] [--verbose] NOM
```

| Option | Effet |
|---|---|
| `name` | Nom du profil. |
| `--json` | Résultat JSON sur stdout. |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Journal détaillé (sortie brute des outils). |

**Exemples**

```bash
muxiveo --cli profile show Films
```

### `profile save`

Enregistrer un profil depuis un fichier JSON (validé).

```text
muxiveo --cli profile save [-h] [--name PROFILE_NAME] [--force] [--json] [--log-format {text,jsonl,events}]
                           [--verbose] FICHIER
```

| Option | Effet |
|---|---|
| `file` | Fichier JSON du profil (decision-profile v1). |
| `--name` | Nom enregistré (défaut : celui du fichier). |
| `--force` | Remplacer un profil du même nom. |
| `--json` | Résultat JSON sur stdout. |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Journal détaillé (sortie brute des outils). |

**Exemples**

```bash
muxiveo --cli profile save profil.json --force
```

### `profile delete`

Supprimer un profil enregistré.

```text
muxiveo --cli profile delete [-h] [--json] [--log-format {text,jsonl,events}] [--verbose] NOM
```

| Option | Effet |
|---|---|
| `name` | Nom du profil. |
| `--json` | Résultat JSON sur stdout. |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Journal détaillé (sortie brute des outils). |

**Exemples**

```bash
muxiveo --cli profile delete Ancien
```

### `profile capture`

Construit le job (sources -i, ou --config) puis en capture la sélection, les métadonnées, les flags, l'ordre et les variantes audio ; --save l'enregistre parmi les profils de l'interface.

```text
muxiveo --cli profile capture [-h] [--sync-mode {physical,container}] [--sync-subtitles {mirror,none}]
                              [--clean-nfo | --no-clean-nfo]
                              [--remove-closed-captions | --no-remove-closed-captions]
                              [--crossfade-ms CROSSFADE_MS] [--auto-forced-subs]
                              [--forced-threshold FORCED_THRESHOLD] [--auto-sdh] [--auto-sync]
                              [--auto-sync-all] [--calibration CALIBRATION] [--detect-cuts]
                              [--quick-sync-check | --no-quick-sync-check] [--reject-coherent-sync]
                              [--drift-threshold-ms DRIFT_THRESHOLD_MS] [--cadence-auto | --no-cadence-auto]
                              [--cadence-method {auto,atempo,asetrate}] [--export-workflow EXPORT_WORKFLOW]
                              [--config CONFIG] [--ffmpeg FFMPEG] [--ffprobe FFPROBE] [--mediainfo MEDIAINFO]
                              [--work-dir WORK_DIR] [--threads THREADS] [--mux-backend {auto,native,ffmpeg}]
                              [--output-template OUTPUT_TEMPLATE] [--output-all]
                              [--log-format {text,jsonl,events}] [--verbose] [-i INPUT] [-o OUTPUT]
                              [--name PROFILE_NAME] [--description DESCRIPTION] [--no-selection]
                              [--no-metadata] [--no-flags] [--no-order] [--no-audio-variants] [--save]
                              [--force] [--json]
```

| Option | Effet |
|---|---|
| `--sync-mode` | Synchro des sources : physical (pistes réécrites, coupures et cadence) ou container (décalage du conteneur). — `physical`, `container` |
| `--sync-subtitles` | Sous-titres : mirror (suivent la synchro de leur source) ou none. — `mirror`, `none` |
| `--clean-nfo`, `--no-clean-nfo` | Nettoyer le NFO (balises techniques retirées). |
| `--remove-closed-captions`, `--no-remove-closed-captions` | Retirer les sous-titres embarqués EIA-608/708 détectés, sans réencodage |
| `--crossfade-ms` | Fondu aux raccords de synchro (ms, 0–1000). |
| `--auto-forced-subs` | Repérer les sous-titres forcés (peu d'événements) et les marquer. |
| `--forced-threshold` | Nombre d'événements sous lequel une piste de sous-titres est jugée forcée. (défaut : `50`) |
| `--auto-sdh` | Repérer les sous-titres SDH (malentendants) et les marquer. |
| `--auto-sync` | Analyser et recalibrer automatiquement les sources dynamiquement. |
| `--auto-sync-all` | Synchroniser chaque source ≥ 1 face à la source 0 (sources sans bloc `sync` du job). |
| `--calibration` | Fichier JSON de calibration explicite (outrepasse l'analyse dynamique). |
| `--detect-cuts` | Détecter les coupures et ruptures temporelles (multi-segments). |
| `--quick-sync-check`, `--no-quick-sync-check` | Avec --detect-cuts : pas d'analyse continue si les mesures ponctuelles concordent (confiance ≥ 0,90). Défaut : [sync] quick_audio_check. |
| `--reject-coherent-sync` | Avec --detect-cuts : refuser une synchro cohérente en partie non vérifiée (zones sans correspondance entourées du même décalage). Défaut : acceptée avec un avertissement. |
| `--drift-threshold-ms` | Seuil de dérive en ms pour détecter une coupure. (défaut : `25`) |
| `--cadence-auto`, `--no-cadence-auto` | Détection et conversion automatique de cadence PAL <-> Cinéma. (défaut : `True`) |
| `--cadence-method` | Méthode de conversion audio pour cadence : auto (analyse acoustique du pitch, défaut), atempo (tonalité préservée) ou asetrate (hauteur naturelle / pitch shift). — `auto`, `atempo`, `asetrate` |
| `--export-workflow` | Sauvegarder le workflow exact sans exécuter. |
| `--config` | Fichier JSON job/template. |
| `--ffmpeg` | Chemin ffmpeg override. |
| `--ffprobe` | Chemin ffprobe override. |
| `--mediainfo` | Chemin mediainfo override. |
| `--work-dir` | Répertoire de travail override. |
| `--threads` | Nombre de threads ffmpeg. |
| `--mux-backend` | Backend MKV : auto (défaut), native ou ffmpeg. Prioritaire sur le job. — `auto`, `native`, `ffmpeg` |
| `--output-template` | Template du nom de sortie. Placeholders disponibles : {source_name},{title},{year},{season},{episode},{episode_title},{season_episode},{group}, keywords pistes comme {audio-lang:best} (+ {season_num},{episode_num} pour formats numériques). Extension .mkv ajoutée si absente. |
| `--output-all` | Force les keywords pistes de --output-template en mode all (toutes les valeurs finales trouvées). |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Affiche la sortie ffmpeg en direct (progression, codecs, timing). Par défaut, seule la progression des étapes de workflow est affichée. |
| `-i`, `--input` | Source (répétable). |
| `-o`, `--output` | Sortie : fichier, ou dossier (avec / final). |
| `--name` | Nom du profil. |
| `--description` | Description enregistrée. |
| `--no-selection` | Ne pas capturer : selection. |
| `--no-metadata` | Ne pas capturer : metadata. |
| `--no-flags` | Ne pas capturer : flags. |
| `--no-order` | Ne pas capturer : order. |
| `--no-audio-variants` | Ne pas capturer : audio-variants. |
| `--save` | Enregistrer le profil capturé. |
| `--force` | Remplacer une sortie existante. |
| `--json` | Résultat JSON sur stdout. |

**Exemples**

```bash
muxiveo --cli profile capture -i film.mkv --name "Films VF" --save
muxiveo --cli profile capture --config job.json --name Séries --no-order
```

### `tmdb`

TMDB : rechercher un film ou une série, lire une fiche.

```text
muxiveo --cli tmdb [-h] {search,details} ...
```

**Exemples**

```bash
muxiveo --cli tmdb search "Le Bureau des légendes" --kind tv
muxiveo --cli tmdb details 2316 --kind tv --season 2 --episode 1
```

### `tmdb search`

Rechercher sur TMDB (25 résultats au plus).

```text
muxiveo --cli tmdb search [-h] [--kind {all,movie,tv}] [--year YEAR] [--language LANGUAGE]
                          [--tmdb-apikey TMDB_APIKEY] [--json] [--log-format {text,jsonl,events}] [--verbose]
                          TITRE
```

| Option | Effet |
|---|---|
| `query` | Titre recherché. |
| `--kind` | Nature : film (movie), série (tv) ou les deux (all). (défaut : `all`) — `all`, `movie`, `tv` |
| `--year` | Année (repli sans année si aucun résultat). |
| `--language` | Langue des titres (défaut fr-FR). |
| `--tmdb-apikey` | Clé API (défaut : réglage [metadata]). |
| `--json` | Résultat JSON sur stdout. |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Journal détaillé (sortie brute des outils). |

**Exemples**

```bash
muxiveo --cli tmdb search Dune --year 2021
```

### `tmdb details`

Fiche TMDB complète, titre de conteneur et balises MKV.

```text
muxiveo --cli tmdb details [-h] [--kind {movie,tv}] [--season SEASON] [--episode EPISODE]
                           [--language LANGUAGE] [--tmdb-apikey TMDB_APIKEY] [--json]
                           [--log-format {text,jsonl,events}] [--verbose] ID
```

| Option | Effet |
|---|---|
| `tmdb_id` | Identifiant TMDB. |
| `--kind` | Nature : film (movie), série (tv) ou les deux (all). (défaut : `movie`) — `movie`, `tv` |
| `--season` | Saison. |
| `--episode` | Épisode. |
| `--language` | Langue des titres (défaut fr-FR). |
| `--tmdb-apikey` | Clé API (défaut : réglage [metadata]). |
| `--json` | Résultat JSON sur stdout. |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Journal détaillé (sortie brute des outils). |

**Exemples**

```bash
muxiveo --cli tmdb details 438631 --json
```

## Système

### `version`

Version de Muxiveo ; --check compare à la dernière release du canal (code 8 si mise à jour) ; --install --yes l'installe.

```text
muxiveo --cli version [-h] [--check] [--channel {stable,unstable}] [--install] [--yes]
                      [--log-format {text,jsonl,events}]
```

| Option | Effet |
|---|---|
| `--check` | Comparer avec la dernière release GitHub (code retour 8 si mise à jour disponible). |
| `--channel` | Canal de mise à jour (défaut : réglage [ui] update_channel). — `stable`, `unstable` |
| `--install` | Installer la dernière version du canal (installation intégrée ; sans relancer l'interface). |
| `--yes` | Avec --install : confirmer l'installation. |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |

**Exemples**

```bash
muxiveo --cli version --check
muxiveo --cli version --install --yes
```

### `tools`

Chemins des outils résolus par Muxiveo (JSON).

```text
muxiveo --cli tools [-h] [--log-format {text,jsonl,events}]
```

| Option | Effet |
|---|---|
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |

**Exemples**

```bash
muxiveo --cli tools
```

### `config`

Réglages `section.clé` (config show les liste) : valeur effective et provenance (config.ini, réglage de l'interface, défaut). set écrit config.ini ; unset retire la surcharge (le réglage de l'interface peut réapparaître) ; reset revient au défaut.

```text
muxiveo --cli config [-h] {path,show,get,set,unset,reset} ...
```

**Exemples**

```bash
muxiveo --cli config show ui
muxiveo --cli config set ui.theme light
```

### `config path`

Emplacements de config.ini et des réglages de l'interface.

```text
muxiveo --cli config path [-h] [--json] [--log-format {text,jsonl,events}] [--verbose]
```

| Option | Effet |
|---|---|
| `--json` | Résultat JSON sur stdout. |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Journal détaillé (sortie brute des outils). |

**Exemples**

```bash
muxiveo --cli config path
```

### `config show`

Réglages, valeur effective et provenance (ini, settings, default).

```text
muxiveo --cli config show [-h] [--json] [--log-format {text,jsonl,events}] [--verbose] [SECTION]
```

| Option | Effet |
|---|---|
| `section` | Section (paths, tools, ffmpeg, hdr, ui…). |
| `--json` | Résultat JSON sur stdout. |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Journal détaillé (sortie brute des outils). |

**Exemples**

```bash
muxiveo --cli config show tools
```

### `config get`

Un réglage.

```text
muxiveo --cli config get [-h] [--json] [--log-format {text,jsonl,events}] [--verbose] SECTION.CLÉ
```

| Option | Effet |
|---|---|
| `key` | Réglage section.clé (voir config show). |
| `--json` | Résultat JSON sur stdout. |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Journal détaillé (sortie brute des outils). |

**Exemples**

```bash
muxiveo --cli config get ffmpeg.threads
```

### `config set`

Écrire un réglage dans config.ini (valeur validée).

```text
muxiveo --cli config set [-h] [--json] [--log-format {text,jsonl,events}] [--verbose] SECTION.CLÉ VALEUR
```

| Option | Effet |
|---|---|
| `key` | Réglage section.clé (voir config show). |
| `value` | Nouvelle valeur (booléens : true / false). |
| `--json` | Résultat JSON sur stdout. |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Journal détaillé (sortie brute des outils). |

**Exemples**

```bash
muxiveo --cli config set ui.log_max_lines 5000
```

### `config unset`

Retirer la surcharge config.ini d'un réglage.

```text
muxiveo --cli config unset [-h] [--json] [--log-format {text,jsonl,events}] [--verbose] SECTION.CLÉ
```

| Option | Effet |
|---|---|
| `key` | Réglage section.clé. |
| `--json` | Résultat JSON sur stdout. |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Journal détaillé (sortie brute des outils). |

**Exemples**

```bash
muxiveo --cli config unset ui.theme
```

### `config reset`

Revenir à la valeur par défaut.

```text
muxiveo --cli config reset [-h] [--json] [--log-format {text,jsonl,events}] [--verbose] SECTION.CLÉ
```

| Option | Effet |
|---|---|
| `key` | Réglage section.clé. |
| `--json` | Résultat JSON sur stdout. |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Journal détaillé (sortie brute des outils). |

**Exemples**

```bash
muxiveo --cli config reset ui.theme
```

### `workdir`

Dossier de travail : éléments abandonnés, nettoyage.

```text
muxiveo --cli workdir [-h] {list,clean} ...
```

**Exemples**

```bash
muxiveo --cli workdir list
muxiveo --cli workdir clean --dry-run
```

### `workdir list`

Éléments nettoyables (jobs actifs exclus).

```text
muxiveo --cli workdir list [-h] [--work-dir WORK_DIR] [--json] [--log-format {text,jsonl,events}] [--verbose]
```

| Option | Effet |
|---|---|
| `--work-dir` | Dossier de travail (défaut : réglage [paths] work_dir). |
| `--json` | Résultat JSON sur stdout. |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Journal détaillé (sortie brute des outils). |

**Exemples**

```bash
muxiveo --cli workdir list
```

### `workdir clean`

Nettoyer le dossier de travail (comme « Nettoyer » au démarrage).

```text
muxiveo --cli workdir clean [-h] [--dry-run] [--work-dir WORK_DIR] [--json] [--log-format {text,jsonl,events}]
                            [--verbose]
```

| Option | Effet |
|---|---|
| `--dry-run` | Lister sans supprimer. |
| `--work-dir` | Dossier de travail (défaut : réglage [paths] work_dir). |
| `--json` | Résultat JSON sur stdout. |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Journal détaillé (sortie brute des outils). |

**Exemples**

```bash
muxiveo --cli workdir clean
```

### `plugins`

Extensions : moteur vidéo MVO (mvo-rife), accélération TensorRT (mvo-rife-trt).

```text
muxiveo --cli plugins [-h] {list,install,update,remove} ...
```

**Exemples**

```bash
muxiveo --cli plugins list
muxiveo --cli plugins install mvo-rife
```

### `plugins list`

État des extensions.

```text
muxiveo --cli plugins list [-h] [--log-format {text,jsonl,events}] [{mvo-rife,mvo-rife-trt,mvo-fel}]
```

| Option | Effet |
|---|---|
| `name` | Extension (défaut : toutes). — `mvo-rife`, `mvo-rife-trt`, `mvo-fel` |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |

**Exemples**

```bash
muxiveo --cli plugins list
```

### `plugins install`

Installer une extension (TensorRT : --accept-license).

```text
muxiveo --cli plugins install [-h] [--log-format {text,jsonl,events}] [--accept-license] [--force]
                              {mvo-rife,mvo-rife-trt,mvo-fel}
```

| Option | Effet |
|---|---|
| `name` | Extension : mvo-rife, mvo-rife-trt, mvo-fel. — `mvo-rife`, `mvo-rife-trt`, `mvo-fel` |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--accept-license` | Accepter la licence NVIDIA TensorRT for RTX (https://docs.nvidia.com/deeplearning/tensorrt-rtx/latest/reference/sla.html). |
| `--force` | Installer même si la machine semble incompatible. |

**Exemples**

```bash
muxiveo --cli plugins install mvo-rife-trt --accept-license
```

### `plugins update`

Mettre à jour une extension, ou toutes celles installées.

```text
muxiveo --cli plugins update [-h] [--log-format {text,jsonl,events}] [{mvo-rife,mvo-rife-trt,mvo-fel}]
```

| Option | Effet |
|---|---|
| `name` | Extension (défaut : toutes). — `mvo-rife`, `mvo-rife-trt`, `mvo-fel` |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |

**Exemples**

```bash
muxiveo --cli plugins update
```

### `plugins remove`

Supprimer une extension.

```text
muxiveo --cli plugins remove [-h] [--log-format {text,jsonl,events}] {mvo-rife,mvo-rife-trt,mvo-fel}
```

| Option | Effet |
|---|---|
| `name` | Extension : mvo-rife, mvo-rife-trt, mvo-fel. — `mvo-rife`, `mvo-rife-trt`, `mvo-fel` |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |

**Exemples**

```bash
muxiveo --cli plugins remove mvo-rife-trt
```

### `nvencc`

NVEncC : état, installation, mise à jour, suppression de la copie utilisateur.

```text
muxiveo --cli nvencc [-h] {status,install,update,remove} ...
```

**Exemples**

```bash
muxiveo --cli nvencc status --check
muxiveo --cli nvencc install
```

### `nvencc status`

Présence, version et provenance de NVEncC.

```text
muxiveo --cli nvencc status [-h] [--check] [--json] [--log-format {text,jsonl,events}] [--verbose]
```

| Option | Effet |
|---|---|
| `--check` | Comparer à la dernière release (GitHub). |
| `--json` | Résultat JSON sur stdout. |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Journal détaillé (sortie brute des outils). |

**Exemples**

```bash
muxiveo --cli nvencc status --check
```

### `nvencc install`

Installer NVEncC (apt / dnf, sinon dossier utilisateur).

```text
muxiveo --cli nvencc install [-h] [--method {apt,dnf,user,windows}] [--no-sudo] [--json]
                             [--log-format {text,jsonl,events}] [--verbose]
```

| Option | Effet |
|---|---|
| `--method` | Méthode imposée (défaut : selon la machine). — `apt`, `dnf`, `user`, `windows` |
| `--no-sudo` | Sans aucune élévation (ni sudo ni pkexec) : dossier utilisateur. |
| `--json` | Résultat JSON sur stdout. |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Journal détaillé (sortie brute des outils). |

**Exemples**

```bash
muxiveo --cli nvencc install
```

### `nvencc update`

Mettre à jour NVEncC si une version plus récente existe.

```text
muxiveo --cli nvencc update [-h] [--method {apt,dnf,user,windows}] [--no-sudo] [--json]
                            [--log-format {text,jsonl,events}] [--verbose]
```

| Option | Effet |
|---|---|
| `--method` | Méthode imposée (défaut : selon la machine). — `apt`, `dnf`, `user`, `windows` |
| `--no-sudo` | Sans aucune élévation (ni sudo ni pkexec) : dossier utilisateur. |
| `--json` | Résultat JSON sur stdout. |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Journal détaillé (sortie brute des outils). |

**Exemples**

```bash
muxiveo --cli nvencc update
```

### `nvencc remove`

Supprimer la copie utilisateur de NVEncC.

```text
muxiveo --cli nvencc remove [-h] [--json] [--log-format {text,jsonl,events}] [--verbose]
```

| Option | Effet |
|---|---|
| `--json` | Résultat JSON sur stdout. |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |
| `--verbose` | Journal détaillé (sortie brute des outils). |

**Exemples**

```bash
muxiveo --cli nvencc remove
```

### `schema`

Schéma JSON du contrat (jobs, exact-job, profils) ou du catalogue de l'API.

```text
muxiveo --cli schema [-h] [--output OUTPUT] [--version {1,exact-job,decision-profile,all,api}]
                     [--log-format {text,jsonl,events}]
```

| Option | Effet |
|---|---|
| `--output` | Ecrire le schéma JSON dans un fichier. |
| `--version` | Contrat à publier ; « api » : catalogue des opérations de la couche de service. (défaut : `1`) — `1`, `exact-job`, `decision-profile`, `all`, `api` |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |

**Exemples**

```bash
muxiveo --cli schema --version 1
muxiveo --cli schema --version api
```

### `help`

Aide d'une commande, de toutes (--all) ou référence Markdown (--format md).

```text
muxiveo --cli help [-h] [--all] [--format {text,md}] [--language LANGUAGE] [COMMANDE ...]
```

| Option | Effet |
|---|---|
| `topic` | Commande (et sous-commande), ex. profile apply. |
| `--all` | Aide de toutes les commandes. |
| `--format` | text (terminal) ou md (référence Markdown, docs/cli/reference.md). (défaut : `text`) — `text`, `md` |
| `--language` | Langue de l'aide (fra, eng ; défaut : [ui] language). |
| `--log-format` | ==SUPPRESS== (défaut : `text`) |

**Exemples**

```bash
muxiveo --cli help encode
muxiveo --cli help profile apply
muxiveo --cli help --all
muxiveo --cli help --format md > reference.md
```

## API

### `call`

Exécute une opération du catalogue (voir `operations`) avec une requête JSON stricte. Réponse sur stdout ({status, result | error}), événements versionnés sur stderr.

```text
muxiveo --cli call [-h] [--request FICHIER|-] [--ffmpeg FFMPEG] [--ffprobe FFPROBE] [--mediainfo MEDIAINFO]
                   [--work-dir WORK_DIR] [--threads THREADS] [--allow-privileged] OPÉRATION
```

| Option | Effet |
|---|---|
| `operation` | Nom de l'opération (ex. media.inspect). |
| `--request` | Requête JSON : fichier (chemins relatifs à son dossier) ou - pour stdin. Défaut : {}. |
| `--ffmpeg` | Chemin ffmpeg pour cet appel. |
| `--ffprobe` | Chemin ffprobe pour cet appel. |
| `--mediainfo` | Chemin mediainfo pour cet appel. |
| `--work-dir` | Dossier de travail pour cet appel. |
| `--threads` | Threads ffmpeg pour cet appel. |
| `--allow-privileged` | Autoriser une opération privilégiée (configuration, extensions, NVEncC, mise à jour, nettoyage du dossier de travail). |

**Exemples**

```bash
muxiveo --cli call media.inspect --request requete.json
muxiveo --cli call media.extract --request - < requete.json
muxiveo --cli call config.set --allow-privileged --request reglage.json
```

### `operations`

Opérations de la couche de service ; --describe publie leur schéma.

```text
muxiveo --cli operations [-h] [--describe OPÉRATION] [--json] [--log-format {text,jsonl,events}]
```

| Option | Effet |
|---|---|
| `--describe` | Afficher le schéma de requête et de résultat, les erreurs et les métadonnées. |
| `--json` | Liste au format JSON. |
| `--log-format` | Format du journal : text (lisible), jsonl (une ligne JSON par message) ou events (événements versionnés sur stderr, réponse JSON sur stdout). (défaut : `text`) — `text`, `jsonl`, `events` |

**Exemples**

```bash
muxiveo --cli operations
muxiveo --cli operations --describe media.extract
```

### `serve`

JSON-RPC 2.0, un objet JSON par ligne : server.info, job.submit, job.cancel, job.get, job.list, server.shutdown ; notifications job.event et server.exiting. Chemins des requêtes absolus ou relatifs au base_dir de la soumission.

```text
muxiveo --cli serve [-h] [--allow-privileged] [--max-jobs N] [--max-analyses N]
```

| Option | Effet |
|---|---|
| `--allow-privileged` | Autoriser une opération privilégiée (configuration, extensions, NVEncC, mise à jour, nettoyage du dossier de travail). |
| `--max-jobs` | Traitements de médias simultanés (défaut : 1). |
| `--max-analyses` | Analyses simultanées (inspection, aperçus…) pendant un traitement (défaut : 2). |

**Exemples**

```bash
muxiveo --cli serve
muxiveo --cli serve --max-jobs 2 --max-analyses 2
muxiveo --cli serve --allow-privileged
```

## Opérations de la couche de service

Appelables par `muxiveo --cli call OPÉRATION --request requête.json` ; schémas : `operations --describe OPÉRATION`.

| Opération | Rôle | Nature |
|---|---|---|
| `app.update_install` | Télécharger et installer la dernière version (sans relancer l'interface). | longue, écrit |
| `calibration.compose` | Composer deux calibrations (cible face à une référence synchronisée : synchro dans la sortie). | écrit |
| `calibration.edit` | Éditer une calibration comme le Synchro Studio : coupe, silence, jonction retirée, décalage (du départ ou depuis un segment), méthode de cadence (« auto » tranchée par la hauteur). | écrit |
| `calibration.invert` | Inverser une calibration (synchro de la référence face à la cible). | écrit |
| `calibration.show` | Décrire une calibration de synchro : segments, coupes et silences, plages de sortie, cadence. | — |
| `config.get` | Un réglage : valeur effective et provenance. | — |
| `config.path` | Emplacements de config.ini (surcharges) et du fichier de réglages de l'interface. | — |
| `config.reset` | Ramener un réglage au défaut (surcharge config.ini et réglage de l'interface retirés). | écrit |
| `config.set` | Écrire un réglage dans config.ini (comme Enregistrer du panneau Paramètres). | écrit |
| `config.show` | Réglages : valeur effective et provenance (config.ini, réglage de l'interface, défaut). | — |
| `config.unset` | Retirer la surcharge config.ini d'un réglage (le réglage de l'interface peut réapparaître). | écrit |
| `encode.preview_capture` | Aperçu réel d'un job encodé : images ou extrait vidéo encodés avec les réglages du job. | longue, écrit, GPU |
| `encode_preset.delete` | Supprimer un preset d'encodage enregistré. | écrit |
| `encode_preset.export` | Exporter un preset d'encodage dans un fichier JSON. | écrit |
| `encode_preset.import` | Importer un preset d'encodage depuis un fichier JSON. | écrit |
| `encode_preset.list` | Presets d'encodage enregistrés (communs avec l'interface). | — |
| `encode_preset.save` | Enregistrer un preset d'encodage d'après ses champs. | écrit |
| `encode_preset.show` | Contenu d'un preset d'encodage. | — |
| `hdr.inspect` | Inspection HDR : en-têtes, RPU / HDR10+, mesure de l'image, comparaison, FEL Analyzer, rapport. | longue, écrit, GPU |
| `hdr.patch` | Corriger les métadonnées HDR sans réencodage : MaxCLL/MaxFALL, MDCV, RPU (L6, L5, L11, écran, CM v4.0), HDR10+ (retrait, TSDML, profil A, plafond, recalcul), réalignement d'après la mesure. | longue, écrit, GPU |
| `hybrid.plan` | Préparer une hybridation sans l'exécuter : appariement, pistes retenues, calibrations, sorties ; exact-jobs finalisés et recette effective en option. | longue, écrit, GPU |
| `hybrid.run` | Hybrider une paire ou une saison (moteur du Hybrid Studio) : vidéo du master, audio / sous-titres des donneurs calés ; encodage par épisode avec le bloc `encode`. | longue, écrit, GPU |
| `media.batch` | Exécuter un lot de jobs de remux (template + fichier batch ou sources) ; collisions refusées d'avance. | longue, écrit, GPU |
| `media.bluray_titles` | Lister les titres (playlists) d'un Blu-ray avant sélection, du plus long au plus court : durée, segments, flux, taille. | — |
| `media.extract` | Extraire des pistes (flux élémentaire ou Matroska), couches Dolby Vision (EL, BL, RPU), HDR10+ ou pièces jointes. | longue, écrit |
| `media.inspect` | Inspecter des sources : pistes, HDR, chapitres, pièces jointes ; ou générer un exact-job minimal. | — |
| `media.preview` | Résoudre un job de remux ou d'encodage et afficher les commandes prévues. | GPU |
| `media.run` | Exécuter un job de remux ou d'encodage (ou enregistrer son job exact). | longue, écrit, GPU |
| `media.validate` | Résoudre et valider un job de remux ou d'encodage sans l'exécuter. | GPU |
| `nvencc.install` | Installer ou mettre à jour NVEncC (paquet apt/dnf, sinon dossier utilisateur). | longue, écrit |
| `nvencc.remove` | Supprimer la copie utilisateur de NVEncC (un paquet système se retire par apt / dnf). | écrit |
| `nvencc.status` | NVEncC : présence, version, provenance, méthode d'installation, mise à jour disponible. | — |
| `plugins.install` | Télécharger et installer une extension (TensorRT : licence NVIDIA à accepter). | longue, écrit, GPU |
| `plugins.list` | État des extensions (installées, compatibles, mises à jour). | — |
| `plugins.remove` | Supprimer une extension (et le cache de ses moteurs). | écrit |
| `plugins.update` | Mettre à jour une extension, ou toutes celles installées. | longue, écrit, GPU |
| `profile.apply` | Appliquer un profil à des sources et remuxer (ou encoder). | longue, écrit, GPU |
| `profile.batch` | Appliquer un profil décisionnel à un lot de fichiers ; collisions refusées d'avance. | longue, écrit, GPU |
| `profile.capture` | Capturer un profil décisionnel d'après un job (sélection, métadonnées, flags, ordre, variantes). | longue, écrit |
| `profile.delete` | Supprimer un profil décisionnel enregistré. | écrit |
| `profile.list` | Profils décisionnels enregistrés (communs avec l'interface). | — |
| `profile.preview` | Appliquer un profil à des sources sans exécuter : pistes retenues, décisions, commandes. | GPU |
| `profile.save` | Enregistrer un profil décisionnel (validé) parmi les profils de l'interface. | écrit |
| `profile.show` | Contenu d'un profil décisionnel enregistré. | — |
| `profile.validate` | Valider un profil décisionnel v1. | — |
| `schema.get` | Schémas JSON publiés : contrat des jobs, profils, catalogue de l'API. | — |
| `sync.scan` | Analyser le calage d'une piste (audio ou sous-titres) face à une référence. | longue, écrit |
| `sync.shift_subtitles` | Recaler un fichier de sous-titres texte (décalage ou calibration). | écrit |
| `sync.waveform` | Forme d'onde d'une piste audio sur une plage (pics par tranche), données du Synchro Studio. | longue |
| `system.capabilities` | Capacités de la machine : encodeurs, préréglages, Vulkan, NVOF / TensorRT, moteur vidéo MVO. | longue, GPU |
| `system.tools` | Commandes d'outils externes résolues par Muxiveo. | — |
| `system.version` | Version de Muxiveo ; avec check, comparaison à la dernière release du canal. | — |
| `tmdb.details` | Fiche TMDB complète (titre, année, épisode, résumé, genres, couverture) et balises MKV. | — |
| `tmdb.search` | Rechercher un film ou une série sur TMDB (25 résultats au plus). | — |
| `workdir.clean` | Nettoyer le dossier de travail (comme « Nettoyer » au démarrage) ; dry_run : liste seule. | écrit |
| `workdir.list` | Éléments nettoyables du dossier de travail (dossiers abandonnés ; jobs actifs exclus). | — |
