#!/usr/bin/env python3
"""Exercise account-dependent Files and Office paths in an installed desktop build.

The installed Electron process must be started with a loopback-only CDP port, for example:

    posterchan --shell --remote-debugging-address=127.0.0.1 --remote-debugging-port=9223
    PC_CHECK_PORT=9223 venv-unified/bin/python scripts/check_installed_desktop_account.py

This check never reads filenames, file URLs, keys, or document contents from the account.  Office
uses a temporary text document, verifies the live WOPI write/read path, opens the real editor frame,
and deletes the temporary session in a finally block.
"""

import asyncio
import json
import os
import sys
import urllib.error
import urllib.request

import websockets


PORT = int(os.environ.get("PC_CHECK_PORT", "9223"))
BASE = f"http://127.0.0.1:{PORT}"


class CDP:
    def __init__(self, url):
        self.url = url
        self.ws = None
        self.seq = 0

    async def __aenter__(self):
        self.ws = await websockets.connect(self.url, max_size=32 * 1024 * 1024)
        return self

    async def __aexit__(self, *_):
        await self.ws.close()

    async def call(self, method, params=None, events=None, session_id=None):
        self.seq += 1
        call_id = self.seq
        message = {"id": call_id, "method": method, "params": params or {}}
        if session_id is not None:
            message["sessionId"] = session_id
        await self.ws.send(json.dumps(message))
        while True:
            message = json.loads(await self.ws.recv())
            if events is not None and message.get("method"):
                events.append(message)
            if message.get("id") == call_id:
                if "error" in message:
                    raise RuntimeError(message["error"])
                return message["result"]

    async def eval(self, expression, events=None, context_id=None, session_id=None):
        params = {
            "expression": expression,
            "awaitPromise": True,
            "returnByValue": True,
        }
        if context_id is not None:
            params["contextId"] = context_id
        result = await self.call("Runtime.evaluate", params, events, session_id)
        remote = result.get("result", {})
        if remote.get("subtype") == "error":
            raise RuntimeError(remote.get("description") or remote)
        return remote.get("value")


async def choose_authenticated_page():
    pages = [p for p in json.load(urllib.request.urlopen(BASE + "/json/list", timeout=5))
             if p.get("type") == "page" and p.get("url", "").startswith("app://posterchan/")]
    for page in pages:
        async with CDP(page["webSocketDebuggerUrl"]) as cdp:
            if await cdp.eval("!!(window.__PC && __PC.me && __PC.me())"):
                return page
    raise RuntimeError("no authenticated installed PosterChan page is attached")


FILES_CHECK = r"""(async()=>{
  window.__installedCheckErrors=[];
  const onerr=e=>__installedCheckErrors.push(String(e.message||e.reason||e));
  addEventListener('error',onerr); addEventListener('unhandledrejection',onerr);
  await __PC.switchView('blossom'); await new Promise(r=>setTimeout(r,8000));
  // Rendering a few tiles is not proof that the drive is complete.  Pull the canonical account
  // index, then independently ask the server for its privacy-preserving entry count and require the
  // installed client's decrypted in-memory index to agree.  Do not return names, hashes or keys.
  const idx=__PC.filesIdx();
  const pullOk=await idx.ensure();
  const auth=btoa(JSON.stringify(await __PC.signAuth('files-index')));
  const ir=await fetch('/client/files-index',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({pubkey:__PC.me().pubkey,auth})});
  const ij=await ir.json().catch(()=>({}));
  const ptr=ij&&ij.index&&typeof ij.index==='object'?ij.index:{};
  const serverFiles=Number.isFinite(Number(ptr.n)) ? Number(ptr.n)
    : Object.keys(ptr.files&&typeof ptr.files==='object'?ptr.files:{}).length;
  const clientFiles=Object.keys((idx.data&&idx.data.files)||{}).length;
  // Synced roots have three independently meaningful counts: the account listing's plaintext
  // collapse-guard count, the manifest this installed client decrypted, and the actual directory
  // scan performed by the packaged native bridge.  Equality proves substantially more than seeing
  // two root buttons.  Only counts leave the page; no pair labels, local paths or filenames do.
  const syncAudit=[];
  if(window.PCSync&&window.pcFs){
    await PCSync.accountFolders(true);
    const acct=Array.isArray(PCSync.acct())?PCSync.acct():[];
    for(const f of PCSync.folders()){
      const key=f.key||f.name, row=acct.find(x=>x.key===key)||{};
      try{
        const [got,scan]=await Promise.all([
          PCSync.docs.state(key), pcFs.scan(f.id,{excludes:f.excludes||[]})
        ]);
        const state=(got&&got.state)||{};
        syncAudit.push({
          server:Number.isFinite(row.n)?row.n:null,
          manifest:Object.values(state).filter(x=>x&&typeof x==='object'&&!x.deletedAt).length,
          local:Object.keys((scan&&scan.files)||{}).length,
          skipped:((scan&&scan.skipped)||[]).length});
      }catch(e){ syncAudit.push({error:String(e&&e.message||e)}); }
    }
  }
  const q=s=>document.querySelectorAll(s).length;
  return {
    view:__PC.VIEW, explorers:q('.fx-explorer'), folderTiles:q('.fx-home-tile'),
    folderChips:q('.folder-chip'), syncedRoots:q('.syncroot'),
    pullOk:!!pullOk, indexHTTP:ir.status, indexOK:!!ij.ok, serverFiles, clientFiles,
    syncAudit,
    overflow:document.documentElement.scrollWidth>innerWidth+1,
    errors:__installedCheckErrors.slice(0,5)
  };
})()"""


