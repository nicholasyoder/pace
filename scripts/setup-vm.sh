#!/usr/bin/env bash
# One-time VM setup for PACE. Idempotent; run with sudo.
#
#   sudo scripts/setup-vm.sh [PACE_USER]
#
# PACE_USER is the account the PACE server runs as. Spec §11 calls it `pace`;
# during M0 the human dev account stands in for it.
#
# Creates:
#   - the agent user (`pace-agent`) — Claude runs as this user with permission
#     prompts off, so it gets no keys, no gh auth and no access to PACE's data
#   - a sudoers rule letting PACE_USER run only `claude` and `git` as the agent
#   - the data dir: repos/ is agent-readable, everything else is PACE-only
#
# Afterwards, as a human (interactive, once):
#   sudo -u pace-agent -i bash -c 'curl -fsSL https://claude.ai/install.sh | bash'
#   sudo -u pace-agent -i ~pace-agent/.local/bin/claude      # then /login
set -euo pipefail

PACE_USER="${1:-${SUDO_USER:-pace}}"
AGENT_USER="pace-agent"
DATA_DIR="/var/lib/pace"
AGENT_HOME="/home/$AGENT_USER"
AGENT_CLAUDE="$AGENT_HOME/.local/bin/claude"
SUDOERS_FILE="/etc/sudoers.d/pace-agent"

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
id "$PACE_USER" >/dev/null || { echo "no such user: $PACE_USER" >&2; exit 1; }

# Agent user: a real shell (Claude's Bash tool needs one), password locked,
# private home. PACE_USER is not given read access to it — results come back
# through structured output and git fetches only.
if ! id "$AGENT_USER" >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash --user-group "$AGENT_USER"
fi
passwd --lock "$AGENT_USER" >/dev/null
chmod 0700 "$AGENT_HOME"
install -d -o "$AGENT_USER" -g "$AGENT_USER" -m 0700 "$AGENT_HOME/work"

# Data dir, owned by PACE_USER. Only repos/ is traversable by others, so the
# agent can clone the canonical bare repos but can't read the DB or logs.
install -d -o "$PACE_USER" -g "$PACE_USER" -m 0711 "$DATA_DIR"
install -d -o "$PACE_USER" -g "$PACE_USER" -m 0755 "$DATA_DIR/repos"
install -d -o "$PACE_USER" -g "$PACE_USER" -m 0700 "$DATA_DIR/logs"

# sudoers: only claude and git, as the agent, no password. sudo's env_reset and
# secure_path give the agent a scrubbed environment.
tmp="$(mktemp)"
cat >"$tmp" <<EOF
# Managed by pace/scripts/setup-vm.sh
$PACE_USER ALL=($AGENT_USER) NOPASSWD: $AGENT_CLAUDE, /usr/bin/git
EOF
visudo -cf "$tmp" >/dev/null
install -o root -g root -m 0440 "$tmp" "$SUDOERS_FILE"
rm -f "$tmp"

echo "ok: agent=$AGENT_USER pace=$PACE_USER data=$DATA_DIR"
[[ -x "$AGENT_CLAUDE" ]] || echo "next: install claude for $AGENT_USER (see header)"
