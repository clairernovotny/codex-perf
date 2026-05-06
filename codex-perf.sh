#!/usr/bin/env sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
else
  PYTHON=python
fi

"$PYTHON" "$ROOT_DIR/scripts/fix-codex-perf.py" stop -y
set +e
"$PYTHON" "$ROOT_DIR/scripts/fix-codex-perf.py" status --quiet --exit-code
STATUS=$?
set -e
if [ "$STATUS" -eq 2 ]; then
  "$PYTHON" "$ROOT_DIR/scripts/fix-codex-perf.py" repair -y
elif [ "$STATUS" -ne 0 ]; then
  exit "$STATUS"
fi

exec "$PYTHON" "$ROOT_DIR/scripts/codex-perf-launch.py" --no-measure "$@"
