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
5. **Géométrie Dolby Vision à l'échelle 1:1** :
   - La conservation DV avec NVEncC n'autorise pas le rééchantillonnage dans ce workflow. Un resize effectif désactive explicitement la copie Dolby Vision ; le crop reste facultatif et aucun padding ou multiple de 32 DV n'est imposé. Le HDR statique disponible et l'option HDR10+ restent indépendants.
   - Un resize sans changement d'échelle est retiré avant l'alignement, afin de ne pas réétirer l'image après le crop/padding.
   - Les crops en pourcentage sont convertis en pixels source, puis alignés : chaque offset est pair et les dimensions finales sont multiples de 32. La même géométrie absolue est utilisée pour la vidéo et le RPU.
   - Avec des bandes communes aux échantillons (ou un crop utilisateur), le crop est ajusté au multiple de 32 inférieur. Sans bandes communes identifiées, le padding conserve le cadre source et atteint le multiple supérieur. Les bords ajoutés sont pairs, éventuellement asymétriques.
6. **Détection bornée dans le temps et réécriture des métadonnées** :
   - Pour une durée connue, le détecteur L5 cherche 12 images à six positions réparties entre le début et 90 % du film. Les clips de 30 secondes ou moins utilisent au plus trois positions, toujours à l'intérieur du fichier. Les seeks précèdent l'entrée FFmpeg et l'extraction se fait en copie de flux.
   - Seules les bandes communes aux échantillons sont proposées : minimum de chaque offset, y compris lorsqu'un échantillon est plein cadre. Une durée inconnue ou une sonde L5 incomplète n'autorise pas de crop DV automatique. L'échantillonnage reste une heuristique et peut manquer une variation brève entre deux positions.
   - Le bouton Auto-crop utilise L5 lorsqu'il est disponible, sinon FFmpeg cropdetect. Ce dernier prend également le minimum des bandes observées et démarre au début lorsque la durée est inconnue.
   - Au lancement effectif, le workflow extrait le RPU complet pour conserver ses plages d'images. `dovi_tool extract-rpu` accepte MKV/HEVC ; MP4, MOV, TS et les pistes explicitement sélectionnées passent d'abord par l'extraction HEVC Annex B de FFmpeg. `dovi_tool editor` reçoit toujours un fichier RPU, jamais le conteneur.
   - Pour tout crop/padding DV, `dovi_tool export --data level5` fournit les presets et leurs plages d'images. Chaque bord devient `max(0, ancien_offset - crop) + padding`. L'éditeur fonctionne en mode 0 : les autres niveaux, trims, mapping et le nombre/ordre des images sont conservés. Le RPU ainsi édité est transmis à NVEncC ; `crop=true` n'est pas ajouté car il annulerait les offsets variables restants.
   - NVEncC conserve sa lecture directe du conteneur pour l'encodage. Sans modification géométrique, la copie native `--dolby-vision-rpu copy` reste utilisée.
7. **Interface** :
   - Le passage à un resize effectif décoche Dolby Vision et affiche la raison dans le journal.
   - Le bouton Auto-crop renseigne les offsets en pixels et les aligne à 32 pour NVEncC + DV, à 2 sinon.
   - La confirmation avant encodage propose le crop/padding calculé. Le crop en pourcentage est matérialisé en pixels dans l'interface après acceptation.

---

## 5. Tableau Récapitulatif : Options Recommandées, Contraintes et Pièges Toxiques

