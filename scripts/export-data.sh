#!/usr/bin/env bash
# Pack PACE's data for a move to another VM: a consistent DB snapshot, the
# deploy keys and the run logs. Canonical repos re-clone themselves, and agent
# worktrees and Claude sessions are recreated on demand.
#
#   scripts/export-data.sh [DATA_DIR] [OUT.tar.gz]     # as the data dir's owner
#
# Stop the server first, so nothing changes after the snapshot.
set -euo pipefail
DATA_DIR="${1:-/var/lib/pace}"
OUT="${2:-pace-data-$(date +%Y%m%d-%H%M).tar.gz}"
OUT="$(readlink -f "$OUT")"

running="$(sqlite3 -readonly "file:$DATA_DIR/pace.db?mode=ro" \
    "SELECT COUNT(*) FROM runs WHERE status IN ('queued', 'running')")"
[[ "$running" == "0" ]] || { echo "$running run(s) still in progress; stop the server first" >&2; exit 1; }

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
sqlite3 "$DATA_DIR/pace.db" ".backup '$stage/pace.db'"
sqlite3 "$stage/pace.db" "PRAGMA integrity_check" | grep -qx ok
cp -a "$DATA_DIR/keys" "$DATA_DIR/logs" "$stage/"
tar -C "$stage" -czf "$OUT" pace.db keys logs
chmod 0600 "$OUT"   # it contains private deploy keys
echo "wrote $OUT ($(du -h "$OUT" | cut -f1)): $(sqlite3 "$stage/pace.db" \
    "SELECT COUNT(*) || ' projects' FROM projects"), $(ls "$stage/keys" | grep -vc '\.pub$') key(s)"
