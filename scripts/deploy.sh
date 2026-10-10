#!/usr/bin/env bash
# Install or update the PACE server from GitHub, then (re)start it. Run as root.
#
#   sudo scripts/deploy.sh [--force]
#
# The checkout in APP_DIR is deploy-only: it's reset to origin/main each time.
# PACE develops itself by merging to origin; nothing reaches the running server
# until this script runs. It waits until no Claude runs are in progress
# (--force: restart anyway; interrupted runs can be retried).
#
# Needs the PACE project's deploy key at $DATA_DIR/keys/pace_ed25519 (brought
# over by import-data.sh, or generated here on a fresh install), added to the
# GitHub repo with write access.
set -euo pipefail

REPO="${PACE_REPO:-git@github.com:nicholasyoder/pace.git}"
BRANCH="${PACE_BRANCH:-main}"
APP_DIR="${PACE_APP_DIR:-/opt/pace}"
PACE_USER="${PACE_USER:-pace}"
DATA_DIR="/var/lib/pace"
KEY="$DATA_DIR/keys/pace_ed25519"
UNIT=/etc/systemd/system/pace.service
FORCE="${1:-}"

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)" >&2; exit 1; }
as() { runuser -u "$PACE_USER" -- bash -lc "cd ~ && $*"; }
PACE_HOME="$(getent passwd "$PACE_USER" | cut -d: -f6)"
UV="$PACE_HOME/.local/bin/uv"
[[ -x "$UV" ]] || { echo "no uv for $PACE_USER: run scripts/setup-vm.sh first" >&2; exit 1; }

if [[ ! -f "$KEY" ]]; then
    as "ssh-keygen -q -t ed25519 -N '' -C pace-pace -f '$KEY'"
    echo "Generated the PACE deploy key. Add it to ${REPO#*:} → Settings → Deploy keys,"
    echo "with \"Allow write access\" ticked, then run this again:"
    echo; cat "$KEY.pub"; exit 1
fi
SSH_CMD="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"

# --- code ----------------------------------------------------------------------
install -d -o "$PACE_USER" -g "$PACE_USER" -m 0755 "$APP_DIR"
if [[ ! -d "$APP_DIR/.git" ]]; then
    as "git -c core.sshCommand='$SSH_CMD' clone -q --branch '$BRANCH' '$REPO' '$APP_DIR'"
fi
as "cd '$APP_DIR' && git config core.sshCommand '$SSH_CMD' && git fetch -q origin \
    && git reset -q --hard 'origin/$BRANCH' && git clean -qfd"
as "cd '$APP_DIR' && '$UV' sync -q --frozen --no-dev"
rev="$(as "git -C '$APP_DIR' log -1 --format='%h %s'")"

# --- wait for idle -----------------------------------------------------------------
busy() {
    [[ -f "$DATA_DIR/pace.db" ]] || { echo 0; return; }
    sqlite3 -readonly "file:$DATA_DIR/pace.db?mode=ro" \
        "SELECT COUNT(*) FROM runs WHERE status IN ('queued', 'running')" 2>/dev/null || echo 0
}
if systemctl is-active -q pace && [[ "$FORCE" != "--force" ]]; then
    while [[ "$(busy)" != "0" ]]; do
        echo "waiting: $(busy) run(s) in progress (Ctrl-C, or rerun with --force)"
        sleep 30
    done
fi

# --- service ---------------------------------------------------------------------
sed -e "s|@PACE_USER@|$PACE_USER|g" -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@UV@|$UV|g" \
    "$APP_DIR/scripts/pace.service" > "$UNIT.tmp"
mv "$UNIT.tmp" "$UNIT"
systemctl daemon-reload
systemctl enable -q pace
systemctl restart pace
sleep 3
if systemctl is-active -q pace; then
    echo "ok: pace running $rev"
    grep -E '^(bind|port)' /etc/pace/config.toml | tr '\n' ' '; echo
else
    echo "pace failed to start:" >&2
    journalctl -u pace -n 30 --no-pager >&2
    exit 1
fi
