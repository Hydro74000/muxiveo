# Notes de version / Release Notes — Muxiveo v5.2.0

* [Français](#version-française)
* [English](#english-version)

---

<a name="version-française"></a>
# Version Française

*Modifications depuis la version v5.1.1.*

---

## Nouveautés

* **Débruitage MVO-R2 (moteur vidéo MVO 1.15.0).** Nouveau débruiteur appris, proposé par défaut dans le filtre « Débruitage » du panneau Encodage (NLMeans reste disponible au choix). Chaque image est débruitée à partir d'une fenêtre de 5 images recalées par le flux optique du réseau : le grain et le bruit sont retirés, le détail confirmé par les images voisines est gardé.
  * **Niveaux** : *Auto* (dosage décidé plan par plan d'après le grain mesuré : une source propre est à peine touchée), *Léger*, *Moyen*, *Fort*. Contenu animé (genre « Animation » de la fiche TMDB, ou réglage manuel) : l'Auto retire un cran de plus.
  * **Compensation de netteté** (liste « Netteté », active par défaut) : rend une partie du détail fin que le débruitage a retiré, seulement là où il est stable d'une image à l'autre — jamais le grain, jamais plus que ce qui a été retiré.
  * **Mode Auto (par défaut)** : mode rapide au-delà de 2560 px de large — en UHD, il garde le même détail que le mode normal pour environ un tiers de temps en moins — et mode normal en dessous. Mode rapide : flux optique calculé au quart de la définition.
  * **HDR** : traitement en 10 bits ; MaxCLL / MaxFALL de la source conservés quand le débruitage ne les change pas de façon visible, sinon mesurés sur l'image débruitée ; Dolby Vision et HDR10+ copiés puis contrôlés image par image ; compatible avec l'intégration de la couche Dolby Vision FEL.
  * **Débruitage puis interpolation** dans le même passage quand MVO-RIFE est aussi actif.
  * **NVIDIA TensorRT** : débruitage accéléré avec l'extension `mvo-rife-trt` 1.3.0 (repli Vulkan automatique).
  * MVO-R2 est un traitement exigeant : en 4K, c'est lui qui fixe la vitesse de la chaîne d'encodage.
* **Extension « Moteur vidéo MVO (débruitage, interpolation) ».** L'extension `mvo-rife` porte désormais les deux traitements ; la page Extensions la présente sous ce nom.
* **x265 : réglage *frame-threads*** (nombre d'images encodées en parallèle, automatique par défaut). Le nombre de fils FFmpeg n'est plus appliqué à libx265, qui occupe toujours tous les cœurs (limite par `pools=N` dans les paramètres de la piste).
* **x265 : nouveaux paramètres avancés** : `qg-size`, `deblock`, `qcomp`, `cbqpoffs` / `crqpoffs`, `b-intra`, `rc-lookahead` ; les options désactivées (`no-…`) et les valeurs hors des bornes des contrôles sont relues et conservées.
* **Échec d'encodage avec fichiers temporaires.** Quand un encodage lancé depuis l'interface échoue après avoir produit des fichiers, une fenêtre propose de conserver le dossier de travail pour examen ; sinon il est supprimé en arrière-plan.
* **CLI : couche de service et commande `call`.** Premières opérations publiées (`system.version`, `media.inspect`) avec un contrat stable, préparant un futur service : `operations` liste le catalogue, `schema --version api` le publie en JSON, `call <opération> --request fichier.json` (ou `-` pour stdin) exécute une requête JSON stricte (erreurs détaillées par champ) et répond en JSON. Nouveau format `--log-format events` : journal, progression et résultat en événements versionnés. Les sorties historiques des commandes sont inchangées. Feuille de parité avec l'interface : `docs/cli/parite-gui.md`.
* **CLI : Ctrl+C annule proprement un remux ou un lot.** `remux` / `run` / `profile apply` s'arrêtent comme une annulation depuis l'interface : processus arrêtés, fichier partiel et dossier de travail supprimés (code retour 6). Pendant un `batch`, le job en cours est annulé et le lot s'arrête ; les sorties déjà publiées restent et le résumé le signale (`cancelled`, code retour 7). Toutes les commandes du CLI sont désormais des opérations de la couche de service, appelables par `call` (lots : un job enfant par fichier, statut `succeeded` / `partial` / `failed` / `cancelled`). Ctrl+C annule aussi la mesure HDR et le FEL Analyzer de `inspect --hdr`, et arrête `hybrid`.
* **CLI : encodage complet.** Nouvelle commande `encode` et bloc `encode` des jobs JSON (aussi pour `run`, `batch` et les profils) : codec et mode de débit, préréglage, profondeur, recadrage (dont la détection automatique), redimensionnement, filtres, débruitage MVO-R2, interpolation MVO-RIFE, HDR10 statique (source, saisi ou estimé), copie Dolby Vision / HDR10+, intégration FEL, tone mapping, réencodage audio, presets d'encodage de l'interface. Mêmes règles que le panneau Encodage (réglages par défaut selon la source, politique HDR par codec) ; `validate --json` publie le plan résolu, `--dry-run` les commandes prévues. Nouvelle commande `capabilities` : encodeurs, préréglages, Vulkan, NVOF, TensorRT, moteur vidéo MVO. Aperçus image et vidéo encodés (`encode --preview-image` / `--preview-video`), presets d'encodage de l'interface gérés en CLI (`encode-preset list|show|save|import|export|delete`), dossier de travail d'un encodage en échec conservable (`--keep-failed-work-dir`).

## Correctifs

* **Paramètres avancés saisis à la main** : reportés dans le dialogue pour tous les encodeurs (option inconnue conservée ; exclusions NVEncC sans erreur à l'ouverture).
* **Cadence** : la valeur aberrante `500/21` renvoyée par ffprobe pour certaines sources est interprétée comme 23,976 i/s.
* **Synchronisation audio** : sélecteur de flux FFmpeg numérique accepté.
* **TMDB** : jeton d'accès (*bearer*) reconnu lorsqu'il est saisi dans le champ de la clé d'API.
* **macOS** : bibliothèques Qt collectées par les crochets PyInstaller ; signature du paquet final vérifiée.

---
---

<a name="english-version"></a>
# English Version

*Changes since v5.1.1.*

---

## New features

* **MVO-R2 denoising (MVO video engine 1.15.0).** A new learned denoiser, now the default in the Encoding panel's "Denoise" filter (NLMeans remains available). Each frame is denoised from a window of 5 frames aligned by the network's own optical flow: grain and noise are removed while detail confirmed by neighbouring frames is kept.
  * **Levels**: *Auto* (strength decided shot by shot from the measured grain: a clean source is barely touched), *Light*, *Medium*, *Strong*. Animated content (TMDB "Animation" genre, or manual setting): Auto removes one step more.
  * **Sharpness compensation** ("Sharpness" list, on by default): restores part of the fine detail removed by denoising, only where it is stable from frame to frame — never the grain, never more than what was removed.
  * **Auto mode (default)**: fast mode above 2560 px wide — in UHD it keeps the same detail as normal mode in about a third less time — and normal mode below. Fast mode: optical flow computed at a quarter of the resolution.
  * **HDR**: 10-bit processing; source MaxCLL / MaxFALL kept when denoising does not visibly change them, otherwise measured on the denoised picture; Dolby Vision and HDR10+ copied and checked frame by frame; compatible with Dolby Vision FEL layer integration.
  * **Denoising then interpolation** in a single pass when MVO-RIFE is also enabled.
  * **NVIDIA TensorRT**: accelerated denoising with the `mvo-rife-trt` 1.3.0 extension (automatic Vulkan fallback).
  * MVO-R2 is demanding: in 4K, it sets the speed of the encoding chain.
* **"MVO video engine (denoising, interpolation)" extension.** The `mvo-rife` extension now provides both processes and is listed under this name on the Extensions page.
* **x265: *frame-threads* setting** (number of frames encoded in parallel, automatic by default). The FFmpeg thread count is no longer applied to libx265, which always uses every core (limit it with `pools=N` in the track parameters).
* **x265: new advanced parameters**: `qg-size`, `deblock`, `qcomp`, `cbqpoffs` / `crqpoffs`, `b-intra`, `rc-lookahead`; disabled options (`no-…`) and values outside the widget ranges are read back and kept.
* **Failed encode with temporary files.** When an encode started from the interface fails after producing files, a dialog offers to keep the work folder for inspection; otherwise it is removed in the background.
* **CLI: service layer and `call` command.** First operations published (`system.version`, `media.inspect`) with a stable contract, paving the way for a future service: `operations` lists the catalog, `schema --version api` publishes it as JSON, `call <operation> --request file.json` (or `-` for stdin) runs a strict JSON request (field-level errors) and replies in JSON. New `--log-format events`: log, progress and result as versioned events. Existing command outputs are unchanged. GUI parity sheet: `docs/cli/parite-gui.md`.
* **CLI: Ctrl+C cleanly cancels a remux or a batch.** `remux` / `run` / `profile apply` stop like a cancellation from the GUI: processes stopped, partial file and work folder removed (exit code 6). During a `batch`, the current job is cancelled and the batch stops; already published outputs are kept and the summary says so (`cancelled`, exit code 7). Every CLI command is now a service-layer operation callable through `call` (batches: one child job per file, `succeeded` / `partial` / `failed` / `cancelled` status). Ctrl+C also cancels the HDR measurement and FEL Analyzer of `inspect --hdr`, and stops `hybrid`.
* **CLI: full encoding.** New `encode` command and `encode` block in JSON jobs (also for `run`, `batch` and profiles): codec and rate control, encoder preset, bit depth, crop (including automatic detection), resize, filters, MVO-R2 denoising, MVO-RIFE interpolation, static HDR10 (source, manual or estimated), Dolby Vision / HDR10+ copy, FEL baking, tone mapping, audio re-encoding, encoding presets from the GUI. Same rules as the Encoding panel (defaults from the source, per-codec HDR policy); `validate --json` publishes the resolved plan, `--dry-run` the planned commands. New `capabilities` command: encoders, presets, Vulkan, NVOF, TensorRT, MVO video engine. Encoded image and video previews (`encode --preview-image` / `--preview-video`), GUI encoding presets managed from the CLI (`encode-preset list|show|save|import|export|delete`), work folder of a failed encode can be kept (`--keep-failed-work-dir`).

## Fixes

* **Manually entered advanced parameters**: carried into the dialog for every encoder (unknown options kept; NVEncC exclusions without an error on opening).
* **Frame rate**: the bogus `500/21` value reported by ffprobe for some sources is read as 23.976 fps.
* **Audio synchronization**: numeric FFmpeg stream specifier accepted.
* **TMDB**: access (*bearer*) token recognized when entered in the API key field.
* **macOS**: Qt libraries collected through PyInstaller hooks; final bundle signature verified.
