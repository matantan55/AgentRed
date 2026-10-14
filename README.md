# AgentRed: Advanced Adversary Simulation & C2 Research Framework

> **Security Research & Authorized Testing Disclaimer**  
> All offensive techniques, privilege escalation mechanisms, and Command & Control (C2) components are designed strictly for authorized security research, adversary emulation, and testing in controlled lab environments on systems owned by the operator or with explicit written authorization.

---

## 1. Executive Overview

**AgentRed** is an end-to-end adversary simulation framework and post-exploitation platform engineered for Windows environments. It bridges low-level Windows internals exploitation, resilient persistence, asynchronous multi-channel Command & Control (C2), and modern autonomous AI agent orchestration (via Anthropic/OpenRouter LLMs and the Model Context Protocol).

The project studies how modern endpoint detection mechanisms (EDR, AMSI, Windows Defender, User Account Control, and Authenticode signature verification) can be navigated, and provides blue teams with corresponding forensic signatures, detection rules, and remediation mechanisms.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            OPERATOR TIER                                    │
│  ┌───────────────────────┐  ┌──────────────────────┐  ┌──────────────────┐  │
│  │   agent_tui.py        │  │      client.py       │  │  FastMCP Server  │  │
│  │ (Autonomous LLM TUI)  │  │   (Interactive CLI)  │  │ (Claude/Cursor)  │  │
│  └───────────┬───────────┘  └───────────┬──────────┘  └────────┬─────────┘  │
└──────────────┼──────────────────────────┼──────────────────────┼────────────┘
               │                          │                      │
               └──────────────────────────┼──────────────────────┘
                                          │ JSON / TLS :4444
                                          ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                             C2 RELAY TIER                                   │
│                     server.py (Multi-threaded C2 Relay)                     │
│    - Auth & Handshake         - Module Dispatcher     - Push Event Stream   │
└─────────────────────────────────────────┬───────────────────────────────────┘
                                          │ JSON / TLS :4444
                                          ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                             VICTIM TIER                                     │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │ Execution & PrivEsc: red.cpp / privesc.h / process_steal.h             │  │
│  │  - PEB Masquerading (CmdLine, ImagePath, WindowTitle)                 │  │
│  │  - Auto-elevate UAC Registry Hijack (slui.exe)                        │  │
│  │  - Token Duplication (winlogon.exe -> SYSTEM)                         │  │
│  └──────────────────────────────────┬────────────────────────────────────┘  │
│                                     ▼                                       │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │ In-Memory Implant: AppInit_DLLs.dll (dll.cpp)                         │  │
│  │  - Activity & Process Monitor    - Windows Registry Engine            │  │
│  │  - Remote Desktop & SSH Client   - WinEvent Activity Hooking          │  │
│  └──────────────────┬──────────────────────────────────────┬─────────────┘  │
│                     │                                      │                │
│    Failsafe Watchdog│                                      │ Direct TCP     │
│                     ▼                                      │ Shell :4445    │
│  ┌─────────────────────────────────────┐                   ▼                │
│  │ SAFE.exe (Watchdog / Self-Healing)  │        ┌─────────────────────┐     │
│  │  - HKCU Run Persistence             │        │ Raw Interactive     │     │
│  │  - AppInit Registry Self-Heal       │        │ Reverse Shell Stream│     │
│  │  - Strict ACL File Locking          │        └─────────────────────┘     │
│  └─────────────────────────────────────┘                                    │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Key Architecture Components

