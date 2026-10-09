"""
ui/plugin_controller.py — état et opérations des extensions (interpolation mvo-rife, accélération NVIDIA mvo-rife-trt).

Un contrôleur par extension, propriété de la fenêtre principale : la page Extensions, le tableau de bord et le
panneau Encodage lisent son état. Flux des versions publiées, téléchargement, installation, préparation des
moteurs et suppression tournent hors du thread de l'interface ; les résultats reviennent par signaux.
"""

from __future__ import annotations

import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from core import plugins
from core.config import AppConfig
from core.i18n import translate_text
from core.workflows.encode.interpolation import TrtCapability


@dataclass(frozen=True)
class PluginState:
    """Instantané d'une extension pour l'interface."""

    installed: plugins.InstalledPlugin | None = None
    target: str = ""          # version proposée : la plus récente compatible du flux, sinon l'épinglée
    busy: str = ""            # "" | "install" | "update" | "remove" | "warmup"
    progress: int = -1        # pourcentage du téléchargement, -1 si inconnu
    message: str = ""         # dernier résultat (succès ou échec), affiché sous l'état

    @property
    def update_available(self) -> bool:
        return (
            self.installed is not None and bool(self.target)
            and plugins.version_key(self.installed.version) < plugins.version_key(self.target)
        )


@dataclass(frozen=True)
class RifeState(PluginState):
    """Extension d'interpolation : ``engine`` = moteur utilisé hors extension (embarqué, config.ini, PATH)."""

    supported: bool = True
    engine: str = ""

    @property
    def compatible(self) -> bool:
        return self.supported

    @property
    def visible(self) -> bool:
        return self.supported or self.installed is not None

    @property
    def ready(self) -> bool:
        """Interpolation utilisable : extension installée, ou moteur disponible hors extension."""
        return self.installed is not None or bool(self.engine)


@dataclass(frozen=True)
class TrtState(PluginState):
    """Accélération NVIDIA (TensorRT)."""

    capability: TrtCapability | None = None

    @property
    def compatible(self) -> bool:
        return bool(self.capability and self.capability.compatible)

    @property
    def visible(self) -> bool:
        """Extension visible seulement sur une machine compatible, ou si elle est déjà installée."""
        return self.compatible or self.installed is not None

    @property
    def ready(self) -> bool:
        return bool(self.installed and self.capability and self.capability.ready)


class PluginController(QObject):
    """Flux des versions, installation, mise à jour, suppression et état d'une extension."""

    state_changed = Signal(object)        # PluginState
    log_message = Signal(str, str)        # (niveau, message)
    probe_requested = Signal()            # nouvelle sonde du GPU (moteur ou extension changé)
    workflow_changed = Signal()           # outil ou dossier de l'extension à transmettre aux encodages

    spec: plugins.PluginSpec
    label = ""                            # nom affiché (traduit à l'affichage)

    def __init__(self, config: AppConfig, state: PluginState, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._state = state
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=self.spec.id)
        self._cancel = threading.Event()
        self._auto_update_done = False
        self._feed_known = False

    # ------------------------------------------------------------------ état

    def state(self) -> PluginState:
        return self._state

    def _set_state(self, **changes: object) -> None:
        self._state = replace(self._state, **changes)  # type: ignore[arg-type]
        self.state_changed.emit(self._state)

    def _installed(self) -> plugins.InstalledPlugin | None:
        return plugins.installed_plugin(self.spec)

    def refresh_feed(self) -> None:
        """Lit en arrière-plan le flux des versions publiées (version proposée, mise à jour automatique)."""
        self._executor.submit(self._feed_job)

    def _feed_job(self) -> None:
        target = plugins.target_version(self.spec, plugins.fetch_feed(self.spec))
        self._feed_known = True
        self._set_state(target=target, installed=self._installed())
        self._maybe_auto_update()

    def _auto_update_allowed(self) -> bool:
        return self._feed_known

    def _maybe_auto_update(self) -> None:
        if (
            not self._auto_update_done and self._auto_update_allowed() and self._state.update_available
            and self._config.plugins_auto_update and not self._state.busy
        ):
            self._auto_update_done = True
            self.log_message.emit("INFO", translate_text(
                "{label} : mise à jour automatique vers la version {version}.",
                label=translate_text(self.label), version=self._state.target,
            ))
            self.update()

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
        label = translate_text(self.label)
        try:
            installed = plugins.install(
                self.spec, version=self._state.target or None, progress=self._progress, cancel=self._cancel,
            )
        except plugins.PluginCancelled:
            self._finish(translate_text("Installation annulée."), level="WARN")
            return
        except Exception as exc:  # noqa: BLE001 — l'état « occupé » doit toujours être levé
            self._finish(translate_text("{label} : échec ({reason}).", label=label, reason=str(exc)), level="ERROR")
            return
        self._set_state(installed=installed)
        self._changed(installed)
        self.workflow_changed.emit()
        failure = self._verify(installed)
        if failure:
            self._finish(failure, level="WARN")
            return
        done = "mise à jour" if op == "update" else "installée"
        self._finish(translate_text(
            "{label} {done} : version {version}.", label=label, done=translate_text(done), version=installed.version,
        ), level="OK")

    def _changed(self, installed: plugins.InstalledPlugin | None) -> None:
        """Après l'activation ou la suppression d'une version (thread de travail)."""

    def _verify(self, installed: plugins.InstalledPlugin) -> str:
        """Contrôle de bon fonctionnement après installation ; message d'avertissement, "" si correct."""
        return ""

    def remove(self) -> None:
        if self._state.busy:
            return
        self._set_state(busy="remove", progress=-1, message="")
        self._executor.submit(self._remove_job)

    def _remove_job(self) -> None:
        label = translate_text(self.label)
        complete = plugins.remove(self.spec)
        self._set_state(installed=None)
        self._changed(None)
        self.workflow_changed.emit()
        if complete:
            self._finish(translate_text("{label} supprimée.", label=label), level="OK")
        else:
            self._finish(translate_text(
                "{label} désactivée ; fichiers encore utilisés retirés au prochain démarrage.", label=label,
            ), level="WARN")

    def _finish(self, message: str, level: str) -> None:
        self._set_state(busy="", progress=-1, message=message, installed=self._installed())
        self.log_message.emit(level, message)
        self.probe_requested.emit()

    def cleanup(self) -> None:
        """Nettoyage de démarrage : versions remplacées et téléchargements interrompus."""
        self._executor.submit(plugins.cleanup_orphans, self.spec)

    def shutdown(self) -> None:
        self._cancel.set()
        self._executor.shutdown(wait=False, cancel_futures=True)


