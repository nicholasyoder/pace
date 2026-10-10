#!/usr/bin/env bash
# One-time VM setup for PACE. Idempotent; run as root.
#
#   sudo scripts/setup-vm.sh [PACE_USER]
#
# PACE_USER is the account the PACE server runs as (default `pace`, created
# if missing). A dev VM can pass its own login instead.
#
# Creates / installs:
#   - system packages: git, ssh, sqlite3, a C toolchain, plus PROJECT_PACKAGES
#     (build deps the agent can't install itself, e.g. Weft's ALSA/Vulkan)
#   - PACE_USER with uv in its home
#   - the agent user (`pace-agent`): Claude runs as this user with permission
#     prompts off, so it gets no keys, no gh auth and no access to PACE's data;
#     with its own claude install and the toolchains in AGENT_TOOLCHAINS
#   - a sudoers rule letting PACE_USER run only `claude` and `git` as the agent
#   - the agent's global ~/.claude/CLAUDE.md (from scripts/agent-CLAUDE.md)
#   - the data dir: repos/ is agent-readable, everything else is PACE-only
#   - /etc/pace/config.toml (if missing), bound to the Tailscale IP if there is one
#
# Afterwards, once, as a human: log the agent's claude in (interactive):
#   sudo -u pace-agent -i ~pace-agent/.local/bin/claude      # then /login, then /exit
set -euo pipefail

PACE_USER="${1:-pace}"
AGENT_USER="pace-agent"
DATA_DIR="/var/lib/pace"
AGENT_HOME="/home/$AGENT_USER"
AGENT_CLAUDE="$AGENT_HOME/.local/bin/claude"
SUDOERS_FILE="/etc/sudoers.d/pace-agent"
CONFIG="/etc/pace/config.toml"
PROJECT_PACKAGES="${PROJECT_PACKAGES-libasound2-dev mesa-vulkan-drivers}"
AGENT_TOOLCHAINS="${AGENT_TOOLCHAINS-rust uv}"
HERE="$(dirname "$(readlink -f "$0")")"

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)" >&2; exit 1; }
as() { local u="$1"; shift; runuser -u "$u" -- bash -lc "cd ~ && $*"; }

# --- packages ---------------------------------------------------------------
if command -v apt-get >/dev/null; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    # shellcheck disable=SC2086
    apt-get install -y -qq git openssh-client sudo curl ca-certificates sqlite3 \
        build-essential pkg-config $PROJECT_PACKAGES >/dev/null
fi
git_ver="$(git --version | awk '{print $3}')"
[[ "$(printf '%s\n2.38\n' "$git_ver" | sort -V | head -1)" == "2.38" ]] \
    || { echo "git >= 2.38 needed (merge-tree --write-tree), have $git_ver" >&2; exit 1; }

# --- PACE user ----------------------------------------------------------------
if ! id "$PACE_USER" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "/home/$PACE_USER" --shell /bin/bash \
        --user-group "$PACE_USER"
fi
PACE_HOME="$(getent passwd "$PACE_USER" | cut -d: -f6)"
[[ -x "$PACE_HOME/.local/bin/uv" ]] \
    || as "$PACE_USER" 'curl -LsSf https://astral.sh/uv/install.sh | sh -s -- -q'

# --- agent user -----------------------------------------------------------------
# A real shell (Claude's Bash tool needs one), password locked, private home.
# PACE_USER gets no read access to it: results come back through structured
# output and git bundles only.
if ! id "$AGENT_USER" >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash --user-group "$AGENT_USER"
fi
passwd --lock "$AGENT_USER" >/dev/null
chmod 0700 "$AGENT_HOME"
install -d -o "$AGENT_USER" -g "$AGENT_USER" -m 0700 "$AGENT_HOME/work"

