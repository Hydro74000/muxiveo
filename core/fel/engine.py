"""ABI C du plugin FEL : classification et production bornée vers un pipe."""
from __future__ import annotations

import ctypes
import io
import json
import sys
import threading
import weakref
from dataclasses import dataclass, fields
from fractions import Fraction
from pathlib import Path
from typing import IO, Any, Callable

from core import plugins
from core.fel.devices import PRECISION_LABELS, FelDevice, FelDevicePlan


class FelError(RuntimeError):
    """Erreur de reconstruction autorisant une reprise complète depuis le BL."""


class FelCancelled(RuntimeError):
    """Annulation, jamais éligible au repli BL."""


_Write = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t)
_Progress = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int64)


def precision_supported(engine: object) -> bool:
    """Précisions exacte et standard sur GPU (plugin ≥ 0.2.0) ; le CPU est toujours exact."""
    capabilities = getattr(engine, "capabilities", None)
    return isinstance(capabilities, dict) and {"exact", "standard", "fast"} <= set(capabilities.get("precision") or [])


def render_workers_range(engine: object) -> tuple[int, int] | None:
    """Bornes du nombre d'images en vol du rendu GPU (capacité render_workers), sinon None."""
    capabilities = getattr(engine, "capabilities", None)
    bounds = capabilities.get("render_workers") if isinstance(capabilities, dict) else None
    if (isinstance(bounds, list) and len(bounds) == 2 and all(type(v) is int for v in bounds)
            and 1 <= bounds[0] <= bounds[1]):
        return bounds[0], bounds[1]
    return None


def frame_statistics_supported(engine: object) -> bool:
    """Statistiques de l'image produite (plugin ≥ 0.3.0) : HDR10+ et MaxCLL/MaxFALL recalculés."""
    capabilities = getattr(engine, "capabilities", None)
    return isinstance(capabilities, dict) and capabilities.get("frame_statistics") == 1


def measure_supported(engine: object, layer: str = "base") -> bool:
    """Mesure seule (plugin ≥ 0.4.0) : « base » (BL lue comme HDR10) ou « reconstructed »."""
    capabilities = getattr(engine, "capabilities", None)
    return isinstance(capabilities, dict) and layer in (capabilities.get("measure") or [])


def layers_supported(engine: object, layer: str) -> bool:
    """Couche produite choisie (plugin ≥ 0.4.0) : « base », « mapped » ou « reconstructed »."""
    capabilities = getattr(engine, "capabilities", None)
    return isinstance(capabilities, dict) and layer in (capabilities.get("layers") or [])


def _base_only_frames(statistics: str) -> int:
    """Images sans RPU ni EL recomposées sans résiduel (plugin ≥ 0.2.1), 0 si absent ou illisible."""
    try:
        value = json.loads(statistics).get("bl_only_frames", 0)
    except (ValueError, AttributeError):
        return 0
    return value if type(value) is int and value > 0 else 0


