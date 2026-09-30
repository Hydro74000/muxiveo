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

> ⛔ **Statut dans Muxiveo v4.1+ : BLOQUÉ ET DÉSACTIVÉ DANS L'UI**  
> L'encodage Dolby Vision avec le codec `hevc_nvenc` de FFmpeg est formellement bloqué dans Muxiveo (case à cocher désactivée avec pictogramme d'alerte `⚠️`).  
> Ce pipeline externe accumule des **impasses techniques majeures** qui rendent son utilisation imprévisible et provoquent quasi-systématiquement des échecs de lecture sur téléviseur (écran noir, artefacts ou repli forcé en HDR10).

### Pourquoi ce pipeline est-il une impasse technique ?

#### 1. Le blocage fatal du Crop (rognage des bandes noires) et du Resize
* **Incohérence du bloc Level 5 (Active Area)** :
  * Le RPU extrait à l'étape 1 conserve les coordonnées géométriques complètes de la source (ex. $3840\times 2160$ avec $276$ pixels de bandes noires en haut et en bas dans le bloc L5).
  * Si un crop est appliqué dans FFmpeg (ex. `-vf crop=3840:1608:0:276`), la vidéo encodée ne fait plus que $1608$ pixels de haut.
  * L'injection aveugle avec `dovi_tool inject-rpu` applique alors des offsets L5 d'une image avec bandes noires sur une image qui n'en a plus, ou échoue en constatant la discordance de résolution.
* **Le piège de l'alignement matériel NVENC (multiples de 32 ou 64)** :
  * L'encodeur matériel NVENC sous FFmpeg impose que les surfaces mémoires allouées soient des **multiples stricts de 32 ou 64 pixels**.
  * Si la hauteur croppée n'est pas un multiple exact (ex: $1606$ ou $1610$ px), FFmpeg et le driver NVENC appliquent un **padding (rembourrage) matériel automatique** pour atteindre la frontière matérielle supérieure ($1632$ px).
  * Ce padding altère la trame réelle encodée, rompant toute corrélation spatiale avec les métadonnées de luminosité et de tone-mapping du RPU.
* **Complexité d'un contournement manuel** :
  * Pour fonctionner, il faudrait extraire le RPU, écrire un script JSON complexe pour `dovi_tool editor`, recalculer manuellement les offsets L5 de chaque plan, s'assurer que la résolution résultante tombe au pixel près sur un multiple matériel de 32/64, puis réinjecter le tout. Ce processus est fragile, non automatisable universellement et source constante de rejets TV.

#### 2. La rupture du Buffer de Décision (DPB) et l'instabilité des B-frames
* Même en forçant un GOP fermé (`-strict_gop 1`) et en désactivant le mode pyramidal (`-b_ref_mode 0`), l'encodeur FFmpeg NVENC gère la hiérarchie de décodage des trames B de façon autonome dans le silicium NVIDIA.
* Lorsque `dovi_tool inject-rpu` réinsère les trames RPU dans le flux élémentaire `.hevc`, le pointeur d'images de décodage (POC) diverge fréquemment de l'ordre d'affichage (PTS).
* Sur les téléviseurs (notamment les SoC MediaTek et Amlogic des Smart TV LG, Philips, Sony), le processeur matériel Dolby Vision détecte une rupture de continuité dans le buffer DPB et désactive instantanément le traitement DV.

#### 3. Risque de désynchronisation de trames (Frame Drop / VFR)
* Le RPU extrait est strictement indexé trame par trame.
* Si le décodage FFmpeg subit la moindre duplication ou omission de trame (par exemple lors d'une conversion de cadence VFR $\rightarrow$ CFR ou d'un filtre temporel), le nombre total de trames du fichier `.hevc` devient différent de celui de `RPU.bin`.
* `dovi_tool inject-rpu` échoue immédiatement avec une erreur de désalignement de trames, ou décale progressivement toutes les scènes du film.

---

### Conclusion sur le pipeline FFmpeg externe :
Tenter de réencoder du Dolby Vision en dissociant l'encodage vidéo (FFmpeg `hevc_nvenc`) et l'injection du RPU (`dovi_tool`) est un **modèle intrinsèquement vicié** pour du matériel GPU NVENC : l'encodeur matériel ne peut pas synchroniser ses décisions de compression (références DPB, padding, surfaces) avec les métadonnées Dolby Vision injectées a posteriori en aveugle.

C'est pourquoi Muxiveo a formellement **abandonné et bloqué ce pipeline** dans son interface au profit de **`NVEncC (rigaya)`** (qui intègre `libdovi` au cœur même de la boucle d'encodage GPU) et de **`libx265`** (en mode CPU logiciel).

---

## 4. Architecture et Implémentation dans Muxiveo (v4.1+)

Dans le module `core/workflows/encode/` :
1. **Routage NVEncC natif (`nvencc_direct`)** : Quand `video.copy_dv` est actif et que l'utilisateur choisit le backend NVEncC, la commande injecte directement `--dolby-vision-profile 8.1` et `--dolby-vision-rpu copy`. Le calcul du GOP est dynamique (`--gop-len = 2 × fps`).
2. **Sécurité et blocage des codecs incompatibles** : Le catalogue (`catalog.py`) sépare désormais explicitement `supports_dovi` et `supports_hdr10plus`. Les encodeurs incompatibles (`hevc_nvenc`, `hevc_vaapi`, `nvencc_av1`...) ont leur case DV désactivée avec un bandeau d'avertissement et une recommandation vers `nvencc_hevc` ou `libx265`.
3. **Assainissement automatique post-encode** : La fonction `sanitize_dovi_mkv` s'exécute automatiquement après l'encodage NVEncC pour corriger le niveau (Level 6/9), standardiser le FourCC en `dvvC` et valider les blocs EBML.
4. **Assemblage Matroska natif unifié** : L'assemblage final est pris en charge par le muxeur natif Matroska (`compile_assembly_plan`), qui préserve intégralement les éléments `Colour` (`0x55B0`) et `BlockAdditionMapping` sans dépendre d'un remux FFmpeg secondaire.
5. **Préservation stricte de la géométrie (Pas de resize/crop sans adaptation RPU)** : En Dolby Vision, la résolution géométrique et les bandes noires d'origine doivent être strictement conservées en 1:1. L'application d'un redimensionnement ou d'un rognage (crop) sans réécriture mathématique du RPU (notamment des coordonnées L5) brise le tone-mapping matériel des téléviseurs.
