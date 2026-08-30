#!/usr/bin/env python3
"""Drive PosterChan Code in the installed Electron package against a real disposable Git tree.

Prerequisites:
  * an isolated installed Electron exposed on a loopback CDP port;
  * PC_INSTALLED_CODE_ROOT names a disposable Git repository on that machine containing a modified
    ``changed.js``. The check restores that file, so never point it at a real project.

The UI's previous Code state and localStorage record are restored before exit. No file names or
contents outside the explicitly supplied disposable root are read or printed.
"""

import asyncio
import json
import os
import urllib.error
import urllib.request

from check_installed_desktop_account import BASE, CDP


async def choose_page():
    """Select only an installed app:// renderer; Code's local Git gate needs no account."""
    pages = [p for p in json.load(urllib.request.urlopen(BASE + "/json/list", timeout=5))
             if p.get("type") == "page" and p.get("url", "").startswith("app://posterchan/")]
    if not pages:
        raise RuntimeError("no installed PosterChan page is attached")
    return pages[0]


PREPARE = r"""(async root=>{
  if(!root.startsWith('/tmp/pc-code-installed.')) throw new Error('refusing a non-test root');
  if(!window.PCCode||!window.pcHost)throw new Error('installed Code/native bridge is unavailable');
  const S=PCCode._state;
  const keys=[];for(let i=0;i<localStorage.length;i++){
    const k=localStorage.key(i);if(k&&k.startsWith('pccode_'))keys.push([k,localStorage.getItem(k)]);
  }
  window.__pcInstalledCodeBackup={state:JSON.parse(JSON.stringify(S)),keys,
    pickDirectory:pcHost.pickDirectory};
  Object.assign(S,{ready:true,root:'No folder open',hostRoot:'',cwd:'',gate:'',treeErr:'',treeBusy:false,
    tree:[],
    open:[],active:-1,gitOpen:false,git:null,gitBusy:false,gitDiff:null,status:''});
  /* The native chooser cannot be automated over CDP. Verify the Change Working Directory control,
     then seed its already-selected result through the packaged list bridge. Never click the real
     chooser: contextBridge may appear writable while the handler still holds its original closure,
     which blocks the diagnostic behind a native dialog. */
  const listed=await pcHost.list(root);
  Object.assign(S,{hostRoot:root,cwd:listed.path||root,root,
    tree:(listed.entries||[]).map(e=>({name:e.name,path:e.path,dir:!!e.dir,lang:''})),
    open:[],active:-1,gitOpen:false,git:null,gitDiff:null});
  await PCCode.render();
  return true;
})"""

CLEANUP = r"""(async root=>{
  try{
    const status=await pcHost.gitStatus(root);
    if((status.files||[]).some(f=>f.path==='changed.js'))await pcHost.gitAction(root,'restore',['changed.js']);
  }catch(_){}
  const b=window.__pcInstalledCodeBackup;if(!b||!window.PCCode)return false;
  pcHost.pickDirectory=b.pickDirectory;
  for(let i=localStorage.length-1;i>=0;i--){const k=localStorage.key(i);if(k&&k.startsWith('pccode_'))localStorage.removeItem(k);}
  for(const [k,v] of b.keys)if(v!==null)localStorage.setItem(k,v);
  Object.assign(PCCode._state,b.state);await PCCode.render();delete window.__pcInstalledCodeBackup;return true;
})"""


