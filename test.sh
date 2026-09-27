#!/bin/sh
# Run the test suite.
#
#   ./test.sh                     unit + API + browser tests (the whole suite)
#   ./test.sh --fast              skip the browser tests (unit + API only)
#   ./test.sh --install           also run the opt-in installation tests, which
#                                 build a brand-new virtual environment from
#                                 requirements.txt and download packages
#   ./test.sh -h | --help         show this help
#   ./test.sh -k PATTERN          any other argument is passed straight to
#                                 pytest (for example -k, -x, -q, a file)
#
# Expected durations on a typical development machine (wall clock; they scale
# with CPU, disk and Chrome speed):
#
#   unit + API tests    about 1-2 minutes       (about 400 tests)
#   browser tests       about 2-15 minutes      (about 125 tests; the range is
#                                               wide because each one starts a
#                                               real server and Chrome)
#   installation tests  about 2-5 minutes       (only with --install)
#
# The browser tests in tests/e2e drive Google Chrome through Playwright; they
# are skipped automatically when Chrome is not installed and the run then
# finishes in well under a minute. A few tests are skipped unless the optional
# XMP_SAMPLE_DIR or REAL_DNG environment variables point at real files; see
# README.md and docs/install.md.
#
# On first run this creates .venv and installs requirements-dev.txt, the same
# way start.sh creates .venv and installs requirements.txt. Set PYTHON=... to
# pick the interpreter used for a new .venv.
set -e
cd "$(dirname "$0")"

usage() {
  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
}

fast=0
install=0
pyargs=
while [ $# -gt 0 ]; do
  case "$1" in
    --fast) fast=1; shift ;;
    --install) install=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) pyargs="$pyargs $1"; shift ;;
  esac
done

PYTHON=${PYTHON:-python3}
if [ ! -x .venv/bin/python ]; then
  if ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    echo "test.sh: Python 3.11 or newer is required (found: $("$PYTHON" --version 2>&1)); set PYTHON=..." >&2
    exit 1
  fi
  "$PYTHON" -m venv .venv
fi
if [ ! -f .venv/.dev-requirements.stamp ] || [ requirements-dev.txt -nt .venv/.dev-requirements.stamp ]; then
  .venv/bin/pip install -q -r requirements-dev.txt
  touch .venv/.dev-requirements.stamp
fi

# Extra pytest arguments were collected into $pyargs above; --fast prepends its
# ignore flag here.
if [ "$fast" = 1 ]; then
  set -- --ignore=tests/e2e "$@"
  echo "test.sh: running the fast suite (unit + API, no browser) -- estimate about 1-2 minutes"
else
  echo "test.sh: running the full suite (unit + API + browser) -- estimate about 3-17 minutes"
fi

if [ "$install" = 1 ]; then
  echo "test.sh: including the installation tests -- estimate about 2-5 minutes more"
  echo "test.sh: (they build a fresh venv and download packages)"
  export PHOTOS_TEST_INSTALL=1
fi

exec .venv/bin/python -m pytest -q "$@" ${pyargs}