class RifePluginController(PluginController):
    """Extension d'interpolation d'images (mvo-rife) : moteur muxiveo-rife, modèles, préréglages."""

    spec = plugins.RIFE
    label = "Interpolation d'images (MVO-RIFE)"

    def __init__(self, config: AppConfig, parent: QObject | None = None) -> None:
        installed = plugins.installed_plugin(plugins.RIFE)
        super().__init__(config, RifeState(
            installed=installed, target=plugins.RIFE.min_version,
            supported=plugins.RIFE.supports(plugins.platform_tag()), engine=self._engine(config, installed),
        ), parent)

    def state(self) -> RifeState:
        return self._state  # type: ignore[return-value]

    @staticmethod
    def _engine(config: AppConfig, installed: plugins.InstalledPlugin | None) -> str:
        """Moteur hors extension (paquet hors ligne, config.ini, PATH), "" s'il n'y en a pas."""
        if installed is not None:
            return ""
        tool = str(getattr(config, "tool_muxiveo_rife", "") or "")
        return tool if tool and (shutil.which(tool) or Path(tool).is_file()) else ""

    def _changed(self, installed: plugins.InstalledPlugin | None) -> None:
        self._config.refresh_rife_tool()
        self._set_state(engine=self._engine(self._config, installed))


class TrtPluginController(PluginController):
    """Accélération NVIDIA (mvo-rife-trt) : compatibilité sondée par le tableau de bord, préchauffage."""

    spec = plugins.TRT
    label = "Accélération NVIDIA (TensorRT)"

    def __init__(self, config: AppConfig, parent: QObject | None = None) -> None:
        super().__init__(config, TrtState(installed=plugins.installed_plugin(plugins.TRT),
                                          target=plugins.TRT.min_version), parent)

    def state(self) -> TrtState:
        return self._state  # type: ignore[return-value]

    def plugin_dir(self) -> str:
        """Dossier de l'extension à passer à muxiveo-rife ("" : désactivée ou absente)."""
        installed = self._state.installed
        return str(installed.path) if installed and self._config.trt_enabled else ""

    def set_capability(self, capability: TrtCapability) -> None:
        """Compatibilité sondée par le tableau de bord ; déclenche une mise à jour automatique éventuelle."""
        self._set_state(capability=capability, installed=self._installed())
        self._maybe_auto_update()

    def _auto_update_allowed(self) -> bool:
        return self._feed_known and self.state().capability is not None

    def set_enabled(self, enabled: bool) -> None:
        self._config.trt_enabled = bool(enabled)
        self._config.save()
        self.workflow_changed.emit()

    def _verify(self, installed: plugins.InstalledPlugin) -> str:
        self._set_state(busy="warmup", progress=-1)
        rife = getattr(self._config, "tool_muxiveo_rife", None) or ""
        failures = plugins.warm_up(rife, installed) if rife else []
        if not failures:
            return ""
        return translate_text(
            "Accélération NVIDIA (TensorRT) installée, mais inutilisable sur cette machine ({reason}) : "
            "l'interpolation reste sur Vulkan.", reason="; ".join(failures[:2]),
        )
