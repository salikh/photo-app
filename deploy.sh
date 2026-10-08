#!/bin/sh
# Deploy the photo manager to /opt/photos and run it as the systemd service photos.service.
#
#   sudo ./deploy.sh [--dry-run] [--install-deps] [--user=NAME]
#
# The app is pure Python, so there is no build step: the script checks the dependencies in
# /opt/photos/.venv and prints the pip command if they are not satisfied (--install-deps runs it).
# Config lives outside the release, in ~USER/.config/photos/ (config.toml, env, service.args); it
# is seeded when missing and never overwritten. See ticket 183 and docs/operations.md.
set -eu
cd "$(dirname "$0")"
SRC=$(pwd)

INSTALL=/opt/photos
UNIT=/etc/systemd/system/photos.service
DRY=0
INSTALL_DEPS=0
TARGET_USER=${SUDO_USER:-}
for arg in "$@"; do
  case $arg in
    --dry-run) DRY=1 ;;
    --install-deps) INSTALL_DEPS=1 ;;
    --user=*) TARGET_USER=${arg#--user=} ;;
    *) echo "deploy.sh: unknown argument $arg" >&2; exit 2 ;;
  esac
done

die() { echo "deploy.sh: $*" >&2; exit 1; }
say() { echo "==> $*"; }
# run CMD...: execute, or only print under --dry-run.
run() { if [ "$DRY" = 1 ]; then echo "  [dry-run] $*"; else "$@"; fi; }

[ -n "$TARGET_USER" ] && [ "$TARGET_USER" != root ] || die "run as: sudo ./deploy.sh (needs a non-root target user: \$SUDO_USER or --user=NAME)"
[ "$(id -u)" = 0 ] || die "must run as root: sudo ./deploy.sh"
USER_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)
[ -d "$USER_HOME" ] || die "no home directory for user $TARGET_USER"
CONFDIR=$USER_HOME/.config/photos
as_user() { sudo -u "$TARGET_USER" -H "$@"; }
HELPER="python3 $SRC/deploy/helper.py"

command -v rsync >/dev/null || die "rsync is required"
PYTHON=${PYTHON:-python3}
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null \
  || die "Python 3.11 or newer is required (set PYTHON=...)"

# --- (a) install directory, venv and the dependency check -------------------------------------
say "install directory $INSTALL (owner $TARGET_USER)"
run install -d -o "$TARGET_USER" -g "$(id -gn "$TARGET_USER")" "$INSTALL"
run as_user mkdir -p "$INSTALL/app"
if [ ! -x "$INSTALL/.venv/bin/python" ]; then
  say "creating $INSTALL/.venv"
  run as_user "$PYTHON" -m venv "$INSTALL/.venv"
fi
PIP_CMD="$INSTALL/.venv/bin/pip install -r $SRC/requirements.txt"
if [ "$DRY" = 1 ] && [ ! -x "$INSTALL/.venv/bin/python" ]; then
  echo "  [dry-run] would check dependencies against requirements.txt"
else
  say "checking dependencies"
  # No -q: it also suppresses the "Would install ..." line that is matched below.
  PIP_OUT=$(as_user "$INSTALL/.venv/bin/pip" install --dry-run -r "$SRC/requirements.txt" 2>&1) \
    || { echo "$PIP_OUT" >&2; die "pip could not resolve requirements.txt"; }
  if echo "$PIP_OUT" | grep -q "^Would install"; then
    if [ "$INSTALL_DEPS" = 1 ]; then
      run as_user $PIP_CMD
    else
      echo "dependencies are missing or outdated in $INSTALL/.venv. Run:" >&2
      echo "    sudo -u $TARGET_USER $PIP_CMD" >&2
      echo "or re-run with --install-deps." >&2
      [ "$DRY" = 1 ] || exit 1
    fi
  fi
fi

# ffmpeg (ticket 186): not a pip package. Without it videos are listed with placeholder thumbnails.
if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
  if [ "$INSTALL_DEPS" = 1 ]; then
    run apt-get install -y ffmpeg
  else
    echo "ffmpeg/ffprobe are missing (needed for video thumbnails and metadata). Run:" >&2
    echo "    sudo apt install ffmpeg" >&2
    echo "or re-run with --install-deps." >&2
    [ "$DRY" = 1 ] || exit 1
  fi
fi

# --- (b) copy the app ---------------------------------------------------------------------------
say "copying photoapp/ and requirements.txt to $INSTALL/app"
run as_user rsync -a --delete --exclude __pycache__ --exclude '*.pyc' "$SRC/photoapp" "$SRC/requirements.txt" "$INSTALL/app/"
REV=$(as_user sh -c "cd '$SRC' && PATH=\$PATH:\$HOME/.cargo/bin jj log -r @ --no-graph -T 'change_id.short() ++ \" \" ++ description.first_line()' 2>/dev/null || git rev-parse --short HEAD 2>/dev/null" || true)
if [ "$DRY" = 1 ]; then
  echo "  [dry-run] write $INSTALL/DEPLOYED_REV: ${REV:-unknown}"
