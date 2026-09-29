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
4. **Signalisation dans le conteneur Matroska** :
   - Le conteneur doit obligatoirement déclarer `MaxBlockAdditionID = 1` et le FourCC `dvvC` (`0x64767643`) pour le Profil 8.1 (et non `dvcC` réservé aux profils $\le 7$).
   - L'ordre des NAL units sur les images clés doit respecter la norme ITU-T H.265 § 7.4.2.4.4 : `AUD (35) -> VPS (32) -> SPS (33) -> PPS (34) -> Prefix SEI (39) -> VCL Slices -> RPU (62)`.

---

## 2. Méthode 1 : NVEncC (rigaya) avec `libdovi` natif

Il s'agit de la méthode la plus robuste et la plus directe car `NVEncC` synchronise les métadonnées Dolby Vision en interne pendant l'encodage matériel.

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
- `--gop-len (2 × fps, ex: 48 à 24fps, 50 à 25fps, 100 à 50fps)` : Plafonne le cycle maximal d'images clés à 2 secondes pour éviter le débordement du tampon matériel (DPB/RPU) des téléviseurs, tout en laissant l'encodeur libre d'insérer des trames IDR adaptatives sur les changements de scène (pas de `--strict-gop` rigide).
- `--repeat-headers --aud` : Répète les en-têtes VPS/SPS/PPS à chaque image clé et insère les délimiteurs d'Access Unit.

---

## 3. Méthode 2 : Pipeline FFmpeg (`hevc_nvenc`) + `dovi_tool`

Si l'encodage est réalisé via FFmpeg, le flux doit être strictement calibré pour être compatible avec l'injection externe de `dovi_tool`.

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

## 4. Recommandations d'Intégration dans Muxiveo

Dans le module `core/workflows/encode/` :
1. **Sélection NVEncC native** : Quand `video.copy_dv` est actif et que l'utilisateur choisit le backend NVEncC, router directement l'argument `--dolby-vision-profile 8.1` et `--dolby-vision-rpu copy` à `_build_nvencc_command_runtime` au lieu de passer par une extraction/réécriture intermédiaire.
2. **Profil de contrainte FFmpeg** : Si l'utilisateur choisit l'encodeur FFmpeg NVENC pour du contenu HDR/DV, forcer automatiquement `-g 48`, `-forced-idr 1`, `-strict_gop 1`, `-aud 1` et interdire `-b_ref_mode middle`.
