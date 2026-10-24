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
* **Synchronisation : références en chaîne.** Une mesure (automatique, proposition validée ou Synchro Studio) se fait toujours face à l'audio d'origine de la source de référence. Si cette référence est elle-même synchronisée, la source mesurée suit sa position dans la sortie, coupures et cadence comprises. Toute modification de la référence recalcule les sources qui la suivent : nouvelle mesure, Synchro Studio, annulation, décalage saisi à la main ou par un profil. Un décalage saisi à la main détache sa source de sa référence. Si la référence avait été synchronisée à partir de la source mesurée, sa synchro est retirée et la nouvelle mesure prime. Le Synchro Studio montre la cible face à l'audio d'origine de la référence ; validé sans modification, il ne change rien. Les liens entre sources sont conservés dans les workflows sauvegardés.
* **Synchronisation : introduction propre à chaque piste.** Des logos de distributeurs différents en début de piste ne bloquent plus l'analyse. L'introduction est admise si un silence d'au moins 0,5 s la sépare du film dans les deux pistes. Le décalage du premier passage concordant est conservé, et le journal l'indique.
* **Profils : modèle de nom de sortie.** Un profil décisionnel peut porter un modèle de nom de fichier (`output_template`, même syntaxe que `--output-template`).
  * Éditeur de profils : champ « Nom de sortie », clic droit pour insérer un champ, aperçu rendu sur la source chargée.
  * Panneau Conteneur : le nom est calculé quand le profil est appliqué et figure dans l'aperçu de confirmation. Il suit ensuite les pistes et la fiche TMDB, tant que le chemin de sortie n'est pas modifié à la main. Réappliquer le profil garde le dossier de sortie : un modèle à sous-dossiers ne s'imbrique plus.
  * Hybrid Studio et la CLI reprennent aussi ce modèle. Priorité en CLI : `-o fichier` > `--output-template` > modèle du profil. `-o dossier/` donne le dossier du nom calculé, et `batch --profile` le place dans `--output-dir`. Détails dans `Readme_Profiles.md`.
* **Hybrid Studio : analyse de saison bien plus rapide.**
  * Sous-titres forcés et SDH : le texte des pistes est lu grâce à l'index du MKV quand celui-ci référence chaque réplique (contrôle par les statistiques du fichier). Sinon, toutes les pistes d'un fichier sont extraites en une seule lecture, au lieu d'une lecture complète par piste.
  * Quatre épisodes sont préparés en même temps, deux pour la synchro audio.
  * Les inspections, les sous-titres et les réponses TMDB sont réutilisés tant que les fichiers ne changent pas ; un fichier remplacé est relu. La vérification audio et une nouvelle analyse ne relisent donc que l'audio.
  * Le journal donne la durée de préparation de chaque épisode, étape par étape.
* **Hybrid Studio : modèle d'assemblage détaillé.** Le tableau de l'épisode témoin reprend les colonnes du panneau Conteneur : codec, langue, titre et infos de la piste (définition, HDR / Dolby Vision, cadence, canaux, débit, Atmos / DTS:X, nombre de sous-titres, flags défaut / forcé / malentendant / malvoyant…). La langue et le titre se modifient par double-clic et s'appliquent à toute la saison, avant le profil. « Afficher uniquement les sélectionnées » filtre le tableau.
* **Hybrid Studio : plusieurs donneurs.** Tous les donneurs d'un épisode apparaissent dans le plan, avec leur liste de pistes et leur synchro. La liste des donneurs de l'épisode les montre ensemble et permet d'en garder un seul (« Garder uniquement … »). La carte « Recalage et pré-écoute » est repliée par défaut.
* **Interpolation : cache des caractéristiques toujours actif.** L'interpolation en Vulkan est plus rapide avec les préréglages Qualité, Ultra et Light, et les images restent identiques. La case « Cache des caractéristiques » disparaît du panneau Encodage : le cache sert dès que muxiveo-rife le prend en charge. Il se désactive quand la mémoire GPU disponible ne laisse pas assez de réserve. Il n'a pas d'effet avec TensorRT ni avec RIFE v4.6.

---

## 2. Correctifs

* **Workflows sauvegardés** : le rang d'une piste est compté dans l'ordre de la source. Avant, après un réordonnancement des pistes ou l'ajout d'une variante audio, le rechargement pouvait désigner une autre piste. Le panneau Conteneur désigne désormais les pistes par leur identité dans la source. Les workflows déjà enregistrés restent lisibles.
* **Hybrid Studio, plusieurs donneurs** : choisir une entrée dans la liste ne retire plus les autres donneurs. Une piste cochée mais écartée par le profil est signalée au journal.
* **Hybrid Studio, épisode témoin** : une inspection plus ancienne ne remplace plus la plus récente.
* **Détection des sous-titres SDH** : elle fonctionne même sans piste audio active. Les forcés, faute de langue de référence, ne sont alors pas détectés.
* **Détection des sous-titres forcés / SDH** : sur un très gros fichier, l'extraction n'échoue plus au bout de 120 s ; le délai suit la taille du fichier.
* **CLI `hybrid`** : avec `--output-template`, `{season_episode}`, `{season}` et `{episode}` sont remplis sans TMDB, comme dans Hybrid Studio, et `--output-all` est pris en compte.
* **CLI, `--profile`** : une calibration avec conversion de cadence passe en mode physique, comme sans profil ; la correction de vitesse est conservée.