else
  printf '%s  %s\n' "$(date -Is)" "${REV:-unknown}" | as_user tee "$INSTALL/DEPLOYED_REV" >/dev/null
fi

# --- config, seeded only when absent ----------------------------------------------------------------
say "config in $CONFDIR"
run as_user mkdir -p "$CONFDIR"
if [ ! -e "$CONFDIR/config.toml" ]; then
  if [ -f "$SRC/photos.toml" ]; then CFG_SRC=$SRC/photos.toml; else CFG_SRC=$SRC/photos.example.toml; fi
  echo "  seeding config.toml from $CFG_SRC"
  run as_user cp "$CFG_SRC" "$CONFDIR/config.toml"
fi
if [ ! -e "$CONFDIR/env" ]; then
  echo "  seeding env from ENV"
  if [ "$DRY" = 1 ]; then
    echo "  [dry-run] write $CONFDIR/env (mode 600)"
  else
    if [ -f "$SRC/ENV" ]; then $HELPER env "$SRC/ENV" > "$CONFDIR/env"; else : > "$CONFDIR/env"; fi
    chown "$TARGET_USER" "$CONFDIR/env"; chmod 600 "$CONFDIR/env"
    [ -s "$CONFDIR/env" ] || echo "  note: $CONFDIR/env has no GEMINI_API_KEY; edit it for AI rating" >&2
  fi
fi
if [ ! -e "$CONFDIR/service.args" ]; then
  echo "  seeding service.args"
  if [ "$DRY" = 1 ]; then
    echo "  [dry-run] write $CONFDIR/service.args"
  else
    cat > "$CONFDIR/service.args" <<'ARGS'
# Command-line flags of photos.service (systemd EnvironmentFile syntax); restart to apply.
PHOTOS_ARGS=--host 192.168.1.11 --port 8080 --v=5 --load_stop_threshold 4.0 --mem_start_percent 10 --mem_stop_percent 5 --load_start_threshold 1.0
ARGS
    chown "$TARGET_USER" "$CONFDIR/service.args"
  fi
fi

# --- (c) systemd unit -------------------------------------------------------------------------------
say "installing $UNIT"
MOUNT_CFG=$CONFDIR/config.toml
[ -e "$MOUNT_CFG" ] || MOUNT_CFG=${CFG_SRC:-/dev/null}  # --dry-run before the first seeding
MOUNTS=$(as_user env HOME="$USER_HOME" $HELPER mounts "$MOUNT_CFG" 2>/dev/null || true)
UNIT_TEXT=$($HELPER render "$SRC/deploy/photos.service.in" "USER=$TARGET_USER" "INSTALL=$INSTALL" "CONFDIR=$CONFDIR" "MOUNTS=$MOUNTS")
if [ "$DRY" = 1 ]; then
  echo "  [dry-run] write $UNIT:"; echo "$UNIT_TEXT" | sed 's/^/      /'
else
  echo "$UNIT_TEXT" > "$UNIT"
  chmod 644 "$UNIT"
fi

# --- (d) activate ----------------------------------------------------------------------------------
LISTEN=$(as_user $HELPER listen "$MOUNT_CFG" "$CONFDIR/service.args" 2>/dev/null || echo "0.0.0.0 8080")
HOST=${LISTEN% *}; PORT=${LISTEN#* }
CHECK_HOST=$HOST; [ "$HOST" = 0.0.0.0 ] && CHECK_HOST=127.0.0.1
if ! systemctl is-active --quiet photos.service && ss -Hltn "sport = :$PORT" 2>/dev/null | grep -q .; then
  if [ "$DRY" = 1 ]; then echo "  warning: port $PORT is in use by another process; a real run would stop here" >&2
  else die "port $PORT is already in use by another process (a ./start.sh instance?); stop it first"; fi
fi
say "activating photos.service (listening on $HOST:$PORT)"
run systemctl daemon-reload
run systemctl enable photos.service
run systemctl restart photos.service
if [ "$DRY" = 1 ]; then
  echo "  [dry-run] would poll http://$CHECK_HOST:$PORT/ for up to 30 s"
  exit 0
fi
i=0
while [ "$i" -lt 30 ]; do
  if systemctl is-active --quiet photos.service \
     && "$PYTHON" -c "import urllib.request,sys; urllib.request.urlopen('http://$CHECK_HOST:$PORT/', timeout=2)" 2>/dev/null; then
    say "OK: photos.service is up at http://$CHECK_HOST:$PORT/ (rev: ${REV:-unknown})"
    exit 0
  fi
  i=$((i + 1)); sleep 1
done
echo "deploy.sh: service did not become healthy within 30 s" >&2
systemctl --no-pager status photos.service >&2 || true
journalctl -u photos.service -n 20 --no-pager >&2 || true
exit 1
