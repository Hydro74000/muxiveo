# Notes de version / Release Notes — Muxiveo v5.0.3

* [Français](#version-française)
* [English](#english-version)

---

<a name="version-française"></a>
# Version Française

*Modifications depuis la version v5.0.2.*

---

## 1. Nouveautés

* **Synchronisation audio : vérification rapide par défaut.** Si les six mesures ponctuelles donnent le même décalage avec une confiance d'au moins 0,90, l'analyse continue des pistes n'est pas lancée. Si le décalage varie ou si la confiance est plus faible, elle se lance d'elle-même. Réglage « Vérification rapide de la synchronisation audio » dans Paramètres ; en CLI, `--quick-sync-check` / `--no-quick-sync-check` (avec `--detect-cuts`). Seule l'analyse approfondie voit un écart qui se compense entre deux mesures : clic droit sur une piste non synchronisée → « Synchronisation approfondie… », ou bouton « Synchro approfondie » du Synchro Studio.
* **Analyse continue plus rapide** : les deux pistes sont décodées en parallèle.
* **Synchro Studio : coupes et silences manuels.** Un clic droit sur la forme d'onde crée une section. On l'ajuste en glissant ses bords ou avec les champs Début / Fin (hh:mm:ss.mmm). « Couper (supprimer) » retire le contenu de la cible ; « Allonger (ajout de silence) » insère un silence de la même durée et repousse la suite. Une coupure se retire par clic droit, sur la forme d'onde ou dans le tableau des coupures. La cible est dessinée telle qu'elle sortira (morceaux décalés, coupes, silences, cadence) ; pour une piste de sous-titres, le texte des répliques est affiché.
* **Synchro Studio : boutons « Synchro rapide » et « Synchro approfondie »** pour une piste audio. La référence de la dernière synchro est reprise.
* **Conteneur : réinitialisation sur les sources.** Le bouton ↺ à côté de « Appliquer profil » remet les pistes (langue, titre, flags, sélection, ordre), les variantes audio, la synchronisation et les chapitres dans l'état des fichiers sources, sans les réinspecter. Pièces jointes, balises, titre et fichier de sortie sont conservés, ainsi que les réglages du panneau Encodage des pistes d'origine.
* **Conteneur : case « tout cocher » dans l'en-tête** de la colonne de sélection, à la place des boutons « Tout activer » / « Tout désactiver ». Elle est grisée quand seules certaines pistes sont cochées.
* **Décalage négligeable** : un décalage constant de 5 ms ou moins n'est plus appliqué, la piste n'est donc pas réencodée. Le journal l'indique.
* **Hybrid Studio** : les épisodes nommés « S01.E02 », « S01_E02 » ou « 1x02 » sont reconnus.
* **Hybrid Studio : analyse de saison en deux temps.** « Analyser la saison » prépare d'abord les appariements et les pistes de tous les épisodes, sans synchro audio, puis propose la vérification de concordance audio. Le bouton « Vérifier la synchronisation audio » la lance plus tard sur toutes les paires complètes du tableau. Le lancement et la sauvegarde des workflows vérifient d'abord les paires restantes.
* **Langues** : « VFQ » dans le titre d'une piste donne fr-CA. Les profils et sélecteurs enregistrés en fr-FR continuent de s'appliquer à ces pistes.
* **Documentation** : README refondu, désormais aussi en anglais (`README_EN.md`).

---

## 2. Correctifs

* **Profils appliqués dans le panneau Conteneur** : le profil part de l'état du tableau (langues, titres et flags édités, ou déjà modifiés par un profil précédent), y compris pour les critères de titre et de langue source, qui lisaient jusqu'ici le fichier d'origine. L'aperçu de l'éditeur de profils suit la même règle.
* **Chapitres** : après une édition, revenir aux chapitres d'une source (changement de source de chapitres, réinitialisation) redonnait les valeurs éditées au lieu de celles du fichier.
* **Synchronisation, méthode `asetrate`** : la vitesse obtenue était fausse sur une piste qui n'est pas en 48 kHz (44,1 kHz, 96 kHz…). Le débit réel de la piste est désormais utilisé.
* **Synchronisation, cadence** : dans le panneau Conteneur, l'écart de cadence vidéo entre les sources (PAL ↔ cinéma) est transmis à l'analyse, selon le réglage « Appliquer automatiquement la conversion PAL ↔ Cinéma ». Un écart de plus de 5 % (télécinéma 29,97 ↔ 23,976, images doublées 50 ↔ 25) n'est plus pris pour un changement de vitesse de l'audio.
* **Synchronisation avec coupures ou conversion de cadence** : la calibration est conservée pour toute la source. L'annuler sur une piste l'annule pour toutes les pistes de la source, et le journal les liste. Les chapitres de la source suivent la calibration complète, pas seulement le décalage de départ ; un chapitre situé dans une partie coupée est placé à la reprise.
* **Synchro Studio** : une coupe qui commence dans un silence conserve le reste du silence. Une saisie invalide efface la section et bloque l'application. « Synchro rapide » et « Synchro approfondie » transmettent la cadence et la méthode audio.
* **Hybrid Studio, appariement des épisodes** : « S01E02-1080p » n'est plus compté comme deux épisodes. « S01E01E02v2 », « S01E01-E02REPACK » ou « 1x01-02v2 » sont refusés comme ambigus, au lieu d'être appariés au seul premier épisode.
* **Hybrid Studio, saison** : un épisode en échec n'interrompt plus l'analyse ; il est marqué « Échec » (cause en info-bulle et dans le journal) et les suivants sont traités. Le lancement et la sauvegarde des workflows restent refusés tant qu'une paire complète n'a pas de plan. La sauvegarde des workflows ne recalcule plus les épisodes déjà synchronisés (réglages du Sync Studio conservés).
* **Estimation HDR10 (sources Dolby Vision P5)** : l'analyse ne se bloque plus quand l'outil écrit beaucoup de messages. L'annulation est effective, et la fin du journal de l'outil est rapportée en cas d'échec.
* **HDR Studio** : si la publication du fichier corrigé est refusée (destination apparue ou modifiée entre-temps), le fichier corrigé est conservé à côté de la sortie ; le message d'erreur donne son chemin.
* **TMDB** : un nombre du titre (« Blade Runner 2049 », « 1917 », « 2001 A Space Odyssey ») n'est plus pris pour l'année de sortie.
* **AppImage** : une case cochée et grisée (copie HDR10 / Dolby Vision / HDR10+) paraissait décochée, car les icônes manquaient dans le paquet.
* **Thème** : les boutons, cases, listes et fonds suivent le thème, clair comme sombre. Les cartes résultat / erreur de Merge DoVi sont de nouveau colorées. Les derniers textes non traduits le sont.

---
---

<a name="english-version"></a>
# English Version

*Changes since v5.0.2.*

---

## 1. New

* **Audio sync: quick check by default.** If the six spot measurements agree on the offset with a confidence of at least 0.90, the continuous track analysis is skipped. If the offset varies or confidence is lower, it starts on its own. Setting "Quick audio sync check" in Settings; in the CLI, `--quick-sync-check` / `--no-quick-sync-check` (with `--detect-cuts`). Only the in-depth analysis catches a drift that cancels out between two measurements: right-click an unsynced track → "In-depth synchronization…", or the "In-depth sync" button in Sync Studio.
* **Faster continuous analysis**: both tracks are decoded in parallel.
* **Sync Studio: manual cuts and silences.** Right-click the waveform to create a section. Adjust it by dragging its edges or with the Start / End fields (hh:mm:ss.mmm). "Cut (remove)" removes the target content; "Extend (add silence)" inserts a silence of the same length and pushes the rest back. A cut is removed by right-clicking it, on the waveform or in the cuts table. The target is drawn as it will be output (shifted pieces, cuts, silences, frame rate); for a subtitle track, the line text is shown.
* **Sync Studio: "Quick sync" and "In-depth sync" buttons** for an audio track. The reference of the last sync is reused.
* **Container: reset to sources.** The ↺ button next to "Apply profile" returns tracks (language, title, flags, selection, order), audio variants, synchronization and chapters to the state of the source files, without inspecting them again. Attachments, tags, title and output file are kept, as are the Encoding panel settings of the original tracks.
* **Container: "check all" box in the header** of the selection column, replacing the "Enable all" / "Disable all" buttons. It is greyed out when only some tracks are checked.
* **Negligible offset**: a constant offset of 5 ms or less is no longer applied, so the track is not re-encoded. The log says so.
* **Hybrid Studio**: episodes named "S01.E02", "S01_E02" or "1x02" are recognized.
* **Hybrid Studio: two-step season analysis.** "Analyze the season" first prepares the pairings and tracks of every episode, without audio sync, then offers the audio synchronization check. The "Check audio synchronization" button runs it later on every complete pair of the table. Running and saving workflows first check the remaining pairs.
* **Languages**: "VFQ" in a track title gives fr-CA. Profiles and selectors saved with fr-FR still apply to these tracks.
* **Documentation**: README rewritten, now also available in English (`README_EN.md`).

---

## 2. Fixes

* **Profiles applied in the Container panel**: the profile starts from the state of the table (edited languages, titles and flags, or ones already changed by a previous profile), including for source title and source language criteria, which so far read the original file. The profile editor preview follows the same rule.
* **Chapters**: after an edit, going back to a source's chapters (changing the chapter source, reset) brought back the edited values instead of the file's.
* **Sync, `asetrate` method**: the resulting speed was wrong for a track that is not 48 kHz (44.1 kHz, 96 kHz…). The track's actual sample rate is now used.
* **Sync, frame rate**: in the Container panel, the video frame rate difference between sources (PAL ↔ film) is passed to the analysis, according to the "Automatically apply PAL ↔ Film conversion" setting. A difference above 5 % (telecine 29.97 ↔ 23.976, doubled frames 50 ↔ 25) is no longer taken as an audio speed change.
* **Sync with cuts or frame rate conversion**: the calibration is kept for the whole source. Cancelling it on one track cancels it for every track of the source, and the log lists them. The source's chapters follow the full calibration, not only the start offset; a chapter inside a removed part is placed where playback resumes.
* **Sync Studio**: a cut that starts inside a silence keeps the rest of the silence. Invalid input clears the section and blocks applying it. "Quick sync" and "In-depth sync" pass the frame rate and audio method.
* **Hybrid Studio, episode pairing**: "S01E02-1080p" is no longer counted as two episodes. "S01E01E02v2", "S01E01-E02REPACK" or "1x01-02v2" are rejected as ambiguous, instead of being paired with the first episode only.
* **Hybrid Studio, season**: a failing episode no longer stops the analysis; it is marked "Failed" (cause in the tooltip and the log) and the next episodes are processed. Running and saving workflows stay refused while a complete pair has no plan. Saving workflows no longer recomputes episodes that are already synchronized (Sync Studio adjustments are kept).
* **HDR10 estimation (Dolby Vision P5 sources)**: the analysis no longer hangs when the tool writes many messages. Cancellation works, and the end of the tool's log is reported on failure.
* **HDR Studio**: if publishing the corrected file is refused (destination created or modified in the meantime), the corrected file is kept next to the output; the error message gives its path.
* **TMDB**: a number in the title ("Blade Runner 2049", "1917", "2001 A Space Odyssey") is no longer taken as the release year.
* **AppImage**: a checked, greyed-out checkbox (HDR10 / Dolby Vision / HDR10+ copy) looked unchecked, because the icons were missing from the package.
* **Theme**: buttons, checkboxes, lists and backgrounds follow the theme, light and dark. Merge DoVi result / error cards are colored again. The last untranslated texts are translated.
