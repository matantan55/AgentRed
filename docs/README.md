# AgentRed Technical Documentation Suite

Welcome to the **AgentRed** documentation directory. This folder contains all technical reports, architecture specifications, API references, user guides, and presentation materials.

---

## Deliverables Index

| Document | File | Purpose |
| :--- | :--- | :--- |
| **Operator & Agent User Guide** | [USER_GUIDE.md](file:///Users/matanmishali/AntiGravity/AgentRed/docs/USER_GUIDE.md) | Step-by-step practical guide: using the interactive CLI client (`client.py`) and operating the autonomous AI agent (`agent_tui.py` / `agent.py`). |
| **Comprehensive Technical Specification** | [DOCUMENTATION.md](file:///Users/matanmishali/AntiGravity/AgentRed/docs/DOCUMENTATION.md) | Complete technical report: Windows internals, evasion mechanics, privilege escalation, persistence mechanisms, MITRE ATT&CK mapping, and defensive detection engineering. |
| **Protocol & API Specification** | [PROTOCOL.md](file:///Users/matanmishali/AntiGravity/AgentRed/docs/PROTOCOL.md) | Complete wire protocol reference: newline-delimited JSON schema, auth handshakes, module payloads, event streaming, and out-of-band reverse shell socket details. |
| **Technical Architecture & Presentation Deck** | [PRESENTATION.md](file:///Users/matanmishali/AntiGravity/AgentRed/docs/PRESENTATION.md) | 12-slide structured presentation outline with key architectural points, system diagrams, and a technical demonstration walkthrough. |

---

## Primary Subsystems Documented

1. **Evasion & Masquerading**:
   - Authenticode signature transplanting ([`cert_spoofer.py`](file:///Users/matanmishali/AntiGravity/AgentRed/cert_spoofer.py), [`file_analyzer.py`](file:///Users/matanmishali/AntiGravity/AgentRed/file_analyzer.py)).
   - In-memory PEB process masquerading via dynamic signed binary discovery ([`process_steal.h`](file:///Users/matanmishali/AntiGravity/AgentRed/process_steal.h)).
2. **Privilege Escalation**:
   - Zero-prompt UAC auto-elevate registry hijack ([`red.cpp`](file:///Users/matanmishali/AntiGravity/AgentRed/red.cpp)).
   - Token duplication from `winlogon.exe` to `NT AUTHORITY\SYSTEM` ([`privesc.h`](file:///Users/matanmishali/AntiGravity/AgentRed/privesc.h)).
3. **Persistence & Self-Healing**:
   - User32-mediated `AppInit_DLLs` in-memory injection ([`dll.cpp`](file:///Users/matanmishali/AntiGravity/AgentRed/dll.cpp)).
   - Restrictive DACL file locking and registry watchdog ([`SAFE.cpp`](file:///Users/matanmishali/AntiGravity/AgentRed/SAFE.cpp)).
4. **Command & Control Operations**:
   - Multi-threaded asynchronous relay and push broadcast server ([`c2_server/server.py`](file:///Users/matanmishali/AntiGravity/AgentRed/c2_server/server.py)).
   - Interactive operator console ([`c2_server/client.py`](file:///Users/matanmishali/AntiGravity/AgentRed/c2_server/client.py)).
   - Autonomous AI operator TUI with safety approval gates ([`agent/agent_tui.py`](file:///Users/matanmishali/AntiGravity/AgentRed/agent/agent_tui.py)).
   - FastMCP server integration for Claude/Cursor ([`c2_server/mcp/c2_project_mcp.py`](file:///Users/matanmishali/AntiGravity/AgentRed/c2_server/mcp/c2_project_mcp.py)).
5. **Defensive Analysis**:
   - Sysmon Event IDs (1, 7, 12, 13).
   - YARA detection rule.
   - Built-in remediation via `SAFE.exe /remove`.
