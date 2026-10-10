"""
core/version.py — Métadonnées applicatives centralisées.
"""

from __future__ import annotations

APP_NAME = "Muxiveo"
APP_EXECUTABLE_NAME = "muxiveo"
APP_CONFIG_DIR_NAME = "muxiveo"
APP_TEMP_WORK_DIR_NAME = "Muxiveo_work"
APP_VERBOSE_LOG_PREFIX = "Muxiveo-verbose"
APP_ENV_PREFIX = "MUXIVEO"
APP_LOGGER_ROOT = "Muxiveo"
APP_REPOSITORY = "Hydro74000/Muxiveo"
APP_REPOSITORY_URL = f"https://github.com/{APP_REPOSITORY}/"
APP_WEBSITE_URL = "https://muxiveo.fr/"
APP_SCHEMA_BASE_URL = "https://muxiveo.local/schema"
APP_MACOS_BUNDLE_ID = "com.hydro74000.muxiveo"
APP_APPSTREAM_ID = "fr.aotr.muxiveo"
APP_VERSION = "4.3.0"
APP_VERSION_LABEL = f"v{APP_VERSION}"

# Extensions facultatives (dépôt séparé, releases taguées par plugin, jamais « latest »). Version épinglée de
# chacune : plancher, repli quand le flux des extensions est injoignable, version embarquée des paquets hors ligne.
# Au-delà, Muxiveo installe la version la plus récente compatible annoncée par le flux (core/plugins.py).
MUXIVEO_PLUGINS_REPOSITORY = "Hydro74000/muxiveo-plugins"
# Interpolation d'images (moteur muxiveo-rife, modèles, poids du sélecteur, préréglages) : extension mvo-rife.
MVO_RIFE_VERSION = "1.7.1"
MVO_RIFE_RELEASE_TAG = f"mvo-rife-v{MVO_RIFE_VERSION}"
# Contrat avec le moteur (muxiveo-rife --capabilities) : flux y4m, nombre de trames, stderr, codes de sortie.
MVO_RIFE_CONTRACT = 1
# Accélération NVIDIA (TensorRT for RTX) de muxiveo-rife : extension mvo-rife-trt.
MVO_RIFE_TRT_VERSION = "1.1.0"
MVO_RIFE_TRT_RELEASE_TAG = f"mvo-rife-trt-v{MVO_RIFE_TRT_VERSION}"
# Version de développement du contrat FEL ; pas de préinstallation obligatoire avant publication.
MVO_FEL_VERSION = "0.1.0"


def mvo_rife_asset_url(platform: str) -> str:
    """URL de l'archive de l'extension mvo-rife épinglée (``linux-x86_64`` → ``….tar.gz``, Windows → ``.zip``)."""
    extension = "zip" if platform.startswith("windows") else "tar.gz"
    return (
        f"https://github.com/{MUXIVEO_PLUGINS_REPOSITORY}/releases/download/{MVO_RIFE_RELEASE_TAG}/"
        f"mvo-rife-{MVO_RIFE_VERSION}-{platform}.{extension}"
    )

# Version complète du build (ex. « 4.0.0-unstable.20260924.123.abc1234 »), injectée
# par la CI de release dans core/_build_version.py ; absente en exécution depuis les sources.
try:
    from core._build_version import BUILD_VERSION as _BUILD_VERSION  # type: ignore[import-not-found]
except ImportError:
    _BUILD_VERSION = ""
APP_BUILD_VERSION = str(_BUILD_VERSION or APP_VERSION)
APP_IS_UNSTABLE_BUILD = "-unstable" in APP_BUILD_VERSION
APP_USER_AGENT = f"{APP_NAME}/{APP_VERSION}"
WRITING_APPLICATION_TAG = f"AOTR {APP_NAME} {APP_VERSION_LABEL} - {APP_REPOSITORY_URL}"
