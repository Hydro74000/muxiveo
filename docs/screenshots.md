# Captures du wiki et du site

Les captures sont rendues depuis les widgets PySide6 de l’application courante. Les chemins, pistes, profils, calibrations et progressions illustrent des données de démonstration. Aucun remuxage ou encodage n’est lancé. La capture du terminal montre la sortie réelle de `muxiveo --cli hybrid --help`.

Le jeu comprend 30 images françaises et 28 anglaises. Les vues principales montrent le menu, la commande **Remux & Encode**, Hybrid Studio, Merge et le journal communs. Les dialogues sont capturés séparément pour rester lisibles. La vue anglaise du conteneur présente le thème clair.

Depuis le dépôt de l’application, les dépendances Python habituelles suffisent :

```bash
python scripts/capture_documentation.py \
  --site-root ../muxiveo_web \
  --wiki-root ../mediarecode.wiki
```

Le script utilise une configuration temporaire et le rendu Qt hors écran. Il ne modifie ni les réglages, ni les plugins installés de l’utilisateur. Les sondes matérielles sont désactivées pour ce rendu ; les badges ne constituent pas un rapport des capacités de la machine.

Il remplace les images et miniatures de `muxiveo_web/assets/img/screenshots/`, copie le jeu français dans `mediarecode.wiki/assets/img/` et `docs/assets/img/`, puis écrit le catalogue et les empreintes dans [screenshots.json](screenshots.json).

Les anciennes URLs numérotées sont conservées. Les vues supplémentaires couvrent les opérations, les paramètres, les plugins, les sept vues HDR Studio, la reconstruction FEL, l’interpolation, la taille cible, les sous-titres embarqués et le menu Workflow. Les liens du site incluent une empreinte des fichiers pour renouveler le cache des captures et des miniatures. Les miniatures présentent l’image entière.

Regénérer ensuite le site statique depuis le dépôt web, avec PHP et Python disponibles :

```bash
python tools/build_cloudflare_pages.py
```

Vérifier que les copies dans `dist_cloudflare/assets/img/screenshots/` sont identiques aux sources et que toutes les références du wiki et des galeries s’ouvrent avant publication.

## Captures ciblées et schémas

Pour actualiser uniquement les illustrations HDR et d’encodage, tout en conservant les autres captures :

```bash
python scripts/capture_documentation.py --only \
  hdr-overview hdr-luminance hdr-dovi hdr-fel hdr-fel-detail \
  hdr-compare hdr-edit fel-encoding interpolation video-budget \
  closed-captions workflow-menu
```

Les fixtures de [documentation_hdr.py](../scripts/documentation_hdr.py) produisent des courbes, valeurs et motifs géométriques de démonstration dans les widgets actuels. Elles ne constituent ni des mesures d’un film ni un benchmark. Les onglets Comparer et Édition utilisent une fenêtre plus haute pour rendre leurs formulaires complets lisibles.

Les quatre schémas pédagogiques (choix du traitement, niveaux d’inspection HDR, reconstruction FEL et chronologie d’interpolation) sont des SVG accessibles, avec titre et description, générés en FR/EN :

```bash
python scripts/documentation_diagrams.py
```

Ils sont copiés dans `muxiveo_web/assets/img/guides/{fr,en}/`, `mediarecode.wiki/assets/guides/` et `docs/assets/guides/`. Les captures du site utilisent des miniatures et une ouverture en grand ; les guides du wiki les placent près des réglages concernés.
