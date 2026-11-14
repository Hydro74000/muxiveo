# Parité GUI ↔ CLI

Chaque action de l'interface et son équivalent headless : commande CLI, opération de la
couche de service (`call <opération>`) et lot du plan de parité qui l'apporte.

État : ✅ disponible · 🟡 partiel · ⏳ prévu (lot) · ⛔ exclu (motif indiqué)

Opérations publiées : toutes les commandes existantes du CLI, plus l'encodage (`media.run` avec bloc
`encode`), `system.capabilities`, l'extraction `media.extract`, la correction HDR `hdr.patch`, les calibrations `calibration.*` et `sync.waveform` (voir `Muxiveo-cli operations`).

## Tableau de bord

| Action GUI | CLI | Opération | État |
|---|---|---|---|
| Version, recherche de mise à jour | `version [--check]` | `system.version` | ✅ |
| Outils détectés et leurs chemins | `tools` | `system.tools` | ✅ |
| Encodeurs logiciels / matériels, presets, Vulkan, NVOF, TensorRT | `capabilities` | `system.capabilities` | ✅ |
| Nettoyage du dossier de travail au démarrage | `workdir list\|clean` | `workdir.list/clean` | ⏳ L10 |

## Conteneur (remux)

| Action GUI | CLI | Opération | État |
|---|---|---|---|
| Ajouter / ordonner les sources, choisir les pistes | `remux`, `run --config` | `media.run` | ✅ |
| Langue, titre, drapeaux, ordre des pistes | job JSON (`tracks`, `track_order`) | `media.run` | ✅ |
| Variantes audio (piste convertie ajoutée) | job JSON (`audio_variants`) | `media.run` | ✅ |
| Chapitres : source, édition, import | job JSON (`chapters`) | `media.run` | ✅ |
| Pièces jointes de la source, ajout de fichiers | job JSON (`attachments`, `extra_attachments`) | `media.run` | ✅ |
| Balises globales, titre du fichier | job JSON (`tags`, `file_title`) | `media.run` | ✅ |
| Fiche TMDB : recherche avec choix du résultat | `tmdb search` | `tmdb.search` | ⏳ L9 (aujourd'hui : premier résultat ou `--tmdb-id`) |
| Fiche TMDB : détails, couverture | `--auto-tmdb`, `--tmdb-id` | `tmdb.details` | 🟡 opération ⏳ L9 |
| Nom de sortie (gabarit, tag de groupe) | `--output-template` | `media.run` | ✅ |
| Profil décisionnel : appliquer | `--profile`, `profile apply\|batch` | `profile.apply/batch` | ✅ |
| Profil décisionnel : capturer depuis l'état courant | `profile capture` | `profile.capture` | ⏳ L9 |
| Éditeur de profil : enregistrer un profil modifié | `profile save` | `profile.save` | ⏳ L9 |
| Profils enregistrés : lister, supprimer | `profile list\|show\|delete` | `profile.list/show/delete` | ⏳ L9 |
| Exporter le job (« Exporter JSON CLI », exact-job) | `--export-workflow` | `media.run` (`export_workflow`) | ✅ |
| Playlist Blu-ray : choisir parmi les titres | à définir | `media.bluray_titles` | ⏳ L9 (aujourd'hui : chemin de playlist) |
| Retirer les sous-titres EIA-608/708 embarqués | `--remove-closed-captions` | `media.run` | ✅ |
| Forcés / SDH automatiques | `--auto-forced-subs`, `--auto-sdh` | `media.run` | ✅ |
| Backend de muxage | `--mux-backend` | `media.run` | ✅ |
| Aperçu de la commande | `preview` | `media.preview` | ✅ |
| Extraire une piste (sous-titre, audio, vidéo ; flux élémentaire ou Matroska) | `extract --track` | `media.extract` | ✅ |
| Extraire une couche Dolby Vision (EL FEL/MEL, BL d'un profil 7), le RPU ou le HDR10+ | `extract --track v:0 --layer el\|bl\|rpu\|hdr10plus` | `media.extract` | ✅ |
| Extraire une pièce jointe (clic droit dans Pièces jointes) | `extract --attachment N\|NOM` | `media.extract` | ✅ |
| Tout extraire d'une source | `extract --all` | `media.extract` (`all`) | ✅ CLI seul |
| Inspecter le HDR d'une piste | `inspect --hdr` | `hdr.inspect` | ✅ |
| Synchronisation automatique / approfondie d'une source | `--auto-sync` (source 1), `--auto-sync-all`, `sources[i].sync.mode quick\|deep` | `media.run` | ✅ |
| Synchroniser une piste de sous-titres | `sync-scan --type subtitle` | `sync.scan` | ✅ |
| Références de synchro en chaîne (source synchronisée sur une autre source) | job JSON `sources[i].sync` (boucle refusée) | `media.run` | ✅ |
| Synchro propre à une piste, décalage manuel | job JSON (`tracks[].sync`, `time_shift_ms`) | `media.run` | ✅ |
| Synchro Studio : coupes, silences, jonctions, décalage depuis un segment | `calibration edit` (+ `show`, `compose`, `invert`) | `calibration.*` | ✅ |
| Synchro Studio : cadence (méthode audio, résolution) | `--cadence-method` ; `calibration edit --cadence-method --resolve-cadence` | `media.run`, `calibration.edit` | ✅ |
| Synchro Studio : forme d'onde | `sync-waveform` | `sync.waveform` | ✅ (données pour un client graphique) |
| Synchro Studio : pré-écoute | — | — | ⛔ lecture audio locale |
| « Synchro à valider » | `--reject-coherent-sync` (refus), avertissement sinon | `media.run` | ✅ décision explicite |

## Encodage

| Action GUI | CLI | Opération | État |
|---|---|---|---|
| Codec, mode de débit, qualité, débit, taille cible, 2 passes | bloc `encode`, `--video-codec`, `--crf`… | `media.run` | ✅ |
| Préréglage de l'encodeur, paramètres libres | `--encoder-preset`, `--video-set extra_params=…` | `media.run` | ✅ |
| Profondeur Auto / 8 / 10 bits | `--bit-depth` | `media.run` | ✅ |
| Recadrage (manuel, détection automatique), redimensionnement | `--crop auto\|…`, `--resize` | `media.run` | ✅ |
| Désentrelacement, NLMeans | `--deinterlace`, `--denoise nlmeans` | `media.run` | ✅ |
| Débruitage MVO-R2 (niveau, mode, animation, netteté) | `--denoise mvo-r2`, `--video-set restoration.*` | `media.run` | ✅ |
| Interpolation MVO-RIFE (facteur, cadence, préréglage, mode) | `--interpolate`, `--interpolation-preset` | `media.run` | ✅ |
| Tone mapping | `--tonemap` | `media.run` | ✅ |
| HDR10 statique (source, saisie, estimation rapide / précise) | bloc `encode.static_hdr` | `media.run` | ✅ |
| Dolby Vision (copie, profil), HDR10+ | `--dv`, `--hdr10plus` | `media.run` | ✅ |
| Intégrer la couche FEL (précision, périphérique, HDR10+ recalculé) | `--bake-fel`, `--video-set fel.*` | `media.run` | ✅ |
| Pistes audio : codec, débit, cœur TrueHD | `--audio-codec`, bloc `encode.audio` | `media.run` | ✅ |
| Appliquer à toutes les pistes vidéo | `video.default` du bloc `encode` | `media.run` | ✅ |
| Alignement Dolby Vision NVENC (recadrage proposé) | `accept_dovi_nvenc_crop` | `media.run` | ✅ |
| Lancer l'encodage, progression | `run`, `remux`, `encode` | `media.run` | ✅ |
| Conserver les fichiers temporaires après un échec | option dédiée | `media.run` | ✅ |
| Aperçu image / vidéo | `encode --preview-image\|--preview-video` | `encode.preview_capture` | ✅ |
| Aperçu / copie de la commande | `preview` | `media.preview` | ✅ |
| Profils d'encodage : charger, enregistrer, supprimer, importer, exporter | `--encode-preset`, `encode-preset …` | `encode_preset.*` | ✅ |
| Analyser l'apport du FEL | `inspect --hdr-fel` | `hdr.inspect` | ✅ |

## Hybrid Studio

| Action GUI | CLI | Opération | État |
|---|---|---|---|
| Référence + donneur (paire ou saison) | `hybrid --ref/--donor`, `--ref-dir/--donor-dir` | `hybrid.run` | ✅ |
| Plusieurs donneurs, rôles (audio seul, sous-titres seuls) | `--source RÔLE=CHEMIN` | `hybrid.plan/run` | ⏳ L8 |
| Mode de correspondance (épisodes, titres, ordre, manuel) | `--matching`, `--pair` | `hybrid.plan` | ⏳ L8 |
| Modèle de l'épisode témoin (pistes à conserver) | `--recipe` (recette exportée du Studio) | `hybrid.plan` | ⏳ L8 |
| Langues conservées, synchro, décalage manuel, tag | options de recette | `hybrid.plan` | 🟡 partiel ; complet ⏳ L8 |
| Préparer / recalibrer un élément, corrections par épisode | `hybrid --export-jobs` puis `run` / `batch` | `hybrid.plan` | 🟡 `--export-workflow` ; ⏳ L8 |
| Sauvegarder les workflows | `--export-workflow` | `hybrid.plan` | ✅ commande |
| Pré-écoute | — | — | ⛔ lecture audio locale |
| Encoder chaque épisode | bloc `encode` | `hybrid.run` | ⏳ L8 |

## HDR Studio

| Action GUI | CLI | Opération | État |
|---|---|---|---|
| Vue d'ensemble, contrôles, métadonnées dynamiques | `inspect --hdr static\|dynamic` | `hdr.inspect` | ✅ |
| Mesure rapide / complète | `inspect --hdr measure [--hdr-fast]` | `hdr.inspect` | ✅ |
| Comparer deux pistes | `inspect --hdr-compare` | `hdr.inspect` | ✅ |
| Exporter le rapport | `--hdr-report` | `hdr.inspect` | ✅ |
| Luminance : modèle de TV, pic de l'écran, simulation | `inspect --hdr … --hdr-advice --hdr-tv-model --hdr-display-peak` | `hdr.inspect` (`advice`) | ✅ |
| Valeurs proposées (MaxCLL / MaxFALL, écran de mastering) | `--hdr-advice` (`advice.suggested`) ; `hdr-patch --light-levels suggested --mdcv suggested` | `hdr.inspect`, `hdr.patch` | ✅ |
| Réalignement d'après la mesure (propositions, « Reporter les propositions cochées ») | `--hdr-advice` (`advice.realignment`) ; `hdr-patch --realign` | `hdr.inspect`, `hdr.patch` | ✅ |
| Édition : HDR10 statique, MDCV, RPU (L5, L6, L11, écran, CMv4), HDR10+, « Garder identiques » | `hdr-patch` | `hdr.patch` | ✅ |
| FEL Analyzer, test de survie | `inspect --hdr-fel [--hdr-fel-survival]` | `hdr.inspect` | ✅ |
| Moments FEL : comparaison avant / après | `--hdr-fel-moments DIR [--hdr-fel-exposure EV]` (PNG sans FEL, avec FEL, écart) | `hdr.inspect` | ✅ (images fixes ; zoom / trait à glisser : ⛔ rendu interactif) |
| Appliquer les valeurs au panneau Encodage | bloc `encode.static_hdr` | — | ⛔ lien entre panneaux (équivalent : saisir les valeurs) |

## Paramètres, extensions, mises à jour

| Action GUI | CLI | Opération | État |
|---|---|---|---|
| Lire / modifier un réglage, recharger `config.ini` | `config get\|set\|unset\|reset\|show` | `config.*` | ⏳ L10 |
| Thème, échelle, langue de l'interface | `config set ui.*` | `config.set` | ⏳ L10 |
| Extensions MVO-RIFE, TensorRT, FEL : installer, mettre à jour, supprimer | `plugins install\|update\|remove` | `plugins.*` | ✅ |
| Réglages des extensions (TensorRT, mise à jour automatique) | `config set plugins.*` | `config.set` | ⏳ L10 |
| NVEncC : installer, mettre à jour, supprimer | `tools install\|remove nvencc` | `tools.install/remove` | ⏳ L10 |
| Installer une mise à jour de Muxiveo | `version --install --yes` | `app.update_install` | ⏳ L11 |

## Hors périmètre

| Fonction | Motif |
|---|---|
| Merge DoVi / HDR10+ | exclu du plan de parité |
| Thème, disposition, journal à l'écran, glisser-déposer | interface graphique uniquement |
| Pré-écoute audio (Hybrid Studio, Synchro Studio) | lecture audio locale |
