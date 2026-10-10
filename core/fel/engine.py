"""ABI C du plugin FEL : classification et production bornée vers un pipe."""
from __future__ import annotations

import ctypes
import json
import threading
import weakref
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import IO, Any, Callable

from core import plugins
from core.fel.devices import FelDevice, FelDevicePlan


class FelError(RuntimeError):
    """Erreur de reconstruction autorisant une reprise complète depuis le BL."""


class FelCancelled(RuntimeError):
    """Annulation, jamais éligible au repli BL."""


_Write = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t)
_Progress = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int64)


class FelEngine:
    """Bibliothèque privée du plugin, conservée en mémoire pendant le job."""

    def __init__(self, library: Path) -> None:
        self.path = library
        if (library.parent / "manifest.json").is_file():
            try:
                lease = plugins.PluginLease(library.parent)
                weakref.finalize(self, lease.release)
            except (OSError, plugins.PluginError) as exc:
                raise FelError(str(exc)) from exc
        try:
            self.lib = ctypes.CDLL(str(library))
            self.lib.mvo_fel_abi_version.restype = ctypes.c_uint32
            if self.lib.mvo_fel_abi_version() != plugins.FEL_PLUGIN_ABI:
                raise FelError("Interface du plugin FEL incompatible")
            self.lib.mvo_fel_capabilities.restype = ctypes.c_char_p
            capabilities = json.loads(self.lib.mvo_fel_capabilities())
            self.capabilities = capabilities
            if capabilities.get("device_selection") == 1:
                self.lib.mvo_fel_devices.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
                self.lib.mvo_fel_devices.restype = ctypes.c_size_t
                self.lib.mvo_fel_set_device.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
                self.lib.mvo_fel_set_device.restype = ctypes.c_int
                self.lib.mvo_fel_backend.argtypes = [ctypes.c_void_p]
                self.lib.mvo_fel_backend.restype = ctypes.c_char_p
            if capabilities.get("transport") != "nut" or capabilities.get("pixel_format") != "gbrp16le":
                raise FelError("Capacités du plugin FEL incompatibles")
            self.lib.mvo_fel_classify.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
            self.lib.mvo_fel_classify.restype = ctypes.c_int
            self.lib.mvo_fel_create.argtypes = [ctypes.c_char_p, *([ctypes.c_int] * 4)]
            self.lib.mvo_fel_create.restype = ctypes.c_void_p
            self.lib.mvo_fel_run.argtypes = [ctypes.c_void_p, _Write, _Progress, ctypes.c_void_p]
            self.lib.mvo_fel_run.restype = ctypes.c_int
            self.lib.mvo_fel_error.argtypes = [ctypes.c_void_p]
            self.lib.mvo_fel_error.restype = ctypes.c_char_p
            for name in ("mvo_fel_cancel", "mvo_fel_destroy"):
                getattr(self.lib, name).argtypes = [ctypes.c_void_p]
                getattr(self.lib, name).restype = None
        except (OSError, AttributeError, ValueError) as exc:
            raise FelError(f"Chargement de mvo-fel impossible : {exc}") from exc

    @classmethod
    def installed(cls) -> FelEngine:
        installed = plugins.installed_plugin(plugins.FEL)
        if installed is None:
            raise FelError("Extension mvo-fel absente ou incompatible ; disponible dans Extensions")
        return cls(plugins.main_file(plugins.FEL, installed))

    def devices(self) -> tuple[FelDevice, ...]:
        """Énumération dynamique sans démarrer de décodeur ni d'encodeur."""
        if self.capabilities.get("device_selection") != 1:
            return ()
        size = self.lib.mvo_fel_devices(None, 0)
        if not 0 < size <= 65536:
            raise FelError("Énumération des GPU FEL impossible")
        buffer = ctypes.create_string_buffer(size)
        actual = self.lib.mvo_fel_devices(buffer, size)
        if actual != size:
            raise FelError("La liste des GPU FEL a changé pendant sa lecture")
        try:
            return tuple(FelDevice(**item) for item in json.loads(buffer.value))
        except (TypeError, ValueError) as exc:
            raise FelError("Liste des GPU FEL invalide") from exc

    def classify(self, path: Path, cancelled: Callable[[], bool]) -> str:
        """Valide chaque RPU original sans charger le fichier complet en mémoire."""
        classification: set[int] = set()
        count = 0
        buffer = b""

        def inspect(nal: bytes) -> None:
            nonlocal count
            if cancelled():
                raise FelCancelled()
            result = self.lib.mvo_fel_classify(nal, len(nal))
            if result < 0:
                raise FelError(f"RPU invalide à la trame {count}")
            if result != 3:
                classification.add(result)
            count += 1

        with path.open("rb") as source:
            while chunk := source.read(65536):
                buffer += chunk
                # Les fichiers RPU dovi_tool utilisent les start codes Annex B à quatre octets.
                while (end := buffer.find(b"\0\0\0\1", 4)) >= 0:
                    inspect(buffer[:end])
                    buffer = buffer[end:]
                if len(buffer) > (1 << 20):
                    raise FelError("NAL RPU trop volumineux ou fichier non Annex B")
            if buffer:
                inspect(buffer)
        if not count or not classification:
            raise FelError("RPU vide ou sans mapping initial")
        if len(classification) != 1:
            raise FelError("Type de couche variable dans le RPU")
        return {0: "sans_el", 1: "fel", 2: "mel"}[classification.pop()]


