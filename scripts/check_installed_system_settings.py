#!/usr/bin/env python3
"""Exercise System Settings in a real installed Electron renderer.

This gate is account-independent and safe for an isolated diagnostic profile. It opens the shipped
System Settings window, switches every category through the rendered controls, proves categories are
separate pages (not one combined widget dashboard), then focuses another packaged app and returns to
Settings to catch the stale/blank repaint regression. Only windows created by the gate are closed.
"""

import asyncio
import json
import urllib.error
import urllib.request

from check_installed_desktop_account import BASE, CDP


DRIVE = r"""(async()=>{
  if(!window.PCOS||!PCOS.openSystemSettings)throw new Error('installed desktop settings are unavailable');
  if(!PCOS.isOn())PCOS.enter();
  const before=new Set([...document.querySelectorAll('.osw:not(.osw-native)')]);
  window.__pcInstalledSettingsBackup={before,focused:document.querySelector('.osw.focused')};
  PCOS.openSystemSettings();
  for(let i=0;i<120&&!document.querySelector('.osw.focused .os-settings');i++)
    await new Promise(r=>setTimeout(r,50));
  const frame=[...document.querySelectorAll('.osw:not(.osw-native)')].find(w=>
    /System Settings/i.test((w.querySelector('.osw-title')||{}).textContent||''));
  if(!frame)throw new Error('System Settings did not open in its own window');
  const expected=['displays','appearance','sound','network','bluetooth','power','users','updates','about','liveusb'];
  const switched=[];
  for(const page of expected){
    const button=frame.querySelector('[data-page="'+page+'"]');
    if(!button)continue;
    button.click(); await new Promise(r=>setTimeout(r,35));
    const pane=frame.querySelector('[data-settings-page="'+page+'"]');
    if(pane&&!pane.hidden)switched.push(page);
  }
  const mobile=[...frame.querySelectorAll('[data-settings-mobile] option')].map(o=>o.value);
  const widgetControls=frame.querySelectorAll('[data-widgets],[data-widget-add],[data-widget-remove]').length;
  const first={expected,switched,mobile,widgetControls,
    settings:!!frame.querySelector('.os-settings'),
    displayPage:!!frame.querySelector('[data-settings-page="displays"]'),
    aboutPage:!!frame.querySelector('[data-settings-page="about"]'),
    liveUsbPage:!!frame.querySelector('[data-settings-page="liveusb"]')};
  PCOS.routeView('code');await new Promise(r=>setTimeout(r,300));
  (frame.querySelector('.osw-bar')||frame).click();
  for(let i=0;i<80&&!frame.querySelector('.os-settings');i++)await new Promise(r=>setTimeout(r,50));
  return {...first,returned:!!frame.querySelector('.os-settings'),
    isolated:!frame.querySelector('#tl-notes,.post-card,.timeline')};
})()"""

CLEANUP = r"""(async()=>{
  const backup=window.__pcInstalledSettingsBackup;if(!backup)return false;
  const created=[...document.querySelectorAll('.osw:not(.osw-native)')].filter(w=>!backup.before.has(w));
  for(const w of created){const close=w.querySelector('.osw-x');if(close)close.click();}
  if(backup.focused&&backup.focused.isConnected){const bar=backup.focused.querySelector('.osw-bar');if(bar)bar.click();}
  delete window.__pcInstalledSettingsBackup;return true;
})()"""


async def choose_page():
    pages = [p for p in json.load(urllib.request.urlopen(BASE + "/json/list", timeout=5))
             if p.get("type") == "page" and p.get("url", "").startswith("app://posterchan/")]
    if not pages:
        raise RuntimeError("no installed PosterChan page is attached")
    return pages[0]


async def main():
    page = await choose_page()
    async with CDP(page["webSocketDebuggerUrl"]) as cdp:
        try:
            result = await cdp.eval(DRIVE)
            expected = result.get("expected", [])
            assert result.get("settings") and result.get("returned") and result.get("isolated"), result
            assert result.get("switched") == expected, result
            assert all("page:" + page in result.get("mobile", []) for page in expected), result
            assert result.get("widgetControls") == 0, result
            assert all(result.get(k) for k in ("displayPage", "aboutPage", "liveUsbPage")), result
        finally:
            await cdp.eval(CLEANUP)
    print("OK installed System Settings switches separate pages and survives focus")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except (urllib.error.URLError, ConnectionRefusedError) as exc:
        print("SKIP installed Electron is not attached on loopback CDP: " + str(exc))
        raise SystemExit(2)