---
---

<a name="english-version"></a>
# English Version

*Changes since v5.0.3.*

---

## 1. New

* **Audio sync: consistent sync offered for confirmation.** Sometimes the continuous analysis only stumbles on zones without an acoustic match, with the same offset on both sides: a passage longer than 30 s (a scene dubbed differently, for example) or an active start without a match. The sync is then offered instead of being rejected. In the Container panel, the "Sync to confirm" window offers three choices: "Review in Sync Studio", "Apply" or "Cancel". Sync Studio opens on the first zone, with one button per zone; finish with "Confirm track sync". The confirmation is written to the log. The "Quick sync" and "In-depth sync" buttons of the studio also show these zones. Hybrid Studio accepts the sync and lists the zones in the log. In the CLI (`--detect-cuts`), it is accepted with a warning listing the zones, and `--reject-coherent-sync` restores the rejection. An offset change that cannot be verified is still rejected. When the audio layout is not recognized (6.0…), the proposal based on the mono downmix is still offered for confirmation.
* **Sync: chained references.** A measurement (automatic, confirmed proposal or Sync Studio) is always made against the original audio of the reference source. If that reference is itself synchronized, the measured source follows its position in the output, cuts and frame rate included. Any change to the reference recalculates the sources that follow it: new measurement, Sync Studio, cancellation, offset entered manually or by a profile. An offset entered manually detaches its source from its reference. If the reference had been synchronized from the measured source, its sync is removed and the new measurement prevails. Sync Studio shows the target against the original audio of the reference; confirmed without changes, it changes nothing. Links between sources are kept in saved workflows.
* **Sync: intro specific to each track.** Different distributor logos at the start of a track no longer block the analysis. The intro is accepted if a silence of at least 0.5 s separates it from the film in both tracks. The offset of the first matching passage is kept, and the log says so.
* **Profiles: output name template.** A decision profile can carry a file name template (`output_template`, same syntax as `--output-template`).
  * Profile editor: "Output name" field, right-click to insert a field, preview rendered on the loaded source.
  * Container panel: the name is computed when the profile is applied and shown in the confirmation preview. It then follows the tracks and the TMDB entry, as long as the output path is not edited by hand. Applying the profile again keeps the output folder: a template with subfolders no longer nests.
  * Hybrid Studio and the CLI also use this template. CLI priority: `-o file` > `--output-template` > profile template. `-o folder/` gives the folder of the computed name, and `batch --profile` places it in `--output-dir`. Details in `Readme_Profiles.md`.
* **Hybrid Studio: much faster season analysis.**
  * Forced and SDH subtitles: the track text is read through the MKV index when it references every line (checked against the file statistics). Otherwise, all tracks of a file are extracted in a single read, instead of a full read per track.
  * Four episodes are prepared at the same time, two for audio sync.
  * Inspections, subtitles and TMDB responses are reused as long as the files do not change; a replaced file is read again. The audio check and a new analysis therefore only read the audio again.
  * The log gives the preparation time of each episode, step by step.
* **Hybrid Studio: detailed assembly template.** The reference episode table uses the Container panel columns: codec, language, title and track info (resolution, HDR / Dolby Vision, frame rate, channels, bitrate, Atmos / DTS:X, subtitle count, default / forced / hearing impaired / visual impaired flags…). Language and title are edited by double-click and apply to the whole season, before the profile. "Show selected only" filters the table.
* **Hybrid Studio: multiple donors.** All the donors of an episode appear in the plan, with their track list and sync. The episode donor list shows them together and lets you keep only one ("Keep only …"). The "Alignment and audio preview" card is collapsed by default.
* **Interpolation: feature cache always on.** Vulkan interpolation is faster with the Quality, Ultra and Light presets, and frames stay identical. The "Feature cache" checkbox is removed from the Encoding panel: the cache is used whenever muxiveo-rife supports it. It turns off when available GPU memory does not leave enough reserve. It has no effect with TensorRT or RIFE v4.6.

---

## 2. Fixes

* **Saved workflows**: a track's rank is counted in source order. Before, after reordering tracks or adding an audio variant, reloading could point to another track. The Container panel now designates tracks by their identity in the source. Workflows already saved can still be read.
* **Hybrid Studio, multiple donors**: choosing an entry in the list no longer removes the other donors. A checked track that the profile leaves out is reported in the log.
* **Hybrid Studio, reference episode**: an older inspection no longer replaces the most recent one.
* **SDH subtitle detection**: it works even without an active audio track. Forced subtitles, lacking a reference language, are not detected in that case.
* **Forced / SDH subtitle detection**: on a very large file, extraction no longer fails after 120 s; the timeout follows the file size.
* **CLI `hybrid`**: with `--output-template`, `{season_episode}`, `{season}` and `{episode}` are filled in without TMDB, as in Hybrid Studio, and `--output-all` is taken into account.
* **CLI, `--profile`**: a calibration with frame rate conversion switches to physical mode, as without a profile; the speed correction is kept.
