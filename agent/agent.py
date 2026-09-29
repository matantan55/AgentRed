"""
agent.py — the operator agent that wraps client.py.

A human types plain-language tasking ("what's on the screen of the victim?",
"show me the run key", "send this report to the machine"); the LLM plans
one tool call at a time, agent_tools executes it over the C2 protocol, and
the observation is fed back until the agent answers. The human stays in the
loop and approves every destructive action.

Brains are swappable between:
  • OpenRouter (cloud, 100+ models) — default, needs OPENROUTER_API_KEY
  • a LOCAL model server — Ollama, LM Studio, or llama.cpp's server,
    anything exposing POST /v1/chat/completions on your machine, no key.

    # live agent via OpenRouter
    export OPENROUTER_API_KEY=***
    python3 agent.py --host 127.0.0.1 --port 4444 --token labtoken \
        --provider openrouter --model deepseek/deepseek-chat

    # live agent against a local model (Ollama example)
    ollama pull llama3.1:8b
    python3 agent.py --provider local --model llama3.1:8b

    # no model at all? demonstrate the full loop with the built-in rule planner
    python3 agent.py --dry-run

    # attach as a JSON-lines tool server for another framework instead
    python3 agent_tools.py --repl            (see README)

Educational / lab use only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request

import agent_tools

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PROMPT = os.path.join(HERE, "AGENT_PROMPT.md")

# ──────────────────────────────────────────────────────────────────────
# LLM plumbing — OpenRouter or a local model server (stdlib only)
# ──────────────────────────────────────────────────────────────────────

PROVIDERS = {
    # provider:  (default base url,             default model,      needs api key?)
    "openrouter": ("https://openrouter.ai/api/v1", "openrouter/auto",  True),
    "local":      ("http://localhost:11434/v1",    "llama3.1:8b",      False),
}


def resolve_llm(cfg) -> None:
    """Fill base-url / model / api-key according to the chosen provider."""
    base_default, model_default, needs_key = PROVIDERS[cfg.provider]
    cfg.base_url = cfg.base_url or os.environ.get("AGENT_BASE_URL") or base_default
    cfg.model = cfg.model or os.environ.get("AGENT_MODEL") or model_default
    cfg.api_key = ""
    if needs_key:
        cfg.api_key = (os.environ.get(cfg.api_key_env)
                       or os.environ.get("OPENROUTER_API_KEY") or "")
        if not cfg.api_key:
            print(f"[!] OpenRouter needs a key: export {cfg.api_key_env}=<key> "
                  "(get one at https://openrouter.ai/keys), or run with "
                  "--provider local / --dry-run.")
            sys.exit(2)


def llm_call(cfg, messages: list[dict],
             temperature: float = 0.2, max_tokens: int = 900) -> str:
    """POST /chat/completions — the endpoint shape OpenRouter and local
    servers (Ollama, LM Studio, llama.cpp) all expose."""
    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    if "openrouter.ai" in cfg.base_url:  # OpenRouter attribution headers
        headers["HTTP-Referer"] = "http://localhost/agent-dev-lab"
        headers["X-Title"] = "C2 Research Operator (course lab)"
    body = json.dumps({
        "model": cfg.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode()
    req = urllib.request.Request(f"{cfg.base_url.rstrip('/')}/chat/completions",
                                 data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read().decode())
    return data["choices"][0]["message"]["content"]


def extract_json(text: str) -> dict | None:
    """Pull the first balanced JSON object out of a model reply."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


def build_system_prompt(prompt_path: str) -> str:
    try:
        base = open(prompt_path, encoding="utf-8").read()
    except OSError:
        base = ("You are a security research operator for an authorized lab. "
                "Use the tools below to fulfil the researcher's requests.")
    catalog = json.dumps(agent_tools.tool_catalog(), indent=2)
    return (f"{base}\n\n## Your live tool catalog (from agent_tools.py)\n"
            f"```json\n{catalog}\n```\n")


# ──────────────────────────────────────────────────────────────────────
# Confirmation gate — the human decides on destructive operations
# ──────────────────────────────────────────────────────────────────────

