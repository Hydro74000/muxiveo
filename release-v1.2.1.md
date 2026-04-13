# Mediarecode v1.2.1

Date: 2026-04-09

PR associee: `#12`  
Merge commit: `c8a349d94f08583fa1b165dd78802dfda95b29b5`

Depuis `v1.2` (commit `96967a69207088d2b5a20d75b586c855f60504da`), cette version regroupe **5 commits** (dont 1 merge), avec **13 fichiers modifies** (`+793 / -99`).

## Point principal

Cette iteration est principalement une mise en compatibilite avec **mkvmerge v98**.

Le changement majeur confirme dans l'historique Git et dans le diff est un **breaking change sur la gestion des balises de langue IETF** (`language-ietf`). Cette release adapte le remux, l'edition des langues et la detection de version de l'outillage pour continuer a reemettre correctement les langues dans les fichiers MKV.

## Points forts

- Compatibilite avec **mkvmerge v98** et son nouveau comportement autour de `language-ietf`.
- Correction de la reemission des langues lors des operations de remux.
- Mise a jour du build et de la configuration pour tenir compte des differences de version des outils externes.
- Correctif Wine / cross-build pour recuperer les DLL ICU Windows manquantes.

## Correctifs inclus

- Correction du remux lorsque `mkvmerge v98` modifie la mecanique des tags `language-ietf`.
- Correction de la reemission des langues de pistes apres edition ou reorganisation.
- Correction d'un cas de **titre vide** dans le workflow remux.
- Renforcement du packaging Windows cross-platform pour mieux gerer le runtime ICU sous Wine.

## Impact technique

- `core/config.py` introduit une detection de version majeure des outils externes afin d'adapter le comportement en fonction de la version de `mkvmerge`.
- `core/workflows/remux.py` porte l'essentiel de l'adaptation au breaking change `language-ietf`.
- `core/workflows/encode/workflow.py` est ajuste pour garder un comportement coherent lors des editions de langues.
- `package.py` est renforce pour fiabiliser les builds Windows / Wine lies aux DLL ICU.

## Tests et validation

Verification ciblee lancee avec:

```sh
python3 -m pytest -q tests/test_remux.py tests/test_encode_workflow.py tests/test_setup_and_config.py tests/test_package.py
```

Resultat:

- `408 passed`
- `2 failed`

Les 2 echecs restants sont lies a des tests Windows qui patchent `ctypes.windll`, indisponible dans ce runtime Linux.

## Notes de release

- Cette release est associee au PR [#12](https://github.com/Hydro74000/mediarecode/pull/12).
- Conformement a la consigne de release, **aucun nouveau tag GitHub n'a ete cree** pour cette fusion.
- Le tag GitHub conserve pour la release publique reste **`v1.2`**.
