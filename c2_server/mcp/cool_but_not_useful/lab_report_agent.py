#!/usr/bin/env python3
"""
Lab Report Agent — offline analysis agent for the c2_server lab framework.

What it does
    Parses server.py's own audit trail (c2_server.log), reconstructs
    operator/agent sessions from connection→auth→dispatch→disconnect
    timelines, correlates relay attempts and push events, flags anomalies
    (auth failures, relays with no agent, unexpected agent responses),
    and writes a plain-markdown session report.

    It is READ-ONLY over local files. It never connects to the C2 socket,
    never authenticates as operator or agent, and never dispatches a
    command anywhere. This is a post-hoc analyzer for traffic your own
    server already logged.

Usage
    python3 lab_report_agent.py report [--log PATH] [--json] [--out PATH]
    python3 lab_report_agent.py follow [--log PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

DEFAULT_LOG = Path(__file__).resolve().parent.parent / "c2_server.log"
REPORT_DIR = Path(__file__).resolve().parent / "reports"

# ── log line grammar (matches server.py logging.basicConfig format) ──
_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+\[(?P<lvl>\w+)\]\s+(?P<msg>.*)$"
)

EV = {
    "new_conn":  re.compile(r"^New connection from (?P<peer>[\d.]+:\d+)$"),
    "auth_ok":   re.compile(r"^Authenticated: (?P<peer>[\d.]+):(?P<port>\d+)\s+role=(?P<role>\w+)$"),
    "auth_bad":  re.compile(r"^Auth failure from (?P<peer>[\d.]+:\d+)$"),
    "dispatch":  re.compile(r"^Dispatch module=(?P<module>.+) action=(?P<action>.+) by (?P<peer>[\d.]+:\d+)$"),
    "dispatch_err": re.compile(r"^Module (?P<module>.+) raised: (?P<rest>.*)$"),
    "relay":     re.compile(r"^Relay module=(?P<module>.+) action=(?P<action>.+) → agent$"),
    "push":      re.compile(r"^Push event (?P<event>.+) from agent (?P<peer>[\d.]+:\d+)$"),
    "unexpected": re.compile(r"^Unexpected agent response \(no pending relay\): (?P<resp>.*)$"),
    "disconnect": re.compile(r"^Disconnected: (?P<peer>[\d.]+:\d+)\s+\[role=(?P<role>\w+)\]$"),
    "mod_load":  re.compile(r"^Loaded modules: (?P<mods>.+)$"),
    "listen_tls": re.compile(r"^C2 server \(TLS\) listening on (?P<host>[\d.]+):(?P<port>\d+)$"),
    "listen_plaintext": re.compile(r"^C2 server \(NO TLS\) on (?P<host>[\d.]+):(?P<port>\d+)"),
    "shutdown":  re.compile(r"^Shutting down\.$"),
    "conn_closed": re.compile(r"^Connection closed by (?P<peer>[\d.]+:\d+)$"),
}


def _parse_ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S,%f")


@dataclass
class Event:
    ts: datetime
    level: str
    kind: str
    fields: dict = field(default_factory=dict)
    raw: str = ""

    def key(self, name): return self.fields.get(name)


@dataclass
class Peer:
    peer: str
    role: str | None = None
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    auth_ok: int = 0
    auth_fail: int = 0
    closed_by_peer: bool = False
    shut_cleanly: bool = False
    events: list[Event] = field(default_factory=list)


@dataclass
class Anomaly:
    ts: datetime
    kind: str
    detail: str


def parse_log(path: Path) -> list[Event]:
    out: list[Event] = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            m = _LINE.match(line)
            if not m:
                # multi-line traceback continuation or noise — attach as raw
                continue
            ts, lvl, msg = m["ts"], m["lvl"], m["msg"]
            for kind, rx in EV.items():
                mm = rx.match(msg)
                if mm:
                    out.append(Event(_parse_ts(ts), lvl, kind,
                                     {k: v.strip("'\" ") for k, v in mm.groupdict().items()},
                                     msg))
                    break
            else:
                out.append(Event(_parse_ts(ts), lvl, "other", {}, msg))
    return out


def build_report(events: list[Event]) -> dict:
    peers: dict[str, Peer] = {}
    anomalies: list[Anomaly] = []
    dispatches: list[Event] = []
    relays: list[Event] = []
    pushes: list[Event] = []
    server_meta: dict = {}
    current_agent = None  # last authenticated agent peer

    def peer(p: str) -> Peer:
        if p not in peers:
            peers[p] = Peer(peer=p)
        return peers[p]

    for e in events:
        if e.kind == "listen_plaintext":
            server_meta = {"tls": False, "bind": f"{e.key('host')}:{e.key('port')}"}
        elif e.kind == "listen_tls":
            server_meta = {"tls": True, "bind": f"{e.key('host')}:{e.key('port')}"}
        elif e.kind == "mod_load":
            server_meta["modules"] = e.key("mods")
        elif e.kind == "new_conn":
            p = peer(e.key("peer")); p.first_seen = p.first_seen or e.ts; p.last_seen = e.ts
        elif e.kind == "auth_ok":
            full = f"{e.key('peer')}:{e.key('port')}"   # join ip+port like other events
            p = peer(full); p.role = e.key("role"); p.auth_ok += 1
            p.first_seen = p.first_seen or e.ts; p.last_seen = e.ts
            if e.key("role") == "agent":
                current_agent = full
        elif e.kind == "auth_bad":
            p = peer(e.key("peer")); p.auth_fail += 1; p.last_seen = e.ts
            anomalies.append(Anomaly(e.ts, "auth_failure", e.raw))
        elif e.kind == "dispatch":
            dispatches.append(e); peer(e.key("peer")).last_seen = e.ts
        elif e.kind == "relay":
            relays.append(e)
            if current_agent is None:
                anomalies.append(Anomaly(e.ts, "relay_without_agent",
                                         f"relay to module {e.key('module')!r} while no agent was authenticated"))
        elif e.kind == "push":
            pushes.append(e); peer(e.key("peer")).last_seen = e.ts
        elif e.kind == "unexpected":
            anomalies.append(Anomaly(e.ts, "unexpected_agent_response", e.key("resp") or e.raw))
        elif e.kind == "dispatch_err":
            anomalies.append(Anomaly(e.ts, "module_error", e.raw))
        elif e.kind == "disconnect":
            p = peer(e.key("peer")); p.shut_cleanly = True; p.last_seen = e.ts
            if p.role == "agent" and current_agent == p.peer:
                current_agent = None
        elif e.kind == "conn_closed":
            p = peer(e.key("peer")); p.closed_by_peer = True

    by_module: dict[str, int] = defaultdict(int)
    for e in dispatches:
        by_module[f"{e.key('module')}/{e.key('action')}"] += 1
    by_relay: dict[str, int] = defaultdict(int)
    for e in relays:
        by_relay[f"{e.key('module')}/{e.key('action')}"] += 1

    return {
        "log_span": [str(events[0].ts), str(events[-1].ts)] if events else None,
        "server": server_meta,
        "peers": [
            {k: (str(v) if isinstance(v, datetime) else v) for k, v in vars(p).items()
             if k != "events"}
            for p in peers.values()
        ],
        "dispatches_local": dict(by_module),
        "relays_to_agent": dict(by_relay),
        "push_events": [e.key("event") for e in pushes],
        "anomalies": [{"ts": str(a.ts), "kind": a.kind, "detail": a.detail} for a in anomalies],
        "counts": {"lines_parsed": len(events), "peers": len(peers),
                   "dispatches": len(dispatches), "relays": len(relays),
                   "pushes": len(pushes), "anomalies": len(anomalies)},
    }


def render_md(r: dict, log_path: Path) -> str:
    L: list[str] = []
    L.append("# c2_server session report")
    L.append("")
    L.append(f"- source log: `{log_path}`")
    if r["log_span"]:
        L.append(f"- log window: {r['log_span'][0]} → {r['log_span'][1]}")
    s = r["server"]
    if s:
        L.append(f"- server bind: {s.get('bind','?')}  ({'TLS' if s.get('tls') else 'NO TLS — cleartext token+traffic'})")
        if s.get("modules"):
            L.append(f"- server-side modules: {s['modules']}")
    c = r["counts"]
    L.append(f"- lines parsed: {c['lines_parsed']}  |  peers: {c['peers']}  |  "
             f"local dispatches: {c['dispatches']}  |  agent relays: {c['relays']}  |  "
             f"push events: {c['pushes']}")
    L.append("")

    L.append("## Peers")
    L.append("")
    L.append("| peer | role | auth ok | auth fail | first seen | last seen |")
    L.append("|---|---|---:|---:|---|---|")
    for p in sorted(r["peers"], key=lambda x: str(x["first_seen"])):
        L.append(f"| {p['peer']} | {p['role'] or '—'} | {p['auth_ok']} | {p['auth_fail']} "
                 f"| {p['first_seen'] or '—'} | {p['last_seen'] or '—'} |")
    L.append("")

    if r["dispatches_local"]:
        L.append("## Dispatched locally (server-side modules)")
        for k, v in sorted(r["dispatches_local"].items(), key=lambda kv: -kv[1]):
            L.append(f"- {k}: {v}")
        L.append("")
    if r["relays_to_agent"]:
        L.append("## Relayed to agent (unknown module → dll.cpp)")
        for k, v in sorted(r["relays_to_agent"].items(), key=lambda kv: -kv[1]):
            L.append(f"- {k}: {v}")
        L.append("")
    if r["push_events"]:
        from collections import Counter
        L.append("## Push events from agent")
        for k, v in Counter(r["push_events"]).most_common():
            L.append(f"- {k}: {v}")
        L.append("")

    L.append("## Anomalies")
    if not r["anomalies"]:
        L.append("- none detected")
    for a in r["anomalies"]:
        L.append(f"- `{a['ts']}` **{a['kind']}** — {a['detail']}")
    L.append("")

    L.append("## Analyst notes (mapped to the code)")
    L.append("- `auth_failure` spikes ⇒ token guess / misconfig; server logs only peer IP.")
    L.append("- `relay_without_agent` ⇒ operator tried an agent-side module (notify/shell/ssh via DLL)")
    L.append("  while no agent socket existed — server.py answers 'No agent connected'.")
    L.append("- `unexpected_agent_response` ⇒ relay race in server.py (response arrived after its")
    L.append("  pending queue was cleared — commands share one un-ID'd channel per agent).")
    L.append("- relays all bind to the FIRST connected agent (`agents[0]`) regardless of peer count.")
    L.append("")
    return "\n".join(L)


def cmd_report(args) -> int:
    log = Path(args.log)
    if not log.exists():
        print(f"error: log file not found: {log}", file=sys.stderr)
        print("hint: server.py writes c2_server.log to its CWD — pass --log explicitly", file=sys.stderr)
        return 2
    events = parse_log(log)
    r = build_report(events)
    out = Path(args.out) if args.out else REPORT_DIR / (
        "session_report_" + time.strftime("%Y%m%d_%H%M%S") + ".md")
    out.parent.mkdir(parents=True, exist_ok=True)
    if args.json:
        out = out.with_suffix(".json")
        out.write_text(json.dumps(r, indent=2), encoding="utf-8")
    else:
        out.write_text(render_md(r, log), encoding="utf-8")
    print(f"report written: {out}")
    print(f"counts: {r['counts']}")
    return 0


def cmd_follow(args) -> int:
    log = Path(args.log)
    if not log.exists():
        print(f"error: log file not found: {log}", file=sys.stderr)
        return 2
    print(f"following {log} — Ctrl-C to stop", file=sys.stderr)
    with log.open(encoding="utf-8", errors="replace") as fh:
        fh.seek(0, os.SEEK_END)
        while True:
            line = fh.readline()
            if not line:
                # detect truncation/restart
                try:
                    if fh.tell() > log.stat().st_size:
                        fh.seek(0)
                except OSError:
                    pass
                time.sleep(0.3)
                continue
            m = _LINE.match(line.rstrip("\n"))
            if not m:
                continue
            for kind, rx in EV.items():
                mm = rx.match(m["msg"])
                if mm:
                    groups = ", ".join(f"{k}={v.strip(chr(39) + chr(34))}"
                                       for k, v in mm.groupdict().items())
                    print(f"{m['ts']} {kind:<14} {groups}", flush=True)
                    break


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Offline analysis agent for c2_server lab logs")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report", help="one-shot session report from the log")
    r.add_argument("--log", default=str(DEFAULT_LOG))
    r.add_argument("--json", action="store_true", help="emit raw JSON instead of markdown")
    r.add_argument("--out", default=None, help="output path (default mcp/reports/session_report_<ts>.md)")
    r.set_defaults(func=cmd_report)
    f = sub.add_parser("follow", help="live tail: print parsed events as they arrive")
    f.add_argument("--log", default=str(DEFAULT_LOG))
    f.set_defaults(func=cmd_follow)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
