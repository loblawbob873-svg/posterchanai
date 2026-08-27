#!/usr/bin/env python3
"""Drive PosterChan Code in the installed Electron package against a real disposable Git tree.

Prerequisites:
  * installed Electron exposed on loopback CDP port 9223;
  * PC_INSTALLED_CODE_ROOT names a disposable Git repository on that machine containing a modified
    ``changed.js``. The check restores that file, so never point it at a real project.

The UI's previous Code state and localStorage record are restored before exit. No file names or
contents outside the explicitly supplied disposable root are read or printed.
"""

import asyncio
import json
import os
import urllib.error

from check_installed_desktop_account import CDP, choose_authenticated_page


DRIVE = r"""(async root=>{
  if(!root.startsWith('/tmp/pc-code-installed.')) throw new Error('refusing a non-test root');
  if(!window.PCCode && window.PCOS && PCOS.routeView){
    PCOS.routeView('code');
    for(let i=0;i<80&&!window.PCCode;i++)await new Promise(r=>setTimeout(r,50));
  }
  if(!window.PCCode||!window.pcHost)throw new Error('installed Code/native bridge is unavailable');
  if(window.PCOS&&PCOS.routeView)PCOS.routeView('code');
  await new Promise(r=>setTimeout(r,500));
  const S=PCCode._state;
  const keys=[];for(let i=0;i<localStorage.length;i++){
    const k=localStorage.key(i);if(k&&k.startsWith('pccode_'))keys.push([k,localStorage.getItem(k)]);
  }
  window.__pcInstalledCodeBackup={state:JSON.parse(JSON.stringify(S)),keys,
    pickDirectory:pcHost.pickDirectory};
  Object.assign(S,{ready:true,root:'No folder open',hostRoot:'',cwd:'',gate:'',treeErr:'',treeBusy:false,
    tree:[],
    open:[],active:-1,gitOpen:false,git:null,gitBusy:false,gitDiff:null,status:''});
  await PCCode.render();await new Promise(r=>setTimeout(r,200));
  /* Drive the same user-selection path as the Open Folder toolbar button.  The native dialog itself
     cannot safely be automated over CDP, so replace only its return value with the already-guarded
     disposable root; the button handler, state transition, listing and repaint remain production. */
  pcHost.pickDirectory=async()=>root;
  const openFolder=document.querySelector('#pcc-open-folder');
  if(!openFolder)throw new Error('Open Folder button is missing');
  openFolder.click();
  for(let i=0;i<100&&!document.querySelector('[data-file="'+CSS.escape(root+'/changed.js')+'"]');i++)
    await new Promise(r=>setTimeout(r,50));
  const explorer=!!document.querySelector('[data-file="'+CSS.escape(root+'/changed.js')+'"]');
  const gitButton=document.querySelector('[data-code-view="git"]');
  if(!gitButton)throw new Error('Source Control button is missing');gitButton.click();
  for(let i=0;i<80&&!document.querySelector('[data-git-diff="changed.js"]');i++)
    await new Promise(r=>setTimeout(r,50));
  const row=document.querySelector('[data-git-diff="changed.js"]');
  if(row)row.click();
  for(let i=0;i<80&&!((document.querySelector('.pcc-git-diff')||{}).textContent||'').includes('installedCode');i++)
    await new Promise(r=>setTimeout(r,50));
  const diff=(document.querySelector('.pcc-git-diff')||{}).textContent||'';
  const restore=document.querySelector('[data-git-restore="changed.js"]');
  if(restore)restore.click();
  for(let i=0;i<40;i++){
    const yes=document.querySelector('.uiconfirm-bg [data-uc="1"]');
    if(yes){yes.click();break;}await new Promise(r=>setTimeout(r,50));
  }
  for(let i=0;i<100&&!document.body.textContent.includes('Working tree clean');i++)
    await new Promise(r=>setTimeout(r,50));
  const clean=document.body.textContent.includes('Working tree clean');
  const diffClosed=!document.querySelector('.pcc-git-diff');
  const explorerButton=document.querySelector('[data-code-view="explorer"]');if(explorerButton)explorerButton.click();
  await new Promise(r=>setTimeout(r,150));
  return {explorer,sourceRow:!!row,diff:diff.includes('-const installedCode = false;')&&
    diff.includes('+const installedCode = true;'),restore:!!restore,clean,diffClosed,
    explorerBack:!!document.querySelector('[data-file="'+CSS.escape(root+'/changed.js')+'"]')};
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
    page = await choose_authenticated_page()
    async with CDP(page["webSocketDebuggerUrl"]) as cdp:
        try:
            result = await cdp.eval(DRIVE + "(" + json.dumps(root) + ")")
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