| Component | Path | Language | Primary Role |
| :--- | :--- | :--- | :--- |
| **Dropper & PrivEsc** | [`red.cpp`](file:///Users/matanmishali/AntiGravity/AgentRed/red.cpp) | C++17 | Initial execution, PEB masquerading, UAC bypass, and token elevation. |
| **Process Masquerader** | [`process_steal.h`](file:///Users/matanmishali/AntiGravity/AgentRed/process_steal.h) | C++17 | Resolves signed processes via `WinVerifyTrust` and overwrites PEB strings. |
| **Token & Elevation Engine**| [`privesc.h`](file:///Users/matanmishali/AntiGravity/AgentRed/privesc.h) | C++17 | `SeDebugPrivilege` enablement, `winlogon.exe` token duplication to `SYSTEM`. |
| **Core DLL Implant** | [`dll.cpp`](file:///Users/matanmishali/AntiGravity/AgentRed/dll.cpp) | C++17 | Multi-threaded in-memory C2 agent loaded by `user32.dll`. |
| **Self-Healing Watchdog** | [`SAFE.cpp`](file:///Users/matanmishali/AntiGravity/AgentRed/SAFE.cpp) | C++17 | Failsafe persistence monitor with DACL anti-deletion protection. |
| **Authenticode Spoofer** | [`cert_spoofer.py`](file:///Users/matanmishali/AntiGravity/AgentRed/cert_spoofer.py) | Python 3 | PE Authenticode signature extractor, transplant engine, & VT benchmark. |
| **PE Structure Analyzer** | [`file_analyzer.py`](file:///Users/matanmishali/AntiGravity/AgentRed/file_analyzer.py) | Python 3 | Low-level binary PE32/PE32+ header and section parser. |
| **C2 Relay Server** | [`c2_server/server.py`](file:///Users/matanmishali/AntiGravity/AgentRed/c2_server/server.py) | Python 3 | Multi-threaded TCP/TLS event-driven command and push broadcaster. |
| **Operator Console** | [`c2core/client.py`](file:///Users/matanmishali/AntiGravity/AgentRed/c2core/client.py) | Python 3 | Interactive command console + direct raw TCP reverse shell listener. Shared by the agent stack (`from c2core import client`). |
| **Autonomous AI TUI** | [`agent/agent_tui.py`](file:///Users/matanmishali/AntiGravity/AgentRed/agent/agent_tui.py) | Python 3 | Claude-Code-style Textual TUI with human-in-the-loop approval gates. |
| **Agent Tool Framework**| [`agent/agent_tools.py`](file:///Users/matanmishali/AntiGravity/AgentRed/agent/agent_tools.py) | Python 3 | 38 programmatic operational tools wrapping the C2 client for LLMs. |
| **Code Intelligence MCP**| [`c2_server/mcp/c2_project_mcp.py`](file:///Users/matanmishali/AntiGravity/AgentRed/c2_server/mcp/c2_project_mcp.py) | Python 3 | FastMCP server exposing AST code intelligence & detection mapping to IDEs. |

---

## 3. Offensive Capabilities & Techniques

### A. Evasion & Masquerading
- **PEB In-Memory Masquerading ([`process_steal.h`](file:///Users/matanmishali/AntiGravity/AgentRed/process_steal.h))**:
  Dynamically locates running processes verified by `WinVerifyTrust` (`WINTRUST_ACTION_GENERIC_VERIFY_V2`). Queries target PEB via `NtQueryInformationProcess` (`ProcessBasicInformation`) and overwrites the local process's `RTL_USER_PROCESS_PARAMETERS` (`CommandLine`, `ImagePathName`, `WindowTitle`) to deceive security monitoring tools (`Process Explorer`, `Task Manager`, command-line logging).
- **Authenticode Signature Transplanting ([`cert_spoofer.py`](file:///Users/matanmishali/AntiGravity/AgentRed/cert_spoofer.py))**:
  Extracts the `IMAGE_DIRECTORY_ENTRY_SECURITY` certificate table from legitimate signed Windows binaries and appends it to target binaries, adjusting PE Optional Header offsets to bypass naive signature checks.

### B. Privilege Escalation & UAC Bypasses
- **Auto-Elevate Registry Hijacks ([`red.cpp`](file:///Users/matanmishali/AntiGravity/AgentRed/red.cpp))**:
  Hijacks user-controlled file associations in `HKCU\Software\Classes\exefile\shell\open\command` and triggers auto-elevated binaries such as `slui.exe`.
- **Token Stealing to SYSTEM ([`privesc.h`](file:///Users/matanmishali/AntiGravity/AgentRed/privesc.h))**:
  Acquires `SeDebugPrivilege`, opens `winlogon.exe` (running as `NT AUTHORITY\SYSTEM`), duplicates its primary token via `DuplicateTokenEx`, and launches elevated processes using `CreateProcessWithTokenW`.
- **Advanced Kernel & Service PoCs ([`files/`](file:///Users/matanmishali/AntiGravity/AgentRed/files))**:
  Includes standalone exploit research PoCs: `CVE-2026-20817` (Windows Error Reporting ALPC EoP to SYSTEM) and `CVE-2026-50656` (Defender TOCTOU race check).
- **Windows Defender Tampering**:
  Executes `Add-MpPreference -ExclusionPath` via elevated PowerShell to suppress on-access detection for deployed binaries.

### C. Persistent In-Memory Execution & Self-Healing
- **AppInit_DLLs Architecture ([`dll.cpp`](file:///Users/matanmishali/AntiGravity/AgentRed/dll.cpp))**:
  Persists through `HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Windows\AppInit_DLLs`. Automatically injected by `user32.dll` into every desktop GUI process upon initialization.
- **Failsafe Watchdog ([`SAFE.cpp`](file:///Users/matanmishali/AntiGravity/AgentRed/SAFE.cpp))**:
  Maintains secondary persistence via `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`. Continuously monitors AppInit registry keys and restores them if purged. Built-in `/remove` switch provides authorized cleanup.
- **DACL Anti-Deletion Locking**:
  Applies custom Discretionary Access Control Lists (`SetNamedSecurityInfoA`) to deny file deletion rights even to administrative accounts.

### D. Multi-Channel C2 & LLM Automation
- **Asynchronous Wire Protocol**: Newline-delimited JSON messages over TCP/TLS with role separation (`operator` vs `agent`).
- **Victim Event Streaming**: Windows Event Hooks (`SetWinEventHook`) stream active foreground window changes, lock screen events, and user notifications to operators in real-time.
- **Out-of-Band Interactive Shell**: A direct, raw TCP interactive `cmd.exe` socket on port `4445` completely independent of the JSON relay for low-latency shell access.
- **AI-Driven Operations**: Operators can control the target through natural language using [`agent/agent_tui.py`](file:///Users/matanmishali/AntiGravity/AgentRed/agent/agent_tui.py) or Claude/Cursor via MCP ([`c2_server/mcp/`](file:///Users/matanmishali/AntiGravity/AgentRed/c2_server/mcp)).

---

## 4. Documentation Suite (`docs/`)

All technical architecture, API, protocol, and operational documentation is organized inside the [`docs/`](file:///Users/matanmishali/AntiGravity/AgentRed/docs) directory:

- 📖 **[Operator & Agent User Guide (docs/USER_GUIDE.md)](file:///Users/matanmishali/AntiGravity/AgentRed/docs/USER_GUIDE.md)**  
  Step-by-step practical guide: using the interactive CLI client (`client.py`), operating the autonomous AI agent (`agent_tui.py`), 5-phase attack runbook, and OPSEC noise matrix.
- 📘 **[Full Technical Architecture Documentation (docs/DOCUMENTATION.md)](file:///Users/matanmishali/AntiGravity/AgentRed/docs/DOCUMENTATION.md)**  
  Comprehensive technical report detailing Windows internals, evasion mechanics, privilege escalation (incl. CVE PoCs), Secure Boot constraints, MITRE ATT&CK mapping, Volatility 3 forensics, and IOC matrix.
- 📡 **[Wire Protocol & API Reference (docs/PROTOCOL.md)](file:///Users/matanmishali/AntiGravity/AgentRed/docs/PROTOCOL.md)**  
  Full JSON message schemas, hybrid dispatch engine, push event streams, and out-of-band reverse shell socket details.
- 📜 **[Machine-Readable Wire Schema (docs/protocol_schema.json)](file:///Users/matanmishali/AntiGravity/AgentRed/docs/protocol_schema.json)**  
  Formal JSON Schema (Draft-07) validating all client, server, implant, and push messages.
- 🛡️ **[Production Sigma Detection Rules (docs/sigma/)](file:///Users/matanmishali/AntiGravity/AgentRed/docs/sigma)**  
  SIEM detection rules for UAC `slui.exe` bypass, `AppInit_DLLs` persistence, and `winlogon.exe` handle duplication.
- 📽️ **[Technical Architecture & Presentation Deck (docs/PRESENTATION.md)](file:///Users/matanmishali/AntiGravity/AgentRed/docs/PRESENTATION.md)**  
  Marp-compliant 12-slide briefing deck with speaker notes, timing cues, system diagrams, and technical demonstration walkthrough.
- 📂 **[Documentation Hub Index (docs/README.md)](file:///Users/matanmishali/AntiGravity/AgentRed/docs/README.md)**  
  Index and navigation guide for all documentation deliverables.
- 🌐 **[Interactive MkDocs Web Portal (mkdocs.yml)](file:///Users/matanmishali/AntiGravity/AgentRed/mkdocs.yml)**  
  Launch a local web documentation portal with `mkdocs serve`.

---

## 5. Quickstart & Build Instructions

### 1. Build Binaries
```bash
# 1. Compile the In-Memory Agent DLL (dll.cpp -> AppInit_DLLs.dll)
x86_64-w64-mingw32-g++ -shared -o AppInit_DLLs.dll dll.cpp \
    -lws2_32 -lshlwapi -lshell32 -ladvapi32 \
    -lpdh -lpsapi -liphlpapi -lwtsapi32 -loleaut32 \
    -std=c++17 -O2 -fpermissive

# 2. Compile the Watchdog (SAFE.cpp -> SAFE.exe)
x86_64-w64-mingw32-g++ -o SAFE.exe SAFE.cpp \
    -ladvapi32 -lshell32 -lshlwapi \
    -static -std=c++17 -O2 -mwindows

# 3. Compile the Dropper / PrivEsc Loader (red.cpp -> main.exe)
x86_64-w64-mingw32-g++ -static red.cpp -o main.exe \
    -ladvapi32 -lshell32 -lshlwapi -lwintrust -luser32 -mwindows
```

### 2. Start the C2 Infrastructure
```bash
# Set up Python environment
cd c2_server
pip install -r requirements.txt

# Start C2 server (Default: 127.0.0.1:4444, token: changeme)
python server.py --host 0.0.0.0 --port 4444 --token secret123
```

### 3. Connect via Operator Interfaces

**Option A: Interactive CLI Console**
```bash
python c2core/client.py --host 127.0.0.1 --port 4444 --token secret123
```

**Option B: Autonomous AI Operator (Textual TUI)**
```bash
cd ../agent
pip install -r ../c2_server/requirements.txt textual
export OPENROUTER_API_KEY="your-api-key"
python agent_tui.py --host 127.0.0.1 --port 4444 --token secret123 --provider openrouter --model anthropic/claude-3.5-sonnet
```
