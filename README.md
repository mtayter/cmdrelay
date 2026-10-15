# cmdrelay — on-demand shell relay

A human types commands in a terminal on their own machine; the commands
execute on an agent VM; output comes back. Nothing runs unless a session
is active.

Three parts, connected by an SSH tunnel:

- **Server** (human's machine): a small HTTP relay, loopback-only, with
  bearer-token auth. It holds one active session at a time: a queue of
  commands and their results.
- **Worker** (agent VM): opens an SSH tunnel to the server, long-polls
  for commands, runs each in **one persistent bash** (so `cd`, `alias`,
  `export`, shell functions, `pushd`, `source` all stick across
  commands), and posts back output, exit code, and cwd. The worker runs
  persistently under a loop (`worker/cmdrelay-worker-loop.sh`) and
  waits idle when no session is active.
- **Client** (human's machine, same box as the server): `slickterm`,
  the interactive terminal. It owns the server: starts it on entry if
  it isn't running, starts a session, sends commands, polls for
  results, and stops the server on exit. Any client that speaks the
  protocol below also works.

Where each part runs: the worker runs on the agent VM; the server and
the client both run on the human's machine.

## Dependencies

- Python 3.8+ standard library only — no third-party packages
  (`server.py` and `worker.py` use `http.server`, `urllib`, `subprocess`).
- OpenSSH client (`ssh`) on the worker machine, for the tunnel.
- `bash` for `install.sh`.
- `openssl` (or Python 3) for token generation during install.

## Install

Clone the repo on **both** machines, then run the installer with the
role for that machine. Config is written **outside** the repo checkout,
to `~/.config/cmdrelay/` (0600 perms). Re-running the installer is
safe: it reads the existing config for defaults and never regenerates
an existing token.

### 1. Server role (human's machine)

```sh
./install.sh --role server
```

What the installer asks / collects:

| Prompt | Default | Notes |
|---|---|---|
| Bind host | `127.0.0.1` | Loopback only is strongly recommended. |
| Bind port | `8770` | Must match the worker's server port. |
| Data dir | `<install-dir>` | Holds `audit.log` and `server.pid`. |

What it does:

- Copies `server/server.py` to `~/.local/share/cmdrelay/server.py`.
- Generates a random bearer token (32 bytes, hex) into
  `~/.config/cmdrelay/.token` (0600) — **only if none exists**.
- Writes `~/.config/cmdrelay/config.env` (0600).
- **Prints the token ONCE.** Copy it now — it is never shown again.
  If it is lost, delete `~/.config/cmdrelay/.token` and re-run the
  installer to regenerate (then re-install the worker with the new token).

Run the server with:

```sh
python3 ~/.local/share/cmdrelay/server.py
```

(In practice your client starts/stops it on demand; no systemd unit
is installed for the server.)

### 2. Worker role (agent VM)

```sh
./install.sh --role worker
```

What the installer asks / collects:

| Prompt | Default | Notes |
|---|---|---|
| Server SSH host | *(none — required)* | Hostname or `~/.ssh/config` alias of the server machine. |
| Server SSH user | `$USER` | SSH user on the server machine. |
| Server port | `8770` | Must match the server install. |
| Tunnel port | `18770` | Local port on the VM forwarded to the server. |
| Token | *(paste)* | Paste the token printed by the server install. Input is not echoed (`read -s`). Press Enter with no input to keep an already-installed token. |

What it does:

- Copies `worker/worker.py` and `worker/cmdrelay-worker-loop.sh` to
  `~/.local/share/cmdrelay/`.
- Writes the pasted token to `~/.config/cmdrelay/.token` (0600),
  or keeps the existing token if you pressed Enter.
- Writes `~/.config/cmdrelay/config.env` (0600).

Run the worker via the loop with:

```sh
bash ~/.local/share/cmdrelay/cmdrelay-worker-loop.sh
```

The loop respawns `worker.py` on crash and is the canonical keeper —
there is no systemd option (systemd user units do not survive VM
refresh). Or run the worker manually with:

```sh
python3 ~/.local/share/cmdrelay/worker.py
```

Check SSH first: `ssh <user>@<host> true`. The worker idles until a
client starts a session on the server.

### 3. Client role (human's machine, same box as the server)

```sh
./install.sh --role client
```

What it does:

- Copies `client/slickterm` to `~/.local/share/cmdrelay/slickterm`.
  No prompts, no config changes — the client reads the token, port,
  and paths from the config the server install already wrote
  (`~/.config/cmdrelay/config.env`).

Run it with:

```sh
~/.local/share/cmdrelay/slickterm
```

`slickterm` starts the server on entry (if it isn't running) and
stops it on exit. Type commands at the `slick-vm$` prompt; Ctrl+C,
Ctrl+D, or `exit` ends the session and disconnects.

### Non-interactive install

```sh
./install.sh --role server --non-interactive
CMDRELAY_SSH_HOST=<your-hostname> CMDRELAY_TOKEN=<token-from-server-install> \
    ./install.sh --role worker --non-interactive
./install.sh --role client --non-interactive
```

Non-interactive mode reads these environment variables:

- All roles: `CMDRELAY_ROLE`, `CMDRELAY_CONFIG_DIR`
  (default `~/.config/cmdrelay`), `CMDRELAY_INSTALL_DIR`
  (default `~/.local/share/cmdrelay`).
- Server: `CMDRELAY_BIND_HOST`, `CMDRELAY_BIND_PORT`,
  `CMDRELAY_DATA_DIR`.
- Worker: `CMDRELAY_SSH_HOST` (required), `CMDRELAY_SSH_USER`,
  `CMDRELAY_SERVER_PORT`, `CMDRELAY_TUNNEL_PORT`,
  `CMDRELAY_TOKEN`.
- Either role: `CMDRELAY_TOKEN_FILE` (non-default token location),
  `CMDRELAY_TOKEN` (runtime override — prefer the token file).

Passwords are never collected — SSH uses key auth / your
`~/.ssh/config`.

## Files

| File | What it is |
|---|---|
| `server/server.py` | Relay server (loopback HTTP, token auth, session queue). |
| `worker/worker.py` | Worker (SSH tunnel, long-poll, persistent bash). |
| `worker/cmdrelay-worker-loop.sh` | Loop that keeps the worker running (respawns on crash). |
| `client/slickterm` | Interactive client (owns the server: starts/stops it). |
| `install.sh` | Three-sided installer (`--role server\|worker\|client`). |
| `~/.config/cmdrelay/config.env` | Per-machine config (0600, gitignored). |
| `~/.config/cmdrelay/.token` | Bearer token (0600, gitignored, never committed). |
| `~/.cmdrelay_bashrc` | Startup file sourced by the worker's persistent shell (created with a default alias if missing). |

Legacy layouts are still honored: a `.token` next to `server.py` /
`worker.py` is used if no config-dir token exists, and `slickterm`
falls back to the flat `~/cmdrelay/` layout if the installed layout
is absent.

## How a session works

1. The client (`slickterm`) starts the server (if needed) and posts
   `POST /session/start`.
2. The worker's SSH tunnel connects; the worker long-polls
   `GET /take?wait=55` and the client heartbeats
   `POST /session/beat` every 30s.
3. The client posts `POST /cmd {command}` → `{id}`, then polls
   `GET /result/<id>?wait=55` until the result arrives.
4. The worker runs the command in its persistent bash and posts
   `POST /result {id, stdout, stderr, exit_code, truncated, cwd}`.
5. On exit the client posts `POST /session/end`, waits ~1.5s so the
   worker sees `session_end` via its long-poll, then stops the server.
   The worker stops its shell and waits for the next session.

## Protocol (all JSON, all behind `Authorization: Bearer <token>`)

- `POST /session/start` → 200 / 409 if busy
- `POST /session/end`, `POST /session/beat`, `POST /worker/beat`
- `GET /session/status` → `{active, worker_alive}`
- `POST /cmd {command}` → `{id}`
- `GET /take?wait=55` → `{type: command|session_end|none, ...}`
- `POST /result {id, stdout, stderr, exit_code, truncated, cwd}`
- `GET /result/<id>?wait=55` → result / `{status: pending|no_session}`

## Persistent shell (Phase 1)

One `bash --norc --noprofile` lives for the whole session — NOT
`bash -c` per command. Startup: the worker sets
`shopt -s expand_aliases`, `set +m`, then sources
`~/.cmdrelay_bashrc` (created with a default alias if missing).
A dedicated rc file avoids a default `.bashrc` interactive-shell
guard and keeps cmdrelay config separate from the login shell.

- Prompt: the worker reports `cwd` after each command (HOME shortened
  to `~`); a client can show `<host>:<cwd>$`.
- Output: stdout+stderr merged in order, like a real terminal (the
  `/result` `stderr` field is always `""`). Exit code preserved per command.
- Sentinel framing: each command is followed by `echo <marker>:$?`,
  `pwd`, `echo <marker>_PWD`; the marker is random per session. Output
  without a trailing newline is split at the marker so it doesn't merge
  with it.
- Timeout (120s): the shell is killed and respawned fresh (rc
  re-sourced); the timed-out command returns exit 124 + partial output.
  Shell state (cwd/aliases/exports) is lost on timeout — documented,
  not silent.

Still NOT terminal-like in Phase 1 (needs a Phase 2 PTY for these):

- No `vi` / `less` / `top` (full-screen programs need a PTY + raw mode).
- No remote tab-completion or VM-side up-arrow history.
- No streaming output (result still arrives when the command finishes).
- Commands that read stdin (`read`, `ssh`, password prompts) block
  until timeout.
- Ctrl+C in the client still ends the session, not just the running command.

## Limits

- Per-command timeout: 120s (exit code 124 on timeout, shell respawned).
- Output cap: 256KB per command, flagged `truncated`.
- Commands run as the VM user — the same trust level as the agent's
  own shell.

## Security notes

- Server binds loopback only by default; the worker reaches it via an
  `ssh -L` tunnel. No public port, no web exposure.
- Token files are 0600 on both ends. **Anyone holding the token (or
  with local access to the server machine) can run commands on the VM.**
  The audit log records every command and result metadata.
- Never commit `.token`, `config.env`, or logs — see `.gitignore`.
- The audit log (`audit.log` in the data dir) records every command
  (first 200 chars) and result metadata. Protect it like the token.

## Uninstall

```sh
# Stop the worker loop and worker first (on the VM):
pkill -f cmdrelay-worker-loop.sh; pkill -f cmdrelay/worker.py
# Stop the server if a client left it running (on the server machine):
pkill -f cmdrelay/server.py
rm -rf ~/.local/share/cmdrelay ~/.config/cmdrelay
```

## License

This project is licensed under the MIT License — see [LICENSE](LICENSE).
