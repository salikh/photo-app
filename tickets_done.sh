#!/bin/sh
# Usage: tickets_done.sh NNN  -- mark ticket NNN done: status, its own todos,
# and the checkbox for it in its parent epic / TODO.md.
set -e
cd "$(dirname "$0")"
n=$(printf '%03d' "$1")
f=docs/tickets/$n.md
sed -i 's/^- Status: .*/- Status: done/; s/^- \[ \] /- [x] /' "$f"
sed -i "s/^- \[ \] \[$n\]/- [x] [$n]/" docs/tickets/*.md TODO.md
