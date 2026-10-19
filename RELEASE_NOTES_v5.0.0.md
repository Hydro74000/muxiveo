# Notes de version / Release Notes — Muxiveo v5.0.0

* [Français](#version-française)
* [English](#english-version)

---

<a name="version-française"></a>
# Version Française

*Modifications depuis la dernière version stable (v4.2.2).*

---

## 1. Fonctionnalités majeures

* **HDR Studio** (nouvelle page) : inspecte le HDR d'une piste vidéo. On y accède en déposant un fichier, ou par un clic droit « Inspecter le HDR… » sur une piste dans Conteneur ou dans Encodage.
  * Les métadonnées statiques s'affichent instantanément ; le Dolby Vision (RPU) et le HDR10+ du film entier se lisent en quelques secondes.
  * Un audit signale les comptages incohérents, les MDCV et MaxCLL absents ou génériques, un MaxCLL contredit par le contenu, les plans HDR10+ décalés, etc.
  * **Mesure de l'image réelle** : MaxCLL et MaxFALL exacts, en mode rapide (images clés, environ 20 s pour un film) ou complet.
  * **Luminance** : courbes zoomables et simulation d'un téléviseur dont le pic est réglable, qui montre l'écrêtage et l'assombrissement. Les valeurs proposées peuvent être reportées dans le panneau Encodage.
  * **Édition** : corrige sans réencodage MaxCLL/MaxFALL, l'écran de mastering (MDCV), le RPU (L5, L6, L11, CMv4) et le HDR10+, et propose un réalignement d'après la mesure.
  * **Comparer** : deux fichiers ou deux pistes alignés automatiquement, avec les écarts de métadonnées et de courbes.
  * **FEL Analyzer** (sources P7 FEL) : apport visible de la couche FEL par catégorie (ton, hautes lumières, détail, grain, couleur, ombres, stabilité) et moments les plus marqués. Un clic sur une vignette ouvre un comparateur avant/après en pleine définition. L'outil estime aussi ce qui survit à l'encodage.
  * Rapport exportable en HTML ou JSON. En CLI : `inspect --hdr static|dynamic|measure`, `--hdr-compare`, `--hdr-report`, `--hdr-fast`.
* **Intégration de la couche FEL Dolby Vision** (extension mvo-fel) : à l'encodage d'une source P7 FEL, l'image complète (BL + EL) est reconstruite, au lieu de ne garder que la couche de base. Option « Intégrer la couche FEL à l'image avant encodage » de la carte HDR, décochée par défaut.
  * Trois précisions : Lent (recommandé, identique au CPU au bit près), Standard et Rapide. Le GPU se choisit par piste.
  * HDR10+ et MaxCLL/MaxFALL sont recalculés sur l'image reconstruite (sortie HEVC).
  * Compatible FFmpeg, NVEncC et interpolation.
* **Interpolation : moteur hybride** (extension mvo-rife) : RIFE est combiné à une compensation de mouvement et, sur GPU NVIDIA, au flux optique matériel (NVOF). Un sélecteur appris choisit la meilleure méthode zone par zone. Les motifs répétitifs, les grands mouvements et le flou de bougé sont mieux rendus.
  * Préréglages : Rapide, Équilibré (défaut, hybride), Qualité (avec RIFE v4.15 affiné par Muxiveo), Ultra (Qualité + passe inverse) et Light.
  * Nouveau facteur ×2,5 (23,976 → 59,94 i/s, 24 → 60 i/s).
  * Cache en mémoire GPU qui accélère Qualité, Ultra et Light.
* **Accélération NVIDIA TensorRT** (extension facultative mvo-rife-trt, Linux et Windows) : l'interpolation RIFE passe par TensorRT for RTX, avec repli automatique sur Vulkan. L'installation demande d'accepter la licence NVIDIA.
* **Page Extensions** : installer, mettre à jour (automatiquement si souhaité) et supprimer les extensions. Une extension incompatible avec la machine est signalée, avec la raison. En CLI : `plugins list|install|update|remove`.
* **Interface modernisée, exécution unifiée** :
  * un bouton unique lance l'opération du panneau actif (Remux & Encode, Hybrid, Merge DoVi, correction HDR) ; une flèche donne accès aux autres opérations prêtes ;
  * la navigation reste libre pendant un traitement ;
  * le journal est commun et filtrable par origine ;
  * la barre latérale est regroupée en Traitement média et Studios.
* **Hybrid Studio** :
  * Sync Studio avec sélecteur d'épisode, navigation entre les coupures et pré-écoute par donneur ;
  * nom de sortie : champs du template proposés au clic droit, avec aperçu en direct ;
  * recherche TMDB depuis le champ ID ;
  * la relance d'un lot partiellement réussi ne traite que les épisodes restants.
* **Sous-titres embarqués EIA-608/708** : ils sont détectés dans la vidéo, puis conservés ou retirés sans réencodage (HDR et Dolby Vision préservés). L'option est disponible dans Remux, dans Hybrid et en CLI (`--remove-closed-captions`).

---

## 2. Correctifs majeurs

* **TrueHD Atmos copié dans Encodage** : seul le cœur TrueHD était conservé et la couche Atmos était perdue. Le flux est maintenant copié intact. E-AC-3 JOC est aussi préservé en synchronisation physique.
* **Son coupé après un saut dans la lecture** : avec l'interpolation, NVEncC ou l'injection HDR, une source qui contenait de l'audio et des sous-titres rares pouvait donner un fichier dont le son disparaissait après un seek.
* **Synchronisation lors d'un encodage avec le muxer natif** : les corrections (audio coupé ou complété, sous-titres recalés) n'étaient pas appliquées. Elles le sont maintenant.
* **AC-3 / E-AC-3 en synchronisation physique** : le délai propre à l'encodeur audio est conservé.

---

## 3. Mineurs

* Muxage Matroska natif 27 à 46 % plus rapide, avec une mémoire limitée à environ 70 Mio.
* Détection Dolby Vision / HDR10+ avant un encodage ramenée d'environ 15 s à 2 s.
* Hybrid : les profils décisionnels s'appliquent après l'assemblage et la détection des pistes forcées et SDH.
* NFO : les chemins de MediaInfo sont réduits aux noms de fichiers (une option permet de les garder).
* NVEncC : quand on accepte explicitement un RPU plus court, le contrôle final ne refuse plus la sortie.
* Aperçu NVEncC avec interpolation : le GOP est calculé sur la cadence de sortie.
* La légende de progression affiche l'étape d'encodage vidéo.
* À la fermeture, Sync Studio annule ses extractions et supprime ses fichiers temporaires.
* L'application démarre même si un dossier configuré est inaccessible.
* La recherche de mises à jour lit un flux léger (environ 1 Ko), sans passer par le quota de l'API GitHub.
* HDR Studio et Extensions peuvent être choisis comme panneau de démarrage.
* Traductions mises à jour.

---

## 4. Informations

* **Le moteur d'interpolation devient une extension** (mvo-rife). Il s'installe depuis la page Extensions, ou d'un clic sur la ligne MVO-RIFE. Il reste préinstallé dans les paquets « tout inclus », mais setup et Homebrew ne l'installent plus.
* **Les cadences cibles 59,94 et 60 i/s sont remplacées par ×2,5.** Les projets enregistrés avec ces cadences restent appliqués tels quels. L'ancien préréglage « Max » devient « Qualité ».
* **Les sous-titres EIA-608/708 sont retirés par défaut**, dans Remux (ligne décochée) comme dans Hybrid (suppression cochée). Pour les conserver, cocher la ligne dans Remux ou décocher la suppression dans Hybrid.
* **Extensions** : mvo-rife et mvo-fel existent pour Linux x86-64, Windows x86-64 et macOS Apple Silicon ; mvo-rife-trt pour Linux et Windows avec un GPU NVIDIA. La mesure de l'image dans HDR Studio et l'intégration FEL demandent mvo-fel 0.5.0 ou plus récent. Sur macOS, la reconstruction FEL tourne sur le CPU par défaut ; le GPU reste disponible en choix manuel.
* **Dépendances** : PySide6 6.12.0 est exclue sous Python < 3.12, car elle y fait planter l'application.

---
---

<a name="english-version"></a>
# English Version

*Changes since the last stable release (v4.2.2).*

---

## 1. Major features

* **HDR Studio** (new page): inspects the HDR of a video track. Open it by dropping a file, or by right-clicking a track in Container or Encode and choosing "Inspect HDR…".
  * Static metadata shows instantly; the whole film's Dolby Vision (RPU) and HDR10+ are read in a few seconds.
  * An audit flags inconsistent counts, missing or generic MDCV and MaxCLL, a MaxCLL the content contradicts, shifted HDR10+ scenes, and more.
  * **Real picture measurement**: exact MaxCLL and MaxFALL, in fast mode (keyframes, about 20 s for a film) or full mode.
  * **Luminance**: zoomable curves and a simulated TV with an adjustable peak, showing clipping and darkening. Suggested values can be sent to the Encode panel.
  * **Edit**: fixes MaxCLL/MaxFALL, the mastering display (MDCV), the RPU (L5, L6, L11, CMv4) and HDR10+ without re-encoding, and suggests a realignment based on the measurement.
  * **Compare**: two files or two tracks aligned automatically, with metadata and curve differences.
  * **FEL Analyzer** (P7 FEL sources): visible contribution of the FEL layer per category (tone, highlights, detail, grain, colour, shadows, stability) and the most affected moments. Clicking a thumbnail opens a full-resolution before/after viewer. The tool also estimates what survives encoding.
  * Report export to HTML or JSON. From the CLI: `inspect --hdr static|dynamic|measure`, `--hdr-compare`, `--hdr-report`, `--hdr-fast`.
* **Dolby Vision FEL baking** (mvo-fel extension): when encoding a P7 FEL source, the full picture (BL + EL) is reconstructed instead of keeping only the base layer. "Integrate the FEL layer into the image before encoding" option in the HDR card, unchecked by default.
  * Three precisions: Slow (recommended, bit-identical to the CPU), Standard and Fast. The GPU is chosen per track.
  * HDR10+ and MaxCLL/MaxFALL are recomputed on the reconstructed picture (HEVC output).
  * Works with FFmpeg, NVEncC and interpolation.
* **Interpolation: hybrid engine** (mvo-rife extension): RIFE is combined with motion compensation and, on NVIDIA GPUs, hardware optical flow (NVOF). A learned selector picks the best method region by region. Repeating patterns, large motion and motion blur are handled better.
  * Presets: Fast, Balanced (default, hybrid), Quality (with RIFE v4.15 fine-tuned by Muxiveo), Ultra (Quality + reverse pass) and Light.
  * New ×2.5 factor (23.976 → 59.94 fps, 24 → 60 fps).
  * GPU memory cache that speeds up Quality, Ultra and Light.
* **NVIDIA TensorRT acceleration** (optional mvo-rife-trt extension, Linux and Windows): RIFE interpolation runs through TensorRT for RTX, with automatic fallback to Vulkan. Installing it requires accepting the NVIDIA licence.
* **Extensions page**: install, update (automatically if wanted) and remove extensions. An extension that is incompatible with the machine is flagged, with the reason. From the CLI: `plugins list|install|update|remove`.
* **Modernised UI, unified execution**:
  * a single button runs the active panel's operation (Remux & Encode, Hybrid, Merge DoVi, HDR fix); an arrow gives access to the other ready operations;
  * navigation stays free while a job runs;
  * the log is shared and can be filtered by origin;
  * the sidebar is grouped into Media processing and Studios.
* **Hybrid Studio**:
  * Sync Studio with an episode selector, navigation between cuts and per-donor preview;
  * output name: template fields offered on right-click, with a live preview;
  * TMDB search from the ID field;
  * restarting a partly successful batch only processes the remaining episodes.
* **Embedded EIA-608/708 captions**: detected in the video, then kept or removed without re-encoding (HDR and Dolby Vision preserved). The option is available in Remux, in Hybrid and from the CLI (`--remove-closed-captions`).

---

## 2. Major fixes

* **TrueHD Atmos copied in Encode**: only the TrueHD core was kept and the Atmos layer was lost. The stream is now copied intact. E-AC-3 JOC is also preserved in physical sync.
* **Sound dropping out after a seek**: with interpolation, NVEncC or HDR injection, a source that contained audio and sparse subtitles could produce a file whose sound disappeared after seeking.
* **Sync when encoding with the native muxer**: corrections (trimmed or padded audio, retimed subtitles) were not applied. They now are.
* **AC-3 / E-AC-3 in physical sync**: the audio encoder delay is preserved.

---

## 3. Minor

* Native Matroska muxing is 27 to 46% faster, with memory capped at about 70 MiB.
* Dolby Vision / HDR10+ detection before an encode now takes about 2 s instead of 15 s.
* Hybrid: decision profiles apply after assembly and after forced and SDH track detection.
* NFO: MediaInfo paths are reduced to file names (an option keeps them).
* NVEncC: when a shorter RPU is explicitly accepted, the final check no longer refuses the output.
* NVEncC preview with interpolation: the GOP is computed from the output frame rate.
* The progress caption shows the video encoding step.
* When closed, Sync Studio cancels its extractions and removes its temporary files.
* The application starts even when a configured folder is unreachable.
* The update check reads a lightweight feed (about 1 KB), without using the GitHub API quota.
* HDR Studio and Extensions can be chosen as the startup panel.
* Translations updated.

---

## 4. Information

* **The interpolation engine becomes an extension** (mvo-rife). Install it from the Extensions page, or with one click on the MVO-RIFE line. It is still preinstalled in all-inclusive packages, but setup and Homebrew no longer install it.
* **The 59.94 and 60 fps target rates are replaced by ×2.5.** Saved projects that use them still apply them as before. The former "Max" preset becomes "Quality".
* **EIA-608/708 captions are removed by default**, in Remux (line unchecked) and in Hybrid (removal checked). To keep them, check the line in Remux or uncheck the removal in Hybrid.
* **Extensions**: mvo-rife and mvo-fel are available for Linux x86-64, Windows x86-64 and macOS Apple Silicon; mvo-rife-trt for Linux and Windows with an NVIDIA GPU. Picture measurement in HDR Studio and FEL baking require mvo-fel 0.5.0 or later. On macOS, FEL reconstruction runs on the CPU by default; the GPU remains available as a manual choice.
* **Dependencies**: PySide6 6.12.0 is excluded on Python < 3.12, where it makes the application crash.
