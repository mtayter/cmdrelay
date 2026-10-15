#!/usr/bin/env bash
# cmdrelay install.sh — three-sided installer for the cmdrelay shell relay.
#
#   Server role (human's machine): installs server.py, generates a bearer
#       token into ~/.config/cmdrelay/.token (0600), writes config.
#   Worker role (agent VM): installs worker.py + the worker loop,
#       prompts for the server's SSH host/user and the token printed by
#       the server install, writes config. The human carries the token
#       across: display -> paste.
#   Client role (human's machine, same box as the server): installs
#       slickterm, the interactive client that owns the server
#       (starts it on entry, stops it on exit).
#
# Usage:
#   ./install.sh --role server [--non-interactive]
#   ./install.sh --role worker [--non-interactive]
#   ./install.sh --role client [--non-interactive]
#
# Idempotent: safe to re-run. Re-install reads the existing config for
# defaults, NEVER regenerates an existing token.
#
# Secrets: this script never enables tracing around secret handling
# (no `set -x`). The server token is printed ONCE, only when newly
# generated, so it can be carried to the worker install. Passwords are
# never collected — SSH uses key auth / your ~/.ssh/config.
#
# Non-interactive mode (--non-interactive) reads these env vars:
#   CMDRELAY_ROLE            server|worker|client (or pass --role)
#   CMDRELAY_CONFIG_DIR      default ~/.config/cmdrelay
#   CMDRELAY_INSTALL_DIR     default ~/.local/share/cmdrelay
#   Server: CMDRELAY_BIND_HOST (default 127.0.0.1),
#           CMDRELAY_BIND_PORT (default 8770),
#           CMDRELAY_DATA_DIR  (default <install-dir>)
#   Worker: CMDRELAY_SSH_HOST (required, no default),
#           CMDRELAY_SSH_USER (default $USER),
#           CMDRELAY_SERVER_PORT (default 8770),
#           CMDRELAY_TUNNEL_PORT (default 18770),
#           CMDRELAY_TOKEN (token pasted from the server install;
#             prefer the interactive paste prompt — env is only for
#             scripted installs where the token is already protected)
#   Client: no prompts, no config changes — just copies slickterm.
#
# NOTE: there is no systemd option. The worker runs under the
# persistent loop (worker/cmdrelay-worker-loop.sh, installed next to
# worker.py); systemd user units do not survive VM refresh, and the
# loop is the canonical keeper. See README.md.
set -euo pipefail
umask 077

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROLE="${CMDRELAY_ROLE:-}"
NON_INTERACTIVE=0
CONFIG_DIR="${CMDRELAY_CONFIG_DIR:-$HOME/.config/cmdrelay}"
INSTALL_DIR="${CMDRELAY_INSTALL_DIR:-$HOME/.local/share/cmdrelay}"

usage() {
    sed -n '1,60p' "$0"
    exit "${1:-0}"
}

die() { printf 'install.sh: error: %s\n' "$*" >&2; exit 1; }
note() { printf 'install.sh: %s\n' "$*"; }

# --- argument parsing -------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --role) ROLE="${2:-}"; shift 2 ;;
        --role=*) ROLE="${1#--role=}"; shift ;;
        --non-interactive) NON_INTERACTIVE=1; shift ;;
        --config-dir) CONFIG_DIR="${2:-}"; shift 2 ;;
        --config-dir=*) CONFIG_DIR="${1#--config-dir=}"; shift ;;
        --install-dir) INSTALL_DIR="${2:-}"; shift 2 ;;
        --install-dir=*) INSTALL_DIR="${1#--install-dir=}"; shift ;;
        -h|--help) usage 0 ;;
        *) die "unknown argument: $1 (see --help)" ;;
    esac
done

# --- helpers ----------------------------------------------------------------
# Read one KEY from an existing config.env (no sourcing: values stay inert).
config_get() {
    local key="$1" file="$CONFIG_DIR/config.env" line k v
    [ -f "$file" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in ''|'#'*) continue ;; esac
        k="${line%%=*}"; v="${line#*=}"
        if [ "$k" = "$key" ]; then printf '%s' "$v"; return 0; fi
    done < "$file"
    return 0
}

is_tty() { [ -t 0 ]; }

# Prompt with a default. In non-interactive mode the default (or env) wins.
# $1 = var name to set, $2 = prompt text, $3 = default
prompt_default() {
    local var="$1" text="$2" def="$3" ans=""
    if [ "$NON_INTERACTIVE" -eq 1 ] || ! is_tty; then
        printf -v "$var" '%s' "$def"
        return 0
    fi
    printf '%s [%s]: ' "$text" "$def" >&2
    IFS= read -r ans || true
    if [ -z "$ans" ]; then ans="$def"; fi
    printf -v "$var" '%s' "$ans"
}

# Secret prompt: input is never echoed (read -s). Empty input = keep existing.
# $1 = var name to set, $2 = prompt text
prompt_secret() {
    local var="$1" text="$2" ans=""
    printf '%s' "$text" >&2
    IFS= read -r -s ans || true
    printf '\n' >&2
    printf -v "$var" '%s' "$ans"
}

