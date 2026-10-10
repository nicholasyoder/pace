#!/usr/bin/env bash
# Unpack an export-data.sh archive into this VM's data dir. Run as root, after
# setup-vm.sh and before deploy.sh.
#
#   sudo scripts/import-data.sh pace-data-….tar.gz [--force]
#
# Refuses to overwrite an existing DB unless --force (which keeps a backup).
set -euo pipefail
ARCHIVE="$1"
FORCE="${2:-}"
DATA_DIR="/var/lib/pace"
PACE_USER="${PACE_USER:-pace}"

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)" >&2; exit 1; }
[[ -d "$DATA_DIR/keys" ]] || { echo "run scripts/setup-vm.sh first" >&2; exit 1; }
if [[ -f "$DATA_DIR/pace.db" ]]; then
    [[ "$FORCE" == "--force" ]] || { echo "$DATA_DIR/pace.db exists (use --force to replace it)" >&2; exit 1; }
    systemctl stop pace 2>/dev/null || true
    mv "$DATA_DIR/pace.db" "$DATA_DIR/pace.db.before-import-$(date +%s)"
    rm -f "$DATA_DIR/pace.db-wal" "$DATA_DIR/pace.db-shm"
fi

tar -C "$DATA_DIR" --no-same-owner -xzf "$ARCHIVE" pace.db keys logs
chown -R "$PACE_USER:$PACE_USER" "$DATA_DIR/pace.db" "$DATA_DIR/keys" "$DATA_DIR/logs"
chmod 0600 "$DATA_DIR/pace.db"
chmod 0700 "$DATA_DIR/keys" "$DATA_DIR/logs"
find "$DATA_DIR/keys" -type f ! -name '*.pub' -exec chmod 0600 {} +
echo "ok: imported into $DATA_DIR ($(sqlite3 "$DATA_DIR/pace.db" "SELECT group_concat(slug, ', ') FROM projects"))"
