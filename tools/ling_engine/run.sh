#!/bin/sh
set -eu

APP_ROOT=/Applications/oMLX.app/Contents
RESOURCES="$APP_ROOT/Resources"
CPYTHON="$RESOURCES/Python/cpython-3.11"
MLX_SITE="$RESOURCES/Python/framework-mlx-base/lib/python3.11/site-packages"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ ! -x "$CPYTHON/bin/python3" ] || [ ! -d "$MLX_SITE" ]; then
    echo "Bundled oMLX MLX runtime is unavailable" >&2
    exit 1
fi

export PYTHONHOME="$CPYTHON"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$RESOURCES:$MLX_SITE${PYTHONPATH:+:$PYTHONPATH}"
exec "$CPYTHON/bin/python3" "$SCRIPT_DIR/cli.py" "$@"
