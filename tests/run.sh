#!/bin/bash

set -euo pipefail

# Run tests of all components. Extra arguments are passed to pytest,
# e.g. tests/run.sh -k widget -x
# PYTHON overrides the interpreter (default: .venv/bin/python of the repository).

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT_DIR/.venv/bin/python}"

exec "$PYTHON" -m pytest -c "$ROOT_DIR/tests/pytest.ini" "$@"
