"""A Terminal opened at login connects once it can, instead of dying until the app is restarted.

Reported 2026-10-09: "Terminal -> when auto start, it fails to connect until you restart app". A startup
app opens before everything it needs is ready, and every error was treated as a refusal (`want = false`,
never retried). Driven in the shipped bundle against a stand-in for the desktop's local-shell bridge
whose first start fails the way an IPC call fails while the desktop is still coming up.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

BRIDGE = r"""
window.__starts=0;
window.pcTerm={
  start: async()=>{ __starts++; if(__starts===1) throw new Error("Error invoking remote method 'pc:term:start': Error: not ready"); return {id:'t1'}; },
  attach: async()=>true, detach: ()=>{}, close: ()=>{}, write: ()=>{}, resize: ()=>{}, list: async()=>[],
  backlog: async(id,since)=>({d:'$ ', seq:2, alive:true, truncated:false}),
  onData: ()=>()=>{},
};
"""
REFUSED = BRIDGE.replace("throw new Error(\"Error invoking remote method 'pc:term:start': Error: not ready\")",
                         "throw new Error('too many terminals open')").replace("if(__starts===1)", "if(true)")


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def _open_terminal(b):
    await desktop.login(b)
    await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
    await b.js("__PC.switchView('terminal')")
    await b.until("!!document.getElementById('tty-state')")


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_shell_that_was_not_ready_is_retried_and_connects():
    got = {}

    async def check(b):
        await _open_terminal(b)
        for _ in range(60):
            st = await b.js("document.getElementById('tty-state').textContent")
            if "connected" in st.lower():
                break
            await asyncio.sleep(0.25)
        got["state"] = await b.js("document.getElementById('tty-state').textContent")
        got["starts"] = await b.js("__starts")
        got["errors"] = await b.js("__errors")

    asyncio.run(desktop.with_browser("online", "", check, BRIDGE))
    assert got["starts"] == 2, ("it never tried again after the first failure", got)
    assert "connected" in got["state"].lower(), ("still not connected after the retry", got)
    assert not got["errors"], got["errors"]


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_real_refusal_is_still_final():
    got = {}

    async def check(b):
        await _open_terminal(b)
        await asyncio.sleep(4)
        got["starts"] = await b.js("__starts")
        got["state"] = await b.js("document.getElementById('tty-state').textContent")

    asyncio.run(desktop.with_browser("online", "", check, REFUSED))
    assert got["starts"] == 1, ("a real refusal was retried", got)
    assert "too many terminals" in got["state"], got
