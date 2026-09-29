#!/usr/bin/env python3
"""
C2 Project MCP — local Model Context Protocol server for the c2_server codebase.

WHAT THIS IS
    A code-intelligence MCP: it statically analyses the project (server.py,
    client.py, modules/*.py, dll.cpp), exposes the wire-protocol reference,
    lets you search/read the source, runs the server-side Activity module
    locally on this machine for dev testing, and ships grounded detection
    (hunting) notes mapped to the implant's capabilities.

WHAT THIS IS DELIBERATELY NOT
    It never opens a socket to the C2 server and never dispatches commands to
    the Windows agent. No live operator channel is exposed through MCP.

Runs over stdio with the official MCP Python SDK (mcp>=2, MCPServer class).
"""

from __future__ import annotations

import ast
import fnmatch
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVER_PY = PROJECT_ROOT / "server.py"
CLIENT_PY = PROJECT_ROOT / "client.py"
DLL_CPP = PROJECT_ROOT / "dll.cpp"
MODULES_DIR = PROJECT_ROOT / "modules"

SKIP_DIRS = {".venv", "__pycache__", ".git", "node_modules"}
SKIP_FILES = {".DS_Store", "c2_server.log"}

mcp = MCPServer(name="c2-project", version="1.0.0")


# ──────────────────────────────────────────────────────────────────────
# Static analysis: server-side (Python) modules
# ──────────────────────────────────────────────────────────────────────

_GET_PARAM = re.compile(r'(?:payload|p)\s*(?:\.get\(\s*|[\["\']+\s*)(["\'])(\w+)\1')


def _py_params(source: str) -> list[str]:
    """Param names read from a payload dict in a handler body."""
    names = {m.group(2) for m in _GET_PARAM.finditer(source)}
    names |= set(re.findall(r'(?:payload|p)\[\s*["\'](\w+)["\']\s*\]', source))
    return sorted(n for n in names if n != "action")


