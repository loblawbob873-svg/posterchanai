#!/usr/bin/env python3
"""Prove installed PosterChanOS can cover a real native application.

Run against an authenticated installed Electron renderer exposed on a loopback-only CDP port.
The machine must already have a harmless Firefox or Telegram window. No native-window pixels,
titles, profile data, or page contents leave the machine; this checks only compositor state.
"""

import asyncio
import json
import os
import sys
import urllib.error

from check_installed_desktop_account import CDP, choose_authenticated_page


CLICK = r"""(selector=>{
  const e=document.querySelector(selector);if(!e)return false;const r=e.getBoundingClientRect();
  e.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,button:0,pointerId:1,
    clientX:r.left+10,clientY:r.top+10}));
  e.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,button:0,pointerId:1}));
  e.click();return true;
})"""

SETUP = r"""(async()=>{
  await PCOS.refresh();await new Promise(r=>setTimeout(r,800));
  const snap=await pcWM.snapshot(),allowed=/firefox|telegram/i;
  const row=(snap.windows||[]).find(w=>allowed.test(String(w.app||'')));
  if(!row)return {skip:true};
  const created=!!PCOS.routeView('global');await new Promise(r=>setTimeout(r,500));
  return {id:Number(row.id),initiallyStashed:!!row.stashed,created,
    nativeFrames:document.querySelectorAll('.osw-native').length,
    htmlFrames:document.querySelectorAll('.osw:not(.osw-native)').length};
})()"""

STATE = r"""(async id=>{
  const frame=document.querySelector('.osw-native'),snap=await pcWM.snapshot(),row=(snap.windows||[])
    .find(w=>Number(w.id)===Number(id));
  return {nativeFocused:!!(frame&&frame.classList.contains('focused')),
    nativeStashed:!!(frame&&frame.classList.contains('native-stashed')),
    compositorFocused:!!(row&&row.focused),compositorStashed:!!(row&&row.stashed),
    shellFocused:(snap.windows||[]).some(w=>/^posterchan(-desktop)?$/i.test(String(w.app||''))&&w.focused)};
})"""

CLEANUP = r"""(async info=>{
  if(info.created){const close=document.querySelector('.osw:not(.osw-native).focused .osw-x');
    if(close)close.click();}
  if(info.initiallyStashed)await pcWM.hide(info.id);else await pcWM.focus(info.id);
  return true;
})"""


async def main():
    page = await choose_authenticated_page()
    async with CDP(page["webSocketDebuggerUrl"]) as cdp:
        info = await cdp.eval(SETUP)
        if info.get("skip"):
            print("SKIP no Firefox or Telegram surface is available for installed focus testing")
            return 2
        assert info["nativeFrames"] > 0 and info["htmlFrames"] > 0, info
        try:
            assert await cdp.eval(CLICK + "('.osw-native .osw-bar')")
            await asyncio.sleep(1.2)
            native = await cdp.eval(STATE + f"({json.dumps(info['id'])})")
            assert native["nativeFocused"] and native["compositorFocused"], native
            assert not native["nativeStashed"] and not native["compositorStashed"], native

            assert await cdp.eval(CLICK + "('.osw:not(.osw-native) .osw-bar')")
            await asyncio.sleep(1.2)
            covered = await cdp.eval(STATE + f"({json.dumps(info['id'])})")
            assert covered["shellFocused"], covered
            assert not covered["nativeFocused"] and not covered["compositorFocused"], covered
            assert covered["nativeStashed"] and covered["compositorStashed"], covered
        finally:
            await cdp.eval(CLEANUP + f"({json.dumps(info)})")

    print("OK installed native app yields to an overlapping PosterChan window")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except (urllib.error.URLError, ConnectionRefusedError) as exc:
        print("SKIP installed Electron is not attached on the loopback CDP port: " + str(exc))
        raise SystemExit(2)
