# c2_server session report

- source log: `/Users/matanmishali/Downloads/c2_server/c2_server.log`
- log window: 2026-09-29 13:40:13.677000 → 2026-09-29 13:40:15.513000
- server bind: 127.0.0.1:4555  (NO TLS — cleartext token+traffic)
- server-side modules: ['ssh', 'rdp', 'registry', 'activity']
- lines parsed: 19  |  peers: 3  |  local dispatches: 2  |  agent relays: 2  |  push events: 1

## Peers

| peer | role | auth ok | auth fail | first seen | last seen |
|---|---|---:|---:|---|---|
| 127.0.0.1:52108 | — | 0 | 1 | 2026-09-29 13:40:14.633000 | 2026-09-29 13:40:14.634000 |
| 127.0.0.1:52109 | operator | 1 | 0 | 2026-09-29 13:40:14.634000 | 2026-09-29 13:40:15.513000 |
| 127.0.0.1:52110 | agent | 1 | 0 | 2026-09-29 13:40:14.698000 | 2026-09-29 13:40:15.513000 |

## Dispatched locally (server-side modules)
- activity/processes: 1
- registry/list_keys: 1

## Relayed to agent (unknown module → dll.cpp)
- notify/start: 1
- shell/status: 1

## Push events from agent
- notification: 1

## Anomalies
- `2026-09-29 13:40:14.633000` **auth_failure** — Auth failure from 127.0.0.1:52108
- `2026-09-29 13:40:14.698000` **relay_without_agent** — relay to module 'notify' while no agent was authenticated
- `2026-09-29 13:40:15.004000` **unexpected_agent_response** — {'status': 'ok', 'note': 'stray response, no pending relay'}

## Analyst notes (mapped to the code)
- `auth_failure` spikes ⇒ token guess / misconfig; server logs only peer IP.
- `relay_without_agent` ⇒ operator tried an agent-side module (notify/shell/ssh via DLL)
  while no agent socket existed — server.py answers 'No agent connected'.
- `unexpected_agent_response` ⇒ relay race in server.py (response arrived after its
  pending queue was cleared — commands share one un-ID'd channel per agent).
- relays all bind to the FIRST connected agent (`agents[0]`) regardless of peer count.