def parse_server_modules() -> list[dict]:
    mods = []
    for path in sorted(MODULES_DIR.glob("*_module.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        text = path.read_text(encoding="utf-8")
        for cls in [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name.endswith("Module")]:
            entry = {
                "scope": "server",
                "source": str(path.relative_to(PROJECT_ROOT)),
                "class": cls.name,
                "description": (ast.get_docstring(cls) or "").strip().splitlines()[0]
                                if ast.get_docstring(cls) else "",
                "actions": {},
                "params_all": set(),
            }
            handle_fn = next((f for f in cls.body if isinstance(f, ast.FunctionDef) and f.name == "handle"), None)
            if handle_fn:
                # handlers = {"action": self._fn, ...}  (first dict literal in handle)
                dispatch: dict[str, str] = {}
                for node in ast.walk(handle_fn):
                    if isinstance(node, ast.Dict) and node.keys and all(isinstance(k, ast.Constant) and isinstance(k.value, str) for k in node.keys):
                        for k, v in zip(node.keys, node.values):
                            if isinstance(v, ast.Attribute):
                                dispatch[k.value] = v.attr
                        if dispatch:
                            break
                for fn in [f for f in cls.body if isinstance(f, ast.FunctionDef)]:
                    try:
                        seg = ast.get_source_segment(text, fn) or ""
                    except Exception:
                        seg = ""
                    params = _py_params(seg)
                    entry["params_all"].update(params)
                    for action, target in dispatch.items():
                        if fn.name == target:
                            entry["actions"][action] = {
                                "handler": f"{cls.name}.{fn.name}",
                                "line": fn.lineno,  # file-relative
                                "params": params,
                            }
            entry["params_all"] = sorted(entry["params_all"])
            if "winreg" in text and not entry.get("availability"):
                entry["availability"] = "Windows only (winreg); errors elsewhere"
            if "PSUTIL_AVAILABLE" in text:
                entry["availability"] = "Cross-platform, requires psutil"
            mods.append(entry)
    return mods


# ──────────────────────────────────────────────────────────────────────
# Static analysis: agent-side (dll.cpp) modules
# ──────────────────────────────────────────────────────────────────────

def parse_agent_modules() -> list[dict]:
    if not DLL_CPP.exists():
        return []
    lines = DLL_CPP.read_text(encoding="utf-8", errors="replace").splitlines()

    # namespace <x>_mod {  markers, in file order
    markers = []
    for i, l in enumerate(lines):
        m = re.match(r"^\s*namespace\s+(\w+_mod)\b", l)
        if m:
            markers.append((m.group(1), i))

    # dispatch chain:  module == "ssh"  result = ssh_mod::handle(payload)
    dispatch: dict[str, str] = {}
    for l in lines:
        m = re.search(r'module\s*==\s*"(\w+)"[^=]*?result\s*=\s*(\w+)::handle', l)
        if m:
            dispatch[m.group(1)] = m.group(2)

    mods = []
    for idx, (ns, start) in enumerate(markers):
        if idx + 1 < len(markers):
            end = markers[idx + 1][1]
        else:
            end = next((j for j, l in enumerate(lines) if "module = msg" in l), len(lines))
        region = "\n".join(lines[start:end])
        actions = []
        seen = set()
        for off, l in enumerate(lines[start:end]):
            m = re.search(r'action\s*==\s*"([\w]+)"', l)
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                actions.append({"action": m.group(1), "line": start + off + 1})
        params = sorted(set(re.findall(r'\bp\[\s*"(\w+)"\s*\]', region)))
        mod_name = next((mn for mn, target in dispatch.items() if target == ns),
                        ns[:-4].replace("_", ""))  # unwired namespaces: guess from name
        mods.append({
            "scope": "agent",
            "source": "dll.cpp",
            "namespace": ns,
            "module": mod_name,
            "wired_in_dispatch": ns in dispatch.values(),
            "actions": actions,
            "params_all": params,
        })
    return mods


# ──────────────────────────────────────────────────────────────────────
# MCP tools
# ──────────────────────────────────────────────────────────────────────

@mcp.tool()
def project_overview() -> str:
    """Architecture summary of the c2_server project: components, files,
    protocol role, ports/auth, and the operator CLI command surface
    (extracted live from the sources)."""
    def stats(p: Path) -> dict:
        try:
            n = len(p.read_text(encoding="utf-8", errors="replace").splitlines())
        except Exception:
            n = -1
        return {"path": str(p.relative_to(PROJECT_ROOT)), "lines": n,
                "bytes": p.stat().st_size if p.exists() else 0}

    client_cmds: list[str] = []
    if CLIENT_PY.exists():
        text = CLIENT_PY.read_text(encoding="utf-8", errors="replace")
        client_cmds = sorted(set(re.findall(r'\bcmd\s*==\s*["\']([\w:-]+)["\']', text)))

    return json.dumps({
        "name": "c2_server",
        "self_description": "Educational/lab C2 framework: TCP server with "
                            "newline-delimited JSON protocol, token auth, "
                            "operator-role and agent-role clients.",
        "components": [
            {"file": "server.py", "role": "C2 server: auth, module dispatch, "
                                          "agent relay, event broadcast to operators"},
            {"file": "client.py", "role": "Interactive operator CLI"},
            {"file": "dll.cpp", "role": "Windows agent (DLL): connects back, "
                                        "dispatches module commands locally"},
            {"file": "modules/*.py", "role": "Server-side Python modules "
                                              "(ssh, rdp, registry, activity)"},
        ],
        "transport": {
            "framing": "newline-delimited JSON over TCP",
            "default_port": 4444,
            "tls": "optional via --certfile/--keyfile (server), --tls (client)",
            "auth": "first message {\"token\": ...}; env C2_AUTH_TOKEN (default 'changeme')",
            "roles": ["operator", "agent (role field in auth message)"],
        },
        "files": [stats(p) for p in [SERVER_PY, CLIENT_PY, DLL_CPP, PROJECT_ROOT / "requirements.txt"]],
        "operator_cli_commands": client_cmds,
        "notes": [
            "server.py defines ClientHandler._handle_line twice; the second "
            "definition (with agent relay/response handling) shadows the first.",
            "In dll.cpp the 'defender' namespace is implemented but NOT wired "
            "into the agent's module dispatch chain, so it is unreachable via "
            "the wire protocol as the code stands.",
        ],
    }, indent=2)


@mcp.tool()
def list_modules() -> str:
    """Full module catalog for the project, parsed live from the sources:
    server-side Python modules (modules/*.py) and agent-side C++ modules
    (dll.cpp namespaces), each with its actions, parameter names, and
    source locations."""
    server = parse_server_modules()
    agent = parse_agent_modules()
    for m in server:
        m["actions"] = {a: {"handler": d["handler"], "line": d["line"], "params": d["params"]}
                        for a, d in m["actions"].items()}
    return json.dumps({"server_modules": server, "agent_modules": agent}, indent=2)


@mcp.tool()
def module_detail(name: str) -> str:
    """One module in depth: actions with file:line for every handler, all
    parameter names, availability notes, and whether the agent-side module
    is wired into dll.cpp's dispatch chain. `name` is e.g. 'ssh', 'registry',
    'activity', 'notify', 'shell', 'defender'."""
    name = name.lower().strip()
    hits = [m for m in parse_server_modules() + parse_agent_modules()
            if name in (m.get("class", "") + m.get("module", "") + m.get("namespace", "")).lower()
            or m.get("class", "").lower().startswith(name) or m.get("module", "").lower() == name]
    if not hits:
        return json.dumps({"error": f"No module matching {name!r}. "
                                    "Call list_modules() for the catalog."})
    return json.dumps(hits, indent=2, default=str)


@mcp.tool()
def protocol_reference() -> str:
    """The wire protocol reference for the c2_server TCP interface: message
    shapes for operator auth, agent auth, commands, responses, push events,
    relay semantics, and timeouts. Documentation only — this MCP does not
    connect to the socket itself."""
    return json.dumps({
        "framing": "One JSON object per line (\\n terminated) over TCP (optionally TLS).",
        "auth": {
            "operator": {"request": {"token": "<C2_AUTH_TOKEN>"},
                          "reply": {"status": "ok", "message": "Authenticated as operator"}},
            "agent": {"request": {"token": "<C2_AUTH_TOKEN>", "role": "agent"},
                       "reply": {"status": "ok", "message": "Authenticated as agent"}},
            "failure": {"status": "error", "message": "Authentication failed"},
        },
        "operator_command": {
            "request": {"module": "<name>", "payload": {"action": "<name>", "<more-params>": "..."}},
            "routing": "Known module name -> executed server-side by the matching "
                        "Python module in server.py MODULES. Unknown module name -> "
                        "relayed to the FIRST connected agent (dll.cpp), 30 s timeout.",
            "response": "<module-specific JSON>",
        },
        "agent_push_event": {
            "request": {"event": "<type>", "<more-fields>": "..."},
            "behavior": "server broadcasts verbatim to every connected operator",
        },
        "agent_response_correlation": "Agent replies to relayed commands are routed to "
                                       "the pending relay queue (one command in flight per agent).",
        "timeouts": {"agent_relay": 30.0, "note": "client.py sets 60 s socket timeout"},
        "defaults": {"host": "0.0.0.0", "port": 4444, "token_env": "C2_AUTH_TOKEN", "token_default": "changeme"},
    }, indent=2)


@mcp.tool()
def search_code(pattern: str, file_glob: str = "*", max_hits: int = 60) -> str:
    """Regex search across project source files (server.py, client.py,
    dll.cpp, modules/*.py, ...). Returns 'path:line: text'. Excludes the
    mcp venv, __pycache__, logs, and .DS_Store."""
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return json.dumps({"error": f"Bad regex: {exc}"})
    hits = []
    for root, dirs, files in os.walk(PROJECT_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f in SKIP_FILES or f.endswith(".log"):
                continue
            rel = str(Path(root, f).relative_to(PROJECT_ROOT))
            if not fnmatch.fnmatch(rel, file_glob):
                continue
            try:
                text = Path(root, f).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{rel}:{i}: {line.strip()[:180]}")
                    if len(hits) >= max_hits:
                        return json.dumps({"hits": hits, "truncated": True,
                                           "pattern": pattern}, indent=2)
    return json.dumps({"hits": hits, "truncated": False, "pattern": pattern}, indent=2)


@mcp.tool()
def read_source(rel_path: str, offset: int = 1, limit: int = 400) -> str:
    """Read a project source file with line numbers. Paths must resolve
    inside the c2_server project directory (no absolute escapes)."""
    try:
        target = (PROJECT_ROOT / rel_path).resolve()
    except OSError:
        return json.dumps({"error": "bad path"})
    if not target.is_relative_to(PROJECT_ROOT):
        return json.dumps({"error": "path escapes project root"})
    if not target.is_file():
        return json.dumps({"error": f"not a file: {rel_path}"})
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    offset = max(1, offset)
    chunk = lines[offset - 1: offset - 1 + max(1, min(limit, 2000))]
    body = "\n".join(f"{offset + i:5d}| {l}" for i, l in enumerate(chunk))
    return json.dumps({"path": rel_path, "total_lines": len(lines),
                       "returned": len(chunk), "text": body}, indent=2)


@mcp.tool()
def local_activity(action: str, params_json: str = "{}") -> str:
    """Run the SERVER-side Activity module in-process on THIS machine
    (macOS/Linux/Windows dev box) — same code path server.py would use for
    local dispatch. Actions: processes | system_stats | network_connections
    | top_cpu | top_mem | start_monitor | stop_monitor | get_snapshots.
    params_json carries optional keys (pid, name, limit, interval).
    The module instance is cached, so the monitor thread and its snapshot
    buffer persist for the life of this MCP process.
    This touches only the local machine via psutil; it never opens the C2
    socket and never reaches an agent."""
    allowed = {"processes", "system_stats", "network_connections", "top_cpu",
               "top_mem", "start_monitor", "stop_monitor", "get_snapshots"}
    if action not in allowed:
        return json.dumps({"error": f"action must be one of {sorted(allowed)}"})
    try:
        params = json.loads(params_json or "{}")
    except json.JSONDecodeError as exc:
        return json.dumps({"error": f"params_json invalid: {exc}"})
    params["action"] = action
    global _ACTIVITY
    if _ACTIVITY is None:
        path = MODULES_DIR / "activity_module.py"
        spec = importlib.util.spec_from_file_location("activity_module_local", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _ACTIVITY = mod.ActivityModule()
    try:
        result = _ACTIVITY.handle(params)
    except Exception as exc:
        result = {"status": "error", "message": str(exc)}
    return json.dumps(result, indent=2, default=str)


_ACTIVITY = None  # cached ActivityModule instance — keeps start_monitor stateful


@mcp.tool()
def hunting_notes(topic: str = "all") -> str:
    """Detection-engineering notes mapped to the capabilities present in
    THIS project's agent (dll.cpp) and server-side modules: ATT&CK technique
    IDs (published level), the telemetry an analyst would see, and log
    sources. Grounded in the technique pages and community rule sets cited
    per topic. Static analysis aid — not an operational C2 channel."""
    topics = {
        "defender_tampering": {
            "maps_to": "defender module in dll.cpp (Set-MpPreference / "
                        "Add-MpPreference / service disable; namespace exists but "
                        "note it is NOT wired into the agent dispatch chain)",
            "attck": ["T1562.001 Impair Defenses: Disable or Modify Tools",
                       "T1112 Modify Registry (direct registry writes bypass API logging)"],
            "telemetry": [
                "Sysmon EID 1 / Security 4688: powershell.exe + 'Set-MpPreference|Add-MpPreference' "
                "with Disable*/Exclusion* args",
                "Windows Defender Operational log EID 5007 (any config change incl. exclusions); "
                "5001/5010/5012 (real-time / scan / update protection state)",
                "Sysmon EID 12/13: value set under HKLM\\SOFTWARE\\Microsoft\\Windows Defender\\Exclusions\\*",
                "System EID 7036/7040: WinDefend / WdNisSvc stop or start-type change",
                "PowerScriptBlockLogging EID 4104; 4657 auditing for direct registry writes",
            ],
            "sources": [
                "https://detection.wiki/attack/T1562.001/",
                "https://df00tech.com/detections/T1562.001",
                "https://inksec.io/blue-team/labs/defender/",
            ],
        },
        "remote_shell": {
            "maps_to": "shell module in dll.cpp (reverse shell, cmd/powershell "
                        "pipes to lhost:lport, default 4445)",
            "attck": ["T1059.001 PowerShell", "T1059.003 Windows Command Shell"],
            "telemetry": [
                "Sysmon EID 1: cmd.exe/powershell.exe with a DLL-host parent "
                "(rundll32, regsvr32, explorer) — parent/child anomaly",
                "Sysmon EID 3: outbound TCP from that process to lport (4445 by default)",
                "Interactive stdin/stdout pipes: low console noise, process "
                "running without a console window",
            ],
            "sources": [],
        },
        "registry_persistence": {
            "maps_to": "registry module (server-side winreg; also the DLL "
                        "winreg-API implementation)",
            "attck": ["T1112 Modify Registry"],
            "telemetry": [
                "Sysmon EID 12/13/14 on Run/RunOnce/Services/Silent Process "
                "Exit Monitor keys by non-installer processes",
                "EID 4657 (Security) for writes without EID 13 coverage",
            ],
            "sources": [],
        },
        "ssh_rdp_access": {
            "maps_to": "ssh module (paramiko / libssh2 exec) and rdp module "
                        "(mstsc/xfreerdp launch, TCP 3389 probe)",
            "attck": ["T1021.001 Remote Services: RDP", "T1021.004 Remote Services: SSH"],
            "telemetry": [
                "Windows Security 4624 type 10 (interactive RDP logon) + "
                "4778/4779 session reconnect; TerminalServices-RemoteConnectionManager logs",
                "Linux auth.log / journal sshd entries with unusual source or "
                "password auth from automation",
                "Sysmon EID 1: mstsc.exe / xfreerdp process creation from "
                "server-side automation; RDP port scans (connect to 3389 from "
                "non-workstation hosts)",
            ],
            "sources": [],
        },
        "activity_discovery": {
            "maps_to": "activity module (process list, system stats, network "
                        "connections, monitor loop)",
            "attck": ["T1057 Process Discovery", "T1082 System Information Discovery",
                       "T1049 System Network Connections Discovery"],
            "telemetry": [
                "Low fidelity alone: repeated CreateToolhelp32Snapshot / "
                "EnumProcesses / GetExtendedTcpTable calls (ETW provider "
                "Microsoft-Windows-Kernel-Process / Sysmon EID 10 access to "
                "lsass-adjacent processes not relevant here, but API call "
                "clustering from an unsigned DLL host is notable)",
                "Correlate with a suspicious parent process and the beacon "
                "connection — discovery alone is noisy, the context is the signal",
            ],
            "sources": [],
        },
        "notification_monitor": {
            "maps_to": "notify module in dll.cpp (SetWinEventHook message pump "
                        "that harvests on-screen notifications, plus outbound "
                        "MessageBox/balloon)",
            "attck": ["T1010 Application Window Discovery (conceptual)",
                       "T1056.002 UI/behavior harvesting (conceptual; MITRE "
                       "does not enumerate toast-harvesting as its own sub-technique)"],
            "telemetry": [
                "SetWinEventHook/EVENT_HOOK_INJECT + WinEventProc from an "
                "unsigned DLL injected/hosted process",
                "Sysmon EID 7 image load of an unsigned config.dll-style module",
                "Outbound MessageBox with a process that has no UI lineage",
            ],
            "sources": [],
        },
        "network_beacon": {
            "maps_to": "agent -> server transport (server.py / dll.cpp): "
                        "single long-lived TCP connection, newline-delimited "
                        "JSON, first line {\"token\": ...}, default port 4444",
            "attck": ["T1071.004-like custom-protocol over raw TCP (conceptual; "
                       "raw TCP + JSON is not T1071 proper — treat as custom)"],
            "telemetry": [
                "Zeek conn.log: persistent single-connection session, low "
                "throughput, port 4444/4445, no TLS or self-signed TLS",
                "Netflow: long-lived flow with tiny bidirectional byte counts",
                "Content: ASCII '{\"token\"' on first line then "
                "'{\"module\"'/'{\"event\"' framed lines — greppable in a "
                "MITM capture even without keys if TLS is absent",
            ],
            "sources": [],
        },
    }
    topic = topic.lower().strip()
    if topic in ("all", "*", ""):
        return json.dumps(topics, indent=2)
    key = next((k for k in topics if topic in k), None)
    if key is None:
        return json.dumps({"error": f"unknown topic {topic!r}", "topics": sorted(topics)})
    return json.dumps({key: topics[key]}, indent=2)


# ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()  # stdio
