# RIFE dans Muxiveo vs `--vpp-rife-ov` de NVEncC

###### tags: `muxiveo` `rife` `nvencc` `benchmark` `interpolation`

> Comparatif du **process + modèle intégrés** de chaque outil, tels que livrés, pour une conversion **23,976 → 59,94 i/s** (×2 en repli), sur **quatre contenus** : deux films 4K HDR, une animation 3D 4K HDR et un anime 2D 1080p. Étude complémentaire des modèles RIFE pour le sélecteur de Muxiveo. Mesures du 2026-10-02.

## TL;DR

:::success
- **24 → 60 : utiliser Muxiveo.** NVEncC 9.36 (`--vpp-rife-ov`) ne produit pas de vrai 59,94 i/s : l'option `fps=` plante, il ne reste que ×2 (47,95 i/s) ou ×3 (71,93 i/s). Pour arriver à 60, il faut ensuite convertir la cadence, ce qui **duplique 1 image sur 5** : le mouvement s'arrête 12 fois par seconde, d'où l'**image saccadée**. Muxiveo calcule directement chaque image à sa bonne position temporelle (0 / 0,4 / 0,8 / 0,2 / 0,6) : mouvement régulier, aucun doublon.
- **×2 : équivalents** en vitesse (± 4 %) et en qualité à modèle égal. Mais NVEncC fond les coupes de scène (images fantômes), perd la dernière image, ne documente aucune extension DoVi / HDR10+ aux images créées, se limite à RIFE v4.6 en 4K et demande 3 à 9 Go de dépendances CUDA plus une carte NVIDIA.
- **Modèle : passer à v4.6.** Meilleur VMAF moyen sur les 4 contenus, 1,6 à 1,8× plus rapide que v4.26 en 24→60, 1,8 Go de VRAM. Abandonner v4.25-heavy (préréglage *Max*), plus lent sans être meilleur.
- **Carte ≤ 8 Go** : v4.6, ou v4.15-lite en mode Fast (1,7 Go en 4K).
- **Anime 2D** : pas de gain mesurable (dessins tenus, RIFE crée des intervalles absents de l'original) ; à réserver aux contenus filmés ou en 3D.
:::

**À faire** : ① préréglage Rapide → v4.6 et retrait de v4.25-heavy ; ② revue à l'œil v4.6 / v4.26 sur des panoramiques lents et des détails fins ; ③ si v4.26 n'apporte rien de visible, v4.6 par défaut.


[TOC]

## En bref

:::success
- **NVEncC ne sait pas faire 24 → 60** avec son filtre RIFE (9.36, Linux) : toute cadence cible échoue, seuls les multiplicateurs entiers fonctionnent. Passer ensuite à 59,94 i/s répète 1 image sur 5 : **image saccadée**. Le comparatif direct se limite donc au ×2.
- **À modèle égal, les deux moteurs donnent le même résultat** : PSNR/VMAF identiques avec v4.6 sur RPO et l'animation 3D. Les écarts viennent du *process* : modèles disponibles, coupes de scène, cadence exacte, dépendances.
- **Vitesse ×2 : à égalité.** En 4K, Muxiveo est même légèrement devant (Avatar 376 s contre 383 s, RPO 659 s contre 682 s) avec un modèle plus lourd ; en 1080p, NVEncC garde 4 % d'avance (100 contre 97 i/s).
- **Muxiveo gère les coupes et les flashs** (duplication) : pire image d'Avatar VMAF 63,7 contre 24,7 ; sur Bleach, riche en coupes, VMAF moyen 19,1 contre 16,7 à modèle identique.
- **Modèles : écart total inférieur à 2 points de VMAF.** **v4.6 est le meilleur en moyenne sur les 4 contenus, le plus rapide et le plus sobre en VRAM** ; en 24→60 réel, il traite ces sources 1,6 à 1,8× plus vite que v4.26 (défaut actuel). Le préréglage *Max* actuel (v4.25-heavy) figure parmi les trois derniers.
- **Anime 2D** : les mesures de fidélité ne peuvent pas montrer l'apport de RIFE (dessins tenus) ; le résultat est au niveau d'une simple duplication, et v4.13 / v4.9 n'y sont pas meilleurs.
:::

## Banc de test

| | |
|---|---|
| GPU / CPU | RTX 4070 Ti SUPER 16 Go (pilote 615.71) · Ryzen 7 7800X3D |
| Système | Fedora 44 (distrobox) |
| NVEncC | 9.36 (r4153) · `--vpp-rife-ov` via ONNX Runtime 1.23.2 (CUDA EP, chemin *cuda-zerocopy*) · CUDA 12.9, cuDNN 9.27 |
| Modèles NVEncC | pack officiel `HWEnc-onnx-models` 20260808 : `rife_v4_0` … `rife_v4_10` (exports vs-mlrt 2022) |
| Muxiveo | muxiveo-rife 1.1.0 (ncnn 20260526, Vulkan, stockage fp16 / calcul fp32) · modèles ncnn TNTwise |
| Encodage (vitesse) | identique pour les deux : NVEncC HEVC 10 bits, QVBR 22, P5 (+ HDR10 statique en HDR), padding au multiple de 32 |

| Contenu | Type | Définition (zone testée) | Format | Vitesse | Passages qualité |
|---|---|---|---|---|---|
| *Avatar : la Voie de l'eau* | film, prise de vue réelle | 3840×2072 (3840×2048) | HEVC 10 bits HDR10 + DV, 23,976 i/s | 180 s | 25 · 95 · 150 s |
| *Ready Player One* | film, mélange réel / CGI | 3840×2160, letterbox 3840×1600 | UHD Blu-ray, HDR10 + DV + HDR10+, 23,976 i/s | 5 premières min | 70 · 110 · 170 s |
| *Star Wars : Tales of the Underworld* | animation 3D stylisée | 3840×2160 (3840×1632) | HEVC 10 bits HDR10 + DV, 24 i/s | — | 150 · 420 · 555 s |
| *Bleach* | anime 2D | 1920×1080 (1920×1056) | H.264 8 bits SDR BT.709, 23,976 i/s | 5 premières min | 360 · 480 · 1200 s |

:::info
**Qualité mesurée contre de vraies images** (pas de référence « parfaite » possible en 60 i/s) :
- **×2** : une image sur deux retirée puis régénérée (t = 0,5) ;
- **schéma 24→60** : une image sur cinq conservée puis interpolée ×2,5 → mêmes positions temporelles qu'une vraie conversion 23,976 → 59,94 (t = 0,2 / 0,4 / 0,6 / 0,8), avec des mouvements 5× plus amples (test sévère).

Indicateurs sur les **seules images interpolées** : VMAF (modèle 4K v0.6.1, modèle HD v0.6.1 pour Bleach), PSNR-Y moyen par image (plafonné à 99 dB), SSIM. Ils mesurent la fidélité, pas les défauts temporels perçus (tremblements, scintillement) : à compléter par une revue visuelle. Zones recadrées au multiple de 32 pour que NVEncC accepte les mêmes images.
:::

## Blocages et différences de fond

| | NVEncC 9.36 `--vpp-rife-ov` | Muxiveo (muxiveo-rife) |
|---|---|---|
| **24 → 60 i/s** | 🛑 `fps=` échoue toujours (*failed to allocate the previous frame*), en 1080p comme en 4K, 8 ou 10 bits. Contournement `multi=5` + `--vpp-select-every 2` : bloqué à 0 image | ✅ `--fps 60000/1001`, rapport rationnel ×2,5 exact |
| **Modèles disponibles** | 🛑 pack limité à v4.0 – v4.10 (2022). En 4K, v4.7 – v4.10 saturent les 16 Go (ONNX Runtime en fp32) → **v4.6 maximum** ; v4.10 passe en 1080p | ✅ v4.6 → v4.26 en 4K (seul v4.26-large sature en 3840×2072) |
| **Coupes de scène, flashs** | ⚠️ interpolés (fondu entre deux plans) | ✅ détectés → image dupliquée |
| **Images figées** | ⚠️ recalculées | ✅ dupliquées à l'identique (207 paires sur l'extrait Avatar) |
| **Cadence réelle / nb d'images** | ⚠️ ×2 → **2N − 1** images sur toutes les sources (ex. 14 391 au lieu de 14 392) | ✅ ⌈N × r⌉ exact : alignement RPU DoVi / HDR10+ et synchro A/V garantis |
| **Métadonnées dynamiques** | non documenté pour les images interpolées | ✅ RPU DoVi et HDR10+ étendus à la nouvelle cadence, coupes conservées |
| **Dimensions** | multiples de 32 obligatoires (pad/crop) | quelconques (padding interne) |
| **Dépendances** | ONNX Runtime GPU + CUDA 12 + cuDNN 9 ≈ **2,8 Go** (+ TensorRT 6,2 Go en option) ; NVIDIA uniquement ; installation manuelle sous Linux | binaire **13 Mo** + modèles **62 Mo**, pilote Vulkan seul (NVIDIA, AMD, Intel, Apple) |
| **Stabilité observée** | ⚠️ un arrêt brutal de la machine, sans aucune trace dans les journaux, pendant NVEncC + v4.10 en 1080p (GPU à 100 %) — cas unique, cause non déterminée | aucun incident sur plusieurs heures de mesures |

