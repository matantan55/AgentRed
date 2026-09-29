# C2 Research Operator Agent

An AI agent that wraps `client.py` so a **human operates in plain language**
and the **agent performs the operations** on the lab Windows target through
the existing C2 protocol. Built for a cyber-security course lab; the agent
adds no capabilities beyond what `client.py` already dispatches.

```
 you (researcher) ──"what's eating CPU?"/"show me notes.txt"──▶ agent.py  (LLM loop)
                                                                 │  one JSON action per step
                                                                 ▼
                                                             agent_tools.py   ← tool layer (38 tools)
                                                                 │  reuses C2Client from
                                                                 ▼
                                                             client.py ──token auth──▶ C2 server ──▶ Windows agent (DLL)
                                                                 ▲
                                              push events (victim toasts) are buffered, not printed
```

## Project layout

```
agent_dev/
├── client.py              (existing) operator console + transport — imported, untouched
├── agent_tools.py         tool layer over client.py's C2Client
├── agent.py               headless/REPL agent (OpenRouter | local | --dry-run)
├── agent_tui.py           Claude-Code-style Textual UI for the same agent
├── AGENT_PROMPT.md        init prompt (persona, RoE, JSON action protocol)
├── README.md
└── tests/                 everything that is test/demo tooling, not runtime
    ├── mock_c2_server.py        lab stand-in: real protocol, fake Windows agent
    │                            (incl. callback cmd.exe over an in-memory FS)
    ├── mock_llm_server.py       scripted chat-completions server (SLEEP/SCENARIO env)
    ├── selftest_tui.py          pilot: connect → /help → read → Approve → Deny
    ├── selftest_busy.py         pilot: busy-spinner lifecycle; writes tui_busy_preview.svg
    ├── screenshot_preview.py    renders tui_preview.svg (regenerate after UI tweaks)
    ├── auth_probe.py            asks a C2 port which token it accepts
    ├── tui_preview.svg          UI artifact
    └── tui_busy_preview.svg     mid-spinner artifact
```

Run the selftests from the repo root with the venv interpreter, e.g.
`.venv/bin/python tests/selftest_tui.py` (needs `tests/mock_c2_server.py`
running on 127.0.0.1:4444; the busy test additionally needs
`SLEEP=1.2 SCENARIO=cpu .venv/bin/python tests/mock_llm_server.py` on :8899).

## Setup

```bash
cd ~/AntiGravity/AgentRed/agent
uv venv --python 3.14 .venv          # already done
uv pip install --python .venv/bin/python textual
```

(Textual 8.x note: the widgets used here are `Input`, `Button`, `Collapsible`,
`VerticalScroll` from `textual.containers`.)

## Quickstart

```bash
# 1. lab target (skip if you have the real server)
#    whole lab stack defaults to 127.0.0.1:4444 + token labtoken — no flags.
.venv/bin/python tests/mock_c2_server.py &

# 2. the UI — offline demo first (no model needed)
.venv/bin/python agent_tui.py --dry-run

# 3. real agent via OpenRouter
export OPENROUTER_API_KEY=***
.venv/bin/python agent_tui.py --provider openrouter --model deepseek/deep-chat

# 4. real agent via a local model (nothing leaves your machine)
ollama serve & ollama pull llama3.1:8b
.venv/bin/python agent_tui.py --provider local --model llama3.1:8b
```

Lab defaults are **127.0.0.1:4444 with token `labtoken`** everywhere (mock,
`agent.py`, `agent_tui.py`, `agent_tools.py`). **Pointing at your real course
server:** pass `--host/--port` and either `--token <server token>` or export
`C2_AUTH_TOKEN` (env beats the default). `client.py` is untouched (still
defaults `changeme`, matching `server.py`). The reverse-shell listener
(`shell_open`) binds 4445, independent of the C2 port.

UI controls: Enter sends, Ctrl+Q quits, `/help /tools /events /clear /quit`.
A **busy indicator** spins (braille ⠋⠙⠹…) beside the prompt the moment you
submit — with phase + elapsed seconds (`thinking…`, `running top_cpu on the
target…`, `awaiting your approval…`) — until the task's output lands and the
bridge goes idle.
Mutations open an **Approve / Deny** panel inline — nothing touches the
target until you click Approve. Victim notification pushes appear in the
transcript in real time. The old keyboard REPL (`agent.py`) still works and
shares all logic with the TUI.

## What the agent can do (tool layer)

- **Activity:** `sysinfo`, `processes`, `top_cpu`, `top_mem`, `network`, `monitor_start/stop/get`
- **Registry:** `reg_list_keys`, `reg_list_values`, `reg_read` + mutating `reg_write`, `reg_delete_value`, `reg_create_key`, `reg_delete_key`
- **Sessions:** `ssh_connect/exec/list/disconnect`, `rdp_probe/open/list/close`
- **Notifications:** `notify_start/stop/status`, `notify_popup`, `notify_balloon`, `events` (victim push feed)
- **Shell channel (same `shell` module as client.py, now programmatic):**
  `shell_open`, `shell_exec`, `shell_status`, `shell_kill`, `shell_close`
- **Files via that channel:** `file_read`, `dir_list`, `file_send` (local → target, base64-chunked `echo` + `certutil -decode`; there is no file-transfer module on the wire, this routes around that with existing primitives)
- **Escape hatch:** `raw` — allowlisted modules only

## Safety model (course talking points)

1. **Destructive gate** — every target-mutating tool returns
   `needs_confirmation` unless the human approved it; the driver asks
   `[y/N]` per action. `--autoconfirm` exists only for demos/CI.
2. **Read-first doctrine** is baked into the init prompt: observe → report →
   propose, not blind mutation.
3. **Out of scope by design:** the agent's `defender` module (disable /
   exclusions) is **not** exposed by the wrapper, and `raw` rejects it. The
   exercise is observation and interaction, not AV tampering.
4. **No new capabilities:** anything the agent does, `client.py` could do by
   hand. The agent automates *operation*, not *capability*.
5. Token auth + optional TLS are inherited unchanged from client.py.

## Troubleshooting

- **"Authentication failed"** → the agent reached a server, but the token
  is wrong *for that server*. Mock wants `--token labtoken`; the real
  course server wants its `C2_AUTH_TOKEN` (default `changeme`). The agent
  error line now says exactly this. To see who owns the port and what it
  accepts: `lsof -nP -iTCP:4444 -sTCP:LISTEN`, then
  `.venv/bin/python tests/auth_probe.py <token>` (prints the server's reply for
  your token plus the fallback `changeme`).
- **"Address already in use" from the mock** → another server (real
  `server.py`, or an older mock) holds 4444. Stop it, or run the mock on
  `--port <other>` and give the agent the same `--port`.

## Limitations / notes

- `file_send` needs an open shell channel (`shell_open` first); it spawns
  `cmd.exe` on the target — the same thing `client.py`'s `shell` command does.
- The LLM protocol is JSON-in-text (not native function-calling) on purpose:
  it works with every OpenRouter model and every local server alike.
- Windows path quoting through `cmd.exe` is fragile by nature; complex file
  ops should still go through `client.py` interactively.
- For another agent framework, use `python3 agent_tools.py --repl` —
  JSON-lines in (`{"tool": ..., "args": {...}}`), JSON out, stateful.

Educational/lab use only — authorized environments, your own VMs.