@dataclass(frozen=True)
class FelSource:
    """Paramètres immuables : un nouveau contexte natif est créé pour chaque passe."""

    engine: FelEngine
    path: Path
    stream: int
    threads: int = 1
    frame_rate: str = ""
    device_plan: FelDevicePlan | None = None

    def start(self, output: IO[Any], cancelled: threading.Event) -> FelProducer:
        return FelProducer(self, output, cancelled)

    def describe(self) -> str:
        return f"Reconstruction FEL interne (mvo-fel), piste {self.stream} → NUT RGB PQ"


class FelProducer:
    """Thread natif supervisé ; le pipe du consommateur assure la contre-pression."""

    def __init__(self, source: FelSource, output: IO[Any], cancelled: threading.Event) -> None:
        self.source, self.output, self.cancelled = source, output, cancelled
        self.error: BaseException | None = None
        self.frames = 0
        self._stop = threading.Event()
        rate = Fraction(source.frame_rate or "0")
        self._handle = source.engine.lib.mvo_fel_create(
            str(source.path).encode("utf-8"), source.stream, source.threads, rate.numerator, rate.denominator,
        )
        if not self._handle:
            raise FelError("Création du contexte FEL impossible")
        self._release_device: Callable[[], None] = lambda: None
        if source.device_plan is not None:
            try:
                device, self._release_device = source.device_plan.acquire()
                if source.engine.capabilities.get("device_selection") == 1:
                    if source.engine.lib.mvo_fel_set_device(self._handle, device.encode("ascii")) != 0:
                        raise FelError("Le plugin refuse le moteur FEL sélectionné")
                elif device != "cpu":
                    raise FelError("Ce plugin FEL ne permet pas de choisir un GPU")
            except BaseException:
                self._release_device()
                source.engine.lib.mvo_fel_destroy(self._handle)
                self._handle = None
                raise
        self._thread = threading.Thread(target=self._run, name="mvo-fel", daemon=True)
        self._watcher = threading.Thread(target=self._watch, name="mvo-fel-cancel", daemon=True)
        try:
            # Le producteur ne doit jamais écrire sans surveillant opérationnel.
            self._watcher.start()
            self._thread.start()
        except BaseException:
            self._stop.set()
            if self._watcher.ident is not None:
                self._watcher.join()
            self._release_device()
            source.engine.lib.mvo_fel_destroy(self._handle)
            self._handle = None
            self.output.close()
            raise

    def _watch(self) -> None:
        while not self._stop.wait(.05):
            if self.cancelled.is_set():
                self.source.engine.lib.mvo_fel_cancel(self._handle)
                return

    def _run(self) -> None:
        lib = self.source.engine.lib
        callback_error: BaseException | None = None

        def write(_opaque: object, data: int, size: int) -> int:
            nonlocal callback_error
            try:
                remaining = memoryview(ctypes.string_at(data, size))
                while remaining:
                    written = self.output.write(remaining)
                    if written is None or written <= 0:
                        raise OSError("Écriture du pipe FEL interrompue")
                    remaining = remaining[written:]
                return 0
            except (OSError, ValueError) as exc:
                callback_error = exc
                return 1

        def progress(_opaque: object, frames: int) -> None:
            self.frames = frames

        try:
            status = lib.mvo_fel_run(self._handle, _Write(write), _Progress(progress), None)
            if self.cancelled.is_set() or status == 3:
                self.error = FelCancelled()
            elif status == 1:
                self.error = FelError(lib.mvo_fel_error(self._handle).decode("utf-8", errors="replace"))
            elif status == 2:
                self.error = callback_error or BrokenPipeError("Consommateur FEL fermé")
            elif status == 4:
                self.error = OSError(lib.mvo_fel_error(self._handle).decode("utf-8", errors="replace"))
            elif status != 0:
                self.error = RuntimeError(f"Statut inconnu du plugin FEL : {status}")
        except BaseException as exc:
            self.error = exc
        finally:
            self._stop.set()
            try:
                if self.source.device_plan is not None:
                    if self.source.engine.capabilities.get("device_selection") == 1:
                        backend = (lib.mvo_fel_backend(self._handle) or b"inconnu").decode("utf-8", errors="replace")
                        self.source.device_plan.log("INFO", f"FEL — moteur exécuté : {backend} ; {self.frames} images.")
            except RuntimeError:
                # L'émetteur Qt peut déjà être fermé pendant l'annulation.
                pass
            finally:
                self._release_device()
                try:
                    self.output.close()
                except OSError:
                    pass

    def check_error(self) -> None:
        """Propage les erreurs ; seul EPIPE est attendu si l'aval s'arrête tôt."""
        if self.error is not None and not isinstance(self.error, BrokenPipeError):
            raise self.error

    def finish(self) -> None:
        self._thread.join()
        self._watcher.join()
        if self._handle:
            self.source.engine.lib.mvo_fel_destroy(self._handle)
            self._handle = None

    def cancel(self) -> None:
        if self._handle:
            self.source.engine.lib.mvo_fel_cancel(self._handle)
