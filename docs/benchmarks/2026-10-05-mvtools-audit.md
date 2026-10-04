# Audit de l’intégration MVTools — 5 octobre 2026

Audit du code introduit sur `devel-mvtools`, depuis le point de départ
`db985f64` jusqu’à `a199e56`, puis des corrections ci-dessous. Le périmètre
couvre les 49 fichiers de l’intégration : moteur natif, streaming, profils,
interface, pipelines FFmpeg/NVEncC, installation, packaging, CI, licences,
tests et rapports. Les appels aux composants existants ont été suivis ;
il ne s’agit pas d’un audit exhaustif de toute l’application ni des dépendances
VapourSynth/MVTools elles-mêmes.

## Défauts confirmés et corrigés

P1 désigne ici un risque de perte de données ; P2 un échec de traitement,
de construction ou une incohérence de configuration ; P3 un cas limite.

| Priorité | Défaut reproduit | Correction |
|---|---|---|
| P1 | `-i source.y4m -o source.y4m` tronquait la source avant sa lecture. Les liens physiques/symboliques pouvaient désigner le même fichier. | Vérifier l’identité des fichiers avant ouverture en écriture ; ouvrir la sortie après validation de l’en-tête, de la cadence et du runtime. |
| P2 | Des jetons tels que `W96junk`, `F24:1junk`, `C420p10junk` et `FRAMEBAD` étaient acceptés. | Analyse complète des entiers et fractions ; signature/marqueurs exacts ; contrôle des doublons et de l’entrelacement ; conservation des fins de ligne CRLF valides. |
| P2 | Une seconde construction incluait l’ancien `manifest.json` dans ses propres empreintes, rendant le paquet incohérent. La version du manifeste était figée à 1.0.0. | Exclure le manifeste de son contenu et lire la version CMake lors de sa régénération. Deux constructions successives ont été vérifiées. |
| P2 | Un manifeste avec `files: []` déclenchait `AttributeError` ; le contrôle du workflow pouvait aussi accepter un runtime altéré tant que l’autotest réussissait. | Valider la structure, les empreintes et les chemins ; vérifier le paquet avant l’autotest ; invalider le cache lors du remplacement d’un composant. |
| P2 | Une cadence décimale acceptée par les profils, comme `59.94`, était envoyée à un CLI qui attend une fraction entière. | Transmettre `2997/50`, sans arrondi ; valider les bornes du CLI. |
| P2 | Après Normal → Light → Standard RIFE, l’interface pouvait afficher Rapide alors que le mode enregistré restait Normal. | Rétablir le mode RIFE conservé dans le contrôle visible en quittant Light. |
| P2 | Charger un profil avec un moteur ou un préréglage MVTools inconnu sélectionnait silencieusement le premier choix du contrôle. | Conserver la valeur invalide pour que la validation refuse le lancement, sans changer de moteur. |
| P3 | Le chemin MVTools n’était pas enregistré par le parcours `AppConfig.save()` utilisant QSettings. | Ajouter la clé de l’outil à ce parcours, déjà présent dans la configuration INI. |
| P3 | Un budget supérieur à 256 CPU entraînait un refus du CLI en mode UHD. Une fraction source valide mais non réduite pouvait dépasser les bornes du calcul. | Borner le nombre de threads à 256 et réduire la fraction source avant les multiplications. |

L’extraction refuse également une destination non vide ou symbolique, ainsi
que les entrées d’archive spéciales. Les chemins de manifeste refusent les
remontées, les séparateurs Windows et les liens symboliques intermédiaires.
Le contrôleur des paquets tout inclus refuse également les chemins ZIP avec
un lecteur Windows, avant extraction dans son répertoire temporaire.
Les autotests utilisent les options subprocess communes à l’application,
notamment l’absence de fenêtre console supplémentaire sous Windows.

Les corrections natives sont versionnées **1.0.1**, avec la même version
épinglée dans l’application. Le graphe de mouvement et les préréglages restent
identiques ; l’empreinte du rendu de l’autotest reste `2b17510182ba8ed5`.

## Validation locale

- **780 tests Python ciblés**, tous réussis : configuration, installation,
  interface, profils, pipeline, progression, ressources, packaging,
  traductions et génération de la formule Homebrew. Les tests ont été exécutés
  par groupes ; seuls les groupes affectés ont été rejoués après correction.
- **77 tests natifs**, tous réussis : comptes/cadences fractionnaires,
  conservation des sources, formats 8–16 bits, raccords 8/16 images,
  coupes/statique/flashes, annulation, pipes arrêtés, runtime absent,
  mémoire selon la durée et nouvelles entrées invalides/alias de fichiers.
- **24 cas d’encodage réels validés** : RIFE, MVTools Standard et UHD,
  FFmpeg/libx264 et NVEncC/HEVC, assemblage FFmpeg et natif, cadence/compte,
  audio et décalage vidéo de +400 ms. La première passe a donné 23 succès
  et un cas ignoré par la sonde NVENC ; ce cas a ensuite réussi isolément.
- Deux compilations successives du runtime local, contrôles SHA-256 et rendu
  8/10 bits réussis. `git diff --check` réussi.
- **76 tests natifs fonctionnels avec AddressSanitizer et
  UndefinedBehaviorSanitizer**, sans erreur détectée sur le wrapper C++.
  Les bibliothèques amont ne sont pas instrumentées ; le test de RAM selon
  la durée est réservé au binaire normal pour éviter le coût d’allocation
  propre aux sanitizers.

Le budget CPU NVEncC a été vérifié : ce parcours n’accepte qu’une piste vidéo,
donc son budget global est cohérent. La découverte du binaire Windows embarqué
était déjà correcte ; un test confirme sa priorité sur un chemin externe.

## Distribution et limites

La qualification de la version 1.0.1 sur Linux x86_64, Windows x86_64 et
macOS arm64, ses poids exacts et sa publication sont consignés après la CI.

La [qualification 1.0.0](2026-10-04-mvtools-integration.md) conserve les mesures
historiques. Les caches 512 Mio/1 Gio ne plafonnent pas la RAM totale.
Les réservations supplémentaires de 2 Gio/6 Gio sont fondées sur les mesures
4K 4:2:0 10 bits : le pic en 8K ou 4:4:4 16 bits n’a pas été qualifié.
La validation complète de l’application packagée sous Windows/macOS reste
distincte des tests du runtime natif sur ces plateformes.

Les extraits de films et leurs conversions 60 i/s de référence n’ont pas été
fournis. La qualité visuelle sur les scènes problématiques demeure à vérifier ;
aucun résultat synthétique ne garantit la disparition des défauts sur ces films.
