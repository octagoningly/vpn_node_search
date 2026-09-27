#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv not found on PATH; install uv first, this script never downloads tools" >&2
  exit 1
fi

PY=""
FOUND_VERSION=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    FOUND_VERSION="$("$candidate" --version 2>&1)"
    if [[ "$FOUND_VERSION" =~ ^Python\ 3\.([0-9]+) ]]; then
      minor="${BASH_REMATCH[1]}"
      if (( minor >= 12 )); then
        PY="$candidate"
        break
      fi
    fi
  fi
done

if [ -z "$PY" ]; then
  if [ -z "$FOUND_VERSION" ]; then
    echo "error: python 3.12 or newer not found on PATH" >&2
  else
    echo "error: ${FOUND_VERSION} is older than python 3.12" >&2
  fi
  exit 1
fi

echo "python=$("$PY" --version 2>&1)"
echo "step=uv sync"
uv sync
echo "step=nodebench doctor"
uv run nodebench doctor
exit $?
