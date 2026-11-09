#!/usr/bin/env bash
# Run omnitop straight from a checkout, without installing it.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}"
exec "${PYTHON:-python3}" -m omnitop "$@"
