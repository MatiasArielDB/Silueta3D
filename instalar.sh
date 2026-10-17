#!/usr/bin/env bash
# Crea el entorno de Python e instala lo necesario (solo hace falta la primera vez). Linux y macOS.
set -e
cd "$(dirname "$0")"

PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
    echo "No se encontró Python 3. Instalalo (ver README) y volvé a ejecutar este script."
    exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "Hace falta Python 3.10 o superior (tenés $("$PY" --version 2>&1))."
    exit 1
fi
if ! "$PY" -c 'import tkinter' 2>/dev/null; then
    echo "Falta tkinter (la librería de ventanas de Python). Instalalo con:"
    echo "  Ubuntu / Debian:  sudo apt install python3-tk python3-venv"
    echo "  Fedora:           sudo dnf install python3-tkinter"
    echo "  Arch:             sudo pacman -S tk"
    echo "  macOS (Homebrew): brew install python-tk"
    exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
    echo "Creando el entorno..."
    "$PY" -m venv .venv || {
        echo "No se pudo crear el entorno. En Ubuntu / Debian: sudo apt install python3-venv"
        exit 1
    }
fi
echo "Instalando dependencias..."
.venv/bin/python -m pip install --upgrade pip >/dev/null
.venv/bin/python -m pip install -r requirements.txt
echo
echo "Listo. Ahora abrí el programa con ./silueta3d.sh"
