# Using the c2-project MCP

The MCP server lives at `mcp/c2_project_mcp.py` and runs over stdio using the
virtualenv at `mcp/.venv`. It is already registered in Hermes as `c2-project`
(`~/.hermes/config.yaml` → `mcp_servers`), so every tool below is available in
a new Hermes session under the name `mcp_c2_project_<tool>`.

What it does: reads the c2_server codebase, answers questions about it, and
lets you exercise the one locally-safe module (Activity). What it does not
do: open the C2 socket, dispatch commands to a connected agent, or run the
SSH/RDP/Registry modules. See "Scope" at the end.

---

## 1. Quick start (in Hermes)

Start a fresh session (MCP tools load at startup) and just ask in plain
language — the tool descriptions make routing obvious:

- "Give me an overview of the c2_server project" → `project_overview`
- "What actions does the registry module expose, and where are they defined?" → `module_detail("registry")`
- "Is every module in dll.cpp actually reachable over the wire?" → `list_modules` (check `wired_in_dispatch`)
- "Show me everywhere Add-MpPreference appears" → `search_code("Add-MpPreference")`
- "Read server.py lines 200-270" → `read_source("server.py", offset=200, limit=70)`
- "Snapshot this Mac's processes like the activity module would" → `local_activity("processes")`
- "What telemetry would a defender disable generate?" → `hunting_notes("defender_tampering")`

## 2. Tool reference

### project_overview()
Returns JSON: components (server.py / client.py / dll.cpp / modules),
transport (port 4444, token auth, newline-JSON, TLS flags), file sizes and
line counts, the operator CLI command surface extracted from client.py, and
known code notes — currently: the duplicate `_handle_line` definition in
server.py (second shadows the first) and the `defender` namespace that is
implemented in dll.cpp but **not wired into the agent dispatch chain**.

### list_modules()
The full catalog, re-parsed live from the sources on every call:

- `server_modules` — the four Python modules `server.py` imports
  (`ssh`, `rdp`, `registry`, `activity`): every action with handler name,
  line number, and the parameter names harvested from the source, plus an
  `availability` note where relevant (registry = Windows only, activity =
  needs psutil).
- `agent_modules` — the seven `dll.cpp` namespaces (`ssh_mod`, `rdp_mod`,
  `reg_mod`, `act_mod`, `notif_mod`, `shell_mod`, `defender_mod`): actions
  with exact file line numbers, all `p["param"]` names, and
  `wired_in_dispatch` telling you whether the module is reachable from the
  operator protocol.

Because parsing happens at call time, edits to the sources are reflected
immediately — no rebuild.

### module_detail(name)
Same data for a single module (accepts partial names: `ssh`, `SSHModule`,
`shell_mod` all work). Returns an array — a name can match one server module
and one agent module (e.g. `activity`).

### protocol_reference()
Documentation-only wire format: auth messages for both roles, the
`{"module": ..., "payload": {...}}` command shape, routing rules (known
module → executed server-side by `MODULES`; unknown module → relayed to the
first connected agent, 30 s timeout), push-event broadcast semantics, and
defaults. Use it to answer "what would I put on the wire for X" without
touching the socket.

### search_code(pattern, file_glob="*", max_hits=60)
Regex across project files, returns `path:line: text`. Skips `.venv`,
`__pycache__`, logs, `.DS_Store`. Examples:

```
search_code("action\\s*==\\s*\"write_value\"")     → dll.cpp registry dispatch
search_code("C2_AUTH_TOKEN", file_glob="*.py")     → env-token references
search_code("SetWinEventHook")                     → notify module internals
```

### read_source(rel_path, offset=1, limit=400)
Line-numbered file reader, jailed to the project directory — `..` escapes
are refused with `{"error": "path escapes project root"}`. Limit is capped
at 2000 lines.

### local_activity(action, params_json="{}")
Imports `modules/activity_module.py` by path and runs `ActivityModule().handle()`
**in-process on this machine**. This is the only tool that executes project
code, and it mirrors exactly what `server.py` does locally for that module.

| action | optional params |
|---|---|
| `processes` | `pid`, `name` (substring filter) |
| `system_stats` | – |
| `network_connections` | – (macOS may need root for full table) |
| `top_cpu` / `top_mem` | `limit` (default 10) |
| `start_monitor` | `interval` seconds (default 5) |
| `stop_monitor` / `get_snapshots` | `limit` (default 10) |

`params_json` is a JSON-encoded string: `local_activity("top_cpu", '{"limit": 5}')`.

Note: `start_monitor` spawns a background thread **inside the MCP server
process**, and snapshots live in that process's memory — `get_snapshots`
works as long as the same MCP instance answers (true within one Hermes
session, not across restarts).

### hunting_notes(topic="all")
Static analysis → detection-engineering map: for each capability area, the
MITRE ATT&CK technique IDs (published level), the concrete telemetry an
analyst would see, and cited sources per topic. Topics:

`defender_tampering` · `remote_shell` · `registry_persistence` ·
`ssh_rdp_access` · `activity_discovery` · `notification_monitor` ·
`network_beacon`

## 3. Manual / scripted use (outside Hermes)

```bash
cd ~/Downloads/c2_server

# protocol-level self-test over real stdio JSON-RPC (all 8 tools + jail check)
mcp/.venv/bin/python mcp/smoke_test.py

# parser sanity checks (module counts, params, dispatch flags)
mcp/.venv/bin/python mcp/catalog_check.py

# monitor-state persistence check (start_monitor → get_snapshots across calls)
mcp/.venv/bin/python mcp/monitor_flow_check.py

# connection test through the Hermes CLI
hermes mcp test c2-project

# call a function directly (they're plain Python; no MCP transport needed)
mcp/.venv/bin/python -c "import sys; sys.path.insert(0,'mcp'); \
import c2_project_mcp as m; print(m.list_modules())"
```

`mcp/.venv` needs `mcp>=2` (the `MCPServer` class — this file breaks against
`mcp<1.9` where it was still named FastMCP) and `psutil` for
`local_activity`. Reinstall: `uv pip install --python mcp/.venv/bin/python mcp psutil`.

## 4. Adding to other MCP clients

Any stdio MCP client works with the same command:

```json
{
  "mcpServers": {
    "c2-project": {
      "command": "/Users/matanmishali/Downloads/c2_server/mcp/.venv/bin/python",
      "args": ["/Users/matanmishali/Downloads/c2_server/mcp/c2_project_mcp.py"]
    }
  }
}
```

## 5. Scope — what will never be added

The server exposes **code intelligence and a local dev harness only**. No
tool connects to `server.py`'s TCP port, registers as an operator, or
dispatches anything to a connected agent — including the `ssh` exec,
`registry` write, `notify`, `shell`, and `defender` paths that exist in the
codebase. If you want those exercised, do it through `client.py` yourself,
where a human is in the loop. `protocol_reference()` documents the exact
wire shapes for that work without the MCP ever joining it.
