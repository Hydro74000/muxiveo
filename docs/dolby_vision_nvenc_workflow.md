# Guide et Spécification Technique : Encodage NVENC et Dolby Vision dans Muxiveo

## 1. Contexte et Problématique

Lors du réencodage d'une source 4K HDR10 / Dolby Vision (notamment Blu-ray UHD Profil 7 ou 8) vers un format réencodé HEVC avec NVENC, la lecture sur téléviseurs (LG webOS, Sony Android TV, Philips...) et boîtiers multimédias (Nvidia Shield, Apple TV 4K, Fire TV Cube) via Plex peut échouer de deux façons :
- **Sur player externe** : Le bandeau d'information affiche « Dolby Vision », mais le téléviseur ne bascule pas et restitue un signal standard 8 bits SDR (« flux normal » sans logo DV ni HDR).
- **Sur l'application TV intégrée** : Le téléviseur rejette immédiatement le Dolby Vision et bascule en mode de repli HDR10.

### Causes techniques identifiées :
1. **Désalignement du Buffer de Décision (DPB) et du GOP** :
   - Le flux Blu-ray d'origine a un GOP court et fermé (~24 images / 1 seconde) avec des images clés `IDR_N_LP` (NAL 20). Les métadonnées dynamiques RPU (`scene_refresh_flag`, `vdr_rpu_id`) sont indexées sur cette cadence.
   - Par défaut, un encodage NVENC (FFmpeg ou NVEncC sans contrainte) utilise un GOP adaptatif très long (souvent 250 images / ~10,4 s) et insère des trames `IDR_W_RADL` (NAL 19) aux coupures de scène détectées.
2. **Impact de `-b_ref_mode middle`** :
   - La pyramide B-frame NVENC en mode `middle` réorganise les trames de référence selon une hiérarchie spécifique. Lorsque `dovi_tool inject-rpu` réinjecte naïvement le RPU dans ce flux, le moteur matériel Dolby Vision du téléviseur constate une rupture de séquence de référence et désactive le traitement DV.
3. **Absence des balises obligatoires (AUD & VUI)** :
   - Les Access Unit Delimiters (NAL 35, `-aud 1`) et les balises VUI complètes (BT.2020 / SMPTE ST 2084 / BT.2020nc) sont indispensables pour que le processeur vidéo délimite chaque image et valide la couche de base.
4. **Signalisation FourCC dans le conteneur Matroska** :
   - Le conteneur doit obligatoirement déclarer `MaxBlockAdditionID = 1` et le FourCC **`dvvC`** (`0x64767643`) pour le Profil 8.1 (et non `dvcC` strictement réservé aux profils $\le 7$).
   - **Symptôme Smart TV** : Si un flux Profil 8 est encapsulé avec le FourCC `dvcC`, les décodeurs de téléviseurs (LG webOS, Tizen, etc.) ne reconnaissent pas la configuration DV pour ce profil et basculent immédiatement en **mode de repli HDR10 simple**.
   - L'ordre des NAL units sur les images clés doit respecter la norme ITU-T H.265 § 7.4.2.4.4 : `AUD (35) -> VPS (32) -> SPS (33) -> PPS (34) -> Prefix SEI (39) -> VCL Slices -> RPU (62)`.
5. **Niveau Dolby Vision (Level) dans `BlockAddIDExtraData`** :
   - Par défaut, certains encodeurs matériels comme `NVEncC` écrivent un niveau maximal arbitraire : **Niveau 10** (0x55 = UHD @ 120 fps).
   - **Symptôme Android TV / ExoPlayer** : Sur Android (Plex, ExoPlayer, Nvidia Shield, Fire TV), le framework `MediaCodec` valide la contrainte `dv_level <= 9`. Face au Niveau 10, le lecteur plante ou refuse le traitement Dolby Vision.
   - **Assainissement requis** : Le niveau doit être ramené automatiquement au **Niveau 6** ($\le 30$ fps) ou **Niveau 9** ($50/60$ fps) selon la cadence d'images réelle du flux.
