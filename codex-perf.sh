#!/usr/bin/env sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
else
  PYTHON=python
fi

nohup "$PYTHON" "$ROOT_DIR/scripts/codex-perf-launch.py" "$@" >/dev/null 2>&1 &
exit 0
