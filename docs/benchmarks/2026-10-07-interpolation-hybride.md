# Interpolation hybride : RIFE + compensation de mouvement + flux optique NVIDIA

###### tags: `muxiveo` `rife` `interpolation` `nvof` `benchmark`

> Moteur `--engine hybrid` de muxiveo-rife 1.3.0, préréglages Équilibré (défaut) et Qualité de Muxiveo. Mesures du 2026-10-07, RTX 4070 Ti SUPER (Linux) et iGPU AMD Raphael (RADV).

## TL;DR

:::success
- **Problème** : sur les motifs fins et répétitifs (barreaux devant un bardage rayé, *Ready Player One* 74–77 s), RIFE retient la mauvaise période : les barreaux ondulent ou se dédoublent. MVTools les garde droits, mais calcule sur CPU (trop lent pour un encodage à la volée) et produit des macroblocs sur les rotations rapides (hélice, 80–81 s).
- **Solution** : compensation de mouvement par blocs sur le GPU (Vulkan), dans le même lot de commandes que RIFE ; RIFE garde la main là où la compensation explique mal l'image (occultations, rotations, bord du cadre).
- **Résultat** : barreaux droits là où RIFE les déforme ; hélice identique à RIFE, sans macrobloc. Avis visuel sur RPO 72–84 s en 24 → 59,94 : RIFE v4.6 « horrible », hybride v4.6 « quelques artefacts », hybride v4.15 « excellent ».
- **Coût** (vrai 24 → 59,94 en 4K) : hybride v4.6 ≈ 1,35 × RIFE, hybride v4.15 ≈ 2 × RIFE.
:::

[TOC]

## Méthode

- Bancs : images paires d'un extrait → interpolation ×2 (ou ×4) → comparaison aux images impaires retirées (PSNR-Y, image entière / zone critique).
  - **Barrières** : RPO 74,0–77,3 s, 4K, zone des barreaux.
  - **Ville + hélice** : RPO 79,0–82,3 s, rotations rapides et rambarde floue au bord gauche.
- Contrôle visuel en vrai 24 → 59,94 (positions t = 0,2 / 0,4 / 0,6 / 0,8), images sensibles 203 et 232 (barreaux) et 47 (hélice).
- Chaîne de vitesse : `ffmpeg | muxiveo-rife | hevc_nvenc`, 12 s de RPO en 4K, décodage, conversion et encodage compris.

## Moteur

1. Pyramide de luminance 1/8 → 1/1 ; recherche bilatérale par blocs de 16 centrée sur l'image à créer : exhaustive (±8 px) au niveau 1/8, puis raffinement avec pénalité de cohérence ; fenêtre de correspondance 32 × 32 aux niveaux 1/1 et 1/2, 48 × 48 aux niveaux grossiers.
2. Deux passes de propagation aux niveaux grossiers (voisins 3 × 3, prédicteur médian) : corrigent les régions prises sur une mauvaise période.
3. Un seul champ de vecteurs par paire (calculé à t = 0,5), réutilisé pour toutes les positions intermédiaires.
4. Reconstruction recouvrante (OBMC) des quatre blocs voisins, en écartant le côté qui échantillonne hors image.
5. Décision par région : RIFE là où la densité de pixels mal expliqués (erreur bilatérale > 20) dépasse 25 % sur 31 px, et au bord du cadre ; compensation ailleurs ; moyenne des deux là où ils coïncident ; transition adoucie sur 15 px.
6. **Flux optique NVIDIA (NVOFA)**, facultatif : flux aller / retour calculé sur un thread pendant l'inférence RIFE (luminance réduite 2 × 2, grille de 2 px ; pleine résolution et grille de 4 px sur Turing), médianes par bloc ajoutées aux candidats du niveau final. Pilote chargé à l'exécution, sans dépendance CUDA au lancement.

## Qualité

PSNR-Y en dB (image entière / zone) :

| Banc | RIFE v4.6 | Hybride v4.6 | Hybride v4.15 |
|---|---|---|---|
| Barrières ×2 | 35,30 / 32,54 | 36,48 / 34,67 | 36,74 |
| Barrières ×4 (t = 0,25 / 0,5 / 0,75) | 31,33 / 29,40 | 31,82 / 30,21 | 32,26 |
| Ville + hélice ×2 | 35,09 / 32,73 | 35,08 / 32,54 | 35,24 |

