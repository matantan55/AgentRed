# c2-project MCP

**Full usage guide: [USAGE.md](USAGE.md)** — per-tool reference, example
prompts, scripted testing, client registration, scope policy.

Local MCP server for the c2_server codebase: architecture + module catalog
parsed live from the sources, wire-protocol reference, code search/read
(jail-scoped to the project), a local-only Activity-module test harness, and
ATT&CK-grounded hunting notes with sources.

## Setup

```bash
cd ~/Downloads/c2_server
uv venv mcp/.venv
uv pip install --python mcp/.venv/bin/python "mcp" psutil   # already done in this checkout
```

## Run (manually)

```bash
mcp/.venv/bin/python mcp/c2_project_mcp.py        # stdio server
mcp/.venv/bin/python mcp/smoke_test.py            # protocol-level test, all tools
```

## Register in Hermes (~/.hermes/config.yaml)

```yaml
mcp_servers:
  c2-project:
    command: /Users/matanmishali/Downloads/c2_server/mcp/.venv/bin/python
    args: [/Users/matanmishali/Downloads/c2_server/mcp/c2_project_mcp.py]
```

Restart Hermes; tools appear as `mcp_c2_project_<tool>`.

## Tools

- project_overview — components, transport, ports/auth, file stats, notes
  (incl. the double `_handle_line` definition and unwired defender namespace)
- list_modules — server-side (Python) + agent-side (dll.cpp) modules with
  every action, its file:line, and harvested parameter names
- module_detail(name) — one module in depth
- protocol_reference — newline-JSON wire format, auth/roles/relay semantics
- search_code(pattern, file_glob, max_hits) — regex over project sources
- read_source(rel_path, offset, limit) — line-numbered, path-jail enforced
- local_activity(action, params_json) — runs the server-side Activity module
  in-process on THIS machine via psutil. Local dev harness only.
- hunting_notes(topic) — detection engineering map of this project's
  capabilities: ATT&CK IDs, Sysmon/ETW/Defender log telemetry, cited sources.

## Lab Report Agent (mcp/cool_but_not_useful/lab_report_agent.py)

Offline analysis agent for your own lab sessions — reads `c2_server.log`
(server.py's audit trail), never connects to the socket:

```bash
mcp/.venv/bin/python mcp/cool_but_not_useful/lab_report_agent.py report   # session report
mcp/.venv/bin/python mcp/cool_but_not_useful/lab_report_agent.py report --json
mcp/.venv/bin/python mcp/cool_but_not_useful/lab_report_agent.py follow   # live-tail parsed events
```

Reconstructs peers/roles/timelines, counts local dispatches vs agent relays,
collects push events, and flags anomalies (auth failures, relay-with-no-agent,
the un-ID'd relay response race). `cool_but_not_useful/drive_lab.py` generates
loopback test traffic for it.

## Scope boundary (by design)

This server is code intelligence + local dev harness. It does NOT open a TCP
connection to the C2 server and does NOT relay commands to a connected agent
(no dispatch of ssh-exec/registry-write/notify/shell through MCP to any
remote host). Treat it as a documentation/testing aid for the codebase.
