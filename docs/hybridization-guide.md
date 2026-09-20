# Hybridation — branche de développement `devel-hybrid`

Le workflow combine la vidéo et les chapitres d'une référence avec les pistes
audio et les sous-titres d'un donneur. Le mux final de `hybrid` utilise le moteur
Matroska natif, sans MKVToolNix. Disponible en Python sur Linux, Windows et macOS ;
les builds AppImage, Windows et macOS embarquent désormais NumPy pour la FFT.

## Une paire ou une saison

```sh
python main.py --cli hybrid --ref master.mkv --donor donneur.mkv -o sortie --dry-run --report-json rapport.json
python main.py --cli hybrid --ref-dir masters --donor-dir donneurs -o sortie --detect-cuts --auto-tmdb 2734 --tag MVO
```

Les dossiers sont parcourus sans récursion. L'appariement accepte `S01E01`
indépendamment de la casse. Un doublon, un épisode multiple ou manquant provoque
une erreur ; aucun choix arbitraire de donneur. `--continue-on-error` autorise la
poursuite après une erreur de traitement. Une sortie existante nécessite `--force`.

`--profile BestOfAll` charge un profil existant (nom ou chemin). Aucun profil de
ce nom n'est créé implicitement. Sans profil, la sélection conserve les pistes
de référence et les pistes audio/sous-titres donneuses. La vidéo donneuse est exclue.

`--dry-run` réalise l'analyse et construit le plan, sans créer de média final.
`--export-workflow fichier.json` exporte une paire sans remux. Pour une saison,
donner un **dossier** : un fichier `SxxExx.exact-job.json` est écrit par épisode.
`--report-json` écrit le statut et la calibration de chaque épisode traité.
Les fichiers temporaires sont nettoyés après chaque remux, même en cas d'échec ;
`--purge-temp` est conservé comme alias explicite de ce comportement.

## Calibration réutilisable

```sh
python main.py --cli sync-scan --ref master.mkv --target donneur.mkv --stream-ref 0:a:0 --stream-target 0:a:1 --detect-cuts --output-json calibration.json
python main.py --cli shift-subs -i fr.srt -o fr-aligned.srt --calibration calibration.json
python main.py --cli shift-subs -i fr.ass -o fr-aligned.ass --offset-ms -125
python main.py --cli hybrid --ref master.mkv --donor donneur.mkv -o sortie --calibration calibration.json
```

Les sélecteurs de `sync-scan` sont des maps FFmpeg : `0:a:0` désigne la première
piste audio, `0:2` le stream d'index absolu 2. Une calibration manuelle utilise
le même format que le scanner :

```json
{
  "version": 1,
  "kind": "sync-calibration",
  "timebase": "donor-ms",
  "confidence": 1.0,
  "segments": [
    {"start_ms": 0, "shift_ms": 103},
    {"start_ms": 600000, "shift_ms": 503},
    {"start_ms": 1200000, "shift_ms": 203}
  ],
  "samples": []
}
```

Tous les temps sont exprimés sur la **chronologie donneuse, en millisecondes**.
Chaque segment applique `référence = donneur + shift_ms`. Le premier débute à
zéro ; les débuts suivants sont finis et strictement croissants. Un saut positif
insère du silence. Un saut négatif retire la portion donneuse qui chevaucherait
la fin du segment précédent. Les sous-titres traversant une rupture sont découpés
selon les mêmes intervalles, sans horaires négatifs ni répliques de durée nulle.
SRT, ASS/SSA et VTT sont pris en charge ; les temps ASS sont arrondis au centième.
Le contenu, les styles ASS et les réglages VTT des répliques sont conservés.

Pour un job remux, placer cette calibration dans `sync_calibrations`, avec
l'index source sous forme de clé, par exemple `"1"` pour le donneur. Une même
source doit partager une chronologie ; utiliser des sources séparées pour des
pistes nécessitant des calibrations différentes.

## Analyse et limites mesurables

Le scanner compare six fenêtres réparties sur la durée commune, après extraction
mono à 16 kHz et filtrage 300–3000 Hz. La FFT compare les enveloppes d'énergie à
1 ms et vérifie la corrélation et la séparation du meilleur pic. Le seuil de
dérive est réglable via `--drift-threshold-ms` (25 par défaut).

`--detect-cuts` recherche des silences et des noirs dans les intervalles où le
décalage change, puis vérifie les fenêtres précédant et suivant chaque candidat.
Une transition ambiguë, une corrélation faible, une musique répétitive, des
doublages très différents ou une dérive continue peuvent nécessiter une
calibration manuelle. Le scanner ne garantit pas une précision de 2 ms sur une
coupure réelle : l'ancrage doit être dans une zone de transition compatible.
Plusieurs ruptures rapprochées peuvent rester indéterminées et sont refusées.

## Synchronisation physique et codecs

