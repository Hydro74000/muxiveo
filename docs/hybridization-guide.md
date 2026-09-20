# Guide de l'hybridation et de la synchronisation audio

L'hybridation dans Muxiveo permet de combiner le meilleur de plusieurs sources pour créer la version parfaite de vos films et séries : par exemple, associer une superbe image 4K ou Blu-ray (la **référence**) avec la piste audio française ou les sous-titres d'une autre édition (le **donneur**).

Muxiveo se charge d'analyser le son des deux versions, de trouver automatiquement le décalage temporel à la milliseconde près, et de synchroniser parfaitement l'audio et les sous-titres sans aucun décalage dans vos lecteurs.

---

## 1. À quoi ça sert ?

Lorsque vous récupérez une piste audio provenant d'une autre source, elle est presque toujours décalée par rapport à votre vidéo :
- Logos d'introduction différents (durées différentes au début du film).
- Présence ou absence de cartons d'avertissement ou de transitions.
- Épisodes TV ayant subi des coupures publicitaires.

**Ce que fait Muxiveo pour vous :**
1. **Détection acoustique intelligente :** Muxiveo « écoute » et compare les empreintes sonores de vos deux fichiers pour déterminer le décalage exact.
2. **Synchronisation physique (*Zero Delay*) :** Au lieu de simplement indiquer un délai virtuel dans l'en-tête du fichier (souvent ignoré par certains téléviseurs ou lecteurs comme Plex), Muxiveo cale physiquement la piste sonore (en ajoutant un silence propre ou en coupant l'excédent au début). Votre vidéo et votre audio sont parfaitement calés partout.
3. **Recalage automatique des sous-titres :** Vos sous-titres (SRT, ASS, VTT) sont automatiquement décalés de la même valeur pour correspondre aux dialogues.

---

## 2. Utilisation dans l'interface graphique

### A. Traiter une saison complète de série : le Studio d'Hybridation

Pour traiter 10, 20 ou 50 épisodes d'un coup sans effort :

1. Cliquez sur **Hybridation** dans le menu de gauche (barre latérale).
2. Renseignez ou glissez-déposez vos deux dossiers :
   - **Référence :** le dossier contenant vos épisodes avec la meilleure vidéo.
   - **Donneur :** le dossier contenant vos pistes audio ou sous-titres additionnels.
   - **Sortie :** le dossier où enregistrer les épisodes hybrides finaux.
3. Cliquez sur **Analyser la saison** :
   - Muxiveo apparie automatiquement chaque épisode (S01E01 avec S01E01, etc.).
   - Le système calcule la synchronisation pour chaque fichier et affiche le résultat dans le tableau.
4. *(Optionnel)* Cliquez sur un épisode pour visualiser les formes d'ondes sonores superposées et tester un extrait via le bouton **Pré-écoute**.
5. Cliquez sur **Lancer l'hybridation** : vos épisodes complets et synchronisés sont générés les uns après les autres.

### B. Traiter un film individuel : le panneau Conteneur

Si vous souhaitez assembler manuellement un film :
1. Rendez-vous dans l'onglet **Conteneur**.
2. Glissez votre fichier vidéo principal et votre fichier audio donneur.
3. L'option **Synchronisation physique (Zero Delay)** est activée par défaut pour vous garantir une compatibilité maximale.
4. Lancez le remuxage ou exportez votre workflow.

### C. Sauvegarder et reprendre : le bouton Workflow

Dans le panneau Conteneur, le bouton **Workflow** vous permet de :
- **Sauvegarder le workflow…** : enregistre toute votre configuration (sélection des pistes, langues, titres, métadonnées) dans un petit fichier JSON facile à conserver ou à réutiliser.
- **Charger un workflow…** : recharge instantanément un travail précédent, même si vous avez redémarré votre ordinateur.
- **Reprendre la dernière session** : restaure votre travail en cours en cas de fermeture inopinée.

---

## 3. Utilisation en ligne de commande (CLI)

Pour les utilisateurs qui souhaitent automatiser leurs traitements sur serveur ou en script :

### Assembler un film
```bash
Muxiveo-cli hybrid --ref video_4k.mkv --donor audio_fr.mkv -o film_final.mkv
```

### Traiter une saison complète de série
```bash
Muxiveo-cli hybrid --ref-dir "Saison 01 (4K)" --donor-dir "Saison 01 (Audio FR)" -o "Saison 01 (Hybride)"
```

### Options pratiques
- `--dry-run` : Simule l'opération et affiche les décalages détectés sans créer de gros fichier vidéo.
- `--detect-cuts` : Détecte automatiquement les coupures (ex: coupures publicitaires TV) et réaligne chaque segment.
- `--export-workflow dossier_json/` : Sauvegarde les fichiers de projet JSON pour chaque épisode sans les encoder immédiatement.
- `--auto-tmdb 2734` : Télécharge et injecte automatiquement les titres des épisodes et les métadonnées depuis TheMovieDB.

---

## 4. Questions fréquentes

**Faut-il installer MKVToolNix ?**  
Non. Muxiveo intègre son propre moteur de création Matroska natif, rapide et sans logiciel externe requis.

**Mes pistes audio en Dolby Atmos ou DTS:X sont-elles modifiées ?**  
Pour les formats home-cinéma complexes (Atmos, DTS:X), Muxiveo privilégie la préservation de la qualité d'origine sans altérer les métadonnées spatiales.

**Que faire si deux épisodes ont une musique ou un doublage très différent ?**  
Dans le Studio d'Hybridation, sélectionnez l'épisode concerné dans le tableau, observez la courbe et utilisez la case d'ajustement en millisecondes pour affiner le décalage à l'oreille grâce au bouton **Pré-écoute**.