| Paramètre / Pratique | Statut | Valeur / Règle | Impact et Justification Technique |
| :--- | :---: | :--- | :--- |
| **Alignement multiple de 32 (Crop vs Pad)** | 🟢 Automatisé | `Crop` si barres<br>`--vpp-pad` si plein écran | Évite le padding interne NVENC (`conformance_window`) qui fait retomber les SoC Android TV (Amlogic) en SDR. Aligne le crop à 32 ou ajoute des bandes paires + recalibrage RPU L5 par scène. |
| **`--dolby-vision-profile`** | 🟢 Recommandé | `8.1` | Normalise en Profil 8.1 universel (couche de base HDR10 compatible + métadonnées dynamiques RPU). |
| **`--dolby-vision-rpu`** | 🟢 Obligatoire | `copy` *(ou chemin fichier)* | Extrait, synchronise et réinjecte le RPU trame par trame dans le GPU via la bibliothèque `libdovi` intégrée à NVEncC. |
| **Double HDR (`HDR10+` + `Dolby Vision`)** | 🟢 Supporté | `--dhdr10-info copy` + `--dolby-vision-rpu copy` | Génère un flux hybride universel : les SEI HDR10+ (ITU-T T.35) et le RPU Dolby Vision coexistent sans conflit sur la base layer HDR10. Chaque téléviseur exploite automatiquement son format dynamique natif (DV sur LG/Sony, HDR10+ sur Samsung). |
| **RPU externe après crop/padding** | 🟢 Obligatoire | L5 ajusté par plage d'images | Évite la remise à zéro globale de `crop=true` ; conserve les bandes restantes et leurs variations par scène. |
| **`--profile`** | 🟢 Obligatoire | `main10` | Profil HEVC Main 10 obligatoire pour encoder en 10 bits (requis pour la compatibilité HDR10 / Dolby Vision). |
| **`--tier`** | 🟢 Recommandé | `high` | Requis en 4K UHD pour supporter les débits de pointe sans saturer les décodeurs matériels. |
| **Longueur de GOP (`--gop-len`)** | 🟢 Obligatoire | `2 × fps` *(ex: 48 à 24fps)* | Obligatoire pour borner le buffer DPB/RPU. Par défaut (250 trames / 10s), le décodeur matériel des téléviseurs sature, provoquant des gels au seeking et des timeouts `MediaCodec`. Les specs Dolby Vision imposent $\le 2\text{ s}$. |
| **`--repeat-headers`** | 🟢 Obligatoire | *Activé* | Répète VPS/SPS/PPS à chaque image clé (indispensable pour l'accroche HDMI, le seeking et la stabilité de lecture). |
| **`--aud`** | 🟢 Obligatoire | *Activé* | Insère les NAL 35 (Access Unit Delimiter) avant chaque trame, indispensables pour synchroniser le flux RPU et les images. |
| **VUI HDR (`--colormatrix`, etc.)** | 🟢 Obligatoire | `auto` ou `bt2020nc` / `bt2020` / `smpte2084` | Renseigne la VUI HDR. Sans cela, le téléviseur interprète le flux en SDR (image délavée, fade et terne). |
| **Positionnement chroma (`--chromaloc`)**| 🟢 Recommandé | `auto` ou `2` | Positionne le sous-échantillonnage chroma (Type 2 = aligné à gauche, standard UHD BD et streaming Web). |
| **FourCC Matroska (`dvvC` / `dvcC`)** | 🟢 Obligatoire | `dvvC` (Profils $> 7$)<br>`dvcC` (Profils $\le 7$) | Spécification Matroska formelle : `dvvC` est requis pour les profils $> 7$ (dont le Profil 8.1 réencodé), tandis que `dvcC` est impératif pour les profils $\le 7$ (ex: remux Profil 7 ou Profil 5). Utiliser `dvcC` sur un flux réencodé en Profil 8 fait échouer la détection sur Smart TV (LG webOS, Tizen...) qui retombent en HDR10 simple. *(Muxiveo sélectionne automatiquement le bon FourCC).* |
| **DoVi Level** | 🟢 Calibrage | `Level 6` ($\le 30$ fps) ou `9` ($50/60$ fps) | Évite le Niveau 10 par défaut de NVEncC qui fait planter le décodeur `MediaCodec` d'Android TV / ExoPlayer. Clamping automatique dans Muxiveo. |
| **`--dolby-vision-profile 10.x` en HEVC** | 🔴 Proscrit | *Ne jamais utiliser en HEVC* | Le profil 10 est exclusif au codec AV1. Le forcer sur du HEVC génère un flux corrompu rejeté par tous les décodeurs. |
| **Redimensionnement (`--vpp-resize`)** | 🟠 Sans copie DV | *Downscale autorisé, Dolby Vision désactivé* | Le workflow conserve DV uniquement à l'échelle 1:1. Sans DV, aucun alignement à 32 n'est imposé. |
| **Crop sans adaptation RPU** | 🔴 Proscrit | *Les offsets vidéo et L5 doivent correspondre* | Le workflow édite L5 par scène avec les mêmes offsets absolus que le crop vidéo. |
| **GOP Strict (`--strict-gop`)** | 🔴 Proscrit | *Ne pas activer* | Interdit à l'encodeur d'insérer des IDR sur les coupures de plan. Le RPU (`scene_refresh_flag = 1`) perd son alignement avec les images clés $\rightarrow$ décalages d'exposition et saccades. |
| **Pipeline FFmpeg `hevc_nvenc` + RPU** | 🔴 Proscrit | *Désactivé dans Muxiveo* | Absence de `libdovi`, padding matériel forcé (multiples 32/64 px) et désynchronisation DPB $\rightarrow$ écran noir ou repli HDR10 quasi-systématique. |
| **`-b_ref_mode middle` / `--b-pyramid`** | 🔴 Proscrit | *Sous FFmpeg hevc_nvenc* | Entrelacement pyramidal complexe sous FFmpeg qui réordonne les trames sans recalage du RPU externe $\rightarrow$ désynchronisation et rupture de buffer DPB. |

---

## 6. Évaluation Technique : Opportunité de débloquer le chemin FFmpeg `hevc_nvenc` ?

Une question légitime se pose : *peut-on et doit-on lever le blocage de `hevc_nvenc` dans FFmpeg maintenant que les règles d'alignement géométrique (multiples de 32) et de RPU sont maîtrisées ?*

### 6.1 Faisabilité théorique
En théorie, un pipeline multi-passes avec FFmpeg `hevc_nvenc` peut être construit :
1. **Passe 1 (Extraction RPU)** : `ffmpeg -i input.mkv -c:v copy -vbsf hevc_mp4toannexb -f hevc - | dovi_tool extract-rpu - -o rpu.bin`
2. **Passe 2 (Recalibrage RPU préalable)** : Si un crop est nécessaire, exécuter `dovi_tool editor` avec un fichier JSON pour écraser les coordonnées L5 (ou appliquer le padding de la Branche 2).
3. **Passe 3 (Encodage vidéo FFmpeg)** :
   `ffmpeg -i input.mkv -c:v hevc_nvenc -preset p7 -tune hq -profile:v main10 -pix_fmt p010le -g <2*fps> -strict_gop 1 -b_ref_mode 0 -aud 1 -vf "crop=..." intermediate.hevc`
4. **Passe 4 (Injection RPU)** : `dovi_tool inject-rpu -i intermediate.hevc --rpu-in rpu_edited.bin -o final.hevc`
5. **Passe 5 (Assemblage Matroska)** : Multiplexage MKV avec FourCC `dvvC` et assainissement Level 6/9.

### 6.2 Pourquoi ce chemin reste strictement déconseillé face à NVEncC

Malgré la faisabilité théorique ci-dessus, ce workflow présente des handicaps majeurs en production :

| Critère | NVEncC (`nvencc_hevc`) | FFmpeg (`hevc_nvenc`) + inject-rpu |
| :--- | :--- | :--- |
| **Intégration `libdovi`** | 🟢 RPU intégré au bitstream par NVEncC ; fichier RPU externe après correction géométrique. | 🔴 **Externe a posteriori** : Nécessite 5 étapes séquentielles et des outils séparés. |
| **Empreinte disque temporaire** | RPU et JSON si crop/padding ; extraction Annex B temporaire pour MP4/MOV/TS. Le MKV encodé reste un artefact intermédiaire du workflow. | 🔴 **Colossale (40 à 80 Go)** : Doit stocker le flux élémentaire `.hevc` brut intermédiaire pour un film UHD complet, usant inutilement les SSD. |
| **Vitesse d'exécution** | Une passe d'encodage matériel, précédée de l'extraction/édition du RPU si la géométrie change. | Étapes supplémentaires de démultiplexage et d'injection après encodage. |
| **Risque de désynchronisation VFR** | 🟢 **Protégé** : NVEncC refuse ou aligne nativement le timing. | 🔴 **Critique** : Si FFmpeg saute ou duplique une seule trame (drop/dup), le nombre de trames du fichier `.hevc` ne correspond plus à `rpu.bin` $\rightarrow$ échec fatal de `dovi_tool` ou clignotement / corruption colorimétrique du film. |
| **Gestion des B-frames & DPB** | 🟢 **Compatible B-pyramid** : NVEncC gère la table des références en synchronisation avec le RPU. | 🔴 **Très fragile** : Nécessite de désactiver B-pyramid (`-b_ref_mode 0`) sous peine de rupture de séquence DPB sur les Smart TV. |

### 6.3 Conclusion et Décision d'architecture
Débloquer le chemin `hevc_nvenc` pour Dolby Vision dans FFmpeg n'apporte **aucune valeur ajoutée** pour l'utilisateur par rapport à `nvencc_hevc`, tout en introduisant une complexité d'I/O disque et des risques élevés d'échec sur les cadences variables (VFR).  

**Recommandation finale** : Maintenir le blocage de `hevc_nvenc` pour Dolby Vision dans l'interface de Muxiveo avec la recommandation claire vers **`NVEncC (rigaya)`** pour l'accélération matérielle NVIDIA, ou vers **`libx265`** pour l'encodage logiciel CPU.