class FelEngine:
    """Bibliothèque privée du plugin, conservée en mémoire pendant le job."""

    def __init__(self, library: Path, *, direct_ffmpeg: Path | None = None) -> None:
        self.path = library
        # Prototype fourni explicitement ; aucun remplacement du FFmpeg configuré.
        self.direct_ffmpeg = direct_ffmpeg
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
            if capabilities.get("statistics") == 1:
                self.lib.mvo_fel_statistics.argtypes = [ctypes.c_void_p]
                self.lib.mvo_fel_statistics.restype = ctypes.c_char_p
            if capabilities.get("device_selection") == 1:
                self.lib.mvo_fel_devices.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
                self.lib.mvo_fel_devices.restype = ctypes.c_size_t
                self.lib.mvo_fel_set_device.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
                self.lib.mvo_fel_set_device.restype = ctypes.c_int
                self.lib.mvo_fel_backend.argtypes = [ctypes.c_void_p]
                self.lib.mvo_fel_backend.restype = ctypes.c_char_p
            if "float64" in (capabilities.get("device_features") or []):
                self.lib.mvo_fel_devices_features.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
                self.lib.mvo_fel_devices_features.restype = ctypes.c_size_t
            if precision_supported(self):
                self.lib.mvo_fel_set_precision.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
                self.lib.mvo_fel_set_precision.restype = ctypes.c_int
            if render_workers_range(self) is not None:
                self.lib.mvo_fel_set_render_workers.argtypes = [ctypes.c_void_p, ctypes.c_int]
                self.lib.mvo_fel_set_render_workers.restype = ctypes.c_int
            if capabilities.get("fd_output") == 1:
                self.lib.mvo_fel_run_fd.argtypes = [ctypes.c_void_p, ctypes.c_ssize_t, _Progress, ctypes.c_void_p]
                self.lib.mvo_fel_run_fd.restype = ctypes.c_int
            if capabilities.get("crop") == 1:
                self.lib.mvo_fel_set_crop.argtypes = [ctypes.c_void_p, *([ctypes.c_int] * 4)]
                self.lib.mvo_fel_set_crop.restype = ctypes.c_int
            if "zero" in (capabilities.get("timestamps") or []):
                self.lib.mvo_fel_set_timestamps.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
                self.lib.mvo_fel_set_timestamps.restype = ctypes.c_int
            if "yuv420p10le" in (capabilities.get("output_formats") or []):
                self.lib.mvo_fel_set_output.argtypes = [ctypes.c_void_p, *([ctypes.c_char_p] * 3)]
                self.lib.mvo_fel_set_output.restype = ctypes.c_int
            if capabilities.get("seek") == 1:
                self.lib.mvo_fel_set_start.argtypes = [ctypes.c_void_p, ctypes.c_int64]
                self.lib.mvo_fel_set_start.restype = ctypes.c_int
            if capabilities.get("frame_statistics") == 1:
                self.lib.mvo_fel_set_frame_statistics.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
                self.lib.mvo_fel_set_frame_statistics.restype = ctypes.c_int
            if capabilities.get("measure"):
                self.lib.mvo_fel_set_measure.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
                self.lib.mvo_fel_set_measure.restype = ctypes.c_int
            if capabilities.get("layers"):
                self.lib.mvo_fel_set_layer.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
                self.lib.mvo_fel_set_layer.restype = ctypes.c_int
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
        direct_name = installed.manifest.get("direct_ffmpeg")
        direct = (installed.path / direct_name if isinstance(direct_name, str)
                  and Path(direct_name).name == direct_name else None)
        return cls(plugins.main_file(plugins.FEL, installed), direct_ffmpeg=direct)

    @property
    def supports_precision(self) -> bool:
        return precision_supported(self)

    def measure(
        self,
        path: Path,
        stream: int,
        statistics_path: Path,
        *,
        layer: str = "base",
        device: str = "auto",
        precision: str = "fast",
        threads: int = 8,
        progress: Callable[[int], None] | None = None,
        cancelled: threading.Event | None = None,
    ) -> str:
        """Mesure seule (aucune image produite) : statistiques de chaque image dans ``statistics_path``.

        Bloquant ; ``cancelled`` interrompt le plugin. Renvoie le moteur exécuté (backend).
        """
        if not measure_supported(self, layer):
            raise FelError("Mesure de l'image indisponible : extension mvo-fel 0.4.0 ou plus récente requise")
        lib = self.lib
        handle = lib.mvo_fel_create(str(path).encode("utf-8"), stream, max(1, min(16, threads)), 0, 0)
        if not handle:
            raise FelError("Création du contexte FEL impossible")
        stop = threading.Event()

        def watch() -> None:
            while not stop.wait(.05):
                if cancelled is not None and cancelled.is_set():
                    lib.mvo_fel_cancel(handle)
                    return

        watcher = threading.Thread(target=watch, name="mvo-fel-measure-cancel", daemon=True)
        try:
            if lib.mvo_fel_set_frame_statistics(handle, str(statistics_path).encode("utf-8")) != 0:
                raise FelError("Statistiques de l'image refusées par le plugin")
            if lib.mvo_fel_set_measure(handle, layer.encode("ascii")) != 0:
                raise FelError("Mesure refusée par le plugin")
            if self.capabilities.get("device_selection") == 1 and lib.mvo_fel_set_device(
                    handle, device.encode("ascii")) != 0:
                raise FelError("Le plugin refuse le moteur FEL sélectionné")
            if precision_supported(self) and lib.mvo_fel_set_precision(handle, precision.encode("ascii")) != 0:
                raise FelError("Précision FEL refusée par le plugin")
            watcher.start()
            # Pointeur d'écriture nul (mesure seule) : instance vide du type de callback.
            status = lib.mvo_fel_run(handle, _Write(), _Progress(lambda _o, frames: progress(frames) if progress else None),
                                     None)
            if (cancelled is not None and cancelled.is_set()) or status == 3:
                raise FelCancelled()
            if status == 4:
                raise OSError(lib.mvo_fel_error(handle).decode("utf-8", errors="replace"))
            if status != 0:
                raise FelError(lib.mvo_fel_error(handle).decode("utf-8", errors="replace"))
            backend = lib.mvo_fel_backend(handle) if self.capabilities.get("device_selection") == 1 else b"cpu"
            return (backend or b"").decode("utf-8", errors="replace")
        finally:
            stop.set()
            if watcher.ident is not None:
                watcher.join()
            lib.mvo_fel_destroy(handle)

    def devices(self) -> tuple[FelDevice, ...]:
        """Énumération dynamique sans démarrer de décodeur ni d'encodeur."""
        if self.capabilities.get("device_selection") != 1:
            return ()
        # Liste enrichie (double natif) si annoncée ; clés inconnues ignorées (versions futures).
        listing = (self.lib.mvo_fel_devices_features if "float64" in (self.capabilities.get("device_features") or [])
                   else self.lib.mvo_fel_devices)
        size = listing(None, 0)
        if not 0 < size <= 65536:
            raise FelError("Énumération des GPU FEL impossible")
        buffer = ctypes.create_string_buffer(size)
        actual = listing(buffer, size)
        if actual != size:
            raise FelError("La liste des GPU FEL a changé pendant sa lecture")
        known = {item.name for item in fields(FelDevice)}
        try:
            return tuple(FelDevice(**{k: v for k, v in item.items() if k in known}) for item in json.loads(buffer.value))
        except (TypeError, ValueError, AttributeError) as exc:
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
    # Départ dans la source (sémantique -ss d'entrée de FFmpeg), en microsecondes.
    start_us: int = 0
    # Images produites : RGB PQ 16 bits, ou Y'CbCr 4:2:0 final tramé par le plugin.
    pixel_format: str = "gbrp16le"
    chroma_location: str = "left"
    dither: str = "blue"
    # "zero" : PTS relatifs à la première image, comme FFmpeg à l'entrée d'un NUT.
    timestamps: str = "source"
    # Marges paires (gauche, haut, droite, bas) retirées par le plugin, sorties Y'CbCr.
    crop: tuple[int, int, int, int] = (0, 0, 0, 0)
    # Précision du rendu GPU (exact, standard, fast) ; transmise si le plugin la connaît.
    precision: str = "exact"
    # Images en vol du rendu GPU ; None : défaut du plugin. Transmis si le plugin l'accepte.
    render_workers: int | None = None
    # Statistiques de chaque image produite (JSON Lines, capacité frame_statistics) ; None : aucune.
    statistics_path: Path | None = None
    # Couche produite (capacité layers) : reconstructed (défaut), mapped (courbes du RPU sans
    # résiduel) ou base (BL lue comme HDR10).
    layer: str = "reconstructed"

    def start(self, output: IO[Any], cancelled: threading.Event) -> FelProducer:
        return FelProducer(self, output, cancelled)

    def describe(self) -> str:
        start = f", départ {self.start_us / 1e6:.3f} s" if self.start_us else ""
        output = "RGB PQ" if self.pixel_format == "gbrp16le" else f"{self.pixel_format} tramé ({self.dither})"
        precision = f", précision {PRECISION_LABELS.get(self.precision, self.precision)}" if precision_supported(self.engine) else ""
        return f"Reconstruction FEL interne (mvo-fel), piste {self.stream}{start}{precision} → NUT {output}"


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
        refused = ""
        if source.start_us and source.engine.lib.mvo_fel_set_start(self._handle, source.start_us) != 0:
            refused = "Départ FEL refusé par le plugin"
        elif source.pixel_format != "gbrp16le" and source.engine.lib.mvo_fel_set_output(
                self._handle, source.pixel_format.encode("ascii"), source.chroma_location.encode("ascii"),
                source.dither.encode("ascii")) != 0:
            refused = "Format de sortie FEL refusé par le plugin"
        elif any(source.crop) and source.engine.lib.mvo_fel_set_crop(self._handle, *source.crop) != 0:
            refused = "Recadrage FEL refusé par le plugin"
        elif source.timestamps != "source" and source.engine.lib.mvo_fel_set_timestamps(
                self._handle, source.timestamps.encode("ascii")) != 0:
            refused = "Horodatages FEL refusés par le plugin"
        elif precision_supported(source.engine) and source.engine.lib.mvo_fel_set_precision(
                self._handle, source.precision.encode("ascii")) != 0:
            refused = "Précision FEL refusée par le plugin"
        elif (source.render_workers is not None and (bounds := render_workers_range(source.engine)) is not None
              and source.engine.lib.mvo_fel_set_render_workers(
                  self._handle, min(max(source.render_workers, bounds[0]), bounds[1])) != 0):
            refused = "Nombre d'images en vol FEL refusé par le plugin"
        elif source.statistics_path is not None and (
                not frame_statistics_supported(source.engine)
                or source.engine.lib.mvo_fel_set_frame_statistics(
                    self._handle, str(source.statistics_path).encode("utf-8")) != 0):
            refused = "Statistiques de l'image refusées par le plugin"
        elif source.layer != "reconstructed" and (
                not layers_supported(source.engine, source.layer)
                or source.engine.lib.mvo_fel_set_layer(self._handle, source.layer.encode("ascii")) != 0):
            refused = "Couche FEL refusée par le plugin (extension mvo-fel 0.4.0 ou plus récente requise)"
        if refused:
            source.engine.lib.mvo_fel_destroy(self._handle)
            self._handle = None
            raise FelError(refused)
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
            target = self._native_target()
            if target is not None:
                # Le plugin écrit lui-même sur le pipe : ni GIL ni copie Python par image.
                status = lib.mvo_fel_run_fd(self._handle, target, _Progress(progress), None)
            else:
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
                    if self.source.engine.capabilities.get("statistics") == 1:
                        timings = self.source.engine.lib.mvo_fel_statistics(self._handle).decode("utf-8")
                        self.source.device_plan.log("INFO", f"FEL — mesures internes : {timings}")
                        base_only = _base_only_frames(timings)
                        if base_only:
                            self.source.device_plan.log(
                                "WARN",
                                f"FEL — {base_only} image(s) source sans RPU ni couche EL : recomposée(s) sans "
                                "résiduel, avec la courbe du RPU précédent.",
                            )
            except RuntimeError:
                # L'émetteur Qt peut déjà être fermé pendant l'annulation.
                pass
            finally:
                self._release_device()
                try:
                    self.output.close()
                except OSError:
                    pass

    def _native_target(self) -> int | None:
        """Descripteur (POSIX) ou HANDLE (Windows) du pipe, si le plugin sait y écrire."""
        if getattr(self.source.engine, "capabilities", {}).get("fd_output") != 1:
            return None
        try:
            self.output.flush()
            descriptor = self.output.fileno()
        except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
            return None
        if not isinstance(descriptor, int):
            return None
        if sys.platform == "win32":
            import msvcrt
            return int(msvcrt.get_osfhandle(descriptor))
        return descriptor

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
