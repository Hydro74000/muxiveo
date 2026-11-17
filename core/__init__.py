"""Cœur de Muxiveo.

Python 3.15 (PEP 810) : ``concurrent.futures`` déclare ``ThreadPoolExecutor`` en import paresseux. Une fois
PySide6 importé (son ``__import__`` remplace celui de Python), l'entrée du module n'est jamais résolue :
``from concurrent.futures import ThreadPoolExecutor`` exécuté dans un fil secondaire rend l'objet paresseux
(« 'lazy_import' object is not callable »). L'entrée est remplacée ici par la classe elle-même, importée de son
sous-module, au premier import du cœur.
"""

import concurrent.futures as _futures

if type(_futures.__dict__.get("ThreadPoolExecutor")).__name__ == "lazy_import":
    from concurrent.futures.thread import ThreadPoolExecutor as _ThreadPoolExecutor

    _futures.ThreadPoolExecutor = _ThreadPoolExecutor  # type: ignore[misc]
