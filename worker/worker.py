#!/usr/bin/env python3
"""cmdrelay worker — runs persistently on the agent VM.

Opens an SSH tunnel to the relay server on the human's machine,
long-polls /take, executes each command in ONE persistent bash (so cd /
alias / export / functions stick across commands), posts back
output/exit code/cwd. When a session ends, the shell is stopped and the
worker waits for the next session — it does NOT exit and does NOT stop
the server. The client on the human's machine owns the server: it
starts the server on entry (if needed) and stops it on exit.

Configuration: environment variables (CMDRELAY_*) override
~/.config/cmdrelay/config.env (written by install.sh).
Token: CMDRELAY_TOKEN_FILE, else ~/.config/cmdrelay/.token,
else .token next to this file (legacy).

No LLM tokens are involved: this is plain Python long-polling.
Idle cost is one SSH tunnel + a /session/status poll every 2s.

Phase 1 design (see README):
- Persistent `bash --norc --noprofile` per session, NOT `bash -c` per command.
- `shopt -s expand_aliases` + source ~/.cmdrelay_bashrc on startup.
  A dedicated rc file (not ~/.bashrc) avoids the interactive-shell guard
  in a default .bashrc and keeps cmdrelay config separate.
- stdout+stderr merged (like a real terminal; avoids pipe deadlock).
  The /result `stderr` field is therefore always "".
- Each command is wrapped with sentinels that report exit code + cwd:
  the worker sends the user command, then a marker block, and reads
  until both markers arrive. The exit code is saved to a variable
  first so the marker echo doesn't clobber $?. If the command's
  output lacks a trailing newline, the marker lands on the same line
  (e.g. `fooMARK:0`); the parser splits at the marker so the partial
  line is still returned as output.
- Timeout (120s): the shell is killed and respawned fresh (rc re-sourced);
  the timed-out command returns exit 124 + partial output. Shell state
  is lost on timeout — the same tradeoff as before, now explicit.
"""
import json
import os
import queue
import secrets
import shlex
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_config():
    """Config file (written by install.sh) + environment, env wins.

    Config file keys use the CMDRELAY_ prefix, one KEY=VALUE per line.
    """
    cfg = {}
    config_dir = os.environ.get("CMDRELAY_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".config", "cmdrelay")
    try:
        with open(os.path.join(config_dir, "config.env")) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                cfg[key.strip()] = val.strip().strip('"').strip("'")
    except OSError:
        pass
    for key, val in os.environ.items():
        if key.startswith("CMDRELAY_"):
            cfg[key] = val
    cfg.setdefault("CMDRELAY_CONFIG_DIR", config_dir)
    return cfg


_CFG = _load_config()


def _int_cfg(key, default):
    try:
        return int(_CFG.get(key, str(default)))
    except ValueError:
        raise SystemExit("cmdrelay worker: %s is not a number" % key)


TUNNEL_PORT = _int_cfg("CMDRELAY_TUNNEL_PORT", 18770)
SERVER_PORT = _int_cfg("CMDRELAY_SERVER_PORT", 8770)
SSH_HOST = _CFG.get("CMDRELAY_SSH_HOST", "")
SSH_USER = _CFG.get("CMDRELAY_SSH_USER", "")
BASE = "http://127.0.0.1:%d" % TUNNEL_PORT
CMD_TIMEOUT = 120          # max seconds per command
OUT_CAP = 256 * 1024       # max bytes captured per command
RC_PATH = os.path.expanduser(
    _CFG.get("CMDRELAY_RC_FILE", "~/.cmdrelay_bashrc"))
if not os.path.exists(RC_PATH):
    _legacy_rc = os.path.expanduser("~/.slickterm_bashrc")
    if os.path.exists(_legacy_rc):
        RC_PATH = _legacy_rc  # pre-rename installs keep their rc file
RC_DEFAULT = """# cmdrelay startup file — sourced by the persistent shell for each session.
# Edit freely: aliases, functions, exports persist for the session.
# (Kept separate from ~/.bashrc so cmdrelay doesn't depend on interactive-shell guards.)
alias ls='ls -l'
"""

TOKEN = _CFG.get("CMDRELAY_TOKEN", "")
if not TOKEN:
    _token_file = (
        _CFG.get("CMDRELAY_TOKEN_FILE")
        or os.path.join(_CFG["CMDRELAY_CONFIG_DIR"], ".token")
    )
    if not os.path.exists(_token_file) and os.path.exists(
            os.path.join(HERE, ".token")):
        _token_file = os.path.join(HERE, ".token")  # legacy location
    try:
        with open(_token_file) as f:
            TOKEN = f.read().strip()
    except OSError:
        raise SystemExit(
            "cmdrelay worker: no token found at %s "
            "(run install.sh --role worker first)" % _token_file)
if not TOKEN:
    raise SystemExit("cmdrelay worker: empty token")


def api(method, path, data=None, timeout=70):
    req = urllib.request.Request(
        BASE + path, method=method,
        headers={"Authorization": "Bearer " + TOKEN,
                 "Content-Type": "application/json"},
        data=json.dumps(data).encode() if data is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP %d on %s" % (e.code, path))


def ensure_rc():
    """Create the rc file with the default alias if it doesn't exist."""
    try:
        if not os.path.exists(RC_PATH):
            with open(RC_PATH, "w") as f:
                f.write(RC_DEFAULT)
            os.chmod(RC_PATH, 0o644)
    except OSError as e:
        print("worker: could not create %s: %s" % (RC_PATH, e), flush=True)


class PersistentShell:
    """One long-lived bash; commands run in it sequentially."""

    def __init__(self):
        # Random per-session marker: negligible chance user output contains it.
        self.marker = "__CMDRELAY_END_%s__" % secrets.token_hex(8)
        self.home = os.path.expanduser("~")
        self.last_cwd = "~"
        self.proc = None
        self.lines = None  # queue.Queue[str]
        self._spawn()

    def _reader(self, proc, q):
        try:
            for line in proc.stdout:
                q.put(line)
        except Exception:
            pass
        finally:
            q.put(None)  # EOF: shell died

    def _spawn(self):
        ensure_rc()
        self.proc = subprocess.Popen(
            ["bash", "--norc", "--noprofile"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1)
        self.lines = queue.Queue()
        threading.Thread(target=self._reader, args=(self.proc, self.lines),
                         daemon=True).start()
        # Setup: alias expansion on, no job-control noise, source the rc file.
        # Drain setup output up to the sentinel so rc echoes never leak
        # into the first command's result.
        setup = (
            "shopt -s expand_aliases\n"
            "set +m\n"
            "[ -f %s ] && source %s\n"
            "echo %s_SETUP\n" % (shlex.quote(RC_PATH), shlex.quote(RC_PATH),
                                 self.marker)
        )
        try:
            self.proc.stdin.write(setup)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        self._drain_until(self.marker + "_SETUP", timeout=10)

    def _drain_until(self, sentinel, timeout):
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty:
                return
            if line is None:
                return
            if line.rstrip("\n") == sentinel:
                return

    def _respawn(self):
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.kill()
        except Exception:
            pass
        try:
            if self.proc:
                self.proc.wait(timeout=5)
        except Exception:
            pass
        self._spawn()

    def _shorten(self, cwd):
        if cwd == self.home:
            return "~"
        if cwd.startswith(self.home + "/"):
            return "~" + cwd[len(self.home):]
        return cwd

    def run(self, cmd):
        """Run one command in the persistent shell.

        Returns (output_text, exit_code, cwd, truncated).
        """
        if self.proc is None or self.proc.poll() is not None:
            self._respawn()
        marker = self.marker
        # Save $? before the bare echo (which would clobber it), then emit
        # exit code, cwd, and a second marker delimiting the cwd line.
        block = (
            cmd + "\n"
            "__cmdrelay_code=$?; echo %s:$__cmdrelay_code; "
            "unset __cmdrelay_code; pwd; echo %s_PWD\n" % (marker, marker)
        )
        try:
            self.proc.stdin.write(block)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            self._respawn()
            return ("(shell died, respawned — command not run)\n", 1,
                    self.last_cwd, False)

        deadline = time.time() + CMD_TIMEOUT
        out_lines = []
        out_bytes = 0
        truncated = False
        exit_code = None
        cwd = None
        phase = "output"  # output -> cwd -> done
        while phase != "done":
            remaining = deadline - time.time()
            if remaining <= 0:
                # Timeout: kill the shell (the running command dies with it)
                # and respawn fresh. State is lost, but the session survives.
                partial = "".join(out_lines)
                self._respawn()
                return (partial, 124, self.last_cwd, True)
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty:
                partial = "".join(out_lines)
                self._respawn()
                return (partial, 124, self.last_cwd, True)
            if line is None:
                # Shell died mid-command; respawn and report.
                partial = "".join(out_lines)
                self._respawn()
                return (partial + "(shell died, respawned)\n", 1,
                        self.last_cwd, False)
            stripped = line.rstrip("\n")
            if phase == "output":
                mpos = stripped.find(marker + ":")
                if mpos >= 0:
                    # Output before the marker on the same line is a
                    # partial line (command printed without trailing \n).
                    if mpos > 0:
                        prefix = stripped[:mpos]
                        if out_bytes < OUT_CAP:
                            out_lines.append(prefix + "\n")
                            out_bytes += len(prefix.encode("utf-8", "replace")) + 1
                    try:
                        exit_code = int(stripped[mpos + len(marker) + 1:])
                    except ValueError:
                        exit_code = 1
                    phase = "cwd"
                    continue
                if out_bytes < OUT_CAP:
                    out_lines.append(line)
                    out_bytes += len(line.encode("utf-8", "replace"))
                    if out_bytes >= OUT_CAP:
                        truncated = True
                else:
                    truncated = True
            else:  # phase == "cwd": exactly one pwd line, then the PWD marker
                if stripped == marker + "_PWD":
                    phase = "done"
                    continue
                if cwd is None:
                    cwd = stripped
                # Any extra lines before the PWD marker are ignored.
        if cwd is None:
            cwd = self.last_cwd
        else:
            self.last_cwd = self._shorten(cwd)
            cwd = self.last_cwd
        if exit_code is None:
            exit_code = 1
        return ("".join(out_lines), exit_code, cwd, truncated)

    def stop(self):
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.stdin.write("exit\n")
                self.proc.stdin.flush()
                self.proc.wait(timeout=3)
        except Exception:
            pass
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.kill()
        except Exception:
            pass


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S") + " worker: " + msg, flush=True)


def _stop_tunnel(tun):
    if tun is None:
        return
    try:
        tun.terminate()
        tun.wait(timeout=5)
    except Exception:
        try:
            tun.kill()
        except Exception:
            pass


def _serve_one_session():
    """One tunnel + one session. Returns when the session ends or the
    server/tunnel goes away. Never kills the server — the client owns it."""
    if not SSH_HOST:
        raise SystemExit(
            "cmdrelay worker: no SSH host configured "
            "(set CMDRELAY_SSH_HOST or run install.sh --role worker)")
    ssh_target = ("%s@%s" % (SSH_USER, SSH_HOST)) if SSH_USER else SSH_HOST
    tun = subprocess.Popen(
        ["ssh", "-N", "-o", "ExitOnForwardFailure=yes",
         "-L", "%d:127.0.0.1:%d" % (TUNNEL_PORT, SERVER_PORT), ssh_target],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    shell = None
    try:
        # Idle wait: server is down until the client starts it. Poll quietly
        # (no log spam) until a session appears.
        while True:
            try:
                st = api("GET", "/session/status", timeout=15)
            except Exception:
                time.sleep(2)
                continue
            if st.get("active"):
                break
            time.sleep(2)
        log("session active, starting shell")
        try:
            api("POST", "/worker/beat", {}, timeout=15)
        except Exception:
            pass
        shell = PersistentShell()
        fails = 0
        while True:
            try:
                msg = api("GET", "/take?wait=55", timeout=70)
            except Exception:
                fails += 1
                if fails >= 5:
                    log("server/tunnel gone mid-session, ending session")
                    break
                time.sleep(2)
                continue
            fails = 0
            t = msg.get("type")
            if t == "session_end":
                break
            if t != "command":
                try:
                    api("POST", "/worker/beat", {}, timeout=15)
                except Exception:
                    pass
                continue
            cmd, cid = msg["command"], msg["id"]
            out, code, cwd, trunc = shell.run(cmd)
            try:
                api("POST", "/result", {
                    "id": cid,
                    "stdout": out,
                    "stderr": "",
                    "exit_code": code,
                    "truncated": trunc,
                    "cwd": cwd,
                }, timeout=30)
            except Exception:
                # Result lost (server went away); session is over.
                break
            try:
                api("POST", "/worker/beat", {}, timeout=15)
            except Exception:
                pass
        log("session ended")
    finally:
        if shell is not None:
            shell.stop()
        _stop_tunnel(tun)


def main():
    # Persistent: loop forever across sessions. The loop wrapper
    # (worker-loop.sh) respawns us if we crash; this loop handles the
    # normal session-end -> wait-for-next-session transition.
    while True:
        try:
            _serve_one_session()
        except Exception as e:
            log("cycle error: %s" % e)
        time.sleep(2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