### Pourquoi le 24 → 60 « façon NVEncC » saccade

Le mode `fps=` de `--vpp-rife-ov` est correct sur le papier : le code source (`NVEncFilterRifeOV.cpp`, `planSpan`) calcule pour chaque image produite sa position exacte entre deux images source, comme Muxiveo. Mais il échoue en 9.36 sous Linux (*failed to allocate the previous frame*), même en 1080p 8 bits. Il ne reste que des multiplicateurs entiers, qui ne tombent pas sur 60 :

| Voie NVEncC | Cadence obtenue | Passage à 59,94 i/s | Effet |
|---|---|---|---|
| `multi=2` | 47,95 i/s | 4 images → 5 : **1 image sur 5 répétée** | arrêt du mouvement 12 fois par seconde |
| `multi=3` | 71,93 i/s | 6 images → 5 : **1 image sur 6 supprimée** | saut de mouvement 12 fois par seconde |
| `multi=2` lu tel quel sur un écran 60 Hz | 47,95 i/s | le lecteur affiche 1 image sur 4 deux fois | même saccade, faite à la lecture |
| Muxiveo `--fps 60000/1001` | 59,94 i/s | aucun : chaque image est calculée à t = 0 / 0,4 / 0,8 / 0,2 / 0,6 | mouvement régulier |

