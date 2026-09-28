#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
PY=/usr/bin/python3
if [[ ! -x .venv/bin/python ]]; then
  "$PY" -m venv --system-site-packages .venv
fi
if [[ ! -f .venv/.installed || requirements.txt -nt .venv/.installed ]]; then
  .venv/bin/pip install -q -r requirements.txt
  touch .venv/.installed
fi
exec .venv/bin/python hold_to_talk.py
