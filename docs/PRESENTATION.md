# AgentRed: Architecture Presentation & Technical Overview
**Adversary Simulation, Windows Evasion, Resilient Persistence & Autonomous C2 Operations**

---

## Slide 1: Title & Research Motivation
### AgentRed: Investigating User-Mode Evasion & Autonomous Post-Exploitation
- **Topic**: Advanced Windows Evasion, Persistence & Autonomous C2 Operations
- **Core Research Question**: 
  *How do modern endpoint protection systems evaluate trust, process identity, and persistence mechanisms—and how can human operators collaborate with autonomous AI agents in post-exploitation scenarios?*
- **Scope**: Controlled laboratory environment; analysis of Windows internals and detection engineering.

---

## Slide 2: The Attack Surface & Problem Statement
- **Defense-in-Depth Assumptions**:
  1. Signed binaries are trustworthy.
  2. The process name and command line in process monitors represent true execution context.
  3. Administrative accounts can easily remove unauthorized persistence items.
  4. C2 implants require manual operator execution for every command.
- **AgentRed's Objective**:
  Systematically evaluate weaknesses in these assumptions and engineer defensive telemetry to detect them.

---

## Slide 3: End-to-End System Architecture
- **Three-Tier Architecture**:
  1. **Operator Tier**:
     - Interactive CLI (`client.py`)
     - AI Operator TUI with human approval gates (`agent_tui.py`)
     - FastMCP integration for LLMs (`c2_project_mcp.py`)
  2. **Relay Tier**:
     - Multi-threaded TCP/TLS relay (`server.py`) with role-based routing and push event broadcast.
  3. **Victim Tier**:
     - Initial loader & PEB masquerading (`red.cpp`, `process_steal.h`)
     - AppInit_DLLs in-memory implant (`dll.cpp`)
     - Failsafe self-healing watchdog (`SAFE.cpp`)
     - Out-of-band direct reverse shell (:4445)

---

## Slide 4: PE Analysis & Authenticode Signature Transplanting
- **The Concept**: PE Security Directory (`IMAGE_DIRECTORY_ENTRY_SECURITY`).
- **Mechanics (`file_analyzer.py` & `cert_spoofer.py`)**:
  - The digital certificate resides at the end of the file, not in loaded virtual memory.
  - Extracting a valid PKCS#7 certificate from a legitimate binary (e.g., Microsoft/Google) and appending it to an unsigned binary.
- **Experimental Findings**:
  - `WinVerifyTrust()` fails cryptographic hash checks.
  - However, naive heuristic engines and triage scanners evaluate the binary with reduced scrutiny based solely on directory presence.

---

## Slide 5: Dynamic In-Memory PEB Masquerading
- **The Evasion Technique (`process_steal.h`)**:
  - EDR and monitoring tools (Process Explorer, Task Manager) inspect user-mode `RTL_USER_PROCESS_PARAMETERS`.
- **Workflow**:
  1. Enumerate active processes and locate an authenticated, signed binary via `WinVerifyTrust`.
  2. Use `NtQueryInformationProcess` to locate the target PEB.
  3. Overwrite local PEB strings (`CommandLine`, `ImagePathName`, `WindowTitle`).
- **Impact**:
  A malicious process visually and programmatically presents itself as `svchost.exe` or `spoolsv.exe`.

---

## Slide 6: Zero-Prompt Privilege Escalation & Token Stealing
- **UAC Auto-Elevate Hijack (`red.cpp`)**:
  - Exploits user-writable file association keys in `HKCU\Software\Classes\exefile\shell\open\command`.
  - Invokes auto-elevated `slui.exe` via `runas` to gain High Integrity without user consent dialogs.
- **Token Stealing to SYSTEM (`privesc.h`)**:
  - Enables `SeDebugPrivilege`.
  - Queries `winlogon.exe` (running as `SYSTEM`).
  - Calls `OpenProcessToken` ➔ `DuplicateTokenEx` ➔ `CreateProcessWithTokenW`.
  - Spawns persistent installation processes directly as `NT AUTHORITY\SYSTEM`.