def ask_human_confirm(tool_name: str, args: dict, autoconfirm: bool) -> bool:
    print(f"\n  ⚠  The agent wants to run the MUTATING tool '{tool_name}'")
    print(f"     args: {json.dumps(args)}")
    if autoconfirm:
        print("     (--autoconfirm: approved without asking)")
        return True
    try:
        ans = input("     allow? [y/N] ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


# ──────────────────────────────────────────────────────────────────────
# Dry-run planner — keyword→tool rules so the loop works with no API key
# ──────────────────────────────────────────────────────────────────────

DRY_RULES: list[tuple[re.Pattern, str, dict]] = [
    (re.compile(r"\bcpu\b", re.I), "top_cpu", {}),
    (re.compile(r"\bmem(ory|ory usage)?\b", re.I), "top_mem", {}),
    (re.compile(r"\b(stats|system info|uptime|os version)\b", re.I), "sysinfo", {}),
    (re.compile(r"\bnetwork|connections?\b", re.I), "network", {}),
    (re.compile(r"\bprocess(es)?\b.*\b(chrome|svchost|cmd|powershell)\b", re.I),
     "processes", {}),
    (re.compile(r"\bprocess(es)?|what.*(running|installed)\b", re.I), "processes", {}),
    (re.compile(r"\bhosts file\b", re.I), "file_read", {"path": r"C:\Windows\System32\drivers\etc\hosts"}),
    (re.compile(r"\b(read|show|cat|open)\b.*\bfile\b", re.I), "file_read", {"path": r"C:\Users\lab\notes.txt"}),
    (re.compile(r"\bls\b|\bdir\b|list.*\b(folder|directory)\b", re.I), "dir_list", {"path": "C:\\Users\\lab"}),
    (re.compile(r"\b(add|write|create|set)\b.*\b(run key|reg(istry)? value)\b", re.I),
     "reg_write", {"hive": "HKCU",
                   "key_path": r"Software\Microsoft\Windows\CurrentVersion\Run",
                   "value_name": "LabNote", "value_data": "notepad.exe"}),
    (re.compile(r"\bregistry\b|\brun key\b|hkcu|hklm", re.I),
     "reg_read", {"hive": "HKCU",
                  "key_path": r"Software\Microsoft\Windows\CurrentVersion\Run"}),
    (re.compile(r"\bnotification(s)?\b|\bevents\b|\btoast", re.I), "events", {}),
    (re.compile(r"\bpopup|message box", re.I), "notify_popup",
     {"title": "Lab notice", "body": "Test popup from the research agent", "type": "info"}),
    (re.compile(r"\bmonitor\b", re.I), "monitor_get", {"limit": 3}),
    (re.compile(r"\bssh\b", re.I), "ssh_list", {}),
    (re.compile(r"\brdp\b", re.I), "rdp_list", {}),
    (re.compile(r"\bshell\b", re.I), "shell_status", {}),
]


def dry_plan(task: str, tried: set[str]) -> tuple[str, dict] | tuple[None, str]:
    for pat, name, args in DRY_RULES:
        if pat.search(task) and name not in tried:
            return name, args
    if tried:
        return None, (f"Task handled: ran {sorted(tried)}. "
                      "The read-outs above are the findings — switch to an LLM "
                      "provider for follow-up reasoning.")
    return None, (f"No rule matched {task!r}. This is the offline planner — "
                  "configure OpenRouter (--provider openrouter) or a local model "
                  "(--provider local) for real intent handling.")


# ──────────────────────────────────────────────────────────────────────
# The agent loop
# ──────────────────────────────────────────────────────────────────────

def execute_step(name: str, args: dict, cfg) -> dict:
    """Run a tool, honouring the human confirmation gate. Returns observation."""
    obs = agent_tools.run_tool(name, args)
    if obs.get("status") == "needs_confirmation":
        if ask_human_confirm(name, args, cfg.autoconfirm):
            obs = agent_tools.run_tool(name, args, confirm=True)
        else:
            obs = {"status": "denied_by_human",
                   "message": "The researcher declined this action. Choose a read-only alternative or ask what they would prefer instead."}
    return obs


def trim(obj, limit: int = 4000) -> str:
    s = json.dumps(obj, default=str) if not isinstance(obj, str) else obj
    return s if len(s) <= limit else s[:limit] + f" …[truncated {len(s) - limit} chars]"


def run_task_llm(task: str, messages: list, cfg) -> None:
    messages.append({"role": "user", "content": f"TASK: {task}"})
    tried: list[str] = []
    for step in range(cfg.max_steps):
        reply = llm_call(cfg, messages)
        parsed = extract_json(reply)
        if parsed is None:
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "user", "content":
                             "Invalid reply. Respond with exactly one JSON object "
                             "({\"action\":...} or {\"final\":...})."})
            continue
        thought = parsed.get("thought", "")
        if thought:
            print(f"  🧠 {thought}")
        if "final" in parsed:
            print(f"\n  ✅ AGENT: {parsed['final']}\n")
            messages.append({"role": "assistant", "content": json.dumps(parsed)})
            return
        action = parsed.get("action") or {}
        name, args = action.get("tool"), action.get("args", {})
        if not name:
            messages.append({"role": "assistant", "content": json.dumps(parsed)})
            messages.append({"role": "user", "content":
                             "Missing 'action.tool'. Reply with an action or a final."})
            continue
        tried.append(name)
        print(f"  🔧 {name} {trim(args, 200)}")
        obs = execute_step(name, args, cfg)
        print(f"  👁  {trim(obs, 300)}")
        messages.append({"role": "assistant", "content": json.dumps(parsed)})
        messages.append({"role": "user", "content": f"OBSERVATION: {trim(obs)}"})
    messages.append({"role": "user", "content":
                     "Step limit reached. Produce a final summary now."})
    reply = llm_call(cfg, messages)
    parsed = extract_json(reply) or {"final": reply}
    print(f"\n  ✅ AGENT: {parsed.get('final', trim(parsed, 1500))}\n")


