# AgentRed Technical Documentation Suite

Welcome to the **AgentRed** documentation repository. This directory contains technical reports, architecture specifications, API references, user guides, detection rules, and presentation materials.

---

## Deliverables Index

| Deliverable | File | Purpose |
| :--- | :--- | :--- |
| **Operator & Agent User Guide** | [`USER_GUIDE.md`](file:///Users/matanmishali/AntiGravity/AgentRed/docs/USER_GUIDE.md) | Step-by-step practical guide: interactive CLI (`client.py`), autonomous AI operator (`agent_tui.py`), 5-phase attack runbook, and OPSEC noise matrix. |
| **Comprehensive Technical Specification** | [`DOCUMENTATION.md`](file:///Users/matanmishali/AntiGravity/AgentRed/docs/DOCUMENTATION.md) | Complete technical report: Windows internals, evasion mechanics, privilege escalation (incl. WER ALPC & RoguePlanet PoCs), Secure Boot constraints, MITRE ATT&CK mapping, Volatility 3 forensics, and IOC matrix. |
| **Wire Protocol & API Specification** | [`PROTOCOL.md`](file:///Users/matanmishali/AntiGravity/AgentRed/docs/PROTOCOL.md) | Complete wire protocol reference: newline-delimited JSON framing, hybrid dispatch engine, push event streaming, and out-of-band reverse shell socket details. |
| **Machine-Readable Protocol Schema** | [`protocol_schema.json`](file:///Users/matanmishali/AntiGravity/AgentRed/docs/protocol_schema.json) | Formal JSON Schema (Draft-07) validating all client/server handshakes, commands, responses, and telemetry events. |
| **Production Sigma Detection Rules** | [`sigma/`](file:///Users/matanmishali/AntiGravity/AgentRed/docs/sigma) | Ready-to-deploy Sigma rules for SIEMs: `slui.exe` UAC bypass, `AppInit_DLLs` persistence, and `winlogon.exe` handle duplication. |
| **Architecture Presentation Deck** | [`PRESENTATION.md`](file:///Users/matanmishali/AntiGravity/AgentRed/docs/PRESENTATION.md) | Marp-compliant 12-slide briefing deck with speaker notes, timing cues, system diagrams, and demonstration walkthrough. |

---

## Documentation Subsystems Overview

1. **Evasion & Masquerading**:
   - Authenticode signature transplanting ([`cert_spoofer.py`](file:///Users/matanmishali/AntiGravity/AgentRed/cert_spoofer.py), [`file_analyzer.py`](file:///Users/matanmishali/AntiGravity/AgentRed/file_analyzer.py)).
   - In-memory PEB process masquerading via dynamic signed binary discovery ([`process_steal.h`](file:///Users/matanmishali/AntiGravity/AgentRed/process_steal.h)).
2. **Privilege Escalation**:
   - Zero-prompt UAC auto-elevate registry hijack ([`red.cpp`](file:///Users/matanmishali/AntiGravity/AgentRed/red.cpp)).
   - Token duplication from `winlogon.exe` to `NT AUTHORITY\SYSTEM` ([`privesc.h`](file:///Users/matanmishali/AntiGravity/AgentRed/privesc.h)).
   - Windows Error Reporting ALPC EoP research PoC ([`files/CVE-2026-20817_PoC.cpp`](file:///Users/matanmishali/AntiGravity/AgentRed/files/CVE-2026-20817_PoC.cpp)).
   - Defender TOCTOU race condition testing ([`files/CVE-2026-50656.cpp`](file:///Users/matanmishali/AntiGravity/AgentRed/files/CVE-2026-50656.cpp)).
3. **Persistence & Self-Healing**:
   - User32-mediated `AppInit_DLLs` in-memory injection ([`dll.cpp`](file:///Users/matanmishali/AntiGravity/AgentRed/dll.cpp)).
   - Windows 10/11 Secure Boot and code signing constraint analysis.
   - Restrictive DACL file locking and registry watchdog ([`SAFE.cpp`](file:///Users/matanmishali/AntiGravity/AgentRed/SAFE.cpp)).
4. **Command & Control Operations**:
   - Multi-threaded asynchronous relay and push broadcast server ([`c2_server/server.py`](file:///Users/matanmishali/AntiGravity/AgentRed/c2_server/server.py)).
   - Interactive operator console ([`c2_server/client.py`](file:///Users/matanmishali/AntiGravity/AgentRed/c2_server/client.py)).
   - Autonomous AI operator TUI with safety approval gates ([`agent/agent_tui.py`](file:///Users/matanmishali/AntiGravity/AgentRed/agent/agent_tui.py)).
   - Code intelligence FastMCP server for IDE assistants ([`c2_server/mcp/c2_project_mcp.py`](file:///Users/matanmishali/AntiGravity/AgentRed/c2_server/mcp/c2_project_mcp.py)).
5. **Defensive Engineering & Forensics**:
   - Expanded Sysmon Event IDs (1, 3, 7, 10, 12, 13) and PowerShell ScriptBlock 4104.
   - Production Sigma detection rules in [`docs/sigma/`](file:///Users/matanmishali/AntiGravity/AgentRed/docs/sigma).
   - Volatility 3 memory analysis workflow for uncovering PEB masquerading.
   - Consolidated Indicators of Compromise (IOC) matrix.
   - Built-in remediation via `SAFE.exe /remove`.

---

## Viewing as an Interactive Web Portal (MkDocs)

To serve these documents locally with the modern MkDocs Material theme (including full-text offline search, tabs, and dark mode):

```bash
# Install mkdocs and material theme
pip install mkdocs-material

# Start local documentation server
mkdocs serve
```
Visit `http://127.0.0.1:8000` in your web browser.
