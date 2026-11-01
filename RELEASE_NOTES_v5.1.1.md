# Notes de version / Release Notes — Muxiveo v5.1.1

* [Français](#version-française)
* [English](#english-version)

---

<a name="version-française"></a>
# Version Française

*Modifications depuis la version v5.1.0.*

---

## Correctifs

* **Synchronisation audio : alignement sur la chronologie de lecture réelle.** L'analyse acoustique et le découpage temporel s'appuient désormais systématiquement sur la chronologie de restitution (`aresample=async=1:first_pts=0`). Les flux audio présentant un départ tardif (paquets avec décalage initial `start_time` ou horodatage absolu TS/M2TS), des silences ou des trous de diffusion (gaps), ou encore des logos/intros présents d'un seul côté ne faussent plus la mesure ni l'application du calage : le temps audio mesuré correspond fidèlement à ce qu'entend le lecteur vidéo, sans compter les échantillons à partir du premier paquet reçu.
* **Synchronisation multilingue : analyse hors dialogues renforcée et gestion des flux multicanaux complexes.** Lors d'une synchronisation entre pistes de langues différentes où les voix masquent la musique et les effets, si l'analyse mono initiale échoue ou laisse des zones non vérifiées, Muxiveo retente automatiquement une seconde passe en isolant les canaux d'ambiance (surrounds ou L−R). Cette séparation prend désormais en charge les flux multicanaux (5.1 ou 7.1) dont la disposition spatiale exacte n'est pas explicitement nommée par les métadonnées (notamment les pistes E-AC-3 de certaines sources WEB).
* **Synchronisation multilingue : pas d'abandon d'office si l'analyse continue échoue.** Lorsque deux pistes ont des mixages trop distincts pour établir une corrélation continue sur tout le métrage, l'analyse n'échoue plus avec un abandon brutal exigeant un calage entièrement manuel : si au moins trois mesures ponctuelles concordent (à 20 ms près ou sous les seuils psycho-acoustiques), Muxiveo propose ce décalage à l'utilisateur (« Synchro incertaine à valider »). Il peut être validé directement ou inspecté à l'écoute dans le Synchro Studio.
* **Synchronisation audio : décalage incertain négligeable non appliqué.** Si le décalage calculé d'après les seules mesures ponctuelles est inférieur ou égal à 10 ms (seuil d'imperceptibilité), il est laissé tel quel sans réencodage de la piste, avec un avertissement consigné dans le journal, évitant d'appliquer un décalage inaudible qui pourrait n'être qu'un léger artefact de mesure.
* **Compatibilité graphique : exclusion des versions défectueuses de PySide6 (6.11.3 et 6.12.0).** Les versions 6.11.3 et 6.12.0 de Qt / PySide6 rognent la zone de saisie des champs numériques stylés (spinbox), rendant les valeurs difficiles à lire. De plus, PySide6 6.12.0 provoque sous Python < 3.12 un plantage critique du processus (« deallocating None »). Ces deux versions sont désormais explicitement exclues des prérequis et automatiquement réinstallées/remplacées par une version saine lors du packaging ou de la vérification de l'environnement, avec des tests de non-régression dédiés sur la géométrie des contrôles.

---
---

<a name="english-version"></a>
# English Version

*Changes since v5.1.0.*

---

## Fixes

* **Audio synchronization: alignment with real playback timeline.** Acoustic analysis and segment trimming now consistently rely on playback presentation time (`aresample=async=1:first_pts=0`). Audio streams with a delayed start (packets with an initial `start_time` or absolute TS/M2TS timestamps), transmission gaps, or studio logos/intros present on only one side no longer skew measurement or offset application: measured audio time matches exactly what the video player outputs, rather than counting samples from the first received packet.
* **Multilingual synchronization: enhanced dialogue-free analysis and support for complex surround layouts.** When synchronizing tracks in different languages where speech masks background music and shared effects, if the initial mono analysis fails or leaves unverified sections, Muxiveo automatically attempts a second pass isolating ambient channels (surrounds or L−R). This channel separation now supports multichannel streams (5.1 or 7.1) whose channel layout is not explicitly named in container metadata (notably E-AC-3 streams from certain WEB sources).
* **Multilingual synchronization: no outright failure when continuous analysis is inconclusive.** When two audio tracks have mix differences too significant to achieve continuous correlation across the whole feature, synchronization no longer fails abruptly requiring a fully manual setup: if at least three spot measurements agree (within 20 ms or below psychoacoustic perception thresholds), Muxiveo proposes this offset to the user ("Uncertain sync to validate"). It can be applied directly or checked by ear in the Sync Studio.
* **Audio synchronization: negligible uncertain offset ignored.** If the offset derived from spot measurements alone is 10 ms or less (imperceptible threshold), the track is left unchanged without re-encoding, accompanied by a log warning, avoiding applying an inaudible shift that might merely be a measurement artifact.
* **GUI compatibility: exclusion of defective PySide6 versions (6.11.3 and 6.12.0).** Qt / PySide6 versions 6.11.3 and 6.12.0 truncate the text editing area of styled numeric input fields (spinboxes), making numbers hard to read. In addition, PySide6 6.12.0 causes a fatal process crash ("deallocating None") under Python < 3.12. Both versions are now explicitly excluded from requirements and automatically reinstalled/replaced with a healthy version during packaging or environment verification, backed by non-regression tests covering input box geometry.
