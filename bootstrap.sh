#!/usr/bin/env bash
# Cross-Unix one-command bootstrap. Usage:
#   ./bootstrap.sh            -> bootstrap --update (pull + install + doctor + start + warmup + TG)
#   ./bootstrap.sh doctor     -> any `mo` subcommand (no implicit update)
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "[mo] python3 not found. Install Python 3.11+ first." >&2
  exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "[mo] creating .venv ..."
  "$PY" -m venv .venv
fi
VPY=".venv/bin/python"

if ! "$VPY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "[mo] .venv uses Python < 3.11. Remove .venv and retry with 3.11+." >&2
  exit 1
fi

if [ ! -f ".venv/.mo-ready" ]; then
  echo "[mo] installing project ..."
  "$VPY" -m pip install --upgrade pip
  "$VPY" -m pip install -e .
  : > ".venv/.mo-ready"
fi

if [ "$#" -eq 0 ]; then
  set -- bootstrap --update
fi
exec "$VPY" scripts/mo.py "$@"
