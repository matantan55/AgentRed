"""
RDP Module — C2 Feature #2
Opens RDP sessions to a target and probes RDP port availability.
Uses subprocess to launch an OS-appropriate RDP client
(mstsc on Windows, xfreerdp on Linux, Microsoft Remote Desktop on macOS).
For educational/lab use only.
"""

import subprocess
import platform
import socket
import threading
import json
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class RDPSession:
    session_id: str
    host: str
    port: int
    username: str
    pid: Optional[int] = None
    status: str = "pending"  # pending | running | closed | error


class RDPModule:
    """Manages RDP connection attempts on the target agent."""

    def __init__(self):
        self.sessions: dict[str, RDPSession] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def handle(self, payload: dict) -> dict:
        """
        Expected payload keys:
            action      : "open" | "close" | "probe" | "list"
            session_id  : str
            host        : str  (for open/probe)
            port        : int  (default 3389)
            username    : str  (for open)
            password    : str  (for open — passed via /p: flag, lab only)
            geometry    : str  (e.g. "1280x720", optional)
            fullscreen  : bool (optional)
        """
        action = payload.get("action", "")
        handlers = {
            "open":  self._open,
            "close": self._close,
            "probe": self._probe,
            "list":  self._list,
        }
        fn = handlers.get(action)
        if fn is None:
            return {"status": "error", "message": f"Unknown RDP action: {action!r}"}
        return fn(payload)

    # ------------------------------------------------------------------
    # Internal handlers
    # ------------------------------------------------------------------

    def _probe(self, p: dict) -> dict:
        """Check whether TCP/3389 is open without launching a client."""
        host = p.get("host")
        port = int(p.get("port", 3389))
        if not host:
            return {"status": "error", "message": "host is required"}

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            try:
                s.connect((host, port))
                return {"status": "ok", "reachable": True,
                        "message": f"RDP port {port} is open on {host}"}
            except (socket.timeout, ConnectionRefusedError, OSError) as exc:
                return {"status": "ok", "reachable": False, "message": str(exc)}

    def _open(self, p: dict) -> dict:
        host       = p.get("host")
        port       = int(p.get("port", 3389))
        username   = p.get("username", "")
        password   = p.get("password", "")
        geometry   = p.get("geometry", "1280x720")
        fullscreen = p.get("fullscreen", False)
        session_id = p.get("session_id", f"rdp-{host}:{port}")

        if not host:
            return {"status": "error", "message": "host is required"}

        session = RDPSession(session_id=session_id, host=host,
                             port=port, username=username)

        try:
            cmd = self._build_cmd(host, port, username, password,
                                  geometry, fullscreen)
            proc = subprocess.Popen(cmd)
            session.pid    = proc.pid
            session.status = "running"
        except FileNotFoundError as exc:
            session.status = "error"
            with self._lock:
                self.sessions[session_id] = session
            return {"status": "error",
                    "message": f"RDP client not found: {exc}. "
                               "Install mstsc / xfreerdp / Microsoft Remote Desktop."}
        except OSError as exc:
            session.status = "error"
            with self._lock:
                self.sessions[session_id] = session
            return {"status": "error", "message": str(exc)}

        with self._lock:
            self.sessions[session_id] = session

        return {"status": "ok", "session_id": session_id,
                "pid": session.pid,
                "message": f"RDP client launched → {host}:{port}"}

    def _close(self, p: dict) -> dict:
        session_id = p.get("session_id")
        with self._lock:
            session = self.sessions.get(session_id)
        if session is None:
            return {"status": "error", "message": f"No session: {session_id!r}"}

        if session.pid:
            try:
                import os, signal
                os.kill(session.pid, signal.SIGTERM)
                session.status = "closed"
                return {"status": "ok", "message": f"Session {session_id!r} terminated"}
            except ProcessLookupError:
                session.status = "closed"
                return {"status": "ok", "message": "Process already exited"}
            except OSError as exc:
                return {"status": "error", "message": str(exc)}

        return {"status": "error", "message": "No PID recorded for this session"}

    def _list(self, _p: dict) -> dict:
        with self._lock:
            data = [asdict(s) for s in self.sessions.values()]
        return {"status": "ok", "sessions": data}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_cmd(host: str, port: int, username: str,
                   password: str, geometry: str, fullscreen: bool) -> list[str]:
        """Build the platform-appropriate RDP client command."""
        system = platform.system()
        width, height = (geometry.split("x") + ["720"])[:2]

        if system == "Windows":
            # mstsc — built-in Windows RDP client
            args = [
                "mstsc",
                f"/v:{host}:{port}",
            ]
            if fullscreen:
                args.append("/f")
            # mstsc doesn't accept password on the CLI (security by design);
            # credentials are entered in the GUI dialog.
            return args

        elif system == "Darwin":
            # macOS — open Microsoft Remote Desktop via open(1)
            # or fall back to a URL-scheme if the app is installed
            rdp_url = (
                f"rdp://full%20address=s:{host}:{port}"
                f"&username=s:{username}"
                f"&screen+mode+id=i:{2 if fullscreen else 1}"
                f"&desktopwidth=i:{width}&desktopheight=i:{height}"
            )
            return ["open", rdp_url]

        else:
            # Linux — xfreerdp
            args = [
                "xfreerdp",
                f"/v:{host}:{port}",
                f"/u:{username}",
                f"/size:{geometry}",
            ]
            if password:
                args.append(f"/p:{password}")
            if fullscreen:
                args.append("/f")
            return args
