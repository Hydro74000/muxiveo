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
APP_VERSION = "4.0.1"
APP_VERSION_LABEL = f"v{APP_VERSION}"

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
