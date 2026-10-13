---
marp: true
theme: gaia
_class: lead
paginate: true
backgroundColor: #0d1117
color: #c9d1d9
style: |
  section {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
    font-size: 26px;
    padding: 40px;
  }
  h1, h2 {
    color: #f85149;
  }
  h3 {
    color: #58a6ff;
  }
  code {
    background-color: #161b22;
    color: #7ee787;
    font-size: 20px;
  }
  footer {
    font-size: 14px;
    color: #8b949e;
  }
---

# AgentRed: Architecture & Technical Briefing
### Investigating User-Mode Windows Evasion, Resilient Persistence & Autonomous C2 Operations

**Security Research & Adversary Simulation**
*Presenter: Security Research Team | Target OS: Windows 10/11 x64*

<!-- Note: [Timing: 00:00 - 01:30]
Welcome everyone. Today we are presenting AgentRed, an end-to-end adversary simulation and post-exploitation framework designed to test core trust assumptions in modern endpoint detection, user-mode Windows internals, and autonomous LLM orchestration. -->

---

## Slide 2: The Attack Surface & Problem Statement

### Core Research Hypothesis
*How do modern EDR platforms evaluate trust, process identity, and persistence mechanisms—and how can operators leverage autonomous AI safely in post-exploitation?*

### Challenging Defensive Assumptions
1. **Trust Heuristics**: Are signed binaries inherently trusted without verifying cryptographic hash chains?
2. **Process Identity**: Does the process name in Task Manager or Process Explorer represent true memory execution?
3. **Administrative Authority**: Can standard administrative accounts easily delete persistence artifacts protected by custom DACLs?
4. **Manual Post-Exploitation**: Can an autonomous LLM reliably operate multi-module C2 tools under strict Human-in-the-Loop gates?

<!-- Note: [Timing: 01:30 - 03:00]
Security products often rely on fast heuristics to reduce scan overhead. AgentRed systematically investigates these boundary assumptions. -->

---

