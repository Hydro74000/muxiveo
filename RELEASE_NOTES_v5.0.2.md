# Notes de version / Release Notes — Muxiveo v5.0.2

* [Français](#version-française)
* [English](#english-version)

---

<a name="version-française"></a>
# Version Française

*Modifications depuis la version v5.0.1.*

---

## 1. Nouveautés

* **Encodage : conversion Dolby Vision P7 → P8.1 sans réencodage.** Une case « Convertir en P8.1 (retire la couche d'amélioration MEL/FEL) » apparaît juste sous « Copier le RPU Dolby Vision depuis la source ». En codec Copie, elle retire la couche d'amélioration et convertit le RPU en P8.1 ; l'image de base est recopiée telle quelle, avec les horodatages d'origine. Décochée, une piste P7 est recopiée sans changement. En réencodage d'une source P7, la case est cochée et grisée : la couche d'amélioration ne s'applique qu'à l'image de base d'origine, la sortie est donc toujours P8.1. Pour garder l'apport du FEL, cochez « Intégrer la couche FEL ». La description du traitement prévu s'affiche à côté de la case.
* **HDR Studio : FEL Analyzer plus rapide, sans perte de précision.** Les mesures donnent les mêmes valeurs qu'auparavant. Les analyses progressent de la densité la plus grossière à la plus fine, avec un verdict provisoire entre les passes, et les instants déjà mesurés sont repris du cache. Avec l'extension mvo-fel 0.6.0, les deux couches comparées sont produites par un seul décodage, sur GPU en précision exacte (identique au calcul CPU). Mettez l'extension à jour depuis la page Extensions.
* **Bouton « Soutenir »** dans le pied de la barre latérale (PayPal).

---

## 2. Correctifs

* **Encodage, Copie + conversion P8.1** : le traitement échouait systématiquement à la remise en MKV du flux converti (« Can't write packet with unknown timestamp »). Le timing provient désormais de la piste d'origine.
* **Fenêtre « Contrôle final rejeté »** : les boutons Oui / Non restaient en anglais dans l'interface française, et le motif de l'audit des trames restait en français dans l'interface anglaise. Les boutons standard des boîtes de dialogue suivent désormais la langue choisie.
* **HDR Studio** : une erreur à l'ouverture d'une session de capture FEL est remontée proprement, dans l'interface comme en CLI.

---
---

<a name="english-version"></a>
# English Version

*Changes since v5.0.1.*

---

## 1. New

* **Encoding: Dolby Vision P7 → P8.1 conversion without re-encoding.** A "Convert to P8.1 (removes the MEL/FEL enhancement layer)" checkbox appears right below "Copy Dolby Vision RPU from source". With the Copy codec, it removes the enhancement layer and converts the RPU to P8.1; the base layer is copied as is, with its original timestamps. Unchecked, a P7 track is copied unchanged. When a P7 source is re-encoded, the checkbox is checked and greyed out: the enhancement layer only applies to the original base layer, so the output is always P8.1. To keep the FEL contribution, check "Integrate the FEL layer". The planned processing is described next to the checkbox.
* **HDR Studio: faster FEL Analyzer, with no loss of accuracy.** Measurements return the same values as before. Analyses progress from the coarsest to the finest density, with a provisional verdict between passes, and instants already measured are reused from the cache. With the mvo-fel 0.6.0 extension, both compared layers come from a single decode, on the GPU in exact precision (identical to the CPU computation). Update the extension from the Extensions page.
* **"Support" button** in the sidebar footer (PayPal).

---

## 2. Fixes

* **Encoding, Copy + P8.1 conversion**: processing always failed when the converted stream was put back into MKV ("Can't write packet with unknown timestamp"). Timing now comes from the original track.
* **"Final validation rejected" dialog**: the Yes / No buttons stayed in English in the French interface, and the frame audit reason stayed in French in the English interface. Standard dialog buttons now follow the selected language.
* **HDR Studio**: an error while opening an FEL capture session is now reported cleanly, in the interface and in the CLI.
