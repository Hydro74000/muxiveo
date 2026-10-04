# muxiveo-mvtools 1.0.1

Moteur d’interpolation CPU autonome de Muxiveo. Aucun modèle neuronal (0 Mio).
Il charge VapourSynth R80 et MVTools v29_2 via l’API C, sans Python installé chez
l’utilisateur. Python et Meson servent uniquement à construire les dépendances.

## Construction

Prérequis : Python >=3.12, Meson 1.12.1, CMake >=3.20, Ninja, pkg-config,
compilateur C/C++17, nasm sur x86_64, patchelf et libstdc++ statique sur Linux.
Sous Windows, lancer dans un environnement MSVC x64 ; sous macOS arm64,
`MACOSX_DEPLOYMENT_TARGET=12.0`. Linux distribué est construit sous manylinux_2_28.

```sh
python native/muxiveo-mvtools/scripts/build.py --jobs 4
build/muxiveo-mvtools/bundle/muxiveo-mvtools --self-test --json
python native/muxiveo-mvtools/scripts/archive.py --platform linux-x86_64 --sources
MUXIVEO_MVTOOLS_BIN=build/muxiveo-mvtools/bundle/muxiveo-mvtools python -m pytest --noconftest tests/native/test_muxiveo_mvtools.py
```

`dependencies.json` épingle les sources et SHA-256. La construction conserve
les sous-projets internes épinglés par VapourSynth. Le manifeste du runtime
recense les révisions internes, tailles et empreintes des fichiers distribués.
Les archives excluent bindings Python, SDK, symboles et variante CPU znver4.
Une adaptation ARM de l’arrondi pair d’AverageFrames (FRINTN, même résultat)
permet la compilation avec Apple Clang du runner. Ce filtre n’est pas utilisé
dans le graphe MVTools. Les noyaux AVX2/AVX512 restent sélectionnés par détection CPU amont ; la variante
baseline est toujours livrée. zimg 3.0.6 et FFTW float 3.3.11 sont liés statiquement.

## Flux et cadences

```sh
ffmpeg -i input.mkv -map 0:v:0 -strict -1 -f yuv4mpegpipe - | muxiveo-mvtools --fps 60000/1001 --mode standard --threads 4 | ffmpeg -f yuv4mpegpipe -i - output.mkv
muxiveo-mvtools -i input.y4m -o output.y4m --factor 2 --mode uhd --scene-threshold 10
```

Les formats planaires YUV 420/422/444 entiers 8 à 16 bits sont conservés octet
pour octet aux horodatages communs. Dimensions >=64×64, multiples du
sous-échantillonnage, jusqu’à 16384×16384. Désentrelacer avant l’entrée. Les jetons
Y4M supplémentaires sont conservés ; Muxiveo rétablit le marquage HDR à l’encodage.
`--matrix`, `--range limited|full`, `--chroma-loc left|center|topleft` renseignent
les propriétés du graphe sans conversion des valeurs YUV.

Un rapport rationnel p/q produit ceil(N×p/q) images. L’index de sortie est global,
les images sources coïncidentes sont copiées, la dernière image est prolongée.
Une fenêtre contient 8 images utiles et 2 gardes de chaque côté, dupliquées aux
extrémités. Les gardes sont réutilisées ; une seule fenêtre et au plus 8 graphes
FlowInter de phases sont actifs. `--window-frames 16` sert au test des raccords.
Les E/S sont synchrones, sans file d’images supplémentaire (donc <=2 images).
Le graphe est détruit après chaque fenêtre : la mémoire ne dépend pas de la durée.

Le MAFD, son historique et les paires statiques reprennent RIFE (seuil 10).
Sur coupe/paires identiques, répétition de l’image précédente. FlowInter utilise
`blend=False`, ml=100, thscd1=400, thscd2=130 et peut également répéter une image
si les vecteurs sont inutilisables. Les timestamps sont rationnels exacts mais
la phase de calcul MVTools est quantifiée à **1/256** par l’amont.