## Slide 3: End-to-End System Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            OPERATOR TIER                                    │
│  ┌───────────────────────┐  ┌──────────────────────┐  ┌──────────────────┐  │
│  │   agent_tui.py        │  │      client.py       │  │ c2_project_mcp.py│  │
│  │ (Autonomous LLM TUI)  │  │   (Interactive CLI)  │  │ (Code Intelligence│ │
│  └───────────┬───────────┘  └───────────┬──────────┘  └──────────────────┘  │
└──────────────┼──────────────────────────┼───────────────────────────────────┘
               │                          │ JSON / TLS :4444
               ▼                          ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                             C2 RELAY TIER                                   │
│                     server.py (Multi-threaded C2 Relay)                     │
│    - Auth & Handshake         - Module Dispatcher     - Push Event Stream   │
└─────────────────────────────────────────┬───────────────────────────────────┘
                                          │ JSON / TLS :4444
                                          ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                             VICTIM TIER                                     │
│  Dropper (red.cpp) ➔ AppInit DLL Implant (dll.cpp) ➔ Watchdog (SAFE.cpp)    │
│  + Direct Raw TCP Reverse Shell Stream on Port :4445                        │
└─────────────────────────────────────────────────────────────────────────────┘
```

<!-- Note: [Timing: 03:00 - 04:30]
Notice the three distinct tiers: Operator, Relay, and Victim. We also separate the JSON command relay on port 4444 from an out-of-band direct reverse shell on port 4445. -->

---

## Slide 4: PE Analysis & Authenticode Signature Transplanting

### Exploiting the Security Data Directory
- **Location**: `OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_SECURITY]` (Index 4).
- **Disk vs. Memory**: The certificate table resides at the physical end of the PE binary; it is **never mapped** into loaded virtual process memory.
- **Transplant Workflow (`cert_spoofer.py`)**:
  1. Extract raw PKCS#7 signed certificate blob from legitimate binary (`calc.exe`, `explorer.exe`).
  2. Append certificate blob to unsigned dropper (`main.exe`).
  3. Adjust `VirtualAddress` and `SizeOfCertificateTable` in PE Optional Header.
- **Empirical Finding**: While `WinVerifyTrust()` fails cryptographic verification, naive file reputation heuristics and triage filters evaluate the binary with reduced scrutiny.

<!-- Note: [Timing: 04:30 - 06:00]
We built file_analyzer.py to dissect PE headers from scratch, and cert_spoofer.py to benchmark VirusTotal detection variance before and after certificate transplants. -->

---

## Slide 5: Dynamic In-Memory PEB Masquerading

### Evading User-Mode Process Inspection (`process_steal.h`)
- Diagnostic tools (Task Manager, Process Explorer, Sysinternals) read `RTL_USER_PROCESS_PARAMETERS` from user-mode memory.
- **Workflow**:
  1. Dynamically scan active processes and identify a legitimate signed binary via `WinVerifyTrust`.
  2. Resolve target process memory using `NtQueryInformationProcess`.
  3. Read target `CommandLine`, `ImagePathName`, and `WindowTitle`.
  4. Overwrite local PEB parameters in current process memory.
- **Result**: A malicious dropper visibly and programmatically presents itself as `svchost.exe -k netsvcs` or `spoolsv.exe`.

<!-- Note: [Timing: 06:00 - 07:30]
This deceptive technique targets the user-mode perception layer. Later in detection engineering, we show how kernel memory forensics still uncovers the discrepancy. -->

---

## Slide 6: Zero-Prompt PrivEsc & SYSTEM Token Stealing

### 1. UAC Auto-Elevate Registry Hijack (`red.cpp`)
- Exploits user-writable file association keys in `HKCU\Software\Classes\exefile\shell\open\command`.
- Spawns auto-elevated `slui.exe` using `ShellExecuteW("runas", ...)`.
- Elevates directly to High Integrity with **zero UAC consent dialogs**.

### 2. Token Stealing to SYSTEM (`privesc.h`)
- Enables `SeDebugPrivilege` via `AdjustTokenPrivileges`.
- Opens `winlogon.exe` (running as `NT AUTHORITY\SYSTEM`).
- Duplicates primary token via `DuplicateTokenEx(..., TokenPrimary, ...)`.
- Spawns persistence registration process via `CreateProcessWithTokenW`.

<!-- Note: [Timing: 07:30 - 09:00]
This combination takes an unprivileged local user all the way to NT AUTHORITY\SYSTEM without ever alerting the interactive user. -->

---

## Slide 7: Advanced Research Exploits in `files/`

### 1. CVE-2026-20817: Windows Error Reporting ALPC EoP
- Local privilege escalation in Windows Error Reporting service.
- The `\WindowsErrorReportingService` ALPC port improperly handles `SvcElevatedLaunch` (`0x0D`).
- Crafts shared memory ALPC message to trigger `WerFault.exe` execution as `SYSTEM`.
- Yields tokens containing `SeDebugPrivilege` and `SeImpersonatePrivilege`.

### 2. CVE-2026-50656: RoguePlanet Defender TOCTOU Race
- Investigates Time-of-Check to Time-of-Use race conditions during quarantine in `MsMpEng.exe`.
- Analyzes whether file system junction redirection in `%TEMP%` can redirect privileged file actions.

<!-- Note: [Timing: 09:00 - 10:30]
In addition to the primary dropper, our repository preserves and analyzes cutting-edge local privilege escalation vectors that explore kernel and service ALPC boundaries. -->

---

## Slide 8: Resilient Persistence: AppInit_DLLs & DACL Locking

### 1. In-Memory Injection via AppInit_DLLs (`dll.cpp`)
- Configured in `HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Windows`.
- Injected automatically by `user32.dll` into **all GUI processes** during initialization (`InitUserApiHook`).
- Runs entirely in-memory without traditional cross-process injection handles.

### 2. DACL Anti-Deletion File Locking (`SAFE.cpp`)
- Explicit `DENY` ACE applied to `Everyone` for `DELETE`, `WRITE_DAC`, and `WRITE_OWNER`.
- Standard users and local administrators are blocked from deleting the implant files.

### 3. Failsafe Self-Healing Watchdog (`SAFE.exe`)
- Autostarts via `HKCU\Run` ("Windows Security Health") and verifies/repairs keys every 10 seconds.
- Built-in `SAFE.exe /remove` switch authorizes clean uninstallation for post-lab cleanup.

<!-- Note: [Timing: 10:30 - 12:00]
AppInit_DLLs gives ubiquitous presence across all interactive applications, while SAFE.exe protects the configuration from administrative tampering. -->

---

## Slide 9: Real-Time Event Telemetry & Direct Reverse Shell

### 1. Asynchronous Wire Protocol (`server.py`)
- Newline-delimited JSON stream over TCP/TLS on port `4444`.
- Server handles local Python modules (`ssh`, `rdp`, `registry`, `activity`) and relays victim actions (`notify`, `shell`).

### 2. WinEvent Push Streaming
- Hooks `EVENT_SYSTEM_FOREGROUND` via `SetWinEventHook`.
- Broadcasts victim window focus changes (e.g. browser navigation, password prompts) to operators in real-time.

### 3. Out-of-Band Direct TCP Reverse Shell (:4445)
- Dedicated raw interactive socket directly between victim and operator.
- Avoids JSON serialization latency and supports interactive command-line sessions.
- `Ctrl+]` detaches from the shell while keeping the session persistent.

<!-- Note: [Timing: 12:00 - 13:30]
Separating command control from the raw terminal shell ensures low-latency interactive responsiveness without clogging the primary JSON event feed. -->

---

## Slide 10: Next-Gen C2: Autonomous AI Operations

### Autonomous AI Operator (`agent/agent_tui.py`)
- Built using Python's Textual framework, styled after Claude Code.
- Powered by LLMs via OpenRouter (Claude 3.5 Sonnet, DeepSeek) or local Ollama (`llama3.1:8b`).

### Human-in-the-Loop Safety Gate Architecture
- **Read-Only Operations**: (`sysinfo`, `processes`, `network`, `reg_read`) execute automatically.
- **Mutating Operations**: (`reg_write`, `shell_open`, `notify_popup`) pause execution and render an interactive confirmation modal.
- Operator explicitly approves or denies each proposed action before any packet reaches the target.

<!-- Note: [Timing: 13:30 - 15:00]
We demonstrate that autonomous LLM post-exploitation is highly practical when guarded by strict human-in-the-loop authorization gates. -->

---

## Slide 11: Blue Team Detection Engineering & Forensics

### Detection Artifacts Across the Kill Chain
- **Sysmon Event ID 1**: Process creation anomalies (child processes spawned by `slui.exe`).
- **Sysmon Event ID 7**: Image loading of `AppInit_DLLs.dll` across user GUI processes.
- **Sysmon Event ID 10**: Handle acquisition on `winlogon.exe` with `PROCESS_DUP_HANDLE` (`0x0040`).
- **Sysmon Event ID 12/13**: Modifications to `Windows NT\CurrentVersion\Windows\AppInit_DLLs`.
- **PowerShell Event ID 4104**: ScriptBlock logging capturing `Add-MpPreference -ExclusionPath`.

### Memory Forensics (Volatility 3)
- `windows.psscan` vs `windows.cmdline`: Discovers discrepancy between user-mode PEB strings and kernel `EPROCESS` image file tracking.

<!-- Note: [Timing: 15:00 - 16:30]
For blue teams, every attack technique in AgentRed has a corresponding detection engineering signature and forensic analysis workflow. -->

---

## Slide 12: Live Demonstration & Remediation

### Demonstration Lifecycle (Phases 1 - 5)
1. **Launch Infrastructure**: Start C2 server on port 4444.
2. **Execute Dropper**: Run `main.exe` on Windows test VM; verify zero UAC prompts.
3. **Connect Operator**: Launch `agent_tui.py`; inspect target system information in natural language.
4. **Trigger Telemetry**: Switch windows on victim; observe live push banners in operator console.
5. **Interactive Access**: Spawn out-of-band reverse shell on port 4445.
6. **Remediation**: Run `SAFE.exe /remove` as Administrator; verify clean uninstallation.

### Conclusion
- User-mode indicators (PEB, signatures) are vulnerable to manipulation without kernel privileges.
- Defensive posture requires enforcing Secure Boot, script block auditing, and strict DACL monitoring.

<!-- Note: [Timing: 16:30 - 18:00]
Thank you. We are now open for technical questions and architecture discussion. -->
