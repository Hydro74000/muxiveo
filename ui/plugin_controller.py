"""
ui/plugin_controller.py — état et opérations de l'extension d'accélération NVIDIA (TensorRT) de MVO-RIFE.

Un seul contrôleur, propriété de la fenêtre principale : le tableau de bord lui transmet la compatibilité
sondée, la section Extensions des Paramètres et le panneau Encodage lisent son état. Téléchargement,
installation, préparation des moteurs et suppression tournent hors du thread de l'interface ; les résultats
reviennent par signaux.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace

from PySide6.QtCore import QObject, Signal

from core import plugins
from core.config import AppConfig
from core.i18n import translate_text
from core.version import MVO_RIFE_TRT_VERSION
from core.workflows.encode.interpolation import TrtCapability


@dataclass(frozen=True)
class TrtState:
    """Instantané de l'extension pour l'interface."""

    capability: TrtCapability | None = None
    installed: plugins.InstalledPlugin | None = None
    busy: str = ""            # "" | "install" | "update" | "remove" | "warmup"
    progress: int = -1        # pourcentage du téléchargement, -1 si inconnu
    message: str = ""         # dernier résultat (succès ou échec), affiché sous l'état

    @property
    def compatible(self) -> bool:
        return bool(self.capability and self.capability.compatible)

    @property
    def visible(self) -> bool:
        """Extension visible seulement sur une machine compatible, ou si elle est déjà installée."""
        return self.compatible or self.installed is not None

    @property
    def update_available(self) -> bool:
        return self.installed is not None and self.installed.version != MVO_RIFE_TRT_VERSION

    @property
    def ready(self) -> bool:
        return bool(self.installed and self.capability and self.capability.ready)


class TrtPluginController(QObject):
    """Installation, mise à jour, suppression et état de l'extension mvo-rife-trt."""

    state_changed = Signal(object)        # TrtState
    log_message = Signal(str, str)        # (niveau, message)
    probe_requested = Signal()            # nouvelle sonde du GPU (extension installée ou retirée)
    workflow_changed = Signal()           # dossier de l'extension à transmettre aux encodages

    def __init__(self, config: AppConfig, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._state = TrtState(installed=plugins.installed_plugin())
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mvo-rife-trt")
        self._cancel = threading.Event()
        self._auto_update_done = False

    # ------------------------------------------------------------------ état

    def state(self) -> TrtState:
        return self._state

    def _set_state(self, **changes: object) -> None:
        self._state = replace(self._state, **changes)  # type: ignore[arg-type]
        self.state_changed.emit(self._state)

    def plugin_dir(self) -> str:
        """Dossier de l'extension à passer à muxiveo-rife ("" : désactivée ou absente)."""
        installed = self._state.installed
        return str(installed.path) if installed and self._config.trt_enabled else ""

    def set_capability(self, capability: TrtCapability) -> None:
        """Compatibilité sondée par le tableau de bord ; déclenche une mise à jour automatique éventuelle."""
        self._set_state(capability=capability, installed=plugins.installed_plugin())
        if (
            not self._auto_update_done and self._state.update_available and self._config.plugins_auto_update
            and not self._state.busy
        ):
            self._auto_update_done = True
            self.log_message.emit("INFO", translate_text(
                "Accélération NVIDIA (TensorRT) : mise à jour automatique vers la version {version}.",
                version=MVO_RIFE_TRT_VERSION,
            ))
            self.update()

    def set_enabled(self, enabled: bool) -> None:
        self._config.trt_enabled = bool(enabled)
        self._config.save()
        self.workflow_changed.emit()

    def set_auto_update(self, enabled: bool) -> None:
        self._config.plugins_auto_update = bool(enabled)
        self._config.save()

    # ------------------------------------------------------------ opérations

    def install(self) -> None:
        self._start("install")

    def update(self) -> None:
        self._start("update")

    def cancel(self) -> None:
        self._cancel.set()

    def _start(self, op: str) -> None:
        if self._state.busy:
            return
        self._cancel.clear()
        self._set_state(busy=op, progress=0, message="")
        self._executor.submit(self._install_job, op)

    def _progress(self, done: int, total: int) -> None:
        percent = int(done * 100 / total) if total > 0 else -1
        if percent != self._state.progress:
            self._set_state(progress=percent)

    def _install_job(self, op: str) -> None:
        try:
            installed = plugins.install(progress=self._progress, cancel=self._cancel)
        except plugins.PluginCancelled:
            self._finish(translate_text("Installation annulée."), level="WARN")
            return
        except Exception as exc:  # noqa: BLE001 — l'état « occupé » doit toujours être levé
            self._finish(translate_text("Accélération NVIDIA (TensorRT) : échec ({reason}).", reason=str(exc)), level="ERROR")
            return
        self._set_state(installed=installed, busy="warmup", progress=-1)
        self.workflow_changed.emit()
        rife = getattr(self._config, "tool_muxiveo_rife", None) or ""
        failures = plugins.warm_up(rife, installed) if rife else []
        if failures:
            self._finish(translate_text(
                "Accélération NVIDIA (TensorRT) installée, mais inutilisable sur cette machine ({reason}) : "
                "l'interpolation reste sur Vulkan.", reason="; ".join(failures[:2]),
            ), level="WARN")
        else:
            done = "mise à jour" if op == "update" else "installée"
            self._finish(translate_text(
                "Accélération NVIDIA (TensorRT) {done} : version {version}.", done=translate_text(done),
                version=installed.version,
            ), level="OK")

    def remove(self) -> None:
        if self._state.busy:
            return
        self._set_state(busy="remove", progress=-1, message="")
        self._executor.submit(self._remove_job)

    def _remove_job(self) -> None:
        complete = plugins.remove()
        self._set_state(installed=None)
        self.workflow_changed.emit()
        if complete:
            self._finish(translate_text("Accélération NVIDIA (TensorRT) supprimée."), level="OK")
        else:
            self._finish(translate_text(
                "Accélération NVIDIA (TensorRT) désactivée ; fichiers encore utilisés retirés au prochain démarrage."
            ), level="WARN")

    def _finish(self, message: str, level: str) -> None:
        self._set_state(busy="", progress=-1, message=message, installed=plugins.installed_plugin())
        self.log_message.emit(level, message)
        self.probe_requested.emit()

    def cleanup(self) -> None:
        """Nettoyage de démarrage : versions remplacées et téléchargements interrompus."""
        self._executor.submit(plugins.cleanup_orphans)

    def shutdown(self) -> None:
        self._cancel.set()
        self._executor.shutdown(wait=False, cancel_futures=True)
