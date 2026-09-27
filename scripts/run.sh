#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE="${NODEBENCH_PROFILE:-local}"

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv not found on PATH; install uv first, this script never downloads tools" >&2
  exit 1
fi

cd "$ROOT"
mkdir -p output/logs
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="$ROOT/output/logs/run-${STAMP}.log"

echo "profile=${PROFILE} log=${LOG}"
set +e
uv run nodebench run --profile "$PROFILE" "$@" >"$LOG" 2>&1
CODE=$?
set -e

cat "$LOG"
echo "exit=${CODE} log=${LOG}"
exit "$CODE"