OFFICE_CHECK = r"""(async()=>{
  let s=null,wrap=null; const out={};
  try{
    await __PC.ensureAiSession();
    const B=String(window.__PC_API_BASE__||'').replace(/\/$/,'');
    const fd=new FormData();
    fd.append('file',new File(['office smoke one\n'],'posterchan-office-smoke.txt',{type:'text/plain'}));
    fd.append('mode','edit');
    let r=await __PC.authFetch(B+'/client/office/session',{method:'POST',body:fd});
    out.create=r.status; if(!r.ok) throw new Error('create HTTP '+r.status); s=await r.json();
    const q='?access_token='+encodeURIComponent(s.token);
    r=await fetch(B+'/wopi/files/'+s.id+q); out.info=r.status;
    const info=await r.json(); out.name=info.BaseFileName==='posterchan-office-smoke.txt';
    r=await fetch(B+'/wopi/files/'+s.id+'/contents'+q); out.read1=r.status;
    out.initial=(await r.text())==='office smoke one\n';
    r=await fetch(B+'/wopi/files/'+s.id+'/contents'+q,{method:'POST',
      headers:{'Content-Type':'application/octet-stream'},body:'office smoke two\n'}); out.put=r.status;
    r=await fetch(B+'/client/office/session/'+s.id+'/contents'+q); out.read2=r.status;
    out.updated=(await r.text())==='office smoke two\n';

    wrap=document.createElement('div'); wrap.id='pc-office-installed-smoke';
    wrap.style='position:fixed;left:-10000px;width:800px;height:600px';
    const frame=document.createElement('iframe'); frame.name='pc-office-installed-smoke'; wrap.appendChild(frame);
    const form=document.createElement('form'); form.method='post'; form.action=s.editor_url; form.target=frame.name;
    for(const [n,v] of [['access_token',s.token],['access_token_ttl',String(s.expires*1000)]]){
      const i=document.createElement('input'); i.name=n; i.value=v; form.appendChild(i);
    }
    wrap.appendChild(form); document.body.appendChild(wrap);
    const loaded=new Promise((resolve,reject)=>{const t=setTimeout(()=>reject(new Error('editor iframe timeout')),20000);
      frame.onload=()=>{clearTimeout(t);resolve(true)};});
    form.submit(); await loaded; await new Promise(r=>setTimeout(r,2500)); out.frameLoaded=true;
    window.__pcOfficeInstalledSmoke={session:s,wrap};
    return out;
  }catch(e){
    out.error=String(e&&e.message||e); if(wrap)wrap.remove();
    if(s){const B=String(window.__PC_API_BASE__||'').replace(/\/$/,'');
      await fetch(B+'/client/office/session/'+s.id+'?access_token='+encodeURIComponent(s.token),
        {method:'DELETE'}).catch(()=>{});}
    return out;
  }
})()"""

