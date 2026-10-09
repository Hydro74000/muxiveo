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

# Outil natif muxiveo-rife (native/muxiveo-rife) : release GitHub épinglée,
# publiée par le workflow muxiveo-rife.yml sur ce même dépôt.
MUXIVEO_RIFE_VERSION = "1.6.0"
MUXIVEO_RIFE_RELEASE_TAG = f"muxiveo-rife-v{MUXIVEO_RIFE_VERSION}"

# Extensions facultatives (dépôt séparé, releases taguées par plugin, jamais « latest »).
MUXIVEO_PLUGINS_REPOSITORY = "Hydro74000/muxiveo-plugins"
# Accélération NVIDIA (TensorRT for RTX) de muxiveo-rife : version épinglée du plugin mvo-rife-trt.
MVO_RIFE_TRT_VERSION = "1.1.0"
MVO_RIFE_TRT_RELEASE_TAG = f"mvo-rife-trt-v{MVO_RIFE_TRT_VERSION}"


def muxiveo_rife_asset_url(platform_suffix: str) -> str:
    """URL de l'archive muxiveo-rife (ex. ``linux-x86_64.tar.gz``) de la release épinglée."""
    return (
        f"https://github.com/{APP_REPOSITORY}/releases/download/{MUXIVEO_RIFE_RELEASE_TAG}/"
        f"muxiveo-rife-{MUXIVEO_RIFE_VERSION}-{platform_suffix}"
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