async def main():
    root = os.environ.get("PC_INSTALLED_CODE_ROOT", "")
    if not root.startswith("/tmp/pc-code-installed."):
        print("SKIP PC_INSTALLED_CODE_ROOT must name a disposable /tmp/pc-code-installed.* tree")
        return 2
    page = await choose_page()
    async with CDP(page["webSocketDebuggerUrl"]) as cdp:
        try:
            desktop = await cdp.eval(r"""(async()=>{
              for(let i=0;i<100&&!(window.PCOS&&PCOS.toggle&&window.__PC);i++)
                await new Promise(r=>setTimeout(r,100));
              if(!(window.PCOS&&PCOS.toggle))return {on:false,root:false,body:false};
              if(!PCOS.isOn())PCOS.toggle();
              for(let i=0;i<80&&!PCOS.isOn();i++)await new Promise(r=>setTimeout(r,100));
              return {on:PCOS.isOn(),root:!!document.querySelector('#os-root'),
                body:document.body.classList.contains('os-on')};
            })()""")
            if not desktop or not all(desktop.values()):
                raise RuntimeError("installed PosterChanOS desktop mode did not activate: " + str(desktop))
            # Require the real navigation control, then invoke the same owning switchView operation
            # its handler calls. A CSS-hidden classic sidebar still has ``hidden == false`` while
            # Desktop mode is active, so synthetic ``element.click()`` could select that parked
            # control and race the shell repaint. switchView is the product navigation operation;
            # unlike PCOS.routeView alone it also runs the renderer continuation.
            opened = await cdp.eval(r"""(async()=>{
              if(!document.querySelector('[data-view="code"]'))return false;
              if(!(window.__PC&&__PC.switchView))return false;
              await __PC.switchView('code');return true;
            })()""")
            if not opened:
                raise RuntimeError("installed Code navigation control is missing")
            await asyncio.sleep(1)
            await cdp.eval(PREPARE + "(" + json.dumps(root) + ")")
            await asyncio.sleep(.5)

            async def wait_value(expression, attempts=100):
                for _ in range(attempts):
                    value = await cdp.eval(expression)
                    if value:
                        return value
                    await asyncio.sleep(.05)
                return None

            file_selector = json.dumps('[data-file="' + root + '/changed.js"]')
            change_working_directory = bool(await wait_value("!!document.querySelector('#pcc-open-folder')"))
            explorer = bool(await wait_value(f"!!document.querySelector({file_selector})"))
            await cdp.eval("document.querySelector('[data-code-view=\"git\"]')?.click();true")
            row = bool(await wait_value("!!document.querySelector('[data-git-diff=\"changed.js\"]')"))
            await cdp.eval("document.querySelector('[data-git-diff=\"changed.js\"]')?.click();true")
            diff = await wait_value("(document.querySelector('.pcc-git-diff')||{}).textContent||''", 80) or ""
            restore = bool(await cdp.eval("!!document.querySelector('[data-git-restore=\"changed.js\"]')"))
            await cdp.eval("document.querySelector('[data-git-restore=\"changed.js\"]')?.click();true")
            await wait_value("!!document.querySelector('.uiconfirm-bg [data-uc=\"1\"]')", 40)
            await cdp.eval("document.querySelector('.uiconfirm-bg [data-uc=\"1\"]')?.click();true")
            clean = bool(await wait_value("document.body.textContent.includes('Working tree clean')"))
            diff_closed = not bool(await cdp.eval("!!document.querySelector('.pcc-git-diff')"))
            await cdp.eval("document.querySelector('[data-code-view=\"explorer\"]')?.click();true")
            explorer_back = bool(await wait_value(f"!!document.querySelector({file_selector})"))
            result = {"changeWorkingDirectory": change_working_directory,
                      "explorer": explorer, "sourceRow": row,
                      "diff": "-const installedCode = false;" in diff and "+const installedCode = true;" in diff,
                      "restore": restore, "clean": clean, "diffClosed": diff_closed,
                      "explorerBack": explorer_back}
            if not all(result.values()):
                result["ownership"] = await cdp.eval(r"""(()=>({
                  view:window.__PC&&__PC.VIEW, codeReady:!!window.PCCode,
                  feeds:[...document.querySelectorAll('#feed')].map(e=>e.className),
                  slots:[...document.querySelectorAll('.osw-slot')].map(e=>e.className),
                  windows:[...document.querySelectorAll('.osw')].map(w=>({
                    title:(w.querySelector('.osw-title')||{}).textContent||'',
                    focused:w.classList.contains('focused'),native:w.classList.contains('osw-native')}))
                }))()""")
            assert result and all(result.values()), result
        finally:
            await cdp.eval(CLEANUP + "(" + json.dumps(root) + ")")
    print("OK installed Code selected a project, rendered its real diff, restored it, and returned to Explorer")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except (urllib.error.URLError, ConnectionRefusedError) as exc:
        print("SKIP installed Electron is not attached on loopback CDP: " + str(exc))
        raise SystemExit(2)
