# Notes de version / Release Notes — Muxiveo v5.2.0

* [Français](#version-française)
* [English](#english-version)

---

<a name="version-française"></a>
# Version Française

*Modifications depuis la version v5.1.1.*

---

## Nouveautés

* **Débruitage MVO-R2 (moteur vidéo MVO 1.14.0).** Nouveau débruiteur appris, proposé par défaut dans le filtre « Débruitage » du panneau Encodage (NLMeans reste disponible au choix). Chaque image est débruitée à partir d'une fenêtre de 5 images recalées par le flux optique du réseau : le grain et le bruit sont retirés, le détail confirmé par les images voisines est gardé.
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

* **MVO-R2 denoising (MVO video engine 1.14.0).** A new learned denoiser, now the default in the Encoding panel's "Denoise" filter (NLMeans remains available). Each frame is denoised from a window of 5 frames aligned by the network's own optical flow: grain and noise are removed while detail confirmed by neighbouring frames is kept.
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

## Fixes

* **Manually entered advanced parameters**: carried into the dialog for every encoder (unknown options kept; NVEncC exclusions without an error on opening).
* **Frame rate**: the bogus `500/21` value reported by ffprobe for some sources is read as 23.976 fps.
* **Audio synchronization**: numeric FFmpeg stream specifier accepted.
* **TMDB**: access (*bearer*) token recognized when entered in the API key field.
* **macOS**: Qt libraries collected through PyInstaller hooks; final bundle signature verified.
