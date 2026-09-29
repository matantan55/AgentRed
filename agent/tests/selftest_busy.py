"""Headless test: busy indicator lifecycle (start → phase changes → clears on idle).

Needs: mock_c2_server on :4444 AND a slow scripted LLM on :8899, e.g.
    SLEEP=1.2 SCENARIO=cpu python tests/mock_llm_server.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.argv = ["t"]
HERE = os.path.dirname(os.path.abspath(__file__))
import agent_tui as T
from textual.widgets import Input

loop = None

async def wait_until(app, pilot, pred, to=30, what="condition"):
    end = loop.time() + to
    while loop.time() < end:
        await pilot.pause(0.15)
        if pred(app):
            return
    raise AssertionError("timeout waiting for " + what)


def kinds(app):
    return [e[0] for e in app.transcript]


async def main():
    global loop
    loop = asyncio.get_running_loop()
    cfg = T.build_cfg(["--provider", "local", "--base-url", "http://127.0.0.1:8899/v1",
                       "--model", "scripted-test"])
    app = T.OperatorApp(cfg)
    async with app.run_test(size=(110, 45)) as pilot:
        # 1. startup: connecting spinner, then idle once the banner lands
        await wait_until(app, pilot, lambda a: "connecting to target…" in
                         [e[1] for e in a.transcript if e[0] == "busy"], what="connecting busy")
        print("✓ busy shows 'connecting to target…' on startup")
        await wait_until(app, pilot, lambda a: "idle" in kinds(a), what="startup idle")
        assert app._busy_timer is None, "timer should be stopped at idle"
        print("✓ idle clears the spinner timer")

        # 2. task: spinner active with 'thinking…' while the (slow) model answers
        inp = app.query_one(Input)
        inp.value = "which process is using the most cpu?"
        await pilot.press("enter")
        await wait_until(app, pilot, lambda a: "thinking…" in
                         [e[1] for e in a.transcript if e[0] == "busy"], what="thinking busy")
        await pilot.pause(0.4)
        assert app._busy_timer is not None, "spinner timer should run mid-task"
        assert not any(e[0] == "tool" for e in app.transcript), "no tool output yet"
        busy_text = str(app.query_one("#busy").render())
        app.save_screenshot(filename="tui_busy_preview.svg", path=HERE)
        print("✓ spinner live during model call; captured mid-busy screenshot")

        # 3. first output flips the phase and the run completes back to idle
        await wait_until(app, pilot, lambda a: any(e[0] == "tool" for e in a.transcript),
                         what="first tool output")
        idx_busy = [i for i, e in enumerate(app.transcript)
                    if e[0] == "busy" and str(e[1]).startswith("running top_cpu")]
        idx_tool = [i for i, e in enumerate(app.transcript) if e[0] == "tool"]
        assert idx_busy and idx_busy[-1] <= idx_tool[-1], "phase flips before/with tool line"
        await wait_until(app, pilot, lambda a: "final" in kinds(a) and "idle" in
                         kinds(a)[kinds(a).index("final"):], what="final then idle")
        assert app._busy_timer is None, "spinner stopped after task"
        print("✓ phase 'running top_cpu on the target…' → final → idle")
        print("BUSY INDICATOR: ALL CHECKS PASSED")


asyncio.run(main())
