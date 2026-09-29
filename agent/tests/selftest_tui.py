"""Headless pilot test for agent_tui.OperatorApp (needs mock_c2_server on :4444)."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.argv = ["test"]
import agent_tui as T
from textual.widgets import Button, Input


def kinds(app):
    return [k for k, *_ in app.transcript]


async def wait_for(app, pilot, pred, timeout=25.0, what="condition"):
    loop = asyncio.get_event_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        await pilot.pause(0.2)
        if pred(app):
            return True
    raise AssertionError(f"timeout waiting for {what}")


async def run():
    cfg = T.build_cfg(["--token", "labtoken", "--dry-run"])
    app = T.OperatorApp(cfg)
    inp = None
    async with app.run_test(size=(110, 45)) as pilot:
        # 1. startup + connection banner
        inp = app.query_one(Input)
        await wait_for(app, pilot, lambda a: "conn" in kinds(a), what="connection line")
        print("✓ connected banner rendered")

        # 2. /help
        inp.value = "/help"
        await pilot.press("enter")
        await wait_for(app, pilot, lambda a: any("every tool" in str(e) for e in a.transcript),
                        what="/help output")
        print("✓ /help renders")

        # 3. read-only task: tool lines + collapsible observations + final
        inp.value = "show top cpu"
        await pilot.press("enter")
        await wait_for(app, pilot, lambda a: "final" in kinds(a), what="first final")
        tool_names = [str(e[1]) for e in app.transcript if e[0] == "tool"]
        assert any("top_cpu" in n for n in tool_names), tool_names
        assert any("processes" in n for n in tool_names), tool_names
        obs = [e for e in app.transcript if e[0] == "tool" and "⎿" in str(e[1])]
        assert obs, "no ⎿ observation lines"
        print("✓ dry read task ran: top_cpu + processes, ⎿ observations, final")

        # 4. mutation → Approve panel → approved → write executes
        n_finals = kinds(app).count("final")
        inp.value = "add a run key entry"
        await pilot.press("enter")
        await wait_for(app, pilot, lambda a: "confirm" in kinds(a), what="confirm panel")
        cid = next(e[1] for e in app.transcript if e[0] == "confirm")
        yes = app.query_one(f"#cf-{cid}-y", Button)
        yes.press()
        await wait_for(app, pilot, lambda a: kinds(app).count("final") > n_finals,
                        what="post-approval final")
        wr = [str(e[1]) for e in app.transcript if e[0] == "tool" and "reg_write" in str(e[1])]
        assert wr, "reg_write line missing"
        assert app.query_one(f"#cf-{cid}-y", Button).disabled, "approve button still live"
        print("✓ Approve gate: panel → button disabled → mutation executed")

        # 5. mutation again → Deny → nothing executes
        cid2_seen = [e[1] for e in app.transcript if e[0] == "confirm"]
        inp.value = "add a run key entry"
        await pilot.press("enter")
        await wait_for(app, pilot, lambda a: [e[1] for e in app.transcript if e[0] == "confirm"] != cid2_seen,
                        what="second confirm panel")
        cid2 = [e[1] for e in app.transcript if e[0] == "confirm"][-1]
        app.query_one(f"#cf-{cid2}-n", Button).press()
        await wait_for(app, pilot, lambda a: any("declined" in str(e[1]) for e in app.transcript if e[0] == "final"),
                        what="denied final")
        print("✓ Deny gate: panel → denied_by_human fed back, task stopped")

        # 6. /quit binding path
        await pilot.press("ctrl+q")
    print("TUI PILOT: ALL CHECKS PASSED")


asyncio.run(run())
