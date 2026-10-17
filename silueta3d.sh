#!/usr/bin/env bash
# Abre Silueta3D (Linux y macOS). La primera vez instala lo necesario.
cd "$(dirname "$0")"
if [ ! -x ".venv/bin/python" ]; then
    echo "Primera vez: se instala lo necesario."
    ./instalar.sh || exit 1
fi
exec .venv/bin/python programa/llavero_app.py "$@"
