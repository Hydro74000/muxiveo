# Qualification MVTools — 4 octobre 2026

Intégration sur `devel-mvtools`, créée depuis `devel-cli`, publiée et suivie sur
`origin/devel-mvtools`. RIFE reste le moteur par défaut. Le choix MVTools propose
Standard et Lent UHD (CPU), sans modifier les paramètres RIFE enregistrés.

## Versions et méthode

Wrapper 1.0.0 ; VapourSynth R80 ; MVTools v29_2 ; zimg 3.0.6 ; FFTW float 3.3.11.
Versions, révisions et SHA-256 : `native/muxiveo-mvtools/dependencies.json`, puis
`mvtools-runtime/manifest.json` dans chaque paquet. Construction native sans
bindings Python. Aucune variante CPU n’impose AVX2 au binaire baseline.

Machine locale : AMD Ryzen 7 7800X3D 8-Core Processor, 16 CPU logiques, Linux x86_64/Fedora 44.
RIFE épinglé 1.2.1, modèle v4.6, mode normal sans TTA, GPU RTX 4070 Ti SUPER.
Archive officielle Linux de la release `muxiveo-rife-v1.2.1`.

Banc synthétique 256×144, 10 bits, 24 images sources à 24 i/s, sorties à 60 i/s.
Référence à 120 i/s (source = une image sur cinq, cible = une image sur deux).
Les dernières sorties prolongent la dernière source, comme le contrat du moteur.
Les modes MVTools reçoivent un budget de 4 threads (Standard 2, UHD 4).
Les scènes couvrent barreaux, grilles, escaliers, panoramique, zoom et occultation.
Les coupes sont désactivées pour mesurer l’interpolation ; un test dédié couvre
les coupes, frontières de fenêtres et flashes avec le seuil par défaut.

La MAE est exprimée sur une échelle Y de 8 bits. Les JSON fournissent aussi
l’écart des gradients horizontaux, la proportion de contours forts éloignés de
la référence (>2 pixels), la variation temporelle de l’erreur, le décalage
horizontal résiduel et les répétitions exactes. Les mesures de contours et de
clignotement sont des **indicateurs**, pas une qualification visuelle de films.
Le décalage horizontal est estimé uniquement sur les mouvements compatibles,
sans zoom/occultation. Les répétitions comprennent le prolongement final attendu.

## Qualité mesurée

| Scène | RIFE v4.6 MAE | MVTools Standard MAE | MVTools Lent UHD MAE |
|---|---:|---:|---:|
| bars | 3.074 | 8.501 | 4.873 |
| grid | 2.878 | 3.450 | 3.244 |
| stairs | 3.495 | 10.554 | 4.458 |
| pan | 0.348 | 0.682 | 0.353 |
| zoom | 9.363 | 3.250 | 14.652 |
| occlusion | 3.144 | 4.272 | 5.285 |

UHD améliore Standard sur barreaux, grilles, escaliers et panoramique dans ce
banc. Standard est meilleur sur le zoom et l’occultation. RIFE a la MAE la plus
faible sur ces six séquences ; cela ne permet pas d’affirmer que MVTools élimine
les dédoublements/clignotements des films concernés. Les motifs périodiques
restent ambigus lorsque le déplacement ressemble à plusieurs périodes.
La réduction des défauts sur les extraits réels demeure à vérifier sur le master
et la conversion 60 i/s de référence, qui n’ont pas été fournis à ce banc.

Résultats détaillés : [comparatif synthétique Linux](mvtools/2026-10-04-linux-visual.json).

## Ressources mesurées en 4K 10 bits

Barreaux 3840×2160, 24→60, 16 images sources, 40 images produites, budget 16 threads.
Le RSS concerne le moteur seul ; FFmpeg et l’encodeur ajoutent leur propre coût.
La génération de la source n’est pas incluse dans les durées.

| Mode | Threads | Durée | Débit sortie | Pic RSS |
|---|---:|---:|---:|---:|
| Standard | 4 | 15,84 s | 2,525 i/s | 1,915 Gio |
| Lent UHD | 16 | 177,76 s | 0,225 i/s | 3,643 Gio |

La réservation du contrôleur d’encodages parallèles ajoute **2 Gio** en Standard
et **6 Gio** en UHD à l’estimation de l’encodeur. Cette marge couvre le pic local
observé ; ce n’est pas un plafond RAM. Les caches VapourSynth sont limités à
512 Mio/1 Gio, avec au plus 8 graphes de phase et une fenêtre active.
Les E/S synchrones n’ajoutent aucune file d’images. L’entrée conserve au plus
8 images utiles et les 4 gardes, réutilisées entre fenêtres.

[Mesure 16 images](mvtools/2026-10-04-linux-4k-16frames.json) ;
[mesure courte, 4 images](mvtools/2026-10-04-linux-4k-short.json).
Le test de durée compare également 32 et 512 images et tolère au plus 64 Mio
de croissance RSS supplémentaire : il passe sur Linux.

## Poids et plateformes

