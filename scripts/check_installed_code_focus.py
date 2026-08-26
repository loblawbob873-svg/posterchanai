#!/usr/bin/env python3
"""Prove installed PosterChan keeps Code and Terminal sizing isolated across focus changes."""

import asyncio
import urllib.error

from check_installed_desktop_account import CDP, choose_authenticated_page


DRIVE = r"""(async()=>{
  if(!window.PCOS||!PCOS.routeView)throw new Error('desktop window manager is unavailable');
  const before=new Set([...document.querySelectorAll('.osw:not(.osw-native)')]);
  PCOS.routeView('code');await new Promise(r=>setTimeout(r,700));
  const code=[...document.querySelectorAll('.osw:not(.osw-native)')].find(w=>
    /code/i.test((w.querySelector('.osw-title')||{}).textContent||''))||
    [...document.querySelectorAll('.osw:not(.osw-native)')].find(w=>w.querySelector('.feed-code'));
  PCOS.routeView('terminal');await new Promise(r=>setTimeout(r,700));
  const term=[...document.querySelectorAll('.osw:not(.osw-native)')].find(w=>
    /terminal/i.test((w.querySelector('.osw-title')||{}).textContent||''))||
    [...document.querySelectorAll('.osw:not(.osw-native)')].find(w=>w.querySelector('.feed-term'));
  if(!code||!term||code===term)throw new Error('Code and Terminal did not open as distinct windows');
  const parkedCode=code.querySelector('.osw-slot');
  const terminalLive=term.querySelector('#feed')||document.querySelector('#feed');
  const first={code:parkedCode?parkedCode.className:'',term:terminalLive?terminalLive.className:''};
  (code.querySelector('.osw-bar')||code).dispatchEvent(new PointerEvent('pointerdown',
    {bubbles:true,button:0,pointerId:91,clientX:10,clientY:10}));
  (code.querySelector('.osw-bar')||code).dispatchEvent(new PointerEvent('pointerup',
    {bubbles:true,button:0,pointerId:91,clientX:10,clientY:10}));
  (code.querySelector('.osw-bar')||code).click();await new Promise(r=>setTimeout(r,700));
  const codeLive=code.querySelector('#feed')||document.querySelector('#feed');
  const parkedTerm=term.querySelector('.osw-slot');
  const second={code:codeLive?codeLive.className:'',term:parkedTerm?parkedTerm.className:''};
  const created=[...document.querySelectorAll('.osw:not(.osw-native)')].filter(w=>!before.has(w));
  return {first,second,created:created.length,
    pass:first.code.includes('feed-code')&&!first.code.includes('feed-term')&&
      first.term.includes('feed-term')&&!first.term.includes('feed-code')&&
      second.code.includes('feed-code')&&!second.code.includes('feed-term')&&
      second.term.includes('feed-term')&&!second.term.includes('feed-code')};
})()"""


async def main():
    page = await choose_authenticated_page()
    async with CDP(page["webSocketDebuggerUrl"]) as cdp:
        result = await cdp.eval(DRIVE)
        assert result and result.get("pass"), result
    print("OK installed Code and Terminal retain exclusive full-height sizing across focus")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except (urllib.error.URLError, ConnectionRefusedError) as exc:
        print("SKIP installed Electron is not attached on loopback CDP: " + str(exc))
        raise SystemExit(2)
