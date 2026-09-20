#!/bin/sh
# Bring up the photo manager server locally.
#
#   ./start.sh                       defaults: /zoo/Pictures, /zoo/Thumbs
#   ./start.sh --port=9000 --pictures_dir=/tmp/pics --state_dir=/tmp/state
#
# Extra arguments are passed to `python -m photoapp` as flags (see
# photoapp/config.py). Creates .venv and installs requirements.txt on first
# run, and again whenever requirements.txt changes.
set -e
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
if [ ! -f .venv/.requirements.stamp ] || [ requirements.txt -nt .venv/.requirements.stamp ]; then
  .venv/bin/pip install -q -r requirements.txt
  touch .venv/.requirements.stamp
fi

# Report the URL, taking --host/--port overrides into account.
host=0.0.0.0
port=8080
for arg in "$@"; do
  case "$arg" in
    --host=*) host=${arg#--host=} ;;
    --port=*) port=${arg#--port=} ;;
  esac
done
[ "$host" = 0.0.0.0 ] && shown=localhost || shown=$host
echo "Photos: http://$shown:$port/" >&2

exec .venv/bin/python -m photoapp --logtostderr "$@"
