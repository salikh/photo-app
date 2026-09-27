#!/bin/sh
# Bring up the photo manager server locally.
#
#   ./start.sh                       uses ./photos.toml (see photos.example.toml)
#   ./start.sh --port=9000 --pictures_dir=/tmp/pics --state_dir=/tmp/state
#
# Extra arguments are passed to `python -m photoapp` as flags (see
# photoapp/config.py and docs/install.md). Creates .venv and installs
# requirements.txt on first run, and again whenever requirements.txt changes.
# PYTHON=/path/to/python3.12 ./start.sh   picks the interpreter for a new .venv.
set -e
cd "$(dirname "$0")"

PYTHON=${PYTHON:-python3}
if [ ! -x .venv/bin/python ]; then
  if ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    echo "start.sh: Python 3.11 or newer is required (found: $("$PYTHON" --version 2>&1)); set PYTHON=..." >&2
    exit 1
  fi
  "$PYTHON" -m venv .venv
fi
if [ ! -f .venv/.requirements.stamp ] || [ requirements.txt -nt .venv/.requirements.stamp ]; then
  .venv/bin/pip install -q -r requirements.txt
  touch .venv/.requirements.stamp
fi

exec .venv/bin/python -m photoapp --logtostderr "$@"
