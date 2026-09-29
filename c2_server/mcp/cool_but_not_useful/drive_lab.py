#!/usr/bin/env python3
"""
Lab traffic generator for TESTING lab_report_agent only.

Starts the real server.py on 127.0.0.1:4555, then speaks the *documented*
wire protocol with plain sockets to produce a representative c2_server.log:
operator local dispatch, an agent-role connection, a push event, a relay
attempt with no agent connected, and a bad-token attempt.

It exercises the user's own server locally on loopback — it is not a C2
operator tool and does not touch any real agent/target.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 4555
TOKEN = "labtest123"


def conn():
    s = socket.create_connection(("127.0.0.1", PORT), timeout=10)
    return s


def send(s, obj):
    s.sendall((json.dumps(obj) + "\n").encode())


def readline(s):
    buf = b""
    while b"\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            return None
        buf += chunk
    return buf.split(b"\n", 1)[0].decode()


def main():
    log = ROOT / "c2_server.log"
    if log.exists():
        log.unlink()  # fresh test artifact
    proc = subprocess.Popen(
        [str(ROOT / "mcp/.venv/bin/python"), "server.py", "--host", "127.0.0.1",
         "--port", str(PORT), "--token", TOKEN],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.2)
    try:
        # 1) bad token
        s = conn(); send(s, {"token": "wrong"})
        print("bad-auth reply:", readline(s)); s.close()

        # 2) operator: local dispatch to activity + registry (registry errors on mac)
        op = conn(); send(op, {"token": TOKEN}); print("op auth:", readline(op))
        send(op, {"module": "activity", "payload": {"action": "processes", "limit": 3}})
        r = json.loads(readline(op)); print("activity:", r.get("status"), "procs:", r.get("count"))
        send(op, {"module": "registry", "payload": {"action": "list_keys", "hive": "HKCU", "key_path": "SOFTWARE"}})
        print("registry:", json.loads(readline(op)).get("message"))
        send(op, {"module": "notify", "payload": {"action": "start"}})  # unknown locally → relay, no agent
        print("relay-no-agent:", json.loads(readline(op)).get("message"))

        # 3) agent-role connects, pushes an event, sends a stray response
        ag = conn(); send(ag, {"token": TOKEN, "role": "agent"}); print("agent auth:", readline(ag))
        send(ag, {"event": "notification", "title": "lab event", "body": "hello from the lab"})
        time.sleep(0.3)
        print("operator got broadcast:", readline(op))
        send(ag, {"status": "ok", "note": "stray response, no pending relay"})
        time.sleep(0.3)

        # 4) relay attempt now HAS an agent: agent must reply or server blocks 30s —
        #    reply promptly from the agent socket.
        send(op, {"module": "shell", "payload": {"action": "status"}})
        time.sleep(0.2)
        cmd = readline(ag)
        if cmd and '"shell"' in cmd:
            send(ag, {"status": "error", "message": "synthetic agent reply"})
        print("relay-with-agent:", readline(op))

        ag.close(); op.close()
    finally:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=5)
    print("--- server stopped, log generated ---")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