# Agent's global CLAUDE.md (§11): set deliberately, not copied from a human's.
install -d -o "$AGENT_USER" -g "$AGENT_USER" -m 0700 "$AGENT_HOME/.claude"
install -o "$AGENT_USER" -g "$AGENT_USER" -m 0644 "$HERE/agent-CLAUDE.md" \
    "$AGENT_HOME/.claude/CLAUDE.md"
# Trust the canonical repos PACE_USER owns (otherwise git refuses: "dubious ownership").
as "$AGENT_USER" "git config --global --get-all safe.directory | grep -qx '$DATA_DIR/repos/*' \
    || git config --global --add safe.directory '$DATA_DIR/repos/*'"

# Agent's own claude (auto-updates) and toolchains, all in its home.
[[ -x "$AGENT_CLAUDE" ]] || as "$AGENT_USER" 'curl -fsSL https://claude.ai/install.sh | bash'
for tc in $AGENT_TOOLCHAINS; do
    case "$tc" in
        rust) [[ -x "$AGENT_HOME/.cargo/bin/cargo" ]] || as "$AGENT_USER" \
                  'curl -sSf https://sh.rustup.rs | sh -s -- -y -q --profile minimal -c clippy,rustfmt' ;;
        uv)   [[ -x "$AGENT_HOME/.local/bin/uv" ]] || as "$AGENT_USER" \
                  'curl -LsSf https://astral.sh/uv/install.sh | sh -s -- -q' ;;
        *)    echo "unknown toolchain: $tc" >&2; exit 1 ;;
    esac
done

# --- data dir --------------------------------------------------------------------
# Owned by PACE_USER. Only repos/ is traversable by others, so the agent can
# clone the canonical bare repos but can't read the DB, logs or keys.
install -d -o "$PACE_USER" -g "$PACE_USER" -m 0711 "$DATA_DIR"
install -d -o "$PACE_USER" -g "$PACE_USER" -m 0755 "$DATA_DIR/repos"
for d in logs keys bundles; do
    install -d -o "$PACE_USER" -g "$PACE_USER" -m 0700 "$DATA_DIR/$d"
done

# --- sudoers --------------------------------------------------------------------------
# Only claude and git, as the agent, no password. sudo's env_reset and
# secure_path give the agent a scrubbed environment. CWD=* permits `sudo -D`:
# PACE can't chdir into the agent's private worktrees itself, and claude has no
# --cwd flag.
tmp="$(mktemp)"
cat >"$tmp" <<EOF
# Managed by pace/scripts/setup-vm.sh
$PACE_USER ALL=($AGENT_USER) CWD=* NOPASSWD: $AGENT_CLAUDE, /usr/bin/git
EOF
visudo -cf "$tmp" >/dev/null
install -o root -g root -m 0440 "$tmp" "$SUDOERS_FILE"
rm -f "$tmp"

# --- config ------------------------------------------------------------------------------
if [[ ! -f "$CONFIG" ]]; then
    bind="$(tailscale ip -4 2>/dev/null | head -1 || true)"
    install -d -m 0755 /etc/pace
    cat >"$CONFIG" <<EOF
# PACE settings (see pace/config.py). Restart pace after editing.
data_dir = "$DATA_DIR"
bind = "${bind:-127.0.0.1}"   # there is no app auth: keep this on the Tailscale IP
port = 8420
agent_user = "$AGENT_USER"
agent_home = "$AGENT_HOME"
claude = "$AGENT_CLAUDE"
run_timeout_minutes = 60
concurrency = 2
EOF
    chmod 0644 "$CONFIG"
    [[ -n "$bind" ]] || echo "note: no Tailscale IP found; edit bind in $CONFIG"
fi

echo "ok: pace=$PACE_USER agent=$AGENT_USER data=$DATA_DIR config=$CONFIG"
if [[ ! -s "$AGENT_HOME/.claude/.credentials.json" ]]; then
    echo "next: log the agent's claude in: sudo -u $AGENT_USER -i $AGENT_CLAUDE   (/login, then /exit)"
fi