Mesure sur Avatar (95 s, 1,5 s de travelling) : mouvement entre deux images successives (YDIF), sur les fichiers réellement produits.

```echarts
{
  title: { text: "Mouvement entre images successives à 59,94 i/s", subtext: "Avatar, 95 s · un 0 = image répétée = à-coup" },
  tooltip: { trigger: "axis" },
  legend: { top: 52, data: ["NVEncC ×2 converti en 59,94", "Muxiveo 24→60 natif"] },
  grid: { left: "3%", right: "4%", bottom: "3%", top: 95, containLabel: true },
  xAxis: { type: "category", name: "image", data: [1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25] },
  yAxis: { type: "value", name: "YDIF" },
  series: [
    { name: "NVEncC ×2 converti en 59,94", type: "line", symbolSize: 7, data: [14.6,14.5,14.2,0.0,15.2,15.0,14.4,14.2,0.0,13.9,13.8,13.2,12.7,0.0,12.7,12.3,11.7,11.0,0.0,10.5,10.3,10.5,12.3,0.0,13.8] },
    { name: "Muxiveo 24→60 natif", type: "line", symbolSize: 7, data: [11.8,12.5,9.9,12.6,12.2,12.4,12.5,9.4,12.2,11.6,11.1,11.6,8.8,11.2,10.1,10.1,10.4,7.2,9.7,8.6,8.2,9.3,7.5,11.4,10.7] }
  ]
}
```

:::warning
Côté NVEncC, le mouvement avance par pas de ~14 puis s'arrête net toutes les 5 images : l'œil perçoit un à-coup régulier. Côté Muxiveo, le mouvement est réparti sur toutes les images (légère respiration toutes les 5 images, probablement due à l'alternance entre images d'origine et images calculées). S'y ajoutent, chez NVEncC, les fondus aux coupes de scène, perçus comme un « flash » fantôme.
:::


## Vitesse — process complet

Décodage + RIFE + encodage NVEncC. Muxiveo : `ffmpeg | muxiveo-rife | nvencc` ; NVEncC : `nvencc --avhw … --vpp-rife-ov` (NVDEC).

Chaque case : **images produites par seconde**, puis durée totale et facteur temps réel (1× = aussi vite que la lecture).

#### ×2 — 23,976 → 47,95 i/s (seule conversion possible pour les deux)

| Configuration | Avatar · 4K · 180 s | RPO · 4K · 5 min | Bleach · 1080p · 5 min |
|---|:---:|:---:|:---:|
| NVEncC · v4.6 | **22,6** i/s<br>383 s · 0,47× temps réel | **21,1** i/s<br>682 s · 0,44× temps réel | **100,4** i/s<br>143 s · 2,09× temps réel |
| Muxiveo · v4.26 | **23,0** i/s<br>376 s · 0,48× temps réel | **21,8** i/s<br>659 s · 0,46× temps réel | **96,7** i/s<br>149 s · 2,02× temps réel |
| NVEncC · v4.10 | 🛑 VRAM | 🛑 VRAM | ≈ **55,6** i/s<br>interrompu (plantage) |
| Muxiveo · v4.10 | **20,6** i/s<br>420 s · 0,43× temps réel | — | — |
| *Muxiveo v4.26 vs NVEncC v4.6* | *+2 %* | *+4 %* | *−4 %* |

#### 24 → 60 — 23,976 → 59,94 i/s

| Configuration | Avatar · 4K · 180 s | RPO · 4K · 5 min | Bleach · 1080p · 5 min |
|---|:---:|:---:|:---:|
| NVEncC (tout modèle) | 🛑 impossible | 🛑 impossible | 🛑 impossible |
| Muxiveo · v4.26 (défaut actuel) | **15,2** i/s<br>713 s · 0,25× temps réel | **14,8** i/s<br>1219 s · 0,25× temps réel | **66,6** i/s<br>270 s · 1,11× temps réel |
| Muxiveo · v4.26 Fast | **18,2** i/s<br>592 s · 0,30× temps réel | **18,4** i/s<br>980 s · 0,31× temps réel | **73,4** i/s<br>245 s · 1,22× temps réel |
| Muxiveo · v4.6 | **24,4** i/s<br>442 s · 0,41× temps réel | **23,5** i/s<br>764 s · 0,39× temps réel | **118,8** i/s<br>151 s · 1,98× temps réel |
| Muxiveo · v4.10 | **13,5** i/s<br>798 s · 0,23× temps réel | — | — |
| *v4.6 vs v4.26* | *×1,61* | *×1,59* | *×1,78* |
| *Fast vs Normal* | *+20 %* | *+24 %* | *+10 %* |