validate_port() {
    local p="$1" what="$2"
    case "$p" in
        ''|*[!0-9]*) die "$what must be a number, got: $p" ;;
    esac
    if [ "$p" -lt 1 ] || [ "$p" -gt 65535 ]; then
        die "$what must be 1-65535, got: $p"
    fi
}

generate_token() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 32
    else
        python3 -c 'import secrets; print(secrets.token_hex(32))'
    fi
}

# Write config.env atomically with 0600 perms. $1.. = KEY=VALUE lines.
write_config() {
    local tmp="$CONFIG_DIR/.config.env.tmp.$$"
    {
        printf '# cmdrelay config — generated by install.sh. DO NOT COMMIT.\n'
        printf '# Per-machine local config. Permissions 0600.\n'
        for line in "$@"; do printf '%s\n' "$line"; done
    } > "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$CONFIG_DIR/config.env"
}

# --- role selection ---------------------------------------------------------
if [ -z "$ROLE" ]; then
    if [ "$NON_INTERACTIVE" -eq 1 ] || ! is_tty; then
        die "no role given: pass --role server|worker|client or set CMDRELAY_ROLE"
    fi
    printf 'Install role [server/worker/client]: ' >&2
    IFS= read -r ROLE || true
fi
case "$ROLE" in
    server|worker|client) ;;
    *) die "--role must be server, worker, or client, got: $ROLE" ;;
esac

command -v python3 >/dev/null 2>&1 || die "python3 is required"
command -v ssh >/dev/null 2>&1 || die "ssh is required"

mkdir -p "$INSTALL_DIR" "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR" 2>/dev/null || true

TOKEN_FILE="$CONFIG_DIR/.token"

# --- client role ------------------------------------------------------------
if [ "$ROLE" = client ]; then
    cp "$REPO_DIR/client/slickterm" "$INSTALL_DIR/slickterm"
    chmod 755 "$INSTALL_DIR/slickterm"
    printf 'Client installed.\n'
    printf '  Run:      %s/slickterm\n' "$INSTALL_DIR"
    printf '  (slickterm starts the server on entry and stops it on exit;\n'
    printf '   run ./install.sh --role server first if the server is not installed.)\n'
    exit 0
fi

# --- server role ------------------------------------------------------------
if [ "$ROLE" = server ]; then
    cp "$REPO_DIR/server/server.py" "$INSTALL_DIR/server.py"
    chmod 755 "$INSTALL_DIR/server.py"

    TOKEN_GENERATED=0
    if [ -s "$TOKEN_FILE" ]; then
        note "keeping existing token in $TOKEN_FILE (not regenerated)"
    else
        generate_token > "$TOKEN_FILE"
        chmod 600 "$TOKEN_FILE"
        TOKEN_GENERATED=1
        note "generated new token in $TOKEN_FILE (0600)"
    fi

    BIND_HOST="${CMDRELAY_BIND_HOST:-$(config_get CMDRELAY_BIND_HOST)}"
    BIND_HOST="${BIND_HOST:-127.0.0.1}"
    BIND_PORT="${CMDRELAY_BIND_PORT:-$(config_get CMDRELAY_BIND_PORT)}"
    BIND_PORT="${BIND_PORT:-8770}"
    DATA_DIR="${CMDRELAY_DATA_DIR:-$(config_get CMDRELAY_DATA_DIR)}"
    DATA_DIR="${DATA_DIR:-$INSTALL_DIR}"
    if [ "$NON_INTERACTIVE" -eq 0 ] && is_tty; then
        prompt_default BIND_HOST "Bind host (loopback only is recommended)" "$BIND_HOST"
        prompt_default BIND_PORT "Bind port" "$BIND_PORT"
        prompt_default DATA_DIR "Data dir (audit.log, server.pid)" "$DATA_DIR"
    fi
    validate_port "$BIND_PORT" "bind port"
    mkdir -p "$DATA_DIR"

    write_config \
        "CMDRELAY_ROLE=server" \
        "CMDRELAY_CONFIG_DIR=$CONFIG_DIR" \
        "CMDRELAY_INSTALL_DIR=$INSTALL_DIR" \
        "CMDRELAY_BIND_HOST=$BIND_HOST" \
        "CMDRELAY_BIND_PORT=$BIND_PORT" \
        "CMDRELAY_DATA_DIR=$DATA_DIR" \
        "CMDRELAY_TOKEN_FILE=$TOKEN_FILE"
    chmod 600 "$CONFIG_DIR/config.env"
    note "wrote $CONFIG_DIR/config.env (0600)"

    if [ "$TOKEN_GENERATED" -eq 1 ]; then
        # The ONLY place the token is printed: once, at generation time,
        # so it can be carried to the worker install. Never logged.
        printf '\n===== COPY THIS TOKEN TO THE WORKER INSTALL (shown once) =====\n'
        cat "$TOKEN_FILE"
        printf '\n===== END TOKEN — store it in the worker install, then clear this screen =====\n\n'
    else
        note "token unchanged — the worker already has it, or re-run the worker install with the saved token"
        note "if the token is lost: delete $TOKEN_FILE and re-run this installer to regenerate (then re-install the worker with the new token)"
    fi
    printf 'Server installed.\n'
    printf '  Run:      python3 %s/server.py\n' "$INSTALL_DIR"
    printf '  (Your client starts/stops the server on demand; no systemd unit is installed for the server.)\n'
    printf '  Next:     on the worker machine, run: ./install.sh --role worker\n'
    printf '            and paste the token above when prompted.\n'
    printf '            On this machine, run: ./install.sh --role client\n'
    printf '            to install slickterm.\n'
    exit 0
