"""
agent_tui.py — Claude Code-style terminal UI for the C2 research operator.

Same agent as agent.py (OpenRouter / local model / --dry-run, human-confirmed
mutations, the 38-tool layer over client.py), but presented as a proper chat:
message log, ❯ prompt bar, ⏺ tool lines with collapsible ⎿ results, and
Approve/Deny panels for target mutations.

    .venv/bin/python agent_tui.py --token labtoken --dry-run
    .venv/bin/python agent_tui.py --token labtoken --provider openrouter --model deepseek/deep-chat
    .venv/bin/python agent_tui.py --token labtoken --provider local  --model llama3.1:8b

The blocking C2/LLM work runs in a Bridge thread; the Textal UI talks to it
through two queues, so the interface never freezes mid-request.

Educational / lab use only.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import threading
import time
import uuid
from types import SimpleNamespace

from rich.markup import escape as esc
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Collapsible, Input, Static

import agent as A            # reuse the proven agent.py internals
import agent_tools

LIME = "#a6e22e"   # hackery lime accent

HELP_LINES = [
    "/tools   list every tool the agent can operate",
    "/events  pull the victim push-event buffer into the chat",
    "/clear   clear the transcript",
    "/quit    disconnect and exit  (also Ctrl+Q)",
    "",
    "Plain language goes to the agent: 'what is eating CPU?', 'show me",
    "notes.txt on the machine', 'add a run key value' — anything that",
    "mutates the target stops first for an Approve/Deny panel.",
]


# ──────────────────────────────────────────────────────────────────────
# Bridge — the agent loop running off the UI thread
# ──────────────────────────────────────────────────────────────────────

class Bridge(threading.Thread):
    def __init__(self, cfg):
        super().__init__(daemon=True, name="agent-bridge")
        self.cfg = cfg
        self.out: queue.Queue[tuple] = queue.Queue()
        self.inbox: queue.Queue[str | None] = queue.Queue()
        self.stop = threading.Event()
        self._pending_confirm: dict[str, tuple[threading.Event, dict]] = {}
        self.messages: list[dict] = []
        if not cfg.dry_run:
            self.messages.append({"role": "system",
                                  "content": A.build_system_prompt(cfg.prompt)})

    # -- plumbing -------------------------------------------------------
    def emit(self, ev: tuple):
        self.out.put(ev)

    def submit_task(self, text: str):
        self.inbox.put(text)

    def confirm(self, cid: str, ok: bool):
        slot = self._pending_confirm.get(cid)
        if slot:
            slot[1]["ok"] = ok
            slot[0].set()

    def shutdown(self):
        self.stop.set()
        self.inbox.put(None)

    # -- worker ---------------------------------------------------------
    def run(self):
        self.emit(("busy", "connecting to target…"))
        try:
            r = agent_tools.connect(self.cfg.host, self.cfg.port, self.cfg.token,
                                    self.cfg.tls, self.cfg.ca_cert)
            self.emit(("connected", r["message"]))
        except ConnectionError as exc:
            # could be a refused socket OR a rejected token — the message itself
            # is now self-diagnosing either way (see agent_tools.connect)
            self.emit(("connerror", str(exc)))
        except Exception as exc:
            self.emit(("connerror", f"cannot reach C2 server at "
                                    f"{self.cfg.host}:{self.cfg.port} — {exc}"))
        self.emit(("idle",))
        while not self.stop.is_set():
            try:
                task = self.inbox.get(timeout=0.2)
            except queue.Empty:
                continue
            if task is None:
                break
            try:
                if task == "__events__":
                    self._drain_push()
                elif self.cfg.dry_run:
                    self._task_dry(task)
                else:
                    self._task_llm(task)
            except Exception as exc:
                self.emit(("error", f"{type(exc).__name__}: {exc}"))
            self.emit(("idle",))
            self._drain_push()

    # -- execution with the human gate ----------------------------------
    def _execute(self, name: str, args: dict) -> dict:
        self.emit(("busy", f"running {name} on the target…"))
        obs = agent_tools.run_tool(name, dict(args or {}))
        if obs.get("status") == "needs_confirmation":
            ok = self._ask_confirm(name, args)
            if ok:
                self.emit(("busy", f"running {name} on the target…"))
                obs = agent_tools.run_tool(name, dict(args or {}), confirm=True)
            else:
                obs = {"status": "denied_by_human",
                       "message": "The researcher declined this action. "
                                  "Choose a read-only alternative or ask what they would prefer instead."}
        return obs

    def _ask_confirm(self, name: str, args: dict) -> bool:
        cid = uuid.uuid4().hex[:6]
        ev, slot = threading.Event(), {"ok": False}
        self._pending_confirm[cid] = (ev, slot)
        self.emit(("confirm", cid, name, args))
        self.emit(("busy", "awaiting your approval…"))
        while not ev.wait(0.4):
            if self.stop.is_set():
                break
        self._pending_confirm.pop(cid, None)
        return slot["ok"]

    def _drain_push(self):
        try:
            ev = agent_tools.run_tool("events", {})
        except Exception:
            return
        for e in ev.get("events", []):
            self.emit(("push", e))

    # -- task loops (mirror of agent.py, event-driven) -------------------
    def _task_dry(self, task: str):
        self.emit(("busy", "planning (offline rules)…"))
        tried: set[str] = set()
        for _ in range(self.cfg.max_steps):
            planned = A.dry_plan(task, tried)
            if planned[0] is None:
                self.emit(("final", planned[1]))
                return
            name, args = planned
            tried.add(name)
            self.emit(("tool", name, args))
            obs = self._execute(name, args)
            self.emit(("observation", obs))
            if obs.get("status") == "denied_by_human":
                self.emit(("final", "Action declined by the operator — stopping here."))
                return
            if len(tried) >= 3 or name in ("file_read", "dir_list", "reg_read"):
                self.emit(("final", "The read-outs above answer the task (offline planner stops early)."))
                return

    def _task_llm(self, task: str):
        self.messages.append({"role": "user", "content": f"TASK: {task}"})
        for _ in range(self.cfg.max_steps):
            if self.stop.is_set():
                return
            self.emit(("busy", "thinking…"))
            try:
                reply = A.llm_call(self.cfg, self.messages)
            except Exception as exc:
                self.emit(("error", f"model call failed: {exc} — check --provider/--model/key"))
                return
            parsed = A.extract_json(reply)
            if parsed is None:
                self.emit(("info", "model reply was not a JSON action — nudging it"))
                self.messages += [{"role": "assistant", "content": reply},
                                  {"role": "user", "content":
                                   'Invalid reply. Respond with exactly one JSON object ({"action":...} or {"final":...}).'}]
                continue
            if parsed.get("thought"):
                self.emit(("thought", parsed["thought"]))
            if "final" in parsed:
                self.messages.append({"role": "assistant", "content": json.dumps(parsed)})
                self.emit(("final", str(parsed["final"])))
                return
            action = parsed.get("action") or {}
            name, args = action.get("tool"), action.get("args", {})
            if not name:
                self.messages += [{"role": "assistant", "content": json.dumps(parsed)},
                                  {"role": "user", "content":
                                   "Missing 'action.tool'. Reply with an action or a final."}]
                continue
            self.emit(("tool", name, args))
            obs = self._execute(name, args)
            self.emit(("observation", obs))
            self.messages += [{"role": "assistant", "content": json.dumps(parsed)},
                              {"role": "user", "content": f"OBSERVATION: {A.trim(obs)}"}]
        self.messages.append({"role": "user",
                              "content": "Step limit reached. Produce a final summary now."})
        try:
            parsed = A.extract_json(A.llm_call(self.cfg, self.messages)) or {"final": "step limit"}
            self.emit(("final", str(parsed.get("final", A.trim(parsed, 1500)))))
        except Exception as exc:
            self.emit(("error", f"model call failed at summary: {exc}"))


# ──────────────────────────────────────────────────────────────────────
# Chat widgets
# ──────────────────────────────────────────────────────────────────────

class ConfirmPanel(Vertical):
    def __init__(self, cid: str, tool_name: str, args: dict, **kw):
        super().__init__(**kw)
        self.cid, self.tool_name, self.args = cid, tool_name, args

    def compose(self) -> ComposeResult:
        yield Static(
            f"[{LIME}]⚠ MUTATION[/] — the agent wants to run [b]{esc(self.tool_name)}[/b] "
            f"with [dim]{esc(A.trim(self.args, 180))}[/dim]",
            classes="confirmline")
        yield Horizontal(
            Button("Approve", id=f"cf-{self.cid}-y", variant="success", classes="cbtn"),
            Button("Deny",    id=f"cf-{self.cid}-n", variant="error",   classes="cbtn"),
            classes="btnrow")

    def resolve(self, ok: bool):
        for b in self.query(Button):
            b.disabled = True
        self.mount(Static(
            f"[green]✔ approved by operator[/]" if ok else "[red]✘ denied by operator[/]",
            classes="resolved"))


# ──────────────────────────────────────────────────────────────────────
# The app
# ──────────────────────────────────────────────────────────────────────

class OperatorApp(App):
    TITLE = "SENTINEL — C2 Research Operator"
    CSS = f"""
    Screen {{ background: #0c110c; }}
    #banner {{ color: {LIME}; text-style: bold; padding: 0 1 0 1; }}
    #meta    {{ color: #78716b; padding: 0 1 1 1; }}
    #chat    {{ width: 100%; height: 1fr; padding: 0 1; }}
    #chat Static {{ width: 100%; }}
    .user   {{ color: #dcd8d3; }}
    .user .arrow {{ }}
    .thought {{ color: #78716b; text-style: italic; }}
    .tool   {{ color: #cfa96a; }}
    .obsbody {{ color: #8a847d; }}
    .final  {{ color: #e8e6e3; padding-bottom: 1; }}
    .push   {{ color: #b78ac9; }}
    .error  {{ color: #f25c54; }}
    .info, .conn {{ color: #78716b; }}
    .conn {{ color: #6f9f6f; }}
    .resolved {{ color: #78716b; }}
    ConfirmPanel {{ border: round {LIME}; padding: 0 1; margin-bottom: 1; }}
    .btnrow {{ height: auto; }}
    .cbtn {{ margin: 0 2 0 0; }}
    #promptrow {{ height: auto; border-top: solid #26332a; padding: 0 1; }}
    #prompt-sign {{ color: {LIME}; text-style: bold; width: 2; }}
    Input {{ width: 1fr; background: #0c110c; color: #e8e6e3;
             border: solid #26332a; }}
    Input:focus {{ border: solid {LIME}; }}
    #busy {{ width: auto; min-width: 0; max-width: 34; color: {LIME}; padding: 0 2; }}
    #hints {{ color: #5a5450; padding: 0 1; }}
    """
    BINDINGS = [Binding("ctrl+q", "quit", "Quit", priority=True)]

    def __init__(self, cfg, initial_task: str | None = None):
        super().__init__()
        self.cfg = cfg
        self.bridge = Bridge(cfg)
        self.initial_task = initial_task
        self.transcript: list[tuple] = []   # plain-data mirror for tests
        # busy-indicator state
        self._busy_phase = ""
        self._busy_t0 = 0.0
        self._busy_frame = 0
        self._busy_timer = None

    # -- layout -----------------------------------------------------------
    def compose(self) -> ComposeResult:
        brain = ("offline planner (dry-run)" if self.cfg.dry_run
                 else f"{self.cfg.provider} · {self.cfg.model}")
        yield Static("▚ SENTINEL — C2 Research Operator", id="banner")
        yield Static(f"target {self.cfg.host}:{self.cfg.port}   ·   {brain}   ·   "
                     "type /help — mutations always ask before they touch the box",
                     id="meta")
        yield VerticalScroll(id="chat")
        yield Horizontal(Static("❯", id="prompt-sign"),
                         Input(placeholder="Ask the agent to do something on the lab target…",
                               id="prompt"),
                         Static("", id="busy"),
                         id="promptrow")
        yield Static("Enter send · Ctrl+Q quit · ⏺ tool calls with ⎿ collapsible results",
                     id="hints")

    def on_mount(self) -> None:
        self.bridge.start()
        self.query_one(Input).focus()
        self.set_interval(0.12, self._drain_queue)
        if self.initial_task:
            self._handle_user_line(self.initial_task)

    # -- mount helpers ------------------------------------------------------
    def _mount(self, w):
        chat = self.query_one("#chat", VerticalScroll)
        chat.mount(w)
        try:
            chat.scroll_end(animate=False)
        except TypeError:
            chat.scroll_end()

    def _line(self, cls: str, markup: str):
        self.transcript.append((cls, markup))
        self._mount(Static(markup, classes=cls))

    # -- bridge event pump ---------------------------------------------------
    def _drain_queue(self):
        drained = False
        while True:
            try:
                ev = self.bridge.out.get_nowait()
            except queue.Empty:
                break
            drained = True
            self._render(ev)
        if drained and not self.bridge._pending_confirm:
            self.query_one(Input).focus()

    def _render(self, ev: tuple):
        kind = ev[0]
        if kind == "user":
            self._line("user", f"[{LIME}]❯[/] {esc(ev[1])}")
        elif kind == "thought":
            self._line("thought", f"  ⠿ {esc(ev[1])}")
        elif kind == "tool":
            self._line("tool", f"  ⏺ [b]{esc(ev[1])}[/b] {esc(A.trim(ev[2], 160))}")
        elif kind == "observation":
            obs = ev[1]
            status = str(obs.get("status", "?"))
            body = esc(A.trim(obs, 1200))
            tone = ("[green]" if status == "ok" else
                    "[red]" if status in ("error", "denied_by_human") else "[yellow]")
            self._line("tool",
                       f"  ⎿  {tone}{esc(status)}[/] "
                       f"[dim]{body[:110].replace(chr(10), ' ')}{'…' if len(body) > 110 else ''}[/dim]")
            self._mount(Collapsible(Static(body, classes="obsbody"),
                                    title=f"⎿ full result ({len(body)} chars)",
                                    collapsed=True))
        elif kind == "final":
            self.transcript.append(("final", ev[1]))
            self._mount(Static(f"[{LIME}]▚[/] {esc(ev[1])}", classes="final"))
        elif kind == "confirm":
            self.transcript.append(("confirm", ev[1], ev[2]))
            self._mount(ConfirmPanel(ev[1], ev[2], ev[3]))
        elif kind == "push":
            e = ev[1]
            self._line("push", f"  📡 victim push #{e.get('seq')} "
                               f"[dim]{esc(str(e.get('received_at', '')))}[/dim] "
                               f"[b]{esc(str(e.get('title', '')))}[/b] — {esc(str(e.get('body', '')))}")
        elif kind == "error":
            self._line("error", f"  ✘ {esc(ev[1])}")
        elif kind == "info":
            self._line("info", f"  ℹ {esc(ev[1])}")
        elif kind == "connected":
            self._line("conn", f"  ⚿ {esc(ev[1])}")
        elif kind == "connerror":
            self._line("error", f"  ⚿ {esc(ev[1])}")
        elif kind == "status":
            self.query_one("#hints", Static).update(
                ev[1] or "Enter send · Ctrl+Q quit · ⏺ tool calls with ⎿ collapsible results")
        elif kind == "busy":
            self._set_busy(ev[1])
        elif kind == "idle":
            self._set_idle()

    # -- busy spinner ---------------------------------------------------------
    BUSY_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def _set_busy(self, phase: str) -> None:
        self.transcript.append(("busy", phase))
        self._busy_phase = phase
        self._busy_t0 = time.monotonic()
        if self._busy_timer is None:
            self._busy_timer = self.set_interval(0.1, self._tick_busy)
        self._tick_busy()

    def _tick_busy(self) -> None:
        frame = self.BUSY_FRAMES[self._busy_frame % len(self.BUSY_FRAMES)]
        self._busy_frame += 1
        elapsed = int(time.monotonic() - self._busy_t0)
        try:
            self.query_one("#busy", Static).update(
                f"{frame} {esc(self._busy_phase)} · {elapsed}s")
        except Exception:
            pass    # widget gone (screen switch) — ignore

    def _set_idle(self) -> None:
        self.transcript.append(("idle",))
        if self._busy_timer is not None:
            self._busy_timer.stop()
            self._busy_timer = None
        try:
            self.query_one("#busy", Static).update("")
        except Exception:
            pass

    # -- user input -----------------------------------------------------------
    def _handle_user_line(self, text: str):
        self._render(("user", text))
        if text == "__events__":
            self.bridge.submit_task("__events__")
        else:
            self.bridge.submit_task(text)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        low = text.lower()
        if low in ("/quit", "/exit", "quit", "exit"):
            self.action_quit()
        elif low == "/help":
            self._render(("user", text))
            self._line("info", "\n".join(esc(l) for l in HELP_LINES))
        elif low == "/tools":
            self._render(("user", text))
            lines = []
            for t in agent_tools.tool_catalog():
                mark = "[yellow]⚠[/]" if t["destructive"] else "  "
                lines.append(f"  {mark} [b]{esc(t['name'])}[/b] — {esc(t['description'])}")
            self._line("info", "\n".join(lines))
        elif low == "/clear":
            self._render(("user", text))
            self.query_one("#chat", VerticalScroll).remove_children()
            self.transcript.clear()
        elif low == "/events":
            self._handle_user_line("__events__")
        else:
            self._handle_user_line(text)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id or ""
        parts = btn_id.split("-")          # cf-<cid>-<y|n>
        if len(parts) == 3 and parts[0] == "cf":
            ok = parts[2] == "y"
            panel, w = None, event.button
            while w is not None and not isinstance(w, ConfirmPanel):
                w = w.parent
            panel = w
            if panel:
                panel.resolve(ok)
            self.bridge.confirm(parts[1], ok)

    def action_quit(self) -> None:
        self.bridge.shutdown()
        self.exit()


# ──────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────

def build_cfg(argv: list[str]) -> SimpleNamespace:
    p = argparse.ArgumentParser(description="Claude Code-style TUI for the C2 research agent.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4444)
    p.add_argument("--token", default=os.environ.get("C2_AUTH_TOKEN", "labtoken"))
    p.add_argument("--tls", action="store_true")
    p.add_argument("--ca-cert", default=None)
    p.add_argument("--prompt", default=A.DEFAULT_PROMPT)
    p.add_argument("--provider", choices=sorted(A.PROVIDERS),
                    default=os.environ.get("AGENT_PROVIDER", "openrouter"))
    p.add_argument("--model", default=None)
    p.add_argument("--base-url", default=None)
    p.add_argument("--api-key-env", default="OPENROUTER_API_KEY")
    p.add_argument("--max-steps", type=int, default=8)
    p.add_argument("--dry-run", action="store_true",
                   help="offline rule planner instead of an LLM (UI demo without a key)")
    p.add_argument("--task", default=None, help="send this task immediately on startup")
    cfg = SimpleNamespace(**vars(p.parse_args(argv)))
    cfg.autoconfirm = False
    cfg.api_key = ""
    if not cfg.dry_run:
        A.resolve_llm(cfg)
    return cfg


def main():
    cfg = build_cfg([])
    OperatorApp(cfg, initial_task=cfg.task).run()


if __name__ == "__main__":
    main()