```echarts
{
  height: 460,
  title: { text: "Images produites par seconde", subtext: "Process complet · plus haut = mieux · NVEncC : 24→60 impossible" },
  tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
  legend: { top: 52, data: ["NVEncC · v4.6", "Muxiveo · v4.26", "Muxiveo · v4.10", "NVEncC · v4.10", "Muxiveo · v4.26 Fast", "Muxiveo · v4.6"] },
  grid: { left: "3%", right: "4%", bottom: "3%", top: 110, containLabel: true },
  xAxis: { type: "category", data: ["Avatar ×2", "RPO ×2", "Bleach ×2", "Avatar 24→60", "RPO 24→60", "Bleach 24→60"] },
  yAxis: { type: "value", name: "i/s" },
  series: [
    { name: "NVEncC · v4.6", type: "bar", data: [22.6, 21.1, 100.4, null, null, null] },
    { name: "Muxiveo · v4.26", type: "bar", data: [23.0, 21.8, 96.7, 15.2, 14.8, 66.6] },
    { name: "Muxiveo · v4.10", type: "bar", data: [20.6, null, null, 13.5, null, null] },
    { name: "NVEncC · v4.10", type: "bar", data: [null, null, 55.6, null, null, null] },
    { name: "Muxiveo · v4.26 Fast", type: "bar", data: [null, null, null, 18.2, 18.4, 73.4] },
    { name: "Muxiveo · v4.6", type: "bar", data: [null, null, null, 24.4, 23.5, 118.8] },
  ]
}
```

:::info
- **×2** : les deux process se valent ; NVEncC profite du décodage NVDEC et d'un process unique, Muxiveo d'un modèle bien plus rapide à qualité égale et de la duplication des coupes et images figées. En 1080p, le coût fixe des pipes `ffmpeg | muxiveo-rife | nvencc` pèse davantage : NVEncC prend 4 % d'avance.
- **24 → 60 dans Muxiveo** : le choix du modèle domine tout le reste. v4.6 traite Avatar 1,6×, RPO 1,6× et Bleach 1,8× plus vite que v4.26, et **Bleach passe à 2× le temps réel** (151 s pour 5 min). Le mode Fast apporte +20 à +24 % en 4K, seulement +10 % en 1080p.
:::

## Qualité — duel des process (×2)

Seule conversion réalisable par les deux outils. Valeurs moyennes des trois passages : **VMAF** (PSNR-Y entre parenthèses).

| Configuration | Avatar | RPO | Anim. 3D | Bleach |
|---|---:|---:|---:|---:|
| NVEncC · v4.6 | 89,4 (49,70 dB) | 77,4 (36,32 dB) | 85,4 (41,23 dB) | 16,7 (27,00 dB) |
| NVEncC · v4.10 | 🛑 VRAM | 🛑 VRAM | 🛑 VRAM | 16,6 (26,94 dB) |
| Muxiveo · v4.26 | 90,6 (50,79 dB) | 76,3 (35,86 dB) | 84,8 (40,77 dB) | 19,4 (28,34 dB) |
| Muxiveo · v4.26 Fast | 90,0 (50,38 dB) | 75,2 (35,47 dB) | 83,8 (40,47 dB) | 19,1 (28,39 dB) |
| Muxiveo · v4.6 | 90,5 (51,12 dB) | 77,4 (36,32 dB) | 85,4 (41,23 dB) | 19,1 (28,18 dB) |
| Muxiveo · v4.10 | 90,3 (51,00 dB) | 75,4 (35,76 dB) | 84,4 (41,03 dB) | 18,9 (28,17 dB) |
| Référence : duplication | 66,9 (45,12 dB) | 44,1 (25,54 dB) | 68,8 (35,24 dB) | 21,4 (28,85 dB) |

**Pire image interpolée** (VMAF minimal sur les trois passages) — révèle le traitement des coupes de scène :

| Configuration | Avatar | RPO | Anim. 3D | Bleach |
|---|---:|---:|---:|---:|
| NVEncC · v4.6 | 24,7 | 33,0 | 68,8 | 0,0 |
| Muxiveo · v4.26 | 57,7 | 33,6 | 68,8 | 0,0 |
| Muxiveo · v4.6 | 63,6 | 33,0 | 68,8 | 0,0 |
| Référence : duplication | 31,7 | 24,3 | 46,7 | 0,0 |

