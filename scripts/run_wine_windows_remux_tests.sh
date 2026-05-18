#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

WINE_TEST_ROOT=${WINE_TEST_ROOT:-/tmp/Muxiveo-wine-remux-tests}
WINEPREFIX=${WINEPREFIX:-$WINE_TEST_ROOT/prefix}
KEEP_WINE_TEST_ROOT=${KEEP_WINE_TEST_ROOT:-0}
PYTHON_INSTALLER=${PYTHON_INSTALLER:-$REPO_ROOT/python-3.11.9-amd64.exe}
WINDOWS_PY="$WINEPREFIX/drive_c/Python311/python.exe"
WINDOWS_PIP="$WINEPREFIX/drive_c/Python311/Scripts/pip.exe"
WINDOWS_FFMPEG_BIN="$WINEPREFIX/drive_c/tools/ffmpeg/bin"
FFMPEG_URL=${FFMPEG_URL:-https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip}
FFMPEG_ARCHIVE="$WINE_TEST_ROOT/ffmpeg-release-essentials.zip"
FFMPEG_EXTRACT_ROOT="$WINE_TEST_ROOT/ffmpeg-extract"
TEST_ARGS=${TEST_ARGS:-"tests/test_remux_timeline_sync.py tests/test_remux_ffmpeg_workflow.py"}

export WINEPREFIX
export WINEDEBUG=${WINEDEBUG:--all}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || {
        printf 'Missing required command: %s\n' "$1" >&2
        exit 1
    }
}

require_cmd wine
require_cmd curl

PYTHON_UNIX=$(command -v python3 || command -v python || true)
if [ -z "${PYTHON_UNIX:-}" ]; then
    printf 'Missing required command: python3 (or python)\n' >&2
    exit 1
fi

mkdir -p "$WINE_TEST_ROOT"

cleanup() {
    if [ "${KEEP_WINE_TEST_ROOT}" = "1" ]; then
        return
    fi
    case "$WINE_TEST_ROOT" in
        /tmp/*) rm -rf "$WINE_TEST_ROOT" ;;
    esac
}

trap cleanup EXIT HUP INT TERM

to_wine_path() {
    printf 'Z:%s' "$(printf '%s' "$1" | sed 's,/,\\\\,g')"
}

ensure_prefix() {
    if [ ! -x "$WINDOWS_PY" ]; then
        rm -rf "$WINEPREFIX"
        mkdir -p "$WINE_TEST_ROOT"
        wineboot -u
        wine "$PYTHON_INSTALLER" /quiet InstallAllUsers=0 PrependPath=0 Include_test=0 Include_launcher=0 Include_pip=1 TargetDir=C:\\Python311
    fi
}

ensure_pytest() {
    if ! wine "$WINDOWS_PY" -c "import pytest" >/dev/null 2>&1; then
        wine "$WINDOWS_PIP" install pytest
    fi
}

ensure_windows_ffmpeg() {
    if [ -x "$WINDOWS_FFMPEG_BIN/ffmpeg.exe" ] && [ -x "$WINDOWS_FFMPEG_BIN/ffprobe.exe" ]; then
        return
    fi

    mkdir -p "$WINDOWS_FFMPEG_BIN"
    rm -rf "$FFMPEG_EXTRACT_ROOT"
    mkdir -p "$FFMPEG_EXTRACT_ROOT"

    if [ ! -f "$FFMPEG_ARCHIVE" ]; then
        curl -L "$FFMPEG_URL" -o "$FFMPEG_ARCHIVE"
    fi

    "$PYTHON_UNIX" - "$FFMPEG_ARCHIVE" "$FFMPEG_EXTRACT_ROOT" <<'PY'
import sys
import zipfile
from pathlib import Path

archive = Path(sys.argv[1])
target = Path(sys.argv[2])
with zipfile.ZipFile(archive) as zf:
    zf.extractall(target)
PY

    FFMPEG_SRC=$(find "$FFMPEG_EXTRACT_ROOT" -type f -iname 'ffmpeg.exe' | head -n 1)
    FFPROBE_SRC=$(find "$FFMPEG_EXTRACT_ROOT" -type f -iname 'ffprobe.exe' | head -n 1)
    if [ -z "${FFMPEG_SRC:-}" ] || [ -z "${FFPROBE_SRC:-}" ]; then
        printf 'Unable to locate ffmpeg.exe/ffprobe.exe in %s\n' "$FFMPEG_ARCHIVE" >&2
        exit 1
    fi

    cp "$FFMPEG_SRC" "$WINDOWS_FFMPEG_BIN/ffmpeg.exe"
    cp "$FFPROBE_SRC" "$WINDOWS_FFMPEG_BIN/ffprobe.exe"
}

run_tests() {
    REPO_WINE=$(to_wine_path "$REPO_ROOT")
    CMD="cd /d $REPO_WINE && set PATH=C:\\tools\\ffmpeg\\bin;C:\\Python311;C:\\Python311\\Scripts;%PATH% && C:\\Python311\\python.exe scripts\\windows_pytest_bootstrap.py $TEST_ARGS"
    wine cmd /c "$CMD"
}

ensure_prefix
ensure_pytest
ensure_windows_ffmpeg
run_tests
