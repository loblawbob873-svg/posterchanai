#!/usr/bin/env python3
"""Exercise Admin Relay → Preview auto-clean without letting PosterChanOS fall into Classic.

Attach this to an authenticated, disposable installed Electron process.  The operation is the real
dry run: it only counts the notes the configured cleaner would remove.  No filenames, identities,
tokens, note contents, or count leave the process; this verifier reports only boolean invariants.
"""

import asyncio
import json
import os
import sys
import urllib.error
import urllib.request

from check_installed_desktop_account import CDP, choose_test_page


PORT = int(os.environ.get("PC_CHECK_PORT", "9223"))
BASE = f"http://127.0.0.1:{PORT}"


async def targets():
    return json.load(urllib.request.urlopen(BASE + "/json/list", timeout=5))


async def parent_page():
    # Use the same guarded throwaway-login path as the installed Files/Office gate. Release checks
    # run in an isolated diagnostic profile and must not require an operator's personal session.
    # choose_test_page refuses arbitrary key paths and first proves that host identity mutation is
    # disabled, so sharing it does not weaken this gate's authority boundary.
    page = await choose_test_page()
    async with CDP(page["webSocketDebuggerUrl"]) as cdp:
        ok = await cdp.eval(
            "!!(window.__PC&&__PC.me&&__PC.me()&&window.PCOS&&window.PCOSShell"
            "&&PCOSShell.available())")
    if not ok:
        raise RuntimeError("the authenticated installed page is not a PosterChanOS shell")
    return page


async def admin_frame():
    for _ in range(120):
        for page in await targets():
            if page.get("type") == "iframe" and "/admin" in page.get("url", ""):
                return page
        await asyncio.sleep(0.1)
    raise RuntimeError("the installed Admin iframe did not load")


OPEN_ADMIN = r"""(async()=>{
  if(!PCOS.isOn())PCOS.enter();
  PCOS.routeView('settings');await __PC.switchView('settings');await new Promise(r=>setTimeout(r,250));
  await __PC.switchView('admin');
  for(let i=0;i<120&&!document.querySelector('#admin-host iframe[data-loaded="1"]');i++)
    await new Promise(r=>setTimeout(r,100));
  return {on:PCOS.isOn(),root:!!document.querySelector('#os-root'),view:__PC.VIEW,
    host:!!document.querySelector('#admin-host iframe[data-loaded="1"]')};
})()"""


# Reproduce the compositor/display race deterministically while the dry-run request is active.  The
# descriptor is restored in the same JavaScript turn even when an assertion later fails, so the
# diagnostic cannot leave its renderer at a fake width.
NARROW_RESIZE = r"""(async()=>{
  const d=Object.getOwnPropertyDescriptor(window,'innerWidth');
  try{
    Object.defineProperty(window,'innerWidth',{value:900,configurable:true});
    dispatchEvent(new Event('resize'));await new Promise(r=>setTimeout(r,150));
    return {on:PCOS.isOn(),root:!!document.querySelector('#os-root'),
      osClass:document.body.classList.contains('os-on'),view:__PC.VIEW};
  }finally{
    if(d)Object.defineProperty(window,'innerWidth',d);else delete window.innerWidth;
    dispatchEvent(new Event('resize'));
  }
})()"""


PARENT_STATE = r"""(()=>{const host=document.querySelector('#admin-host'),frame=host&&host.closest('.osw');
  const owner=PCOS.windows().find(w=>w.view==='settings');return {
  on:PCOS.isOn(),root:!!document.querySelector('#os-root'),
  osClass:document.body.classList.contains('os-on'),view:__PC.VIEW,
  host:!!host&&getComputedStyle(host).display!=='none',
  adminWindow:!!frame,focused:!!frame&&frame.classList.contains('focused'),
  ownerView:owner&&owner.appView};})()"""