**Modèles supplémentaires : 0 Mio sur toutes les plateformes.**
La matrice CI a réussi sur les trois plateformes :
[run de qualification](https://github.com/Hydro74000/muxiveo/actions/runs/37231307296).
Chaque plateforme a passé les **63 tests natifs**, l’autotest isolé, le banc
synthétique et la mesure 4K 10 bits. Linux a aussi passé le contrôle glibc ≤2.28
sur tout le runtime et le rendu QEMU Nehalem sans AVX2.

| Paquet CI | Octets installés | Mio installés | Octets compressés | Mio compressés |
|---|---:|---:|---:|---:|
| linux-x86_64 | 24,842,487 | 23.69 | 8,197,743 | 7.82 |
| windows-x86_64 | 11,948,926 | 11.40 | 4,160,245 | 3.97 |
| macos-arm64 | 8,206,636 | 7.83 | 2,847,307 | 2.72 |

L’archive des sources est séparée et exclue du poids installé.

Les artefacts CI contiennent, par plateforme, `*-sizes.json`,
`*-quality.json`, `*-4k-resources.json`, l’archive native et ses licences.
Linux est construit sous manylinux_2_28, contrôlé pour chaque bibliothèque,
et testé sur CPU Nehalem sans AVX2 via QEMU. Windows est x86_64/MSVC avec runtime
C statique ; macOS est arm64 avec cible de déploiement 12.

Mesures des runners CI (4 images sources, 10 images produites, 24→60,
budget 4 threads, 3840×2160 10 bits ; pas comparables au banc local 16 images) :

| Plateforme | Mode | Threads | Durée | Débit sortie | Pic RSS |
|---|---|---:|---:|---:|---:|
| linux-x86_64 | mvtools-standard | 2 | 6.57 s | 1.523 i/s | 1504.0 Mio |
| linux-x86_64 | mvtools-uhd | 4 | 50.78 s | 0.197 i/s | 2938.7 Mio |
| windows-x86_64 | mvtools-standard | 2 | 4.66 s | 2.147 i/s | 1376.0 Mio |
| windows-x86_64 | mvtools-uhd | 4 | 31.80 s | 0.315 i/s | 2347.5 Mio |
| macos-arm64 | mvtools-standard | 2 | 6.11 s | 1.636 i/s | 1363.5 Mio |
| macos-arm64 | mvtools-uhd | 4 | 47.25 s | 0.212 i/s | 2586.8 Mio |

Les JSON de tailles, qualité et ressources sont conservés sous
[`mvtools/`](mvtools/). Les archives natives ont été vérifiées isolément dans
chaque job ; le paquet Linux glibc 2.28 a aussi été extrait et rendu localement.
L’archive des sources a été reconstruite depuis un autre dossier sans bindings
Python dans le paquet, et le hash du rendu d’autotest est identique.

## Contrôles effectués localement

- 63 tests natifs : cadences ×2/×3/×4 et fractionnaires, ceil(N×r), sources 1–2
  images, copie exacte aux timestamps communs, profondeur 8–16 bits en
  420/422/444, dimensions non multiples de blocs, coupes/flashes/frontières,
  fenêtres 8/16 identiques, erreurs, runtime absent, isolation, fermeture
  encodeur, annulation et durée/RSS.
- 12 encodages réels : RIFE + MVTools Standard/UHD, assemblages FFmpeg et natif,
  audio conservé et retard vidéo +400 ms conservé.
- Tests de profils, paramètres indépendants, interfaces, progression, choix
  explicite de moteur et construction NVEncC ; compte HDR fractionnaire partagé.
- 352 tests ciblés initiaux et 296 tests du workflow ; contrôles supplémentaires
  modèles/encodeurs/i18n, réservation mémoire, empreintes d’installation et
  conservation des champs RIFE Light/historiques après les dernières corrections.

La copie et l’expansion DoVi/HDR10+ utilisent les règles existantes, sans
modification du calendrier des métadonnées. Le banc n’utilise pas de film HDR
commercial ni de GPU NVENC pour le rendu final : NVEncC est vérifié par la
construction de sa chaîne de commandes et le runner commun.

## Reproduction

```sh
python native/muxiveo-mvtools/scripts/build.py --jobs 4
MUXIVEO_MVTOOLS_BIN=build/muxiveo-mvtools/bundle/muxiveo-mvtools python -m pytest --noconftest tests/native/test_muxiveo_mvtools.py
python native/muxiveo-mvtools/scripts/benchmark.py --mvtools-bin build/muxiveo-mvtools/bundle/muxiveo-mvtools --rife-bin /chemin/muxiveo-rife --output build/visual.json
python native/muxiveo-mvtools/scripts/benchmark.py --mvtools-bin build/muxiveo-mvtools/bundle/muxiveo-mvtools --width 3840 --height 2160 --frames 16 --threads 16 --scenes bars --resource-only --output build/resources.json
```

Les dépendances de développement du banc sont NumPy et psutil ; elles ne sont
pas ajoutées au runtime de l’application. Les paramètres des préréglages sont
fixés par la première version et ne sont pas ajustés pour favoriser ce banc.
