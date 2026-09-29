"""Protocol-level smoke test for the c2-project MCP server.

Spawns c2_project_mcp.py over stdio using the official MCP SDK client,
lists tools, and calls every tool — exercising the real JSON-RPC path,
not just the Python functions.

Run:  <project>/mcp/.venv/bin/python mcp/smoke_test.py
"""
import asyncio
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE / "c2_project_mcp.py"
PY = sys.executable

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

CALLS = [
    ("project_overview", {}),
    ("list_modules", {}),
    ("module_detail", {"name": "shell"}),
    ("protocol_reference", {}),
    ("search_code", {"pattern": "Add-MpPreference", "max_hits": 5}),
    ("read_source", {"rel_path": "modules/ssh_module.py", "offset": 1, "limit": 20}),
    ("read_source", {"rel_path": "../../etc/passwd"}),          # must be refused
    ("local_activity", {"action": "system_stats"}),
    ("hunting_notes", {"topic": "defender_tampering"}),
]


def text_of(result) -> str:
    parts = []
    for c in (result.content or []):
        if getattr(c, "type", "") == "text":
            parts.append(c.text)
    return "\n".join(parts)


async def main() -> int:
    params = StdioServerParameters(command=PY, args=[str(SERVER)])
    failures = 0
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print(f"connected — {len(names)} tools: {names}")
            for name, args in CALLS:
                try:
                    res = await session.call_tool(name, args)
                    txt = text_of(res)
                    err = getattr(res, "isError", False) or "Error executing tool" in txt
                    first = txt[:90].replace("\n", " ")
                    if err:
                        failures += 1
                    print(f"[{'FAIL' if err else 'ok  '}] {name}({json.dumps(args)[:50]}) "
                          f"-> {len(txt)} chars: {first}")
                except Exception as exc:
                    failures += 1
                    print(f"[FAIL] {name} raised {exc!r}")
            # path-escape check semantics
            res = await session.call_tool("read_source", {"rel_path": "../../etc/passwd"})
            ok = "escapes" in text_of(res) or "error" in text_of(res)
            print(f"[{'ok  ' if ok else 'FAIL'}] read_source path jail enforced")
            failures += 0 if ok else 1
    print("RESULT:", "ALL PASS" if failures == 0 else f"{failures} FAILURES")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