- Sans NVOF (carte non NVIDIA, Pascal, ou `--nvof off`) : hybride en retrait de 0,02 à 0,07 dB seulement ; l'iGPU AMD (RADV) donne la même qualité que la RTX.
- Modèles écartés pour l'hybride : v4.25 (36,50 / 31,91 / 34,86), v4.26 (36,54 / 31,94 / 34,95), v4.25-lite (inférieur) ; v4.15 *ensemble* (export maison) : 36,74 / 32,22 / 35,18 pour 1,5 × le coût de v4.15, sans gain.
- `scale=0.5` (mode Fast, `--uhd`) : −2,3 dB sur les barreaux ; à réserver au préréglage Light.

## Grands mouvements (muxiveo-rife 1.5.0, 2026-10-08)

Retour utilisateur sur RPO (Qualité, 24 → 59,94) : passage derrière un poteau métallique flou et descente en
rappel devant un objet flou montraient des « gouttes » et contours ondulés sur les images créées, absents
d'une interpolation RIFE v4.15 seule (scale 0,5, *ensemble*, par un autre logiciel). Cause : la compensation
par blocs est choisie par défaut et seulement écartée quand son erreur bilatérale est forte ; dans le flou
de bougé, des vecteurs faux coûtent peu. RIFE pleine résolution est propre visuellement, mais RIFE à flux
demi-résolution suit mieux ces grands déplacements.

Correctif : second réseau RIFE à flux demi-résolution là où le déplacement entre sources dépasse 16 px
(rampe jusqu'à 48 px, champ dilaté, plancher sur toute l'image quand le mouvement médian de la paire dépasse
24 px), passe lancée seulement si au moins 1 % des blocs dépassent 24 px.

Images paires interpolées ×2 comparées aux impaires (déplacements doublés : seuil de 32 px dans ce protocole),
PSNR-Y image entière / 1 % des blocs 64 × 64 les pires (zone pour barreaux et hélice) :

| Plan | RIFE v4.15 | RIFE v4.15 scale 0,5 | Hybride v4.15 (1.4) | Hybride v4.15 (1.5) |
|---|---|---|---|---|
| Poteau (12 images) | 27,54 / 16,13 | 29,07 / 16,82 | 27,33 / 16,09 | **29,09 / 16,93** |
| Rappel (31 images) | 34,39 / 19,93 | 34,59 / 20,40 | 34,16 / 19,63 | **34,70 / 20,44** |
| Barrières ×2 (zone) | 35,95 / 33,37 | 33,70 / 30,53 | 36,74 / 34,81 | **36,74 / 34,81** |
| Ville + hélice ×2 (zone) | 35,41 / 33,01 | 35,09 / 32,37 | 35,24 / 32,66 | 35,39 / 32,86 |

*Ensemble* v4.15 : sans effet mesurable (poteau 27,42, rappel 34,29). Vrai 24 → 59,94 sur les barrières :
écart avec l'hybride 1.4 ≥ 44,6 dB dans la zone des barreaux ; +20 % de temps en Vulkan (passe active pour
43 % des paires).

## Vitesse

Vrai 24 → 59,94, 12 s en 4K (RTX 4070 Ti SUPER, `hevc_nvenc`) :

| Configuration | Temps | Rapport |
|---|---|---|
| RIFE v4.6 (Rapide) | 26,1 s | 1 × |
| Hybride v4.6 sans NVOF | 31,3 s | 1,20 × |
| Hybride v4.6 + NVOF (Équilibré) | 35,7 s | 1,37 × |
| Hybride v4.15 + NVOF (Qualité) | 54,5 s | 2,02 × |

Profil par image créée : RIFE 37 ms ; recherche par paire ≈ 12 ms ; reconstruction, décision et mélange ≈ 2,6 ms. Optimisations retenues : NVOF recouvert par RIFE (−24 %), NVOF à demi-résolution, 8 candidats évalués en parallèle par bloc, fenêtre 32 × 32 aux niveaux fins (−8 %), champ unique par paire. Écartées sans gain : arithmétique fp16 (−0,3 à −0,7 dB), passe-haut pour l'estimation (recasse les barreaux), réductions par sous-groupe.

## Préréglages Muxiveo

| Préréglage | Moteur | Modèle | Coût 24 → 60 (4K) |
|---|---|---|---|
| Rapide | RIFE | v4.6 | 1 × |
| Équilibré (défaut) | hybride | v4.6 | ≈ 1,35 × |
| Qualité | hybride | v4.15 | ≈ 2 × |
| Light | RIFE + mode Fast | v4.15-lite | < 1 × |

L'ancien préréglage Max des presets enregistrés devient Qualité. Le flux optique NVIDIA est utilisé automatiquement par les préréglages hybrides ; le tableau de bord indique sa disponibilité (badge « NVOF·CUDA »).