:::warning
**Lecture** : hors coupes de scène, les deux process donnent le même résultat à modèle égal (RPO, animation 3D : valeurs identiques avec v4.6). Les écarts viennent des coupes : sur Avatar (coupe à 150 s), la pire image NVEncC tombe à VMAF 24,7 (deux plans fondus) ; sur Bleach, riche en coupes et en flashs, Muxiveo garde l'avantage en moyenne.

La **référence duplication** (image manquante = image précédente) situe l'apport de RIFE : +23 à +33 points de VMAF sur les films, +16 sur l'animation 3D, **aucun sur l'anime 2D** — l'image réelle intermédiaire y est souvent un dessin tenu, que RIFE remplace par un fondu inexistant dans l'original.
:::

```echarts
{
  title: { text: "Qualité ×2 vs images réelles (VMAF moyen)", subtext: "Images interpolées uniquement · plus haut = mieux" },
  tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
  legend: { top: 52, data: ["NVEncC · v4.6", "NVEncC · v4.10", "Muxiveo · v4.26", "Muxiveo · v4.26 Fast", "Muxiveo · v4.6", "Muxiveo · v4.10", "Référence : duplication"] },
  grid: { left: "3%", right: "4%", bottom: "3%", top: 110, containLabel: true },
  xAxis: { type: "category", data: ["Avatar : la Voie de l'eau", "Ready Player One", "Tales of the Underworld", "Bleach"] },
  yAxis: { type: "value", name: "VMAF", min: 0, max: 100 },
  series: [
    { name: "NVEncC · v4.6", type: "bar", data: [89.4, 77.43, 85.39, 16.74] },
    { name: "NVEncC · v4.10", type: "bar", data: [null, null, null, 16.6] },
    { name: "Muxiveo · v4.26", type: "bar", data: [90.64, 76.29, 84.84, 19.36] },
    { name: "Muxiveo · v4.26 Fast", type: "bar", data: [90.0, 75.24, 83.78, 19.14] },
    { name: "Muxiveo · v4.6", type: "bar", data: [90.45, 77.42, 85.39, 19.07] },
    { name: "Muxiveo · v4.10", type: "bar", data: [90.28, 75.36, 84.42, 18.94] },
    { name: "Référence : duplication", type: "bar", data: [66.9, 44.06, 68.78, 21.36] },
  ]
}
```

## Étude des modèles pour Muxiveo

Tous les modèles exécutés par muxiveo-rife (ncnn/Vulkan). **Débit et VRAM** : inférence pure ×2 sans détection de coupes, 120 images 4K (Avatar 3840×2072) et 1080p (Bleach) ; VRAM relevée par échantillonnage `nvidia-smi` (± 0,3 Go). **Qualité** : VMAF du schéma 24→60, moyenne des trois passages de chaque contenu.

| Modèle | Préréglage actuel | i/s 4K | vs v4.26 | i/s 1080p | VRAM 4K | VMAF Avatar | VMAF RPO | VMAF Anim. 3D | VMAF Bleach | **VMAF moyen** | PSNR moyen |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `v4.6` |  | 34,2 | +54 % | 152,9 | 1,8 Go | 72,7 | 62,5 | 72,0 | 30,4 | **59,4** | 33,54 |
| `v4.15-lite` |  | 26,5 | +19 % | 119,4 | 2,9 Go | 72,0 | 60,5 | 70,9 | 30,2 | **58,4** | 33,53 |
| `v4.16-lite` |  | 26,5 | +19 % | 119,4 | 2,7 Go | 72,0 | 60,4 | 70,8 | 30,2 | **58,3** | 33,51 |
| `v4.12-lite` |  | 25,3 | +14 % | 105,3 | 2,7 Go | 71,7 | 60,7 | 70,8 | 30,5 | **58,4** | 33,42 |
| `v4.9` |  | 25,3 | +14 % | 113,2 | 2,6 Go | 72,3 | 61,0 | 70,7 | 30,2 | **58,6** | 33,35 |
| `v4.22-lite` | Rapide | 24,8 | +12 % | 100,8 | 3,6 Go | 72,4 | 61,1 | 71,0 | 31,2 | **58,9** | 33,41 |
| `v4.17-lite` |  | 24,6 | +11 % | 116,5 | 2,9 Go | 70,7 | 58,8 | 70,4 | 31,6 | **57,9** | 32,78 |
| `v4.25-lite` |  | 23,9 | +8 % | 92,3 | 4,0 Go | 71,2 | 61,0 | 70,3 | 31,0 | **58,4** | 33,16 |
| `v4.13-lite` |  | 23,6 | +6 % | 107,1 | 2,9 Go | 72,3 | 60,4 | 70,8 | 30,6 | **58,5** | 33,40 |
| `v4.26` | Équilibré | 22,2 | +0 % | 88,9 | 3,8 Go | 71,4 | 61,1 | 71,0 | 31,0 | **58,6** | 33,29 |
| `v4.25` |  | 21,6 | −3 % | 88,9 | 3,5 Go | 71,5 | 61,1 | 70,9 | 31,0 | **58,6** | 33,24 |
| `v4.10` |  | 19,3 | −13 % | 83,0 | 2,7 Go | 72,0 | 60,4 | 71,0 | 30,5 | **58,4** | 33,29 |
| `v4.15` |  | 19,2 | −13 % | 83,3 | 2,7 Go | 72,9 | 60,8 | 70,3 | 30,0 | **58,5** | 33,65 |
| `v4.25-heavy` | Max | 18,5 | −17 % | 78,7 | 3,9 Go | 71,3 | 60,6 | 70,7 | 30,5 | **58,2** | 33,20 |
| `v4.13` |  | 18,0 | −19 % | 83,6 | 2,6 Go | 73,1 | 61,2 | 70,7 | 30,1 | **58,8** | 33,63 |
| `v4.14-lite` |  | 17,2 | −23 % | 68,2 | 2,6 Go | 71,1 | 59,3 | 70,3 | 29,6 | **57,6** | 33,34 |
| `v4.26-large` |  | 🛑 VRAM | — | 47,9 | — | — | 60,8 | — | 30,7 | **—** | — |
| *Référence : duplication* | | | | | | 49,0 | 43,1 | 59,1 | 29,6 | *45,2* | |

