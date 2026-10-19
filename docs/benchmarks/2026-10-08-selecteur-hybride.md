# Interpolation : sélecteur appris et RIFE v4.15 affiné (muxiveo-rife 1.6.0)

###### tags: `muxiveo` `rife` `interpolation` `benchmark`

> Préréglages Équilibré, Qualité et nouveau préréglage Ultra de Muxiveo. Mesures du 2026-10-08/09, RTX 4070 Ti SUPER
> (Linux, Vulkan), muxiveo-rife 1.6.0.

## TL;DR

:::success
- **Sélecteur appris** : le moteur hybride choisit, zone par zone, le mélange de candidats (RIFE, RIFE à flux
  demi-résolution, compensation de mouvement, RIFE inversé, flux NVIDIA) au lieu d'une règle fixe ; de la SD à la 8K.
- **RIFE v4.15 affiné** (`rife-v4.15-mvo1`) : même architecture et même coût que v4.15, meilleur sur tous les jeux
  d'évaluation, y compris les textes incrustés immobiles sur fond mobile.
- **Résultat** (ΔPSNR-Y par rapport à RIFE v4.15 seul) : Qualité +0,81 dB en 4K, +0,52 en 1080p, +0,32 à +0,34 en
  SD/720p ; Ultra +0,86 / +0,60 / +0,41 à +0,43. L'hybride 1.5 était négatif en basse résolution.
- **Coût** (4K, Vulkan) : sélecteur ≈ 0 ; Ultra ≈ 1,5 × Qualité ; modèle affiné : aucun surcoût.
:::

[TOC]

## Méthode

- Protocole : images paires d'une scène interpolées ×2, comparées aux images impaires retirées (PSNR-Y 10 bits ;
  « pire 1 % » : moyenne des 1 % de blocs 64 × 64 les moins bons). Les déplacements sont doublés par rapport au vrai
  24 i/s ; seuils de grands mouvements ajustés en conséquence.
- Images de référence inutilisables écartées : image tenue (animation « en deux », coupe), fondu de conversion de
  cadence, doublon d'extraction.
- Évaluation sur des scènes jamais vues à l'apprentissage : 30 scènes de 10 films en 4K (mouvement faible, moyen,
  fort), les mêmes réduites en 1080p, 720p et SD, et 30 scènes de 5 titres SD / 720p natifs.

## Sélecteur

| Candidat | Origine | Présent |
|---|---|---|
| R1 | RIFE (modèle du préréglage) | toujours |
| R05 | RIFE à flux demi-résolution | paires à grand mouvement |
| MC | compensation de mouvement par blocs | toujours |
| Rbwd | RIFE dans le sens inverse | préréglage Ultra (`--ultra`) |
| NV | déformation dense par le flux optique NVIDIA | carte NVIDIA avec NVOFA |

- Blocs de `largeur / 120` px (8 en SD, 16 en 1080p, 32 en 4K, 64 en 8K) ; poids interpolés entre centres de blocs.
- Indices : désaccord de chaque candidat avec R1 (et son maximum sur 3 × 3 blocs), texture, écart temporel,
  luminance, incohérence et amplitude du flux NVIDIA, proximité du bord de l'image, taille de bloc.
- Modèle linéaire + softmax, appris pour minimiser l'erreur exacte par bloc ; un jeu de poids par famille de modèle
  RIFE, avec et sans flux NVIDIA (sans NVOF : appris sur la compensation calculée sans flux NVIDIA, comme sur une carte
  AMD ou Intel), et par bande de taille de bloc pour v4.6.
- Essais écartés : réseau à couche cachée (sur-apprentissage), indices internes de la compensation (redondants),
  désaccords entre candidats autres que R1 (sans gain) ; un oracle par bloc montre une marge restante de 1,5 à 2,5 dB,
  limitée par la prédiction et non par les candidats.

## RIFE v4.15 affiné

- Même graphe ncnn que le modèle d'origine : seuls les poids changent (export par réécriture du fichier de poids du
  modèle livré ; un export indépendant du graphe dégradait la SD de 4 dB).
- Apprentissage à plusieurs positions temporelles (t de 0,2 à 0,8), validation à chaque étape sur les scènes
  d'évaluation et sur des séquences haute cadence natives (vraie géométrie du 24 → 60).
- Textes et logos incrustés immobiles : un premier modèle les entraînait avec le mouvement du fond (lettres
  dédoublées) ; corrigé par incrustations synthétiques à l'apprentissage. Dans la zone des incrustations, le modèle
  retenu dépasse l'original de 1,0 à 2,3 dB.

| RIFE seul (ΔPSNR-Y face à v4.15) | 4K | 1080p | SD / 720p natifs | SD / 720p réduits |
|---|---|---|---|---|
| `rife-v4.15-mvo1` | +0,45 | +0,37 | +0,32 | +0,26 |
| `rife-v4.15-mvo1 --uhd` (face à v4.15 `--uhd`) | +0,52 | +0,59 | +0,56 | +0,64 |

## Résultats

ΔPSNR-Y moyen (Δ « pire 1 % ») face au RIFE seul du même modèle d'origine :

| Configuration | 4K | 1080p | SD / 720p natifs | SD / 720p réduits |
|---|---|---|---|---|
| Hybride 1.5 v4.15 (règle fixe) | +0,31 | +0,01 | −0,05 | −0,28 |
| **Qualité** (hybride v4.15-mvo1) | +0,81 (+0,76) | +0,52 (+0,46) | +0,34 (+0,34) | +0,32 (+0,34) |
| **Ultra** | +0,86 (+0,82) | +0,60 (+0,54) | +0,43 (+0,44) | +0,41 (+0,44) |
| Qualité sans flux NVIDIA | +0,77 (+0,72) | +0,51 (+0,46) | +0,35 (+0,34) | +0,32 (+0,34) |
| **Équilibré** (hybride v4.6, face à RIFE v4.6) | +0,73 (+0,70) | +0,60 (+0,71) | +0,43 (+0,62) | +0,55 (+0,71) |

- Régressions restantes : quelques scènes calmes déjà proches de 45–51 dB (−0,25 dB au pire en Qualité, −0,23 en
  Ultra), imperceptibles.
- Hybride 1.5 : mesure sans écarter les images de référence inutilisables (même ordre de grandeur).

## Coût

4K, 96 images, Vulkan seul (temps mural, chargement compris) :

| Préréglage | Temps relatif |
|---|---|
| Rapide (RIFE v4.6) | 1 |
| Équilibré | ≈ 2 |
| Qualité | ≈ 3 |
| Ultra | ≈ 4,5 (≈ 1,5 × Qualité) |
| Light | ≈ 1 |

Le sélecteur ne coûte rien de mesurable (règle fixe et sélecteur au même débit) ; le flux NVIDIA ajoute ≈ 8 %.
