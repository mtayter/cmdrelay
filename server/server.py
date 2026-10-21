#!/usr/bin/env python3
"""cmdrelay server — on-demand command relay between a human's machine
and an agent VM.

- Listens on the configured bind host/port (default 127.0.0.1:8770,
  loopback only — no public exposure).
- Every endpoint requires `Authorization: Bearer <token>`.
- One active session at a time. The worker long-polls /take; the client
  long-polls /result/<id>.
- Configuration: environment variables (CMDRELAY_*) override
  ~/.config/cmdrelay/config.env (written by install.sh).
  Token: CMDRELAY_TOKEN_FILE, else ~/.config/cmdrelay/.token.
- Run:  python3 server.py   (logs to audit.log in the data dir)
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

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
BIND_HOST = _CFG.get("CMDRELAY_BIND_HOST", "127.0.0.1")
try:
    PORT = int(_CFG.get("CMDRELAY_BIND_PORT", "8770"))
except ValueError:
    raise SystemExit("cmdrelay server: CMDRELAY_BIND_PORT is not a number")
DATA_DIR = _CFG.get("CMDRELAY_DATA_DIR") or HERE
TOKEN_FILE = (
    _CFG.get("CMDRELAY_TOKEN_FILE")
    or os.path.join(_CFG["CMDRELAY_CONFIG_DIR"], ".token")
)
AUDIT_LOG = os.path.join(DATA_DIR, "audit.log")
BEAT_TIMEOUT = 120  # session auto-ends after this long without a CLI heartbeat

TOKEN = _CFG.get("CMDRELAY_TOKEN", "")
if not TOKEN:
    try:
        with open(TOKEN_FILE) as f:
            TOKEN = f.read().strip()
    except OSError:
        raise SystemExit(
            "cmdrelay server: no token found at %s "
            "(run install.sh --role server first)" % TOKEN_FILE)
if not TOKEN:
    raise SystemExit("cmdrelay server: empty token")

_lock = threading.Lock()
_cv = threading.Condition(_lock)
_session = None   # {"id": str, "last_beat": float, "worker_beat": float}
_pending = []     # [{"id": int, "command": str}]
_results = {}     # id -> {"stdout","stderr","exit_code","truncated"}
_next_id = 1


def audit(msg):
    try:
        with open(AUDIT_LOG, "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg + "\n")
    except OSError:
        pass


def _alive():
    return _session is not None and (time.time() - _session["last_beat"] < BEAT_TIMEOUT)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def _authed(self):
        return self.headers.get("Authorization") == "Bearer " + TOKEN

    def _wait(self, wait_secs, ready):
        """Hold the request until ready() is true, session dies, or timeout."""
        deadline = time.time() + wait_secs
        while True:
            if ready():
                return True
            if not _alive():
                return False
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            _cv.wait(timeout=min(remaining, 5))

    def do_GET(self):
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        u = urlparse(self.path)
        q = parse_qs(u.query)

        if u.path == "/health":
            return self._send(200, {"ok": True})

        if u.path == "/session/status":
            with _lock:
                alive = _alive()
                wbeat = _session["worker_beat"] if _session else 0
                return self._send(200, {
                    "active": alive,
                    "worker_alive": alive and (time.time() - wbeat < BEAT_TIMEOUT),
                })

        if u.path == "/take":
            wait = min(int(q.get("wait", ["55"])[0]), 120)
            with _cv:
                if not _alive():
                    return self._send(200, {"type": "session_end"})
                r = self._wait(wait, lambda: bool(_pending))
                if r is True:
                    cmd = _pending.pop(0)
                    return self._send(200, {"type": "command",
                                           "id": cmd["id"], "command": cmd["command"]})
                if r is False:
                    return self._send(200, {"type": "session_end"})
                return self._send(200, {"type": "none"})

        if u.path.startswith("/result/"):
            try:
                cid = int(u.path.rsplit("/", 1)[1])
            except ValueError:
                return self._send(400, {"error": "bad id"})
            wait = min(int(q.get("wait", ["55"])[0]), 120)
            with _cv:
                r = self._wait(wait, lambda: cid in _results)
                if r is True:
                    return self._send(200, _results.pop(cid))
                if r is False:
                    return self._send(200, {"status": "no_session"})
                return self._send(200, {"status": "pending"})

        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        u = urlparse(self.path)
        body = self._read_json()
        global _session, _next_id
        with _cv:
            if u.path == "/session/start":
                if _alive():
                    return self._send(409, {"error": "session already active"})
                _session = {"id": "%d" % int(time.time()),
                           "last_beat": time.time(), "worker_beat": 0}
                _pending.clear()
                _results.clear()
                audit("session start")
                _cv.notify_all()
                return self._send(200, {"session": _session["id"]})

            if u.path == "/session/end":
                if _session:
                    audit("session end")
                _session = None
                _cv.notify_all()
                return self._send(200, {"ok": True})

            if u.path == "/session/beat":
                if _session:
                    _session["last_beat"] = time.time()
                return self._send(200, {"ok": True})

            if u.path == "/worker/beat":
                if _session:
                    _session["worker_beat"] = time.time()
                return self._send(200, {"ok": True})

            if u.path == "/cmd":
                if not _alive():
                    return self._send(409, {"error": "no active session"})
                cmd = str(body.get("command", ""))
                if not cmd.strip():
                    return self._send(400, {"error": "empty command"})
                cid = _next_id
                _next_id += 1
                _pending.append({"id": cid, "command": cmd})
                audit("cmd %d: %s" % (cid, cmd[:200].replace("\n", " ")))
                _cv.notify_all()
                return self._send(200, {"id": cid})

            if u.path == "/result":
                try:
                    cid = int(body.get("id", -1))
                except (TypeError, ValueError):
                    return self._send(400, {"error": "bad id"})
                res = {"stdout": str(body.get("stdout", "")),
                       "stderr": str(body.get("stderr", "")),
                       "exit_code": int(body.get("exit_code", -1)),
                       "truncated": bool(body.get("truncated", False)),
                       "cwd": str(body.get("cwd", ""))}
                _results[cid] = res
                audit("result %d: exit=%d out=%dB err=%dB" % (
                    cid, res["exit_code"], len(res["stdout"]), len(res["stderr"])))
                _cv.notify_all()
                return self._send(200, {"ok": True})

        return self._send(404, {"error": "not found"})


if __name__ == "__main__":
    with open(os.path.join(DATA_DIR, "server.pid"), "w") as f:
        f.write(str(os.getpid()))
    srv = ThreadingHTTPServer((BIND_HOST, PORT), Handler)
    audit("server started")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
