# Notes de version / Release Notes — Muxiveo v5.0.1

* [Français](#version-française)
* [English](#english-version)

---

<a name="version-française"></a>
# Version Française

*Modifications depuis la version v5.0.0.*

---

## 1. Nouveauté

* **Conteneur : type de couche Dolby Vision affiché** pour les sources profil 7. Le libellé devient par exemple « Dolby Vision P7.6 FEL + HDR10+ » ou « … MEL ». Il apparaît aussi dans le bandeau de la source et dans le panneau Encodage. L'information est lue dans le RPU quelques secondes après l'ajout, en arrière-plan, sans ralentir l'interface ni l'inspection des autres fichiers.

---

## 2. Correctif

* **HDR Studio, réalignement d'après la mesure** : la proposition L6 reprenait l'écran de mastering du MDCV, même quand celui-ci était inférieur au pic mesuré de l'image. Exemple : MDCV à 1000 nits pour une image à 2211 nits, alors que le RPU indique 4000 nits. En lecture HDR10 de secours, les téléviseurs qui suivent l'écran de mastering auraient écrêté au-delà de 1000 nits. Désormais, le L6 garde un écran qui couvre l'image, et un MDCV inférieur au pic est lui-même proposé à la valeur du RPU. La lecture Dolby Vision n'était pas concernée.

---
---

<a name="english-version"></a>
# English Version

*Changes since v5.0.0.*

---

## 1. New

* **Container: Dolby Vision layer type shown** for profile 7 sources. The label becomes, for example, "Dolby Vision P7.6 FEL + HDR10+" or "… MEL". It also appears in the source banner and in the Encoding panel. The information is read from the RPU a few seconds after the file is added, in the background, without slowing the interface or the inspection of other files.

---

## 2. Fix

* **HDR Studio, realignment from the measurement**: the L6 proposal reused the MDCV mastering display even when it was below the measured peak of the picture. Example: an MDCV at 1000 nits for a picture at 2211 nits, while the RPU states 4000 nits. In HDR10 fallback playback, TVs that follow the mastering display would have clipped above 1000 nits. The L6 now keeps a mastering display that covers the picture, and an MDCV below the peak is itself proposed at the RPU value. Dolby Vision playback was not affected.
