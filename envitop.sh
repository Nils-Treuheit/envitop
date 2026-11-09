#!/usr/bin/env bash
# Run envitop straight from a checkout, without installing it.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NATIVE="$HERE/native/target/release/envitop-native"
if [[ "${ENVITOP_NATIVE:-0}" == "1" && "${ENVITOP_PYTHON:-0}" != "1" ]]; then
  if [[ ! -x "$NATIVE" ]]; then
    echo "Native binary not found. Build it with ./native/build.sh" >&2
    exit 1
  fi
  exec "$NATIVE" "$@"
fi
export PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}"
exec "${PYTHON:-python3}" -m envitop "$@"
