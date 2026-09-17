#!/usr/bin/env python3
"""Exercise System Settings in a real installed Electron renderer.

This gate is account-independent and safe for an isolated diagnostic profile. It opens the shipped
System Settings window, switches every category through the rendered controls, proves categories are
separate pages (not one combined widget dashboard), then focuses another packaged app and returns to
Settings to catch the stale/blank repaint regression. Only windows created by the gate are closed.

SETTINGS IS ITS OWN TOPLEVEL WINDOW NOW. On PosterChanOS a shell popup is a separate Electron window
(its own CDP page, titled "PosterChan Window — __ossettings"), not an `.osw` frame inside the shell's
page. This gate used to look only for the in-page frame, so on the installed desktop it failed with
"System Settings did not open in its own window" while the window was open, rendered and fully
working beside it (measured on the test desktop, 2026-09-17). It now follows the window wherever the
build puts it: the toplevel page first, the legacy in-page frame second.
"""

import asyncio
import json
import urllib.error
import urllib.request

from check_installed_desktop_account import BASE, CDP, choose_shell_page


OPEN = r"""(async()=>{
  if(!window.PCOS||!PCOS.openSystemSettings)throw new Error('installed desktop settings are unavailable');
  if(!PCOS.isOn())PCOS.enter();
  const before=new Set([...document.querySelectorAll('.osw:not(.osw-native)')]);
  window.__pcInstalledSettingsBackup={before,focused:document.querySelector('.osw.focused')};
  PCOS.openSystemSettings();
  for(let i=0;i<120&&!document.querySelector('.osw.focused .os-settings');i++){
    if(i>=40)break;   // a toplevel window never appears in THIS page; the Python side looks for it
    await new Promise(r=>setTimeout(r,50));
  }
  return true;
})()"""

# Runs in whichever page holds the settings UI. `frame` is the legacy in-page window when there is
# one, otherwise the toplevel window's own document.
DRIVE = r"""(async()=>{
  const frame=[...document.querySelectorAll('.osw:not(.osw-native)')].find(w=>
    /System Settings/i.test((w.querySelector('.osw-title')||{}).textContent||''))
    ||(document.querySelector('.os-settings')?document.body:null);
  if(!frame)throw new Error('System Settings did not open in its own window');
  const waitFor=async(test,label)=>{
    const end=Date.now()+20000;
    while(Date.now()<end){if(test())return;await new Promise(r=>setTimeout(r,50));}
    throw new Error('Timed out waiting for '+label);
  };
  await waitFor(()=>frame.querySelector('[data-page="datetime"]'),'settings navigation');
  /* A toplevel Settings window can open narrow enough that its category list collapses into the
   * phone-width <select> (measured: 434x430 on a 1024x576 output), and a collapsed button has no box
   * to click or icon to see. Maximise it the way a person would before driving the list. */
  const navShown=()=>{const n=frame.querySelector('.os-set-nav');return !!n&&getComputedStyle(n).display!=='none';};
  if(frame===document.body&&!navShown()){
    const max=document.querySelector('button[aria-label="Maximise"]');
    if(max)max.click();
    await waitFor(navShown,'the category list after maximising');
  }
  const required=['displays','appearance','sound','network','bluetooth','power','datetime','users','updates','about','liveusb'];
  if(window.pcPrinters)required.push('printers');
  const expected=[...frame.querySelectorAll('[data-page]')].map(b=>b.dataset.page);
  for(const page of required)if(!expected.includes(page))throw new Error('Missing category '+page);
  const checkIcon=node=>{
    const use=node&&node.querySelector('svg use');
    const href=use&&(use.getAttribute('href')||use.getAttribute('xlink:href'));
    const symbol=href&&href.startsWith('#')&&document.getElementById(href.slice(1));
    if(!symbol||!symbol.children.length)throw new Error('Missing icon for '+(node&&node.textContent));
    const rect=use.closest('svg').getBoundingClientRect();
    if(rect.width<=0||rect.height<=0)throw new Error('Invisible category icon');
  };
  const switched=[];let dateTime=null;
  for(const page of expected){
    const button=frame.querySelector('[data-page="'+page+'"]');
    if(!button)throw new Error('Category disappeared: '+page);
    checkIcon(button);
    button.click();
    await waitFor(()=>{
      const pane=frame.querySelector('[data-settings-page="'+page+'"]');
      return pane&&!pane.hidden;
    },page+' page');
    const pane=frame.querySelector('[data-settings-page="'+page+'"]');
    const visible=[...frame.querySelectorAll('[data-settings-page]')].filter(p=>!p.hidden);
    if(visible.length!==1||visible[0]!==pane)throw new Error('Categories overlap: '+page);
    checkIcon(pane.querySelector('.os-set-pagehead'));
    if(page==='datetime'){
      const clock=pane.querySelector('[data-clock-current]'),zone=pane.querySelector('[data-clock-zone]');
      const auto=pane.querySelector('[data-clock-auto]'),manual=pane.querySelector('[data-clock-time]');
      dateTime={clock:clock&&clock.textContent.trim(),zone:zone&&zone.value,
        zones:zone&&zone.options.length,automatic:!!(auto&&auto.checked),
        manualDisabled:!!(manual&&manual.disabled)};
      if(!dateTime.clock||!dateTime.zone||!dateTime.zones||!auto||!manual)
        throw new Error('Date & Time did not load: '+pane.textContent);
      if(dateTime.manualDisabled!==dateTime.automatic)throw new Error('Manual clock edit state is inconsistent');
      // Inspect only: never toggle NTP or submit a clock/time-zone change on the real machine.
    }
    switched.push(page);
  }
  const mobile=[...frame.querySelectorAll('[data-settings-mobile] option')].map(o=>o.value);
  const widgetControls=frame.querySelectorAll('[data-widgets],[data-widget-add],[data-widget-remove]').length;
  return {expected,switched,mobile,widgetControls,dateTime,toplevel:frame===document.body,
    settings:!!frame.querySelector('.os-settings'),
    displayPage:!!frame.querySelector('[data-settings-page="displays"]'),
    aboutPage:!!frame.querySelector('[data-settings-page="about"]'),
    liveUsbPage:!!frame.querySelector('[data-settings-page="liveusb"]')};
})()"""