fi

# --- worker role ------------------------------------------------------------
cp "$REPO_DIR/worker/worker.py" "$INSTALL_DIR/worker.py"
chmod 755 "$INSTALL_DIR/worker.py"
cp "$REPO_DIR/worker/cmdrelay-worker-loop.sh" "$INSTALL_DIR/cmdrelay-worker-loop.sh"
chmod 755 "$INSTALL_DIR/cmdrelay-worker-loop.sh"

SSH_HOST="${CMDRELAY_SSH_HOST:-$(config_get CMDRELAY_SSH_HOST)}"
SSH_USER="${CMDRELAY_SSH_USER:-$(config_get CMDRELAY_SSH_USER)}"
SSH_USER="${SSH_USER:-${USER:-$(id -un)}}"
SERVER_PORT="${CMDRELAY_SERVER_PORT:-$(config_get CMDRELAY_SERVER_PORT)}"
SERVER_PORT="${SERVER_PORT:-8770}"
TUNNEL_PORT="${CMDRELAY_TUNNEL_PORT:-$(config_get CMDRELAY_TUNNEL_PORT)}"
TUNNEL_PORT="${TUNNEL_PORT:-18770}"

if [ "$NON_INTERACTIVE" -eq 0 ] && is_tty; then
    prompt_default SSH_HOST "Server SSH host (hostname or ssh-config alias)" "$SSH_HOST"
    prompt_default SSH_USER "Server SSH user" "$SSH_USER"
    prompt_default SERVER_PORT "Server port (must match the server install)" "$SERVER_PORT"
    prompt_default TUNNEL_PORT "Local tunnel port" "$TUNNEL_PORT"
fi
[ -n "$SSH_HOST" ] || die "SSH host is required: pass CMDRELAY_SSH_HOST or answer the prompt"
validate_port "$SERVER_PORT" "server port"
validate_port "$TUNNEL_PORT" "tunnel port"

# Token: env var (scripted installs) > interactive paste > keep existing.
NEW_TOKEN=""
if [ -n "${CMDRELAY_TOKEN:-}" ]; then
    NEW_TOKEN="$CMDRELAY_TOKEN"
    note "using token from CMDRELAY_TOKEN env (not echoed)"
elif [ "$NON_INTERACTIVE" -eq 0 ] && is_tty; then
    if [ -s "$TOKEN_FILE" ]; then
        prompt_secret NEW_TOKEN "Paste token from the server install (Enter to keep the existing token): "
    else
        prompt_secret NEW_TOKEN "Paste token from the server install: "
    fi
fi
if [ -n "$NEW_TOKEN" ]; then
    printf '%s\n' "$NEW_TOKEN" > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
    NEW_TOKEN=""
    note "wrote token to $TOKEN_FILE (0600)"
elif [ -s "$TOKEN_FILE" ]; then
    note "keeping existing token in $TOKEN_FILE"
else
    die "no token supplied and none exists: paste the token from the server install"
fi

write_config \
    "CMDRELAY_ROLE=worker" \
    "CMDRELAY_CONFIG_DIR=$CONFIG_DIR" \
    "CMDRELAY_INSTALL_DIR=$INSTALL_DIR" \
    "CMDRELAY_SSH_HOST=$SSH_HOST" \
    "CMDRELAY_SSH_USER=$SSH_USER" \
    "CMDRELAY_SERVER_PORT=$SERVER_PORT" \
    "CMDRELAY_TUNNEL_PORT=$TUNNEL_PORT" \
    "CMDRELAY_TOKEN_FILE=$TOKEN_FILE"
chmod 600 "$CONFIG_DIR/config.env"
note "wrote $CONFIG_DIR/config.env (0600)"

printf 'Worker installed.\n'
printf '  Run manually:  python3 %s/worker.py\n' "$INSTALL_DIR"
printf '  Or via the loop: bash %s/cmdrelay-worker-loop.sh\n' "$INSTALL_DIR"
printf '  (The loop respawns worker.py on crash; it is the canonical keeper.\n'
printf '   There is no systemd option — systemd units do not survive VM refresh.)\n'
printf '  Check SSH first: ssh %s@%s true\n' "$SSH_USER" "$SSH_HOST"
printf '  (The worker idles until a client starts a session on the server.)\n'
exit 0