```echarts
{
  height: 500,
  title: { text: "Modèles RIFE : vitesse vs qualité", subtext: "VMAF moyen 24→60 sur les 4 contenus · taille du point = VRAM 4K" },
  tooltip: { trigger: "item" },
  grid: { left: "3%", right: "8%", bottom: "3%", top: 80, containLabel: true },
  xAxis: { type: "value", name: "i/s (4K ×2)", scale: true },
  yAxis: { type: "value", name: "VMAF", scale: true },
  series: [{
    type: "scatter",
    dimensions: ["i/s (4K ×2)", "VMAF moyen", "VRAM 4K (Go)"],
    label: { show: true, position: "right", formatter: "{b}" },
    data: [{"name": "v4.6", "value": [34.19, 59.4, 1.8], "symbolSize": 17}, {"name": "v4.15-lite", "value": [26.52, 58.4, 2.9], "symbolSize": 23}, {"name": "v4.16-lite", "value": [26.52, 58.33, 2.7], "symbolSize": 21}, {"name": "v4.12-lite", "value": [25.26, 58.43, 2.7], "symbolSize": 22}, {"name": "v4.9", "value": [25.26, 58.56, 2.6], "symbolSize": 21}, {"name": "v4.22-lite", "value": [24.82, 58.91, 3.6], "symbolSize": 26}, {"name": "v4.17-lite", "value": [24.64, 57.89, 2.9], "symbolSize": 22}, {"name": "v4.25-lite", "value": [23.95, 58.39, 4.0], "symbolSize": 28}, {"name": "v4.13-lite", "value": [23.65, 58.53, 2.9], "symbolSize": 22}, {"name": "v4.26", "value": [22.22, 58.64, 3.8], "symbolSize": 27}, {"name": "v4.25", "value": [21.56, 58.6, 3.5], "symbolSize": 25}, {"name": "v4.10", "value": [19.32, 58.44, 2.7], "symbolSize": 21}, {"name": "v4.15", "value": [19.23, 58.51, 2.7], "symbolSize": 22}, {"name": "v4.25-heavy", "value": [18.52, 58.25, 3.9], "symbolSize": 28}, {"name": "v4.13", "value": [18.0, 58.79, 2.6], "symbolSize": 21}, {"name": "v4.14-lite", "value": [17.16, 57.59, 2.6], "symbolSize": 21}]
  }]
}
```

```echarts
{
  title: { text: "VMAF 24→60 par contenu", subtext: "Modèles actuels et candidats · plus haut = mieux" },
  tooltip: { trigger: "axis" },
  legend: { top: 52, data: ["v4.6", "v4.13", "v4.15-lite", "v4.22-lite", "v4.26", "v4.25-heavy", "v4.9"] },
  grid: { left: "3%", right: "4%", bottom: "3%", top: 110, containLabel: true },
  xAxis: { type: "category", data: ["Avatar", "RPO", "Anim. 3D", "Bleach"] },
  yAxis: { type: "value", name: "VMAF", scale: true },
  series: [
    { name: "v4.6", type: "line", symbolSize: 8, data: [72.7, 62.47, 72.0, 30.41] },
    { name: "v4.13", type: "line", symbolSize: 8, data: [73.14, 61.22, 70.68, 30.11] },
    { name: "v4.15-lite", type: "line", symbolSize: 8, data: [72.03, 60.54, 70.86, 30.18] },
    { name: "v4.22-lite", type: "line", symbolSize: 8, data: [72.4, 61.06, 70.98, 31.19] },
    { name: "v4.26", type: "line", symbolSize: 8, data: [71.41, 61.15, 71.0, 30.99] },
    { name: "v4.25-heavy", type: "line", symbolSize: 8, data: [71.26, 60.57, 70.67, 30.48] },
    { name: "v4.9", type: "line", symbolSize: 8, data: [72.28, 61.05, 70.69, 30.22] },
  ]
}
```