def run_task_dry(task: str, cfg) -> None:
    tried: set[str] = set()
    for _ in range(cfg.max_steps):
        planned = dry_plan(task, tried)
        if planned[0] is None:
            print(f"\n  ✅ AGENT: {planned[1]}\n")
            return
        name, args = planned
        tried.add(name)
        print(f"  🔧 (dry-rule) {name} {trim(args, 200)}")
        obs = execute_step(name, args, cfg)
        if obs.get("status") == "denied_by_human":
            print("  ✅ AGENT: action declined by the operator; stopping here.")
            return
        print(f"  👁  {trim(obs, 1000)}\n")
        if len(tried) >= 3 or name in ("file_read", "dir_list", "reg_read"):
            print("  ✅ AGENT: readout above answers the task (offline planner stops early).\n")
            return


def maybe_surface_events():
    ev = agent_tools.run_tool("events", {})
    events = ev.get("events", [])
    if events:
        print(f"  📡 {len(events)} victim push event(s) received since last check:")
        for e in events[-5:]:
            print(f"      [{e.get('seq')}] {e.get('event')}: {e.get('title', '')} — {e.get('body', '')}")


def main():
    ap = argparse.ArgumentParser(description="LLM operator agent wrapping client.py (lab use only).")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4444)
    ap.add_argument("--token", default=os.environ.get("C2_AUTH_TOKEN", "labtoken"))
    ap.add_argument("--tls", action="store_true")
    ap.add_argument("--ca-cert", default=None)
    ap.add_argument("--prompt", default=DEFAULT_PROMPT, help="path to the agent init prompt")
    ap.add_argument("--provider", choices=sorted(PROVIDERS),
                    default=os.environ.get("AGENT_PROVIDER", "openrouter"),
                    help="openrouter = cloud models; local = Ollama/LM Studio/llama.cpp "
                         "chat-completions server on your machine (no key)")
    ap.add_argument("--model", default=None,
                    help="model id (openrouter: e.g. deepseek/deepseek-chat; "
                         "local: e.g. llama3.1:8b). Default per provider.")
    ap.add_argument("--base-url", default=None,
                    help="override the provider endpoint")
    ap.add_argument("--api-key-env", default="OPENROUTER_API_KEY",
                    help="env var holding the OpenRouter key")
    ap.add_argument("--max-steps", type=int, default=8)
    ap.add_argument("--autoconfirm", action="store_true",
                    help="approve destructive tools without asking (demo/CI only)")
    ap.add_argument("--dry-run", action="store_true",
                    help="no LLM: rule-based planner, to test the loop without an API key")
    ap.add_argument("--task", default=None, help="one-shot task, then exit")
    cfg = ap.parse_args()

    if not cfg.dry_run:
        resolve_llm(cfg)

    print(f"Connecting to C2 server {cfg.host}:{cfg.port} …")
    r = agent_tools.connect(cfg.host, cfg.port, cfg.token, cfg.tls, cfg.ca_cert)
    print(f"  {r['message']}")

    messages: list[dict] = []
    if not cfg.dry_run:
        messages.append({"role": "system", "content": build_system_prompt(cfg.prompt)})
        print(f"  Agent initialised from {os.path.basename(cfg.prompt)} "
              f"({len(agent_tools.REGISTRY)} tools, "
              f"provider={cfg.provider}, model={cfg.model} @ {cfg.base_url})")

    print("\n  Ask the agent to do something on the lab target. "
          "/tools, /events, /quit\n")

    def handle(task: str):
        if cfg.dry_run:
            run_task_dry(task, cfg)
        else:
            run_task_llm(task, messages, cfg)
        maybe_surface_events()

    if cfg.task:
        handle(cfg.task)
        return

    while True:
        try:
            task = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break
        if not task:
            continue
        low = task.lower()
        if low in ("/quit", "/exit", "quit", "exit"):
            break
        if low == "/tools":
            for t in agent_tools.tool_catalog():
                d = " [MUTATES — needs your approval]" if t["destructive"] else ""
                print(f"  {t['name']}: {t['description']}{d}")
            continue
        if low == "/events":
            maybe_surface_events()
            continue
        try:
            handle(task)
        except urllib.error.HTTPError as exc:
            print(f"[!] LLM error {exc.code}: {exc.read()[:300]!r}")
        except Exception as exc:  # keep the operator console alive
            print(f"[!] {type(exc).__name__}: {exc}")

    agent_tools.disconnect()


if __name__ == "__main__":
    main()
