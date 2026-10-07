# Notes de version / Release Notes — Muxiveo v4.2.2

* [Français](#version-française)
* [English](#english-version)

---

<a name="version-française"></a>
# Version Française

*Journal des modifications depuis la dernière version stable de `main` (v4.2.1).*

---

## 1. Fonctionnalités

### Nouveautés / Suppressions
* **Dolby Vision sur les encodeurs HEVC matériels** : copie Dolby Vision pour `hevc_nvenc`, `hevc_qsv`, `hevc_vaapi` et `hevc_amf`. Le RPU est réinjecté par `dovi_tool`, et le niveau L5 réaligné sur le recadrage.
* **NVENC et plein cadre UHD** : NVENC code par blocs de 32 lignes, ce qui porte 2160 à 2176 lignes codées, un format refusé par les décodeurs Dolby Vision. Le padding est remplacé par une détection de ce débordement, avec deux choix proposés : recadrer à 2144 lignes (bandes alignées sur 32), ou passer sur un autre encodeur détecté (x265, QSV, VAAPI ou AMF).
* **Politique Dolby Vision unifiée** :
  * en Copy, l'image n'est jamais convertie ;
  * « Normaliser » est refusé sur P5, P8.4 et P8.2, et les profils de sortie P8.4 et P8.2 sont conservés ;
  * une source P5 réencodée est convertie en HDR10, par libplacebo dans FFmpeg ou nativement par NVEncC ;
  * son RPU est converti en P8.1, et ses métadonnées HDR10 statiques sont estimées depuis le RPU.
* **Onglet Video** :
  * modes de débit propres à chaque codec (NVENC, AMF, QSV, VAAPI, NVEncC) ;
  * taille cible portant sur le fichier complet, avec un budget dont chaque poste indique sa provenance (mesuré, estimé ou inconnu) ;
  * profondeur Auto / 8 / 10 bits ;
  * double passe réservée à x264, x265 et SVT-AV1.
* **NLMeans sur GPU (Vulkan)** : `nlmeans_vulkan` en 10 bits sur GPU dédié, avec le badge « Filtres GPU » au tableau de bord et un repli CPU signalé.
* **VAAPI** :
  * détection du pilote (Mesa ou Intel) et presets `-compression_level` libellés ;
  * sur Mesa (AMD), le niveau est un masque de bits (preset, pré-encodage, VBAQ). Le défaut passe à 13 (« Quality + pré-encodage »), au lieu du preset le plus rapide.
* **AMF** : paramètres avancés `preencode`, `vbaq`, `aq_mode` (AV1), préanalyse (AV1) et usages `high_quality` / `lowlatency_high_quality`.
* **NVEncC** :
  * tone mapping par `--vpp-libplacebo-tonemapping` ;
  * filtres YADIF et NLMeans calés sur FFmpeg ;
  * le moteur réel des filtres est indiqué dans l'aperçu et les badges.
* **VBR à 0 kbps (illimité)** pour NVENC et NVEncC.
* **Progression du muxage Matroska natif** affichée dans l'interface, y compris dans le panneau Merge DoVi.
* **Presets d'encodage** : liste filtrée par l'encodeur réellement présent dans FFmpeg (`ffmpeg -h encoder=`). Ajout de `high_quality` (av1_amf) et du preset 13 (SVT-AV1) ; un preset inconnu est refusé ou signalé.

### Améliorations / Fix
* **Intégrité HDR** :
  * la sortie suit le transfert de la source (PQ ou HLG) ;
  * HDR10 décoché retire bien les métadonnées MDCV et CLL de la source ;
  * le marquage VUI est appliqué par `setparams` (FFmpeg ≥ 7 ignore `-color_*` en sortie) ;
  * les sondes HDR visent la piste concernée, et non la première.
* **Paramètres avancés** :
  * mémorisés par codec, avec contrôle de syntaxe ;
  * ajoutés en dernier, ils l'emportent sur l'onglet Video, avec un avertissement ;
  * seules les options incompatibles sont retirées, et le retrait est signalé.
* **Aperçu** : préparation « à blanc » qui prend les mêmes décisions que le lancement. Les arguments sont protégés pour POSIX et `cmd.exe`, y compris dans l'aperçu remux.
* **FFprobe configuré respecté** pour toutes les sondes d'encodage. Les sondes ont un délai maximal et sont annulables ; une expiration n'est plus confondue avec une absence d'HDR.
* **Extraction Dolby Vision et HDR10+ fiabilisée** sur les MKV dont la table d'index (SeekHead) n'est pas lisible par `dovi_tool` et `hdr10plus_tool` (lecture en Annex B par FFmpeg).
* **Profils** : identifiés par leur nom exact, écrits de façon atomique, et un profil illisible est signalé au lieu d'être ignoré.
* **Documents JSON de la CLI** :
  * UTF-8 avec ou sans BOM, `NaN` et `Infinity` refusés ;
  * chemins relatifs résolus depuis le dossier du document ;
  * timecodes de chapitres validés ;
  * énumérations partagées entre le schéma et la validation.
* **MediaManager** : arbre, tailles et statut exacts après une suppression partielle.
* **Correctifs divers** :
  * NVENC en mode qualité : `-b:v 0` (pas de plafond implicite à 2 Mb/s) ;
  * paramètres avancés libx264 transmis ;
  * durée du job prise sur la piste vidéo principale ;
  * conversion de profondeur QSV faite en logiciel.

---

## 2. Performances

### Nouveautés / Suppressions
* **Débruitage NLMeans sur GPU** (Vulkan), au lieu du CPU, très lent en 4K.

### Améliorations / Fix
* **Décodage matériel conservé** quand la profondeur ne change pas, et conversion de profondeur sur GPU (`scale_cuda`, `scale_vaapi`) quand le filtre existe.
* **Caches de sondes** invalidés seulement quand FFprobe ou MediaInfo change.

---

## 3. Sécurité

### Nouveautés / Suppressions
* **Sorties finales protégées** :
  * chaque sortie (remux, encodage, Merge DoVi) est écrite dans un fichier candidat unique, `<nom>.<aléa>.mkv.partial`, le seul supprimé en cas d'échec ;
  * la destination est verrouillée pendant le job et publiée sans écraser un fichier apparu entre-temps.
* **Dossiers de travail** : un verrou système protège les traitements en cours. Un job actif (autre instance GUI ou CLI) n'est jamais nettoyé.
* **Batch, profil batch et hybride** : toute collision de sortie est refusée avant le premier job, même avec `--force`.
* **Téléchargements vérifiés** :
  * FFmpeg et muxiveo-rife sont contrôlés par leur SHA-256 publié ;
  * la provenance de MediaInfo est consignée ;
  * chaque bundle « tout inclus » contient un `tools-manifest.json`.

### Améliorations / Fix
* **muxiveo-rife 1.2.3** :
  * en-têtes y4m et options numériques lus strictement, avec bornes sur les dimensions et la cadence (un dépassement d'entier n'est plus possible) ;
  * tests sous UBSan et ASan.
* **Dépendances Python** : `requirements.txt` est la source unique. Les versions sont vérifiées et seules les dépendances absentes ou trop anciennes sont installées.

---

## 4. Autres (Interface, Packaging, CI, Documentation, i18n)

### Nouveautés / Suppressions
* **CI** :
  * suite complète bloquante : Ruff, Mypy, tous les tests sous Linux, contrats Windows avec Qt réel ;
  * la publication dépend de cette suite sur le même commit.
* **Documentation** : workflow Dolby Vision avec NVEncC, règles des documents JSON et des sorties de batch de la CLI.

### Améliorations / Fix
* **Traductions** mises à jour pour toutes les nouveautés.
* **Code mort retiré** (patch NVENC statique expérimental, anciens utilitaires NVEncC).
* **Réglages `hdr.dovi_profile` / `dovi_compat_id`** signalés comme inutilisés dans les Paramètres.

---
---

<a name="english-version"></a>
# English Version

*Changelog since the last stable release on `main` (v4.2.1).*

---

## 1. Features

### New Features / Removals
* **Dolby Vision on hardware HEVC encoders**: Dolby Vision copy for `hevc_nvenc`, `hevc_qsv`, `hevc_vaapi` and `hevc_amf`. The RPU is reinjected with `dovi_tool`, and its L5 level is realigned to the crop.
* **NVENC and full-frame UHD**: NVENC codes in 32-line blocks, which turns 2160 into 2176 coded lines, a size Dolby Vision decoders reject. Padding is replaced by detection of this overflow, with two options offered: crop to 2144 lines (bars aligned to 32), or switch to another detected encoder (x265, QSV, VAAPI or AMF).
* **Unified Dolby Vision policy**:
  * Copy never converts the picture;
  * "Normalise" is refused on P5, P8.4 and P8.2, and P8.4 / P8.2 output profiles are kept;
  * a re-encoded P5 source is converted to HDR10, through libplacebo in FFmpeg or natively by NVEncC;
  * its RPU is converted to P8.1, and its static HDR10 metadata is estimated from the RPU.
* **Video tab**:
  * rate control modes specific to each codec (NVENC, AMF, QSV, VAAPI, NVEncC);
  * target size covers the whole file, with a budget where each item shows its origin (measured, estimated or unknown);
  * Auto / 8 / 10-bit depth;
  * two-pass encoding limited to x264, x265 and SVT-AV1.
* **GPU NLMeans (Vulkan)**: 10-bit `nlmeans_vulkan` on a dedicated GPU, with a "GPU filters" dashboard badge and a reported CPU fallback.
* **VAAPI**:
  * driver detection (Mesa or Intel) and labelled `-compression_level` presets;
  * on Mesa (AMD), the level is a bitmask (preset, pre-encode, VBAQ). The default becomes 13 ("Quality + pre-encode") instead of the fastest preset.
* **AMF**: advanced `preencode`, `vbaq`, `aq_mode` (AV1) and pre-analysis (AV1) parameters, plus `high_quality` / `lowlatency_high_quality` usages.
* **NVEncC**:
  * tone mapping through `--vpp-libplacebo-tonemapping`;
  * YADIF and NLMeans filters matched to FFmpeg;
  * the actual filter engine is shown in the preview and badges.
* **VBR at 0 kbps (unlimited)** for NVENC and NVEncC.
* **Native Matroska mux progress** shown in the UI, including the Merge DoVi panel.
* **Encoder presets**: list filtered by the encoder actually present in FFmpeg (`ffmpeg -h encoder=`). Added `high_quality` (av1_amf) and preset 13 (SVT-AV1); an unknown preset is refused or reported.

### Improvements / Fixes
* **HDR integrity**:
  * output follows the source transfer (PQ or HLG);
  * unchecked HDR10 now strips the source MDCV and CLL metadata;
  * VUI tagging is applied through `setparams` (FFmpeg ≥ 7 ignores output `-color_*`);
  * HDR probes target the relevant track, not the first one.
* **Advanced parameters**:
  * remembered per codec, with syntax checks;
  * appended last, they override the Video tab, with a warning;
  * only incompatible options are removed, and the removal is reported.
* **Preview**: dry-run preparation making the same decisions as the actual run. Arguments are quoted for POSIX and `cmd.exe`, including the remux preview.
* **Configured FFprobe honoured** by every encoding probe. Probes have a time limit and can be cancelled; a timeout is no longer mistaken for a missing HDR.
* **More reliable Dolby Vision and HDR10+ extraction** from MKV files whose index (SeekHead) `dovi_tool` and `hdr10plus_tool` cannot follow (Annex B read through FFmpeg).
* **Profiles**: identified by their exact name, written atomically, and an unreadable profile is reported instead of being ignored.
* **CLI JSON documents**:
  * UTF-8 with or without BOM, `NaN` and `Infinity` refused;
  * relative paths resolved from the document's folder;
  * chapter timecodes validated;
  * enumerations shared between schema and validation.
* **MediaManager**: tree, sizes and status stay accurate after a partial deletion.
* **Miscellaneous fixes**:
  * NVENC quality mode: `-b:v 0` (no implicit 2 Mb/s cap);
  * libx264 advanced parameters passed through;
  * job duration taken from the primary video track;
  * QSV depth conversion done in software.

---

## 2. Performance

### New Features / Removals
* **GPU NLMeans denoising** (Vulkan) instead of the CPU, which is very slow in 4K.

### Improvements / Fixes
* **Hardware decoding kept** when the bit depth does not change, and depth conversion on the GPU (`scale_cuda`, `scale_vaapi`) when the filter exists.
* **Probe caches** invalidated only when FFprobe or MediaInfo changes.

---

## 3. Security

### New Features / Removals
* **Protected final outputs**:
  * each output (remux, encode, Merge DoVi) is written to a unique candidate file, `<name>.<random>.mkv.partial`, the only file removed on failure;
  * the destination is locked for the job and published without overwriting a file that appeared meanwhile.
* **Work folders**: an OS lock protects running jobs. An active job (another GUI or CLI instance) is never cleaned up.
* **Batch, batch profile and hybrid**: any output collision is refused before the first job, even with `--force`.
* **Verified downloads**:
  * FFmpeg and muxiveo-rife are checked against their published SHA-256;
  * MediaInfo provenance is recorded;
  * every all-inclusive bundle contains a `tools-manifest.json`.

### Improvements / Fixes
* **muxiveo-rife 1.2.3**:
  * y4m headers and numeric options parsed strictly, with bounds on dimensions and frame rate (integer overflow no longer possible);
  * tests under UBSan and ASan.
* **Python dependencies**: `requirements.txt` is the single source. Versions are checked and only missing or outdated dependencies are installed.

---

## 4. Other (UI/UX, Packaging, CI, Documentation, i18n)

### New Features / Removals
* **CI**:
  * full blocking suite: Ruff, Mypy, all tests on Linux, Windows contracts with real Qt;
  * publishing depends on this suite for the same commit.
* **Documentation**: Dolby Vision workflow with NVEncC, CLI JSON document and batch output rules.

### Improvements / Fixes
* **Translations** updated for all new features.
* **Dead code removed** (experimental static NVENC patch, legacy NVEncC helpers).
* **`hdr.dovi_profile` / `dovi_compat_id` settings** flagged as unused in Settings.