RETURN_SETTINGS = r"""(async()=>{PCOS.routeView('settings');await __PC.switchView('settings');
  await new Promise(r=>setTimeout(r,100));const host=document.querySelector('#admin-host'),
  owner=PCOS.windows().find(w=>w.view==='settings'),frame=host&&host.closest('.osw');return {
  on:PCOS.isOn(),view:__PC.VIEW,hostHidden:!!host&&getComputedStyle(host).display==='none',
  focused:!!frame&&frame.classList.contains('focused'),ownerView:owner&&owner.appView};})()"""


async def main():
    parent = await parent_page()
    async with CDP(parent["webSocketDebuggerUrl"]) as cdp:
        opened = await cdp.eval(OPEN_ADMIN)
    assert opened == {"on": True, "root": True, "view": "admin", "host": True}, opened

    frame = await admin_frame()
    ever_off = False
    ever_wrong_route = False
    ever_lost_host = False
    ever_lost_focus = False
    ever_lost_owner = False
    async with CDP(parent["webSocketDebuggerUrl"]) as outer, CDP(frame["webSocketDebuggerUrl"]) as inner:
        ready = await inner.eval(r"""(async()=>{
          const auth=await Promise.race([window.__pcAdminAuth,new Promise(r=>setTimeout(()=>r(false),5000))]);
          const tab=document.querySelector('.tab-btn[data-tab="relay"]');if(tab)tab.click();
          const b=document.querySelector('#relayPruneDryBtn');
          return {auth:auth===true,button:!!b,type:b&&b.type};
        })()""")
        assert ready == {"auth": True, "button": True, "type": "button"}, ready
        assert await inner.eval("(document.querySelector('#relayPruneDryBtn').click(),true)")

        narrow = await outer.eval(NARROW_RESIZE)
        assert narrow == {"on": True, "root": True, "osClass": True, "view": "admin"}, narrow

        complete = False
        failed = False
        for _ in range(360):
            state = await outer.eval(PARENT_STATE)
            ever_off |= not (state["on"] and state["root"] and state["osClass"])
            # A recovered final frame is not enough. The regression was a transient repaint while
            # the request was active, so sample every invariant in the same loop as shell survival.
            ever_wrong_route |= state["view"] != "admin"
            ever_lost_host |= not (state["host"] and state["adminWindow"])
            ever_lost_focus |= not state["focused"]
            ever_lost_owner |= state["ownerView"] != "admin"
            result = await inner.eval(r"""(()=>{const b=document.querySelector('#relayPruneDryBtn'),
              s=document.querySelector('#relayPruneStatus');return {disabled:!!(b&&b.disabled),
              text:String((s&&s.textContent)||'')}})()""")
            failed |= result["text"].lstrip().startswith("✗")
            if not result["disabled"] and result["text"] and result["text"] != "counting…":
                complete = True
                break
            await asyncio.sleep(0.5)

        state = await outer.eval(PARENT_STATE)
        assert complete and not failed, "Preview auto-clean did not complete successfully"
        assert not ever_off, "Preview auto-clean exposed Classic mode while its dry run was active"
        assert not ever_wrong_route, "Preview auto-clean temporarily left the Admin route"
        assert not ever_lost_host, "Preview auto-clean temporarily lost its Admin window host"
        assert not ever_lost_focus, "Preview auto-clean temporarily lost its focused window"
        assert not ever_lost_owner, "Preview auto-clean temporarily changed its Settings-window owner"
        assert state == {"on": True, "root": True, "osClass": True, "view": "admin",
                         "host": True, "adminWindow": True, "focused": True,
                         "ownerView": "admin"}, state
        returned = await outer.eval(RETURN_SETTINGS)
        assert returned == {"on": True, "view": "settings", "hostHidden": True,
                            "focused": True, "ownerView": "settings"}, returned

    print("OK installed Admin Preview auto-clean stayed in its PosterChanOS window")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except (urllib.error.URLError, ConnectionRefusedError) as exc:
        print("SKIP installed Electron is not attached on the loopback CDP port: " + str(exc))
        raise SystemExit(2)