OFFICE_CLEANUP = r"""(async()=>{
  const held=window.__pcOfficeInstalledSmoke; delete window.__pcOfficeInstalledSmoke;
  if(!held)return true;
  if(held.wrap)held.wrap.remove();
  const s=held.session,B=String(window.__PC_API_BASE__||'').replace(/\/$/,'');
  if(s)await fetch(B+'/client/office/session/'+s.id+'?access_token='+encodeURIComponent(s.token),
    {method:'DELETE'}).catch(()=>{});
  return true;
})()"""

OFFICE_FRAME_CHECK = r"""(()=>({
  href:location.href,ready:document.readyState,title:document.title,
  bodyChildren:document.body?document.body.children.length:0,
  controls:document.querySelectorAll('button,input,[role="button"],.unobutton').length,
  workspace:!!document.querySelector('canvas,#document-container,#toolbar-up,.leaflet-container')
}))()"""


async def main():
    page = await choose_authenticated_page()
    async with CDP(page["webSocketDebuggerUrl"]) as cdp:
        files = await cdp.eval(FILES_CHECK)
        assert files["view"] == "blossom", files
        assert files["explorers"] == 1 and files["folderTiles"] > 0 and files["folderChips"] > 0, files
        assert files["syncedRoots"] > 0, files
        assert files["pullOk"] and files["indexHTTP"] == 200 and files["indexOK"], files
        assert files["clientFiles"] == files["serverFiles"], files
        assert len(files["syncAudit"]) == files["syncedRoots"], files
        assert all(not row.get("error") and row["server"] == row["manifest"] == row["local"]
                   and row["skipped"] == 0 for row in files["syncAudit"]), files
        assert not files["overflow"] and not files["errors"], files

        await cdp.call("Network.enable")
        await cdp.call("Runtime.enable")
        events = []
        try:
            office = await cdp.eval(OFFICE_CHECK, events)
            responses = [int(e["params"]["response"].get("status", 0)) for e in events
                         if e.get("method") == "Network.responseReceived"
                         and "/office-code/browser/" in e["params"]["response"].get("url", "")]
            contexts = [e["params"]["context"] for e in events
                        if e.get("method") == "Runtime.executionContextCreated"]
            editor = None
            targets = (await cdp.call("Target.getTargets")).get("targetInfos", [])
            for target in targets:
                if target.get("type") == "iframe" and "/office-code/" in target.get("url", ""):
                    attached = await cdp.call("Target.attachToTarget", {
                        "targetId": target["targetId"], "flatten": True})
                    editor = await cdp.eval(OFFICE_FRAME_CHECK, session_id=attached["sessionId"])
                    break
            for context in reversed(contexts):
                if editor:
                    break
                origin = str(context.get("origin", ""))
                if origin.startswith("https://poster.place"):
                    candidate = await cdp.eval(OFFICE_FRAME_CHECK, context_id=context["id"])
                    if "/office-code/" in str(candidate.get("href", "")):
                        editor = candidate
                        break
            assert not office.get("error"), office
            assert all(office.get(k) for k in ("name", "initial", "updated", "frameLoaded")), office
            assert all(office.get(k) == 200 for k in ("create", "info", "read1", "put", "read2")), office
            assert 200 in responses, {"office": office, "editorResponses": responses}
            context_summary = [{"id": c.get("id"), "origin": c.get("origin"),
                                "name": c.get("name"), "aux": c.get("auxData", {}).get("type")}
                               for c in contexts]
            target_summary = [{"type": t.get("type"), "url": t.get("url", "").split("?", 1)[0]}
                              for t in targets if t.get("type") in ("iframe", "page")]
            assert editor and editor["ready"] == "complete" and editor["bodyChildren"] > 0, {
                "contexts": context_summary, "targets": target_summary}
            assert editor["workspace"] and editor["controls"] > 0, editor
        finally:
            await cdp.eval(OFFICE_CLEANUP)

    print("OK installed authenticated Files/Blossom and Office/WOPI/editor checks")
    print(json.dumps({"folders": files["folderTiles"], "folderEntries": files["folderChips"],
                      "serverFiles": files["serverFiles"], "clientFiles": files["clientFiles"],
                      "syncedRoots": files["syncedRoots"], "syncAudit": files["syncAudit"],
                      "officeEditorHTTP": 200, "officeInteractive": True}))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (urllib.error.URLError, ConnectionRefusedError) as exc:
        print("SKIP installed Electron is not attached on the loopback CDP port; "
              "run this gate on the target desktop (" + str(exc.reason if hasattr(exc, "reason") else exc) + ")")
        sys.exit(2)