# After another app took focus, Settings must still be Settings (not blank, not the timeline).
FOCUS_AWAY = r"""(async()=>{PCOS.routeView('code');await new Promise(r=>setTimeout(r,300));return true;})()"""

RETURN = r"""(async()=>{
  const frame=[...document.querySelectorAll('.osw:not(.osw-native)')].find(w=>
    /System Settings/i.test((w.querySelector('.osw-title')||{}).textContent||''))
    ||(document.querySelector('.os-settings')?document.body:null);
  if(frame&&frame!==document.body)(frame.querySelector('.osw-bar')||frame).click();
  for(let i=0;i<80&&!(frame&&frame.querySelector('.os-settings'));i++)await new Promise(r=>setTimeout(r,50));
  return {returned:!!(frame&&frame.querySelector('.os-settings')),
    isolated:!!frame&&!frame.querySelector('#tl-notes,.post-card,.timeline')};
})()"""

CLEANUP = r"""(async()=>{
  const backup=window.__pcInstalledSettingsBackup;if(!backup)return false;
  const created=[...document.querySelectorAll('.osw:not(.osw-native)')].filter(w=>!backup.before.has(w));
  for(const w of created){const close=w.querySelector('.osw-x');if(close)close.click();}
  if(backup.focused&&backup.focused.isConnected){const bar=backup.focused.querySelector('.osw-bar');if(bar)bar.click();}
  delete window.__pcInstalledSettingsBackup;return true;
})()"""

CLOSE_TOPLEVEL = r"""(()=>{const b=document.querySelector('button[aria-label="Close"]');if(b){b.click();return true;}
  try{window.close();return true;}catch(_){return false;}})()"""


def _pages():
    return [p for p in json.load(urllib.request.urlopen(BASE + "/json/list", timeout=5))
            if p.get("type") == "page" and p.get("url", "").startswith("app://posterchan/")]


async def choose_page():
    if not _pages():
        raise RuntimeError("no installed PosterChan page is attached")
    # THE SHELL, not whatever /json/list happened to list first — see choose_shell_page.
    return await choose_shell_page()


async def settings_page(shell, existing, timeout_s=20.0):
    """The page holding the settings UI.

    A NEW toplevel window wins. The shell page is only accepted for the legacy in-page `.osw` frame:
    the shell also keeps a parked render of Settings in its own document, and testing that copy
    passes while the window the person actually sees could be blank.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    legacy = ("!![...document.querySelectorAll('.osw:not(.osw-native)')].find(w=>"
              "/System Settings/i.test((w.querySelector('.osw-title')||{}).textContent||''))")
    while True:
        for page in _pages():
            if page["id"] in existing or page["id"] == shell["id"]:
                continue
            try:
                async with CDP(page["webSocketDebuggerUrl"]) as cdp:
                    if await cdp.eval("!!document.querySelector('.os-settings')"):
                        return page, True
            except Exception:
                continue
        try:
            async with CDP(shell["webSocketDebuggerUrl"]) as cdp:
                if await cdp.eval(legacy):
                    return shell, False
        except Exception:
            pass
        if asyncio.get_event_loop().time() >= deadline:
            return None, False
        await asyncio.sleep(0.25)


async def main():
    shell = await choose_page()
    existing = {p["id"] for p in _pages()}
    created_toplevel = None
    async with CDP(shell["webSocketDebuggerUrl"]) as sh:
        try:
            await sh.eval(OPEN)
            page, created = await settings_page(shell, existing)
            if page is None:
                raise AssertionError("System Settings did not open in its own window")
            if created:
                created_toplevel = page
            async with CDP(page["webSocketDebuggerUrl"]) as st:
                result = await st.eval(DRIVE)
            await sh.eval(FOCUS_AWAY)
            async with CDP(page["webSocketDebuggerUrl"]) as st:
                result.update(await st.eval(RETURN))
            expected = result.get("expected", [])
            assert result.get("settings") and result.get("returned") and result.get("isolated"), result
            assert result.get("switched") == expected, result
            assert all("page:" + page in result.get("mobile", []) for page in expected), result
            assert result.get("widgetControls") == 0, result
            assert result.get("dateTime", {}).get("clock"), result
            assert all(result.get(k) for k in ("displayPage", "aboutPage", "liveUsbPage")), result
        finally:
            await sh.eval(CLEANUP)
            # Close every toplevel window this gate created (Settings, and the app it focused away to).
            for page in _pages():
                if page["id"] in existing or page["id"] == shell["id"]:
                    continue
                try:
                    async with CDP(page["webSocketDebuggerUrl"]) as st:
                        await st.eval(CLOSE_TOPLEVEL)
                except Exception:
                    pass
    print("OK installed System Settings switches separate pages and survives focus"
          + (" (toplevel window)" if created_toplevel else ""))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except (urllib.error.URLError, ConnectionRefusedError) as exc:
        print("SKIP installed Electron is not attached on loopback CDP: " + str(exc))
        raise SystemExit(2)