:::info
- **v4.6 domine** : meilleur VMAF moyen (59,4), premier sur RPO et l'animation 3D, troisième sur Avatar, et le plus rapide (34 i/s en 4K, 153 i/s en 1080p) pour 1,8 Go de VRAM.
- **Les modèles récents ne sont pas devant** : v4.26 (58,6), v4.25 (58,6), v4.25-heavy (58,3, antépénultième et parmi les plus lents).
- **v4.13** est le meilleur sur Avatar (film réel) mais seulement troisième en moyenne, et parmi les plus lents.
- **Anime 2D (Bleach)** : classement serré et peu fiable (scores au niveau de la duplication) ; les lite récents (v4.17-lite, v4.22-lite) y sont légèrement devant, v4.13 et v4.9 derrière.
- Écart total entre le meilleur et le moins bon modèle : **1,8 point de VMAF** — à comparer aux ~13 points gagnés en moyenne sur la duplication.
:::

### Petites cartes graphiques : VRAM et mode Fast

| Modèle | 4K i/s Normal → Fast | 4K VRAM Normal → Fast | 1080p i/s Normal → Fast | 1080p VRAM Normal → Fast |
|---|---:|---:|---:|---:|
| `v4.6` | 33,3 → 37,6 | 1,8 → 1,9 Go | 152,9 → 176,5 | 0,7 → 0,5 Go |
| `v4.12-lite` | 24,9 → 25,8 | 2,7 → 2,9 Go | 105,3 → 107,1 | 0,8 → 0,6 Go |
| `v4.13-lite` | 24,6 → 26,3 | 2,8 → 2,9 Go | 107,1 → 112,7 | 0,7 → 0,8 Go |
| `v4.15-lite` | 26,8 → 35,1 | 2,6 → 1,7 Go | 119,4 → 148,2 | 0,7 → 0,5 Go |
| `v4.16-lite` | 27,0 → 35,4 | 2,6 → 1,7 Go | 119,4 → 148,2 | 0,7 → 0,4 Go |
| `v4.17-lite` | 26,9 → 32,2 | 2,6 → 2,0 Go | 116,5 → 138,7 | 0,8 → 0,5 Go |
| `v4.22-lite` | 24,3 → 31,4 | 3,4 → 2,1 Go | 100,8 → 125,7 | 1,0 → 0,6 Go |
| `v4.25-lite` | 23,6 → 26,1 | 3,8 → 2,3 Go | 92,3 → 93,8 | 1,1 → 0,7 Go |
| `v4.26` | 21,8 → 26,8 | 3,9 → 2,2 Go | 88,9 → 98,0 | 1,0 → 0,7 Go |
| `v4.25-heavy` | 18,4 → 25,4 | 3,9 → 2,3 Go | 78,7 → 97,2 | 1,4 → 0,8 Go |

```echarts
{
  title: { text: "VRAM requise par muxiveo-rife", subtext: "×2 · Normal vs Fast · 4K et 1080p · plus bas = mieux" },
  tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
  legend: { top: 52, data: ["4K Normal", "4K Fast", "1080p Normal", "1080p Fast"] },
  grid: { left: "3%", right: "4%", bottom: "3%", top: 110, containLabel: true },
  xAxis: { type: "category", axisLabel: { rotate: 30 }, data: ["v4.6", "v4.12-lite", "v4.13-lite", "v4.15-lite", "v4.16-lite", "v4.17-lite", "v4.22-lite", "v4.25-lite", "v4.26", "v4.25-heavy"] },
  yAxis: { type: "value", name: "Go" },
  series: [
    { name: "4K Normal", type: "bar", data: [1.77, 2.73, 2.84, 2.61, 2.6, 2.6, 3.39, 3.85, 3.85, 3.94] },
    { name: "4K Fast", type: "bar", data: [1.85, 2.88, 2.88, 1.68, 1.67, 1.98, 2.09, 2.26, 2.18, 2.28] },
    { name: "1080p Normal", type: "bar", data: [0.7, 0.82, 0.71, 0.68, 0.68, 0.76, 1.04, 1.12, 1.01, 1.43] },
    { name: "1080p Fast", type: "bar", data: [0.5, 0.62, 0.76, 0.46, 0.44, 0.47, 0.6, 0.71, 0.69, 0.84] },
  ]
}
```

:::success
Pour une carte de 6 à 8 Go : **v4.6** (1,8 Go en 4K, le plus rapide) ou **v4.15-lite / v4.16-lite en mode Fast** (1,7 Go en 4K, 35 i/s). En 1080p, tous les modèles tiennent largement ; v4.6 y dépasse 150 i/s.
:::

## Recommandations de Gemini : vérification

