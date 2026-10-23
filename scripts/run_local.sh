#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
export OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 MKL_NUM_THREADS=2 NUMEXPR_NUM_THREADS=2
if [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python" ]]; then
    app_python="${VIRTUAL_ENV}/bin/python"
elif [[ -x .venv-release/bin/python ]]; then
    app_python=.venv-release/bin/python
elif [[ -x .venv/bin/python ]]; then
    app_python=.venv/bin/python
elif command -v python3 >/dev/null 2>&1; then
    app_python=python3
else
    app_python=python
fi
exec "$app_python" scripts/launch_local.py "$@"