| Réglage | Standard | Lent UHD |
|---|---|---|
| pel | 2 | 4 |
| Analyse blocs/recouvrement | 16/8 | 32/16 |
| Recherche/rayon | hexagonale 4/2 | exhaustive 3/8 |
| dct | 0 (SAD) | 5 (SATD) |
| Raffinements | aucun | 16/8 puis 8/4, rayons 4 puis 2, thsad 200 |
| trymany | false | true |
| Threads automatiques | max(1,min(4,budget/2)) | budget complet |
| Cache VapourSynth | 512 Mio | 1 Gio |

Commun : delta=1 bidirectionnel, chroma/truemotion/global=true, search_coarse=3,
padding 32, sharp=2, rfilter=2. Les valeurs non précisées restent celles de v29_2.
Les caches sont des limites souples : ils ne plafonnent pas la RAM totale.
Les modes utilisent tous deux la résolution reçue, y compris UHD en 1080p.
Le nombre de threads est plafonné à 256, comme l’interface native.

Depuis 1.0.1, les nombres et marqueurs Y4M sont validés strictement ; des
cadences équivalentes sont réduites avant calcul. Les chemins d’entrée/sortie
désignant le même fichier (y compris liens) sont refusés sans altérer la source.

Progression sur stderr : `progress in=N out=M scenes=S static=T fps=X`.
Les gardes ne sont pas comptées. stdout contient uniquement le flux Y4M (ou JSON
pour l’autotest). Codes : 0 succès, 1 usage, 2 entrée, 3 runtime/rendu, 4 E/S,
5 allocation mémoire. Le pipeline de Muxiveo ferme les pipes et termine tous
les processus lors de l’annulation. SIGPIPE est converti en erreur E/S.

## Distribution et licences

Le binaire et `mvtools-runtime/` doivent rester voisins. Le chargement automatique
de plugins VapourSynth est désactivé. Les bibliothèques sont chargées depuis
le runtime adjacent ; aucune installation système de VapourSynth n’est requise.

Le wrapper est **GPL-3.0-or-later** (`LICENSE`), avec des E/S externes vers
l’application MIT. VapourSynth : LGPL-2.1-or-later ; MVTools : GPL-2.0-or-later ;
FFTW : GPL-2.0-or-later ; zimg : WTFPL ; glslang et Vulkan-Headers : licences
amont conservées dans `mvtools-runtime/licenses/`. Le lecteur Y4M provient du
projet Muxiveo sous MIT (notice `Muxiveo-MIT.txt`).
Sous Linux, libstdc++/libgcc statiques sont couverts par GPL-3.0 avec l’exception
GCC Runtime Library 3.1, incluse avec les notices.

L’archive `*-sources.tar.gz` contient le wrapper, ses scripts, les archives amont
vérifiées et les sources effectivement modifiées pour la construction, y compris
les sous-projets internes. Elle est publiée séparément des paquets installés.
La CI publie les poids exacts dans `*-sizes.json`, ainsi que les SHA-256.
Les bancs comparatifs et limites de qualification sont dans `docs/benchmarks/`.

## Banc comparatif

Prérequis de développement : `pip install numpy psutil`.

```sh
python native/muxiveo-mvtools/scripts/benchmark.py --mvtools-bin build/muxiveo-mvtools/bundle/muxiveo-mvtools --rife-bin /chemin/muxiveo-rife --output build/quality.json
python native/muxiveo-mvtools/scripts/benchmark.py --mvtools-bin build/muxiveo-mvtools/bundle/muxiveo-mvtools --width 3840 --height 2160 --frames 16 --threads 16 --scenes bars --resource-only --output build/resources-4k.json
```

La CI joint les mesures Standard/UHD sur chaque plateforme et une mesure
4K 10 bits (4 images sources, budget 4 threads) aux artefacts. Le test RSS
compare également 32 et 512 images et refuse une croissance supérieure à 64 Mio.
Les mesures de contours/clignotement sont des indicateurs synthétiques ;
l’examen visuel et les extraits du master restent nécessaires à la qualification.
