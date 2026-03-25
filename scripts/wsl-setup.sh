#!/bin/bash
# wsl-setup.sh — prepare WSL environment for running codesearch tests.
#
# - Creates the Python venv at ~/.local/indexserver-venv/ if absent
# - Installs/updates required packages in the venv
# - Downloads the Typesense binary to ~/.local/typesense/ if absent
# - With --reset: kills any running Typesense/api.py, wipes TYPESENSE_DATA
#
# Environment variables (all optional):
#   TYPESENSE_VERSION  default: 27.1
#   TYPESENSE_DATA     default: /tmp/codesearch-wsl-test
#   CODESEARCH_PORT    default: 8108

set -e

TYPESENSE_VERSION="${TYPESENSE_VERSION:-27.1}"
TYPESENSE_DATA="${TYPESENSE_DATA:-/tmp/codesearch-wsl-test}"
CODESEARCH_PORT="${CODESEARCH_PORT:-8108}"
VENV_DIR="$HOME/.local/indexserver-venv"
TYPESENSE_DIR="$HOME/.local/typesense"

RESET=0
for arg in "$@"; do
    case "$arg" in --reset) RESET=1 ;; esac
done

# ── Python venv ────────────────────────────────────────────────────────────────

if [ ! -x "${VENV_DIR}/bin/python3" ]; then
    echo "[wsl-setup] Creating Python venv at ${VENV_DIR}..."
    python3 -m venv "${VENV_DIR}"
fi

# Bootstrap pip if it wasn't installed with the venv (common on Debian/Ubuntu
# when python3-distutils or ensurepip is missing).
if [ ! -x "${VENV_DIR}/bin/pip" ]; then
    echo "[wsl-setup] pip missing from venv — bootstrapping with ensurepip..."
    "${VENV_DIR}/bin/python3" -m ensurepip --upgrade 2>/dev/null || {
        echo "[wsl-setup] ensurepip failed; trying get-pip.py..."
        curl -fsSL https://bootstrap.pypa.io/get-pip.py | "${VENV_DIR}/bin/python3"
    }
fi

echo "[wsl-setup] Installing/updating Python packages..."
"${VENV_DIR}/bin/python3" -m pip install --quiet --upgrade \
    typesense \
    tree-sitter \
    tree-sitter-c-sharp \
    tree-sitter-python \
    tree-sitter-rust \
    tree-sitter-javascript \
    tree-sitter-typescript \
    tree-sitter-cpp \
    watchdog \
    pathspec \
    pytest

# ── Typesense binary ───────────────────────────────────────────────────────────

mkdir -p "${TYPESENSE_DIR}"
if [ ! -x "${TYPESENSE_DIR}/typesense-server" ]; then
    echo "[wsl-setup] Downloading Typesense ${TYPESENSE_VERSION}..."
    curl -fsSL \
        "https://dl.typesense.org/releases/${TYPESENSE_VERSION}/typesense-server-${TYPESENSE_VERSION}-linux-amd64.tar.gz" \
        | tar -xz -C "${TYPESENSE_DIR}"
    chmod +x "${TYPESENSE_DIR}/typesense-server"
    echo "[wsl-setup] Typesense binary ready."
fi

# ── Reset: kill existing processes, wipe data dir ─────────────────────────────

if [ "${RESET}" = "1" ]; then
    echo "[wsl-setup] Killing existing Typesense and api.py processes..."
    pkill -9 -f "typesense-server" 2>/dev/null || true
    pkill -9 -f "indexserver/api.py" 2>/dev/null || true
    sleep 2

    echo "[wsl-setup] Wiping ${TYPESENSE_DATA}..."
    rm -rf "${TYPESENSE_DATA}"
    echo "[wsl-setup] Reset done."
fi

mkdir -p "${TYPESENSE_DATA}"

echo "[wsl-setup] Done."
