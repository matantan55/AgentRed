"""Renders agent_tui to an SVG preview: a dry task plus a pending Approve/Deny panel."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.argv = ["preview"]
HERE = os.path.dirname(os.path.abspath(__file__))
import agent_tui as T

async def main():
    cfg = T.build_cfg(["--token", "labtoken", "--dry-run"])
    app = T.OperatorApp(cfg)
    from textual.widgets import Input
    async with app.run_test(size=(116, 44)) as pilot:
        loop = asyncio.get_event_loop()
        def wait_for(pred, timeout=20):
            end = loop.time() + timeout
            async def _w():
                while loop.time() < end:
                    await pilot.pause(0.15)
                    if pred(app):
                        return
                raise AssertionError("timeout")
            return _w()
        inp = app.query_one(Input)
        await wait_for(lambda a: any(k == "conn" for k, *_ in a.transcript))
        inp.value = "what is eating CPU on the machine?"
        await pilot.press("enter")
        await wait_for(lambda a: "final" in [k for k, *_ in a.transcript])
        await pilot.pause(0.4)
        inp.value = "add a run key entry"
        await pilot.press("enter")
        await wait_for(lambda a: "confirm" in [k for k, *_ in a.transcript])
        await pilot.pause(0.6)
        path = app.save_screenshot(filename="tui_preview.svg", path=HERE)
        print("saved:", path)
        # deny the pending panel so the thread can exit cleanly
        cid = [e[1] for e in app.transcript if e[0] == "confirm"][-1]
        app.query_one(f"#cf-{cid}-n").press()
        await pilot.pause(0.5)
asyncio.run(main())