6. **Alignement matériel NVENC (multiples de 32 ou 64) et Redimensionnement (Resize / Crop)** :
   - Le hardware NVENC (blocs CTU HEVC 32x32 ou 64x64) impose des surfaces d'encodage dont les dimensions sont des multiples stricts de 32 ou 64 pixels.
   - **Pourquoi c'est fatal sous FFmpeg (`hevc_nvenc`)** :
     - FFmpeg n'a aucune intégration de `libdovi` et traite la vidéo comme une simple grille YUV brute.
     - Lorsqu'un filtre CUDA (`scale_cuda`, `crop`, `hwupload`) produit une résolution non alignée sur ces frontières matérielles, FFmpeg applique un **padding (rembourrage) matériel automatique** pour combler la trame codée (`coded_width` / `coded_height`).
     - Les métadonnées RPU (notamment le bloc **Level 5 / Active Area offsets** qui définit les bandes noires `top`, `bottom`, `left`, `right`) sont calculées au pixel près par rapport au canvas d'origine.
     - Comme l'injection du RPU avec `dovi_tool` a lieu *après coup* à l'aveugle, le RPU d'origine se retrouve injecté dans un flux vidéo aux dimensions physiques modifiées ou paddées.
     - **Conséquence** : Incohérence spatiale totale détectée par le téléviseur, provoquant un rejet du Dolby Vision (repli en HDR10 ou écran noir) ou une colorimétrie corrompue.
   - **Comment NVEncC (rigaya) résout ce problème** :
     - NVEncC intègre **nativement `libdovi`** au sein de son propre pipeline d'encodage.
     - Lors d'un crop, l'option `--dolby-vision-rpu-prm crop=true` indique à `libdovi` de recalculer et d'écraser automatiquement les offsets L5 à zéro pour correspondre à la nouvelle image rognée.
     - En revanche, pour un redimensionnement d'échelle (downscale 4K $\rightarrow$ 1080p), les courbes polynomiales et tables de saturation du RPU ayant été étalonnées pour la résolution 4K d'origine, la règle d'or reste de **conserver la résolution native 1:1**.

---

## 2. Méthode 1 : NVEncC (rigaya) avec `libdovi` natif

Il s'agit de la méthode recommandée et privilégiée dans Muxiveo car `NVEncC` synchronise les métadonnées Dolby Vision en interne pendant l'encodage matériel GPU.

### Commande de référence :
```bash
nvencc -i "source.mkv" \
  -c hevc \
  --profile main10 --tier high \
  --dolby-vision-profile 8.1 \
  --dolby-vision-rpu copy \
  --multipass 2pass-full \
  --lookahead 32 \
  --bframes 4 --b-pyramid \
  --ref 4 \
  --aq --aq-strength 8 --aq-temporal \
  --qvbr 24 --qp-max 32 \
  --gop-len 48 \
  --repeat-headers \
  --aud \
  --colorprim bt2020 --transfer smpte2084 --colormatrix bt2020nc --colorrange limited \
  --chromaloc 2 \
  -o "output_dv.mkv"
```

### Rôle des options clés :
- `--dolby-vision-profile 8.1` & `--dolby-vision-rpu copy` : Extrait et convertit automatiquement le RPU de la source en Profil 8.1 et l'insère trame par trame.
- `--dolby-vision-rpu-prm crop=true` : Si un recadrage (`--crop`) est spécifié, ordonne à `libdovi` de réécrire les offsets L5 de zone active à 0 pour éviter tout décalage géométrique sur le téléviseur.
- `--gop-len (2 × fps, ex: 48 à 24fps, 50 à 25fps, 100 à 50fps)` : Plafonne le cycle maximal d'images clés à 2 secondes pour éviter le débordement du tampon matériel (DPB/RPU) des téléviseurs, tout en laissant l'encodeur libre d'insérer des trames IDR adaptatives sur les changements de scène (pas de `--strict-gop` rigide).
- `--repeat-headers --aud` : Répète les en-têtes VPS/SPS/PPS à chaque image clé et insère les délimiteurs d'Access Unit.

### Assainissement post-encodage (`sanitize_dovi_mkv`) :
Même lorsque NVEncC gère nativement le RPU, le fichier MKV généré nécessite une passe d'assainissement automatique :
1. **Clamping du Niveau DoVi** : Ramène le Niveau 10 par défaut écrit par NVEncC au **Niveau 6** ($\le 30$ fps) ou **Niveau 9** ($50/60$ fps) pour rendre le fichier immédiatement compatible avec le décodeur `MediaCodec` d'Android TV et ExoPlayer.
2. **FourCC `dvvC`** : Enforce le FourCC canonique `dvvC` pour le Profil 8.1 dans le `BlockAddIDType` pour que les téléviseurs (LG webOS, Tizen) activent le pipeline Dolby Vision au lieu de basculer en HDR10 simple.
3. **`MaxBlockAdditionID = 1`** et recalcul du CRC-32 du bloc Tracks.

---

## 3. Méthode 2 : Pipeline FFmpeg (`hevc_nvenc`) + `dovi_tool` (Historique / Déprécié)