| Affirmation | Ce qu'on mesure (VMAF 24→60) | Verdict |
|---|---|---|
| **v4.25 / v4.26 = meilleure qualité** (films) | Avatar : v4.26 71,4 · v4.25 71,5, contre **v4.13 73,1** et **v4.6 72,7**. RPO : v4.26 61,2, contre **v4.6 62,5** | ❌ non confirmé sur deux films. Les gains annoncés (moins de tremblement sur les panoramiques) sont temporels et échappent au VMAF : revue visuelle nécessaire |
| **v4.13 / v4.9 pour l'animation** | Animation 3D : 70,7 / 70,7 contre **v4.6 72,0**. Anime 2D (Bleach) : 30,1 / 30,2, sous la moyenne (meilleurs : v4.17-lite 31,6, v4.22-lite 31,2) | ❌ non confirmé, ni en 3D stylisée ni en anime 2D (signal faible sur l'anime) |
| **v4.15-lite = roi performance/qualité (+30 à +50 %)** | Débit vs v4.26 : **+19 %** en 4K, **+34 %** en 1080p ; VMAF moyen 58,4 contre 58,6 | ⚠️ en partie : le gain annoncé n'est atteint qu'en 1080p. **v4.6 fait mieux** partout (+54 % en 4K, +72 % en 1080p, meilleure qualité) |

## Choix multiple de modèles dans Muxiveo

**Faisable facilement** : un modèle ncnn de plus = une entrée dans `native/muxiveo-rife/models.json` (sha256 épinglé, empaqueté par la CI), une ligne dans `INTERPOLATION_MODELS` et une entrée du menu RIFE. Aucun changement du moteur : les 17 modèles testés passent, y compris en mode Fast.

| Préréglage | Aujourd'hui | Proposé | Pourquoi |
|---|---|---|---|
| **Rapide** | v4.22-lite (24,8 i/s 4K · 3,6 Go) | **v4.6** (34 i/s · 1,8 Go) | meilleur VMAF moyen des 4 contenus, +38 % de vitesse, −1,8 Go de VRAM |
| **Équilibré** (défaut) | v4.26 | **v4.26**, puis **v4.6** si la revue visuelle le confirme | v4.6 est meilleur en mesures et 1,6 à 1,8× plus rapide en 24→60 ; seule inconnue : la stabilité temporelle, non mesurée |
| **Qualité** (ex-Max) | v4.25-heavy (18,5 i/s · 43 Mo) | **v4.13** (18 i/s · 11 Mo), ou suppression du préréglage | heavy n'est meilleur sur aucun contenu ; v4.13 n'est devant que sur film réel (Avatar) |
| **Petites cartes** | — | **v4.15-lite + mode Fast** (35 i/s · 1,7 Go en 4K) | VRAM la plus basse mesurée, qualité au niveau de v4.26 |
| **Animation** | — | pas de préréglage dédié | aucun modèle ne se détache ; l'interpolation d'un anime 2D crée des intervalles absents de l'original : à signaler dans l'infobulle plutôt qu'à « optimiser » |

Effet sur le paquet : modèles livrés **62 Mo → environ 38 Mo** (retrait de v4.25-heavy, 43 Mo).

:::info
**Prochaine étape suggérée** : revue visuelle A/B v4.6 / v4.26 sur 3 ou 4 plans en panoramique lent et détails fins (Avatar, RPO), plus un plan d'anime 2D. Si v4.26 n'y montre pas d'avantage visible, basculer le défaut sur v4.6.
:::

## Empreinte de déploiement

```echarts
{
  title: { text: "Empreinte disque de l'interpolation RIFE", subtext: "Échelle logarithmique · Mo" },
  tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
  legend: { top: 52 },
  grid: { left: "3%", right: "4%", bottom: "3%", top: 95, containLabel: true },
  xAxis: { type: "category", data: ["NVEncC (CUDA)", "NVEncC (TensorRT)", "Muxiveo"] },
  yAxis: { type: "log", name: "Mo" },
  series: [
    { name: "Binaire", type: "bar", label: { show: true, position: "top" }, data: [619, 619, 13] },
    { name: "Runtime IA (ORT, CUDA, cuDNN, TensorRT)", type: "bar", label: { show: true, position: "top" }, data: [2850, 9050, null] },
    { name: "Modèles RIFE", type: "bar", label: { show: true, position: "top" }, data: [201, 201, 62] }
  ]
}
```

:::warning
**Limites** : un seul GPU (RTX 4070 Ti SUPER) ; trois passages par contenu (49 à 121 images) ; VMAF et PSNR mesurent la fidélité image par image, pas les défauts temporels ; VMAF est peu adapté à l'anime 2D (même la duplication y obtient 30 sur 100). Les débits relatifs devraient se transposer à d'autres cartes, pas les valeurs absolues de VRAM.
:::


> Scripts et résultats bruts : `~/.cache/muxiveo-rife-dev/cmp/` (`phase3*.json`, `phase4.json`, `phase5.json`, `phase6.json`, `phase7.json`, `phase8.json` (référence duplication), `speed6.txt`).
