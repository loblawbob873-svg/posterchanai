#!/usr/bin/env python3
"""Prove installed PosterChan keeps Code and Terminal sizing isolated across focus changes."""

import asyncio
import json
import urllib.error
import urllib.request

from check_installed_desktop_account import BASE, CDP, choose_shell_page


DRIVE = r"""(async()=>{
  if(!window.PCOS||!PCOS.routeView)throw new Error('desktop window manager is unavailable');
  const before=new Set([...document.querySelectorAll('.osw:not(.osw-native)')]);
  window.__pcInstalledCodeFocusBackup={before,focused:document.querySelector('.osw.focused')};
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

CLEANUP = r"""(async()=>{
  const backup=window.__pcInstalledCodeFocusBackup;if(!backup)return false;
  const created=[...document.querySelectorAll('.osw:not(.osw-native)')]
    .filter(w=>!backup.before.has(w));
  for(const w of created){const close=w.querySelector('.osw-x');if(close)close.click();}
  if(backup.focused&&backup.focused.isConnected){
    const bar=backup.focused.querySelector('.osw-bar');if(bar)bar.click();
  }
  delete window.__pcInstalledCodeFocusBackup;return true;
})()"""


async def choose_page():
    """Pick the installed shell without requiring account state.

    Code and Terminal window ownership is local Electron/DOM behaviour. Requiring a signed-in
    account made this release gate impossible to run in its deliberately isolated profile and
    encouraged copying a person's multi-gigabyte Chromium profile into /tmp. The native Files gate
    already uses the same safe rule: require the packaged app:// page and then let the runtime
    assertions below prove the shell and bridge are present.
    """
    pages = [p for p in json.load(urllib.request.urlopen(BASE + "/json/list", timeout=5))
             if p.get("type") == "page" and p.get("url", "").startswith("app://posterchan/")]
    if not pages:
        raise RuntimeError("no installed PosterChan page is attached")
    # THE SHELL, not whatever /json/list happened to list first — see choose_shell_page.
    return await choose_shell_page()


async def main():
    page = await choose_page()
    async with CDP(page["webSocketDebuggerUrl"]) as cdp:
        try:
            result = await cdp.eval(DRIVE)
            assert result and result.get("pass"), result
        finally:
            await cdp.eval(CLEANUP)
    print("OK installed Code and Terminal retain exclusive full-height sizing across focus")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except (urllib.error.URLError, ConnectionRefusedError) as exc:
        print("SKIP installed Electron is not attached on loopback CDP: " + str(exc))
        raise SystemExit(2)