> ⚠️ **Note Muxiveo v4.1+** : L'encodage Dolby Vision avec `hevc_nvenc` via FFmpeg est **désactivé et bloqué dans l'UI** de Muxiveo avec une alerte explicite. Le buffer DPB de l'encodeur FFmpeg NVENC provoque des désynchronisations du RPU après injection externe, et le padding matériel automatique (surfaces multiples de 32/64) corrompt les offsets géométriques L5 du RPU. Il est vivement conseillé d'utiliser **`NVEncC — HEVC`** (GPU) ou **`x265`** (CPU).

Si ce pipeline doit néanmoins être utilisé manuellement :

### Étape 1 : Extraction & Normalisation du RPU
```bash
ffmpeg -i "source.mkv" -c:v copy -vbsf hevc_mp4toannexb -f hevc - | dovi_tool -m 2 extract-rpu - -o "RPU.bin"
```
> Le paramètre `-m 2` est capital : il convertit le RPU en Profil 8.1 standard et supprime les métadonnées de reconstruction Enhancement Layer (EL) incompatibles avec un réencodage simple couche.

### Étape 2 : Encodage FFmpeg `hevc_nvenc`
```bash
ffmpeg -i "source.mkv" \
  -c:v hevc_nvenc \
  -profile:v main10 -tier high \
  -multipass qres -rc-lookahead 32 -qmax 32 \
  -spatial-aq 1 -aq-strength 8 -temporal-aq 1 \
  -g 48 -forced-idr 1 -strict_gop 1 -no-scenecut 0 \
  -bf 3 -b_ref_mode 0 \
  -refs 4 \
  -color_primaries bt2020 -color_trc smpte2084 -colorspace bt2020nc -color_range tv \
  -aud 1 \
  -an -sn \
  "video_enc.hevc"
```

#### Différences critiques par rapport à un encodage standard :
- `-g 48 -forced-idr 1 -strict_gop 1` : Ferme impérativement les GOPs à 2 secondes et force des vrais IDR (pas d'IDR ouvert avec leading frames).
- `-b_ref_mode 0` (désactivé) : Évite l'entrelacement pyramidal complexe qui désynchronise le pointeur d'images de `dovi_tool`.
- `-aud 1` : NAL 35 avant chaque trame.

### Étape 3 : Réinjection du RPU
```bash
dovi_tool -m 2 inject-rpu -i "video_enc.hevc" -r "RPU.bin" -o "video_dv.hevc"
```

### Étape 4 : Assemblage Matroska
Le multiplexage dans Muxiveo applique les corrections de conteneur :
- FourCC `dvvC` (`0x64767643`) dans `BlockAddIDType`.
- `MaxBlockAdditionID = 1` (`0x55EE`).
- Ordonnancement canonique des NALs (VPS/SPS/PPS avant les SEI prefix).

---

## 4. Architecture et Implémentation dans Muxiveo (v4.1+)

Dans le module `core/workflows/encode/` :
1. **Routage NVEncC natif (`nvencc_direct`)** : Quand `video.copy_dv` est actif et que l'utilisateur choisit le backend NVEncC, la commande injecte directement `--dolby-vision-profile 8.1` et `--dolby-vision-rpu copy`. Le calcul du GOP est dynamique (`--gop-len = 2 × fps`).
2. **Sécurité et blocage des codecs incompatibles** : Le catalogue (`catalog.py`) sépare désormais explicitement `supports_dovi` et `supports_hdr10plus`. Les encodeurs incompatibles (`hevc_nvenc`, `hevc_vaapi`, `nvencc_av1`...) ont leur case DV désactivée avec un bandeau d'avertissement et une recommandation vers `nvencc_hevc` ou `libx265`.
3. **Assainissement automatique post-encode** : La fonction `sanitize_dovi_mkv` s'exécute automatiquement après l'encodage NVEncC pour corriger le niveau (Level 6/9), standardiser le FourCC en `dvvC` et valider les blocs EBML.
4. **Assemblage Matroska natif unifié** : L'assemblage final est pris en charge par le muxeur natif Matroska (`compile_assembly_plan`), qui préserve intégralement les éléments `Colour` (`0x55B0`) et `BlockAdditionMapping` sans dépendre d'un remux FFmpeg secondaire.
5. **Préservation stricte de la géométrie (Pas de resize/crop sans adaptation RPU)** : En Dolby Vision, la résolution géométrique et les bandes noires d'origine doivent être strictement conservées en 1:1. L'application d'un redimensionnement ou d'un rognage (crop) sans réécriture mathématique du RPU (notamment des coordonnées L5) brise le tone-mapping matériel des téléviseurs.
