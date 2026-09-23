#!/bin/sh
# Usage: tickets_done.sh NNN  -- mark ticket NNN done: status, its own todos,
# and the checkbox for it in its parent epic / TODO.md. TODO.md only holds open
# tickets (STATUS.md holds completed ones); this script just checks the box in
# place, so periodically move newly-checked lines from TODO.md to STATUS.md by
# hand (or in a /loop pass) to keep that convention true.
set -e
cd "$(dirname "$0")"
n=$(printf '%03d' "$(echo "$1" | sed 's/^0*//')")
f=docs/tickets/$n.md
sed -i 's/^- Status: .*/- Status: done/; s/^- \[ \] /- [x] /' "$f"
sed -i "s/^- \[ \] \[$n\]/- [x] [$n]/" docs/tickets/*.md TODO.md
