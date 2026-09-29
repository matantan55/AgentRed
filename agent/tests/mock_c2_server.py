"""
mock_c2_server.py — lab stand-in for the C2 server + Windows agent.

Speaks the real wire protocol (newline-delimited JSON, operator token
auth, module dispatch, push events) so the operator agent can be
developed and demoed without a Windows VM. The `shell start` handler
connects BACK to the operator's listener and emulates cmd.exe over an
in-memory filesystem, so file_read / dir_list / file_send round-trip.

Not part of the deliverable's runtime path — a teaching/test harness.
    python3 mock_c2_server.py                      # 127.0.0.1:4444, token labtoken
    python3 mock_c2_server.py --host 0.0.0.0 --port 4444 --token labtoken
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import socket
import sys
import threading
import time

HOST = "127.0.0.1"
PORT = 4444
TOKEN = "labtoken"

# ── fake victim filesystem ────────────────────────────────────────────
FS = {
    r"C:\Users\lab\notes.txt":
        "Lab 4 notes - C2 telemetry exercise\n"
        "====================================\n"
        "1. Agent DLL loaded via AppInit_DLLs during the snapshot.\n"
        "2. Beacon interval observed: ~5s jittered.\n"
        "3. TODO: correlate notify events with process creation times.\n",
    r"C:\Windows\System32\drivers\etc\hosts":
        "# Copyright (c) 1993-2009 Microsoft Corp.\n"
        "127.0.0.1       localhost\n"
        "::1             localhost\n",
    r"C:\Users\lab\report.csv": "ts,event,detail\n",
}
SHELL_RUNNING = {"v": False}
MONITOR = {"on": False, "snaps": []}

PROCESSES = [
    {"pid": 4,    "name": "System",          "cpu": 0.1, "mem_mb": 8,    "user": "SYSTEM"},
    {"pid": 412,  "name": "svchost.exe",     "cpu": 0.4, "mem_mb": 22,   "user": "NETWORK SERVICE"},
    {"pid": 1024, "name": "explorer.exe",    "cpu": 1.2, "mem_mb": 145,  "user": "LAB\\matan"},
    {"pid": 2048, "name": "chrome.exe",      "cpu": 7.5, "mem_mb": 812,  "user": "LAB\\matan"},
    {"pid": 2340, "name": "cmd.exe",         "cpu": 0.0, "mem_mb": 4,    "user": "LAB\\matan"},
    {"pid": 3100, "name": "Taskmgr.exe",     "cpu": 0.3, "mem_mb": 31,   "user": "LAB\\matan"},
    {"pid": 4096, "name": "MsMpEng.exe",     "cpu": 3.1, "mem_mb": 190,  "user": "NT AUTHORITY"},
]
CONNS = [
    {"proto": "TCP", "laddr": "127.0.0.1:49678", "raddr": "10.10.0.5:4444", "state": "ESTABLISHED", "pid": 4096},
    {"proto": "TCP", "laddr": "192.168.1.40:52133", "raddr": "140.82.113.4:443", "state": "ESTABLISHED", "pid": 2048},
    {"proto": "TCP", "laddr": "192.168.1.40:3389", "raddr": "192.168.1.7:59322", "state": "LISTENING", "pid": 4},
]
RUN_KEY = {"OneDrive": r"C:\Program Files\Microsoft OneDrive\OneDrive.exe /background",
           "SecurityHealth": r"%windir%\system32\SecurityHealthSystray.exe",
           "LabAgent": r"C:\ProgramData\lab\agentloader.exe --cfg beacon.json"}


# ── module handlers ───────────────────────────────────────────────────

def h_activity(p: dict):
    a = p.get("action")
    if a == "system_stats":
        return {"status": "ok", "os": "Windows 11 Pro 23H2 (Build 22631)", "hostname": "LAB-VM-04",
                "cpu_count": 8, "cpu_percent": 17.3, "ram_total_mb": 16384, "ram_used_mb": 9211,
                "uptime_s": 172_830, "disks": {"C:": {"total_gb": 255, "free_gb": 87}}}
    if a == "processes":
        procs = PROCESSES
        if p.get("name"):
            procs = [x for x in procs if p["name"].lower() in x["name"].lower()]
        return {"status": "ok", "count": len(procs), "processes": procs}
    if a == "network_connections":
        return {"status": "ok", "connections": CONNS}
    if a in ("top_cpu", "top_mem"):
        key = "cpu" if a == "top_cpu" else "mem_mb"
        return {"status": "ok", "processes": sorted(PROCESSES, key=lambda x: -x[key])[:int(p.get("limit", 10))]}
    if a == "start_monitor":
        MONITOR["on"] = True
        return {"status": "ok", "message": f"monitor started, interval {p.get('interval', 5)}s"}
    if a == "stop_monitor":
        MONITOR["on"] = False
        return {"status": "ok", "message": "monitor stopped"}
    if a == "get_snapshots":
        snap = {"ts": time.strftime("%H:%M:%S"), "cpu_percent": 22.4,
                "ram_used_mb": 9554, "new_procs": ["Notepad.exe"] if MONITOR["on"] else []}
        MONITOR["snaps"].append(snap)
        return {"status": "ok", "snapshots": MONITOR["snaps"][-int(p.get("limit", 5)):]}
    return {"status": "error", "message": f"unknown activity action {a!r}"}


def h_registry(p: dict):
    a, key = p.get("action"), p.get("key_path", "")
    if a == "list_keys":
        subs = {"Software\\Microsoft": ["Windows", ".NET Framework"],
                r"Software\Microsoft\Windows\CurrentVersion": ["Run", "Explorer", "Policies"]}.get(key, ["Windows"])
        return {"status": "ok", "hive": p.get("hive"), "key_path": key, "subkeys": subs}
    if a == "list_values":
        if key.endswith("\\Run"):
            return {"status": "ok", "values": RUN_KEY}
        return {"status": "ok", "values": {"(default)": ""}}
    if a == "read_value":
        if key.endswith("\\Run") and p.get("value_name") in RUN_KEY:
            return {"status": "ok", "data": RUN_KEY[p["value_name"]], "type": "REG_SZ"}
        return {"status": "error", "message": f"value {p.get('value_name')!r} not found"}
    if a == "write_value":
        RUN_KEY[p["value_name"]] = p["value_data"]
        return {"status": "ok", "message": f"wrote {key}\\{p['value_name']}"}
    if a == "delete_value":
        RUN_KEY.pop(p.get("value_name"), None)
        return {"status": "ok", "message": "deleted"}
    if a in ("create_key", "delete_key"):
        return {"status": "ok", "message": f"{a} {key} (mock)"}
    return {"status": "error", "message": f"unknown registry action {a!r}"}


def h_ssh(p: dict):
    a = p.get("action")
    if a == "connect":
        return {"status": "ok", "session_id": p.get("session_id"),
                "message": f"connected to {p.get('username')}@{p.get('host')}:{p.get('port')}"}
    if a == "exec":
        cmd = p.get("command", "")
        out = {"whoami": "lab-vm-04\\matan", "hostname": "LAB-VM-04",
               "ver": "Microsoft Windows [Version 10.0.22631.5550]"}.get(cmd.strip(),
               f"(mock ssh exec output for: {cmd})")
        return {"status": "ok", "stdout": out, "stderr": "", "exit_code": 0}
    if a == "list":
        return {"status": "ok", "sessions": []}
    if a == "disconnect":
        return {"status": "ok", "message": "disconnected"}
    return {"status": "error", "message": f"unknown ssh action {a!r}"}


def h_rdp(p: dict):
    a = p.get("action")
    if a == "probe":
        return {"status": "ok", "open": True, "banner": "cookie: clrdata 0xd0a002ef (mock)"}
    if a == "open":
        return {"status": "ok", "session_id": p.get("session_id"),
                "message": f"mstsc launched toward {p.get('host')} (headless in mock)"}
    if a == "list":
        return {"status": "ok", "sessions": []}
    if a == "close":
        return {"status": "ok", "message": "closed"}
    return {"status": "error", "message": f"unknown rdp action {a!r}"}


def h_notify(p: dict, send_event):
    a = p.get("action")
    if a == "start":
        threading.Timer(1.0, lambda: send_event({
            "event": "notification", "title": "Microsoft Outlook",
            "body": "Meeting reminder: Cyber Lab sync — 16:00 in room B2",
            "win_class": "ToastContentArea", "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")})).start()
        return {"status": "ok", "message": "notification monitor started"}
    if a == "stop":
        return {"status": "ok", "message": "monitor stopped"}
    if a == "status":
        return {"status": "ok", "running": True}
    if a == "send_popup":
        return {"status": "ok", "message": f"popup shown: {p.get('title')!r}"}
    if a == "send_balloon":
        return {"status": "ok", "message": f"balloon shown: {p.get('title')!r}"}
    return {"status": "error", "message": f"unknown notify action {a!r}"}


# ── fake cmd.exe over the reverse-shell channel ───────────────────────

def fake_cmd(sock: socket.socket):
    def out(s: str):
        sock.sendall(s.encode("utf-8", errors="replace"))
    PROMPT = "C:\\Users\\lab>"
    out(f"Microsoft Windows [Version 10.0.22631.5550]\r\n(c) Microsoft Corporation. All rights reserved.\r\n\r\n{PROMPT}")
    sock.settimeout(None)
    buf = b""
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            while b"\r\n" in buf:
                line, buf = buf.split(b"\r\n", 1)
                cmd = line.decode("utf-8", errors="replace").strip()
                if not cmd:
                    out(PROMPT)
                    continue
                out(handle_cmd(cmd))
                out("\r\n" + PROMPT)
    except OSError:
        pass
    finally:
        SHELL_RUNNING["v"] = False
        try:
            sock.close()
        except OSError:
            pass


def handle_cmd(cmd: str) -> str:
    m = re.match(r'^type\s+"?([^"]+)"?$', cmd, re.I)
    if m:
        path = m.group(1)
        if path.upper() in {k.upper() for k in FS}:
            key = next(k for k in FS if k.upper() == path.upper())
            return "\r\n" + FS[key].replace("\n", "\r\n")
        return "The system cannot find the file specified."
    m = re.match(r'^dir\s+"?([^"]*)"?$', cmd, re.I)
    if m:
        base = (m.group(1) or r"C:\Users\lab").rstrip("\\")
        entries = sorted({k[len(base) + 1:].split("\\")[0]
                          for k in FS if k.upper().startswith(base.upper() + "\\")})
        head = f" Volume in drive C is LAB\r\n Directory of {base}\r\n\r\n"
        return head + "".join(f"29-09-2026  11:32    <DIR>          {e}\r\n" for e in entries) + f"               {len(entries)} File(s)"
    m = re.match(r'^echo\s+(.+?)\s*>>\s*"?([^"]+)"?$', cmd, re.I)
    if m:
        content, path = m.group(1), m.group(2)
        FS[path] = FS.get(path, "") + content.strip() + "\r\n"
        return ""
    m = re.match(r'^del\s+/q\s+"?([^"|]+)"?(?:\s+2>nul)?$', cmd, re.I)
    if m:
        FS.pop(m.group(1).strip(), None)
        return ""
    m = re.match(r'^certutil\s+-decode\s+"([^"]+)"\s+"([^"]+)"', cmd, re.I)
    if m:
        src, dst = m.group(1), m.group(2)
        try:
            blob = "".join(FS.get(src, "").split())
            FS[dst] = base64.b64decode(blob).decode("utf-8", errors="replace")
            return "CertUtil: -decode command completed successfully."
        except Exception as exc:
            return f"CertUtil Error: {exc}"
    if re.match(r"^(cls|cd\s|set$|prompt)", cmd, re.I):
        return ""
    return f"'{cmd.split()[0]}' is not recognized as an internal or external command."


def h_shell(p: dict):
    a = p.get("action")
    if a == "start":
        if SHELL_RUNNING["v"]:
            return {"status": "error", "message": "shell already running"}
        SHELL_RUNNING["v"] = True
        lhost, lport = p["lhost"], int(p["lport"])

        def connect_back():
            time.sleep(0.6)  # let the operator finish binding/listening
            try:
                s = socket.create_connection((lhost, lport), timeout=10)
                fake_cmd(s)
            except OSError:
                SHELL_RUNNING["v"] = False
        threading.Thread(target=connect_back, daemon=True).start()
        return {"status": "ok", "message": f"agent spawning {p.get('shell', 'cmd.exe')} → {lhost}:{lport}"}
    if a == "stop":
        SHELL_RUNNING["v"] = False
        return {"status": "ok", "message": "shell process terminated on target"}
    if a == "status":
        return {"status": "ok", "running": SHELL_RUNNING["v"]}
    return {"status": "error", "message": f"unknown shell action {a!r}"}


# ── server core ───────────────────────────────────────────────────────

SERVER_HANDLERS = {"activity": h_activity, "registry": h_registry,
                   "ssh": h_ssh, "rdp": h_rdp}


def client_loop(conn: socket.socket):
    f = conn.makefile("rwb")
    ev_lock = threading.Lock()

    def send(obj):
        f.write((json.dumps(obj) + "\n").encode())
        f.flush()

    def send_event(obj):  # push events must not interleave with responses
        with ev_lock:
            send(obj)

    # auth
    line = f.readline()
    if not line:
        conn.close()
        return
    try:
        hello = json.loads(line)
    except json.JSONDecodeError:
        conn.close()
        return
    if hello.get("token") != TOKEN:
        send({"status": "error", "message": "Authentication failed"})
        conn.close()
        return
    send({"status": "ok", "message": "Authenticated as operator"})

    while True:
        line = f.readline()
        if not line:
            break
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        module = req.get("module")
        payload = req.get("payload", {})
        if module in SERVER_HANDLERS:
            resp = SERVER_HANDLERS[module](payload)
        elif module == "notify":
            resp = h_notify(payload, send_event)
        elif module == "shell":
            resp = h_shell(payload)
        else:
            resp = {"status": "error", "message": f"no module {module!r} in mock"}
        send(resp)
    conn.close()


def main():
    global TOKEN, HOST
    ap = argparse.ArgumentParser(description="Mock C2 server for the operator-agent lab.")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--token", default=TOKEN)
    args = ap.parse_args()
    HOST, TOKEN = args.host, args.token

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((HOST, args.port))
    except OSError as exc:
        print(f"[!] cannot bind {HOST}:{args.port} — {exc}")
        print("    Something is already listening there. Find it with:"
              f"  lsof -nP -iTCP:{args.port}")
        print("    Either stop it, or move this mock: --port <other> "
              "(then pass the same --port to agent_tui.py / agent.py / agent_tools.py)")
        sys.exit(1)
    srv.listen(8)
    print(f"mock C2 server on {HOST}:{args.port} (token: {TOKEN}) — Ctrl+C to stop")
    while True:
        conn, addr = srv.accept()
        print(f"  operator connected: {addr[0]}:{addr[1]}")
        threading.Thread(target=client_loop, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
