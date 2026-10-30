# Notes de version / Release Notes — Muxiveo v5.1.0

* [Français](#version-française)
* [English](#english-version)

---

<a name="version-française"></a>
# Version Française

*Modifications depuis la version v5.0.3.*

---

## 1. Nouveautés

* **Synchronisation audio : synchro cohérente proposée à la validation.** Parfois l'analyse continue ne bute que sur des zones sans correspondance acoustique, avec le même décalage de part et d'autre : passage de plus de 30 s (scène doublée différemment, par exemple) ou début actif sans correspondance. La synchro est alors proposée au lieu d'être refusée. Dans le panneau Conteneur, la fenêtre « Synchro à valider » offre trois choix : « Relire dans le Synchro Studio », « Appliquer » ou « Annuler ». Le Synchro Studio s'ouvre sur la première zone, avec un bouton par zone ; on termine avec « Valider la synchro des pistes ». La validation est notée au journal. Les boutons « Synchro rapide » et « Synchro approfondie » du studio affichent aussi ces zones. Hybrid Studio accepte la synchro et liste les zones au journal. Dans la CLI (`--detect-cuts`), elle est acceptée avec un avertissement qui liste les zones, et `--reject-coherent-sync` rétablit le refus. Un changement de décalage impossible à vérifier reste refusé. Si la disposition audio n'est pas reconnue (6.0…), la proposition établie sur le mixage mono reste soumise à validation.
* **Synchronisation : conversion de cadence (PAL 25 ↔ 23,976).** Le Synchro Studio propose la conversion (case « Conversion Cadence ») et retient un éventuel refus pour chaque source. En méthode « auto », l'analyse de la hauteur du son choisit automatiquement entre `asetrate` et `atempo`. Un avertissement s'affiche lorsqu'on applique un décalage seul alors que les cadences diffèrent. Dans Hybrid Studio, le décalage manuel suit la conversion.
* **Synchronisation : référence de n'importe quelle disposition.** Une piste stéréo peut se synchroniser sur une référence multicanal, et inversement. L'analyse acoustique s'effectue sur la piste de même langue que la référence.
* **Synchronisation propre à une piste.** Un sous-titre réglé « sans recalage miroir » peut recevoir ses propres coupures ou sa propre cadence, sans toucher à l'audio de sa source. Dans le plan JSON, cela correspond au champ `tracks[].sync_calibration`.
* **Synchronisation : paliers imperceptibles.** Lorsqu'une jonction ne peut pas être vérifiée, Muxiveo applique un décalage constant au lieu de refuser, si tous les écarts observés restent sous les seuils de perception humaine (son en avance ≤ 45 ms, en retard ≤ 125 ms).
* **Synchronisation : analyse continue accélérée.** Les deux pistes sont décodées en parallèle avec une seule lecture par fichier. Pour un flux DTS 5.1 à 48 kHz ou moins, seul le cœur (DTS core) est décodé.
* **Synchronisation : références en chaîne.** Une mesure (automatique, proposition validée ou Synchro Studio) se fait toujours face à l'audio d'origine de la source de référence. Si cette référence est elle-même synchronisée, la source mesurée suit sa position dans la sortie, coupures, cadence et hauteur comprises. Toute modification de la référence recalcule les sources qui la suivent : nouvelle mesure, Synchro Studio, annulation, décalage saisi à la main ou par un profil. Un décalage saisi à la main détache sa source de sa référence. Si la référence avait été synchronisée à partir de la source mesurée, sa synchro est retirée et la nouvelle mesure prime. Le Synchro Studio montre la cible face à l'audio d'origine de la référence ; validé sans modification, il ne change rien. Les liens entre sources sont conservés dans les workflows sauvegardés.
* **Synchronisation : introduction propre à chaque piste.** Des logos de distributeurs différents en début de piste ne bloquent plus l'analyse. L'introduction est admise si un silence d'au moins 0,5 s la sépare du film dans les deux pistes. Le décalage du premier passage concordant est conservé, et le journal l'indique.
* **Profils : modèle de nom de sortie.** Un profil décisionnel peut porter un modèle de nom de fichier (`output_template`, même syntaxe que `--output-template`).
  * Éditeur de profils : champ « Nom de sortie », clic droit pour insérer un champ, aperçu rendu sur la source chargée.
  * Panneau Conteneur : le nom est calculé quand le profil est appliqué et figure dans l'aperçu de confirmation. Il suit ensuite les pistes et la fiche TMDB, tant que le chemin de sortie n'est pas modifié à la main. Réappliquer le profil garde le dossier de sortie : un modèle à sous-dossiers ne s'imbrique plus.
  * Hybrid Studio et la CLI reprennent aussi ce modèle. Priorité en CLI : `-o fichier` > `--output-template` > modèle du profil. `-o dossier/` donne le dossier du nom calculé, et `batch --profile` le place dans `--output-dir`. Détails dans `Readme_Profiles.md`.
* **Hybrid Studio : analyse de saison bien plus rapide et robuste.**
  * Sous-titres forcés et SDH : le texte des pistes est lu grâce à l'index du MKV quand celui-ci référence chaque réplique (contrôle par les statistiques du fichier). Sinon, toutes les pistes d'un fichier sont extraites en une seule lecture, au lieu d'une lecture complète par piste.
  * Quatre épisodes sont préparés en même temps, deux pour la synchro audio.
  * Les inspections, les sous-titres et les réponses TMDB sont réutilisés tant que les fichiers ne changent pas ; un fichier remplacé est relu. La vérification audio et une nouvelle analyse ne relisent donc que l'audio.
  * « Vérifier la synchronisation » demande confirmation avant de remplacer une stratégie manuelle.
  * Un élément dont les réglages ont changé doit être réanalysé pour éviter tout plan périmé.
  * Le journal donne la durée de préparation de chaque épisode, étape par étape.
* **Hybrid Studio : modèle d'assemblage détaillé.** Le tableau de l'épisode témoin reprend les colonnes du panneau Conteneur : codec, langue, titre et infos de la piste (définition, HDR / Dolby Vision, cadence, canaux, débit, Atmos / DTS:X, nombre de sous-titres, flags défaut / forcé / malentendant / malvoyant…). La langue et le titre se modifient par double-clic et s'appliquent à toute la saison, avant le profil. « Afficher uniquement les sélectionnées » filtre le tableau.
* **Hybrid Studio : plusieurs donneurs.** Tous les donneurs d'un épisode apparaissent dans le plan, avec leur liste de pistes et leur synchro. La liste des donneurs de l'épisode les montre ensemble et permet d'en garder un seul (« Garder uniquement … »). La carte « Recalage et pré-écoute » est repliée par défaut.
* **Interpolation : cache des caractéristiques toujours actif.** L'interpolation en Vulkan est plus rapide avec les préréglages Qualité, Ultra et Light, et les images restent identiques. La case « Cache des caractéristiques » disparaît du panneau Encodage : le cache sert dès que muxiveo-rife le prend en charge. Il se désactive quand la mémoire GPU disponible ne laisse pas assez de réserve. Il n'a pas d'effet avec TensorRT ni avec RIFE v4.6.
* **Interpolation : préréglage Qualité par défaut.** Une nouvelle interpolation démarre sur Qualité (moteur hybride + RIFE v4.15 affiné par Muxiveo) au lieu d'Équilibré. Les réglages déjà enregistrés ne changent pas.
* **Mises à jour : fenêtre de nouvelle version.** Au démarrage, une nouvelle version s'annonce dans une fenêtre, une fois par version et par session ; si une opération ou un dialogue est en cours, la fenêtre attend sa fin. Les notes de version se lisent dans un volet replié. La case « Ne plus afficher de popup de mise à jour » la désactive (aussi dans Paramètres → Interface) : la nouvelle version est alors seulement signalée en bas du menu latéral.
* **Mises à jour : installation intégrée pour les archives source et l'application macOS.** En plus de l'installeur Windows et de l'AppImage, l'archive source (à partir de celles de la 5.1.0) et l'application macOS se mettent à jour depuis Muxiveo. La nouvelle version est essayée avant d'être mise en place ; `config.ini` et les fichiers ajoutés au dossier sont conservés. En cas d'échec, l'ancienne version est rétablie et relancée, et le résultat s'affiche au démarrage suivant. Sous macOS, l'application doit se trouver dans Applications ; un dépôt git n'est jamais remplacé (`git pull`). Quand l'installation intégrée n'est pas possible (Flatpak, Homebrew, dossier non modifiable…), le bouton « Installer » est grisé et son info-bulle explique comment mettre à jour.
* **Extensions : point vert.** Un point vert sur « Extensions » dans le menu latéral (pastille sur l'icône en mode compact) signale une mise à jour d'extension à lancer.
* **Licence : GPL-3.0.** Muxiveo passe de la licence MIT à la GNU General Public License v3.0 ou ultérieure (`GPL-3.0-or-later`). Le nom, le logo et les éléments de marque Muxiveo restent la propriété exclusive d'AOTR.

---

## 2. Correctifs

* **Workflows sauvegardés** : le rang d'une piste est compté dans l'ordre de la source. Avant, après un réordonnancement des pistes ou l'ajout d'une variante audio, le rechargement pouvait désigner une autre piste. Le panneau Conteneur désigne désormais les pistes par leur identité dans la source. L'ordre et les décalages de synchro sont fidèlement restaurés, et les méthodes de cadence « auto » non tranchées sont réanalysées en arrière-plan au chargement. Les workflows déjà enregistrés restent lisibles.
* **Synchronisation, méthode atempo** : la piste réécrite pouvait accuser un léger décalage car le silence initial n'était pas comptabilisé. L'écart propre à atempo est désormais compensé.
* **Synchronisation et sous-titres** : un changement de hauteur seul ne modifie plus le calage des sous-titres. Dans Hybrid Studio, une conversion de cadence ou de hauteur impose la réécriture physique.
* **Mise à jour par l'installeur Windows** : l'invite UAC reste visible au premier plan et ne bloque plus. L'installeur est lancé hors de l'objet job du terminal.
* **Synchro Studio & pré-écoute** : la pré-écoute n'écrit plus de sorties FFmpeg parasites dans la console.
* **Balise « Writing application »** : la balise des fichiers MKV pointe désormais vers le site web officiel `https://muxiveo.fr/`.
* **Hybrid Studio, plusieurs donneurs** : choisir une entrée dans la liste ne retire plus les autres donneurs. Une piste cochée mais écartée par le profil est signalée au journal.
* **Hybrid Studio, épisode témoin** : une inspection plus ancienne ne remplace plus la plus récente.
* **Détection des sous-titres SDH** : elle fonctionne même sans piste audio active. Les forcés, faute de langue de référence, ne sont alors pas détectés.
* **Détection des sous-titres forcés / SDH** : sur un très gros fichier, l'extraction n'échoue plus au bout de 120 s ; le délai suit la taille du fichier.
* **CLI `hybrid`** : avec `--output-template`, `{season_episode}`, `{season}` et `{episode}` sont remplis sans TMDB, comme dans Hybrid Studio, et `--output-all` est pris en compte.
* **CLI, `--profile`** : une calibration avec conversion de cadence passe en mode physique, comme sans profil ; la correction de vitesse est conservée.
* **Encodage x265 sur les processeurs à 22 cœurs logiques ou plus** : l'encodeur refusait de s'ouvrir (code 183) et tout encodage x265 échouait. Le nombre de threads transmis à x265 est limité à 16, son maximum.
* **Copie Dolby Vision avec redimensionnement (x265, QSV, VAAPI, AMF)** : la zone active (L5) du RPU suit désormais les dimensions de sortie. Avant, elle restait exprimée en pixels de la source.
* **Interpolation après désentrelacement Bob** : sur une source à cadence variable, une image sur deux était perdue. Le flux remis à RIFE est désormais régularisé une seule fois, au double de la cadence ; la durée de la sortie est juste.

---
---

<a name="english-version"></a>
# English Version

*Changes since v5.0.3.*

---

## 1. New

* **Audio sync: consistent sync offered for confirmation.** Sometimes the continuous analysis only stumbles on zones without an acoustic match, with the same offset on both sides: a passage longer than 30 s (a scene dubbed differently, for example) or an active start without a match. The sync is then offered instead of being rejected. In the Container panel, the "Sync to confirm" window offers three choices: "Review in Sync Studio", "Apply" or "Cancel". Sync Studio opens on the first zone, with one button per zone; finish with "Confirm track sync". The confirmation is written to the log. The "Quick sync" and "In-depth sync" buttons of the studio also show these zones. Hybrid Studio accepts the sync and lists the zones in the log. In the CLI (`--detect-cuts`), it is accepted with a warning listing the zones, and `--reject-coherent-sync` restores the rejection. An offset change that cannot be verified is still rejected. When the audio layout is not recognized (6.0…), the proposal based on the mono downmix is still offered for confirmation.
* **Sync: frame rate conversion (PAL 25 ↔ 23.976).** Sync Studio offers frame rate conversion ("Cadence Conversion" checkbox) and remembers declined conversions per source. In "auto" method, pitch analysis automatically selects between `asetrate` and `atempo`. A warning is displayed when applying an offset alone while frame rates differ. In Hybrid Studio, manual offsets track the conversion.
* **Sync: reference in any audio layout.** A stereo track can synchronize against a multichannel reference, and vice versa. Acoustic analysis is performed on the donor track matching the reference language.
* **Track-specific synchronization.** A subtitle configured without mirror sync can receive its own cuts or cadence adjustments without affecting the audio of its source. In the JSON plan, this is mapped to the `tracks[].sync_calibration` field.
* **Sync: imperceptible drift thresholds.** When a junction cannot be verified acoustically, Muxiveo applies a constant offset instead of rejecting the sync, provided all discrepancies remain within human perception thresholds (audio advance ≤ 45 ms, delay ≤ 125 ms).
* **Sync: faster continuous analysis.** Both tracks are decoded concurrently in a single read pass per file. For DTS 5.1 at 48 kHz or below, only the core is decoded.
* **Sync: chained references.** A measurement (automatic, confirmed proposal or Sync Studio) is always made against the original audio of the reference source. If that reference is itself synchronized, the measured source follows its position in the output, cuts, frame rate, and pitch included. Any change to the reference recalculates the sources that follow it: new measurement, Sync Studio, cancellation, offset entered manually or by a profile. An offset entered manually detaches its source from its reference. If the reference had been synchronized from the measured source, its sync is removed and the new measurement prevails. Sync Studio shows the target against the original audio of the reference; confirmed without changes, it changes nothing. Links between sources are kept in saved workflows.
* **Sync: intro specific to each track.** Different distributor logos at the start of a track no longer block the analysis. The intro is accepted if a silence of at least 0.5 s separates it from the film in both tracks. The offset of the first matching passage is kept, and the log says so.
* **Profiles: output name template.** A decision profile can carry a file name template (`output_template`, same syntax as `--output-template`).
  * Profile editor: "Output name" field, right-click to insert a field, preview rendered on the loaded source.
  * Container panel: the name is computed when the profile is applied and shown in the confirmation preview. It then follows the tracks and the TMDB entry, as long as the output path is not edited by hand. Applying the profile again keeps the output folder: a template with subfolders no longer nests.
  * Hybrid Studio and the CLI also use this template. CLI priority: `-o file` > `--output-template` > profile template. `-o folder/` gives the folder of the computed name, and `batch --profile` places it in `--output-dir`. Details in `Readme_Profiles.md`.
* **Hybrid Studio: much faster and more robust season analysis.**
  * Forced and SDH subtitles: the track text is read through the MKV index when it references every line (checked against the file statistics). Otherwise, all tracks of a file are extracted in a single read, instead of a full read per track.
  * Four episodes are prepared at the same time, two for audio sync.
  * Inspections, subtitles and TMDB responses are reused as long as the files do not change; a replaced file is read again. The audio check and a new analysis therefore only read the audio again.
  * "Verify sync" prompts for confirmation before overwriting a manual strategy.
  * An episode whose settings have changed must be rescanned to prevent stale plans.
  * The log gives the preparation time of each episode, step by step.
* **Hybrid Studio: detailed assembly template.** The reference episode table uses the Container panel columns: codec, language, title and track info (resolution, HDR / Dolby Vision, frame rate, channels, bitrate, Atmos / DTS:X, subtitle count, default / forced / hearing impaired / visual impaired flags…). Language and title are edited by double-click and apply to the whole season, before the profile. "Show selected only" filters the table.
* **Hybrid Studio: multiple donors.** All the donors of an episode appear in the plan, with their track list and sync. The episode donor list shows them together and lets you keep only one ("Keep only …"). The "Alignment and audio preview" card is collapsed by default.
* **Interpolation: feature cache always on.** Vulkan interpolation is faster with the Quality, Ultra and Light presets, and frames stay identical. The "Feature cache" checkbox is removed from the Encoding panel: the cache is used whenever muxiveo-rife supports it. It turns off when available GPU memory does not leave enough reserve. It has no effect with TensorRT or RIFE v4.6.
* **Interpolation: Quality preset by default.** A new interpolation starts on Quality (hybrid engine + RIFE v4.15 fine-tuned by Muxiveo) instead of Balanced. Settings already saved do not change.
* **Updates: new version window.** At startup, a new version is announced in a window, once per version and per session; if an operation or a dialog is in progress, the window waits until it ends. Release notes can be read in a collapsed pane. The "Don't show update popups again" checkbox turns it off (also in Settings → Interface): the new version is then only shown at the bottom of the sidebar.
* **Updates: built-in install for source archives and the macOS app.** In addition to the Windows installer and the AppImage, the source archive (from the 5.1.0 archives onwards) and the macOS app update from within Muxiveo. The new version is tried before it is put in place; `config.ini` and files added to the folder are kept. If anything fails, the previous version is restored and restarted, and the result is shown at the next startup. On macOS, the app must be in Applications; a git repository is never replaced (`git pull`). When the built-in install is not possible (Flatpak, Homebrew, read-only folder…), the "Install" button is greyed out and its tooltip explains how to update.
* **Extensions: green dot.** A green dot on "Extensions" in the sidebar (a badge on the icon in compact mode) signals an extension update waiting to be started.
* **License: GPL-3.0.** Muxiveo moves from the MIT license to the GNU General Public License v3.0 or later (`GPL-3.0-or-later`). The Muxiveo name, logo and brand assets remain the exclusive property of AOTR.

---

## 2. Fixes

* **Saved workflows**: a track's rank is counted in source order. Before, after reordering tracks or adding an audio variant, reloading could point to another track. The Container panel now designates tracks by their identity in the source. Track order and sync links are reliably restored, and undecided "auto" cadence methods are reanalyzed in the background on load. Workflows already saved can still be read.
* **Sync, `atempo` method**: the rewritten track could be slightly offset because initial silence was uncounted. The inherent `atempo` delay is now compensated.
* **Sync and subtitles**: a pitch change alone no longer alters subtitle timing. In Hybrid Studio, a frame rate or pitch conversion strictly enforces physical rewriting.
* **Windows installer update**: the UAC elevation prompt remains visible in the foreground without blocking. The installer process is launched outside the terminal job object.
* **Sync Studio & audio preview**: audio preview playback no longer outputs spurious FFmpeg lines to the console.
* **"Writing application" tag**: the Matroska tag now points to the official website `https://muxiveo.fr/`.
* **Hybrid Studio, multiple donors**: choosing an entry in the list no longer removes the other donors. A checked track that the profile leaves out is reported in the log.
* **Hybrid Studio, reference episode**: an older inspection no longer replaces the most recent one.
* **SDH subtitle detection**: it works even without an active audio track. Forced subtitles, lacking a reference language, are not detected in that case.
* **Forced / SDH subtitle detection**: on a very large file, extraction no longer fails after 120 s; the timeout follows the file size.
* **CLI `hybrid`**: with `--output-template`, `{season_episode}`, `{season}` and `{episode}` are filled in without TMDB, as in Hybrid Studio, and `--output-all` is taken into account.
* **CLI, `--profile`**: a calibration with frame rate conversion switches to physical mode, as without a profile; the speed correction is kept.
* **x265 encoding on processors with 22 or more logical cores**: the encoder refused to open (code 183) and every x265 encode failed. The thread count passed to x265 is capped at 16, its maximum.
* **Dolby Vision copy with resizing (x265, QSV, VAAPI, AMF)**: the RPU active area (L5) now follows the output dimensions. Before, it stayed in source pixels.
* **Interpolation after Bob deinterlacing**: on a variable frame rate source, one frame out of two was lost. The stream handed to RIFE is now regularized only once, at twice the frame rate; the output duration is correct.