---

## Slide 7: Resilient Persistence: AppInit_DLLs & DACL Locking
- **AppInit_DLLs In-Memory Injection (`dll.cpp`)**:
  - Configures `HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Windows\AppInit_DLLs`.
  - Automatically loaded into all GUI applications by `user32.dll`.
- **DACL Anti-Deletion Protection (`SAFE.cpp`)**:
  - Modifies Discretionary Access Control Lists to `DENY` `DELETE` access to the `Everyone` group.
  - Prevents standard users and administrators from deleting the implant files.
- **Failsafe Watchdog**:
  - `SAFE.exe` runs via `HKCU\Run` ("Windows Security Health") and continuously validates/restores AppInit registry keys.

---

## Slide 8: Real-Time Event Telemetry & Direct Reverse Shell
- **Asynchronous Wire Protocol**:
  - Newline-delimited JSON over TCP/TLS (:4444).
  - Synchronous request/response routing for module commands.
- **Victim Event Streaming**:
  - Uses `SetWinEventHook` (`EVENT_SYSTEM_FOREGROUND`) to capture active window changes and stream them to operators.
- **Out-of-Band Interactive Shell**:
  - Direct raw TCP socket on port `4445` dedicated to interactive `cmd.exe` sessions, avoiding JSON encapsulation overhead.

---

## Slide 9: Next-Generation C2: Autonomous AI Operations
- **Autonomous Operator (`agent/agent_tui.py`)**:
  - Terminal user interface styled after Claude Code.
  - Translates plain-English operator intent into structured C2 module calls.
- **Human-in-the-Loop Safety Gate**:
  - Read-only actions (process list, sysinfo, registry read) run autonomously.
  - Destructive or mutating actions (`reg_write`, `shell_open`) pause execution and require explicit operator approval.
- **Model Context Protocol**:
  - Exposes 38 C2 tools via FastMCP to any compatible LLM assistant.

---

## Slide 10: Blue Team Detection Engineering & Forensics
- **Key Indicators of Compromise (IOCs)**:
  - **Registry**: `AppInit_DLLs` populated with custom path; `HKCU\Software\Classes\exefile` key creations.
  - **Sysmon Telemetry**:
    - Event ID 1: Child processes spawned by auto-elevating binaries (`slui.exe`).
    - Event ID 7: `AppInit_DLLs.dll` image load events across GUI binaries.
    - Event ID 12/13: Registry modifications to `Windows NT\CurrentVersion\Windows`.
- **Remediation**:
  - Built-in safe removal switch: `SAFE.exe /remove` resets DACL permissions and cleans registry persistence.

---

## Slide 11: Live Demonstration Walkthrough
1. **Infrastructure Launch**: Start C2 relay server (`python server.py --port 4444`).
2. **Victim Execution**: Run payload on test Windows VM; observe zero UAC prompts.
3. **Operator Connection**: Connect via CLI (`client.py`) and autonomous AI TUI (`agent_tui.py`).
4. **Telemetry Stream**: Switch windows on the victim; view live focus events in operator console.
5. **Interactive Access**: Launch out-of-band reverse shell (:4445).
6. **Teardown**: Execute `SAFE.exe /remove` and verify complete remediation.

---

## Slide 12: Conclusion & Technical Takeaways
- **Key Lessons**:
  - User-mode indicators (PEB strings, file signatures) can be manipulated without elevated kernel access.
  - Legacy Windows features like `AppInit_DLLs` remain viable persistence vectors if not explicitly restricted.
  - Modern C2 architectures benefit from combining structured JSON event channels with dedicated raw streams.
  - Autonomous AI operators require strict human-in-the-loop approval gates for offensive tooling.
- **Q&A**: Open for technical questions and architecture discussion.