`hybrid` utilise `--sync-mode physical` par défaut. Les jobs existants et le remux
historique conservent `container` en l'absence de choix explicite. Le mode physique
est disponible sur les deux backends remux et dans le menu Workflow.

Les décalages non nuls nécessitent un décodage et un réencodage audio : AAC, AC3,
EAC3 et FLAC sont actuellement acceptés. Le débit connu est conservé. Le FLAC
préserve les échantillons décodés hors zones retirées/fondues ; un réencodage avec
pertes n'est pas bit-identique. Les pistes Atmos/DTS:X et les codecs non encodables
sont refusés ; choisir une variante compatible explicitement.

Une calibration nulle permet la copie et la normalisation PTS pour les codecs
compatibles. AAC, Opus et MP3 conservent leur pré-roll au lieu de le supprimer
aveuglément. Le délai utilisateur est matérialisé dans le signal, mais cela ne
constitue pas une promesse de suppression de tout champ MediaInfo `Delay`.
Le `CodecDelay` intrinsèque doit être respecté selon la
[spécification Matroska](https://www.matroska.org/technical/notes.html).

`--crossfade-ms` (80 par défaut, 0 pour désactiver) contrôle ici des **fondus de
bord sans chevauchement**, pour éviter de raccourcir la chronologie. Les filtres
`atrim`, `asetpts`, `adelay`, `afade` et `concat` sont décrits dans la
[documentation FFmpeg](https://ffmpeg.org/ffmpeg-filters.html).

`--sync-subtitles mirror` suit la calibration donneuse. `none` laisse les
sous-titres intacts. Les PGS/VobSub nécessitant un recalage miroir sont refusés
explicitement : aucune conversion OCR ou approximation silencieuse.

## Sous-titres, TMDB et noms de sortie

`--auto-forced-subs --forced-threshold 50` considère une piste comme forcée si
elle contient **entre 1 et 49 répliques connues** et correspond à la langue audio
par défaut. Une piste vide ou de comptage inconnu n'est pas classée forcée.
`--auto-sdh` recherche les métadonnées SDH et les descriptions sonores connues.
Ces heuristiques sont opt-in et doivent être vérifiées lorsque le contenu est atypique.

`--auto-tmdb 2734` utilise l'identifiant de série ; `--auto-tmdb` seul déclenche
la recherche. La configuration TMDB existante est réutilisée. `--tmdb-apikey`
et `--no-cover` sont disponibles.

```sh
python main.py --cli hybrid --ref-dir masters --donor-dir donneurs -o sortie --auto-tmdb 2734 --output-template "{title}.S{season_num:02d}E{episode_num:02d}.{episode_title}.{hybrid_tag}.{channels_str}-{release_group}"
```

`episode_title`, `season_num` et `episode_num` existaient déjà ; `channels_str`,
`hybrid_tag` et `release_group` les complètent. `Hybrid` est un token sans points
extérieurs, inséré lorsque plusieurs sources fournissent des pistes sélectionnées.
`--tag` fournit le groupe. Les NFO conservent le seul nom de fichier par défaut ;
`--no-clean-nfo` rétablit le chemin complet.

## Workflow et studio graphique

Le menu **Workflow** du panneau Conteneur propose la sauvegarde, le chargement,
la reprise de session et le Studio Hybridation. Les raccourcis utilisent les
touches standards de l'OS : Ctrl+S/Ctrl+O, Cmd+S/Cmd+O sur macOS.
La sauvegarde JSON est atomique. Une source déplacée est recherchée à côté du
JSON, puis peut être relocalisée. La nouvelle configuration est inspectée avant
de remplacer l'état courant ; les pistes, l'ordre, les flags, les chapitres,
les tags et la jaquette différée sont restaurés.

Une sauvegarde de session est effectuée toutes les 30 secondes lorsque les
sources sont prêtes, dans `AppConfig.config_dir/session_autosave.json`.
La reprise est explicite via le menu. Il s'agit d'un instantané périodique,
pas d'une garantie de récupération de la toute dernière modification.

Dans le Studio, déposer les deux dossiers, choisir la sortie et lancer l'analyse.
Les options avancées exposent les seuils, les heuristiques, TMDB, les templates,
les modes de synchronisation et une calibration manuelle. Sélectionner une paire
affiche les enveloppes des premières 20 secondes ; la valeur en ms permet un
ajustement global de sa calibration. La pré-écoute rend l'audio donneur recalé.
Les workflows peuvent être exportés avant exécution. Le traitement est séquentiel
et annulable, et refuse d'écraser une sortie existante.

## Validation

Les tests comprennent les contrats et chemins Unicode/Windows, les signes
d'offset, les formats de sous-titres, les heuristiques, les durées audio après
insertion/rognage, les deux backends avec FFmpeg réel, la restauration Qt et i18n.
La CI `ci-hybrid.yml` définit Linux, Windows et macOS avec Python 3.10/3.12.
Un passage Linux local ne vaut pas certification des deux autres OS ni validation
d'un corpus Dolby Vision/HDR10+ de production.
