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

    async def call(self, method, params=None, events=None):
        self.seq += 1
        call_id = self.seq
        await self.ws.send(json.dumps({"id": call_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(await self.ws.recv())
            if events is not None and message.get("method"):
                events.append(message)
            if message.get("id") == call_id:
                if "error" in message:
                    raise RuntimeError(message["error"])
                return message["result"]

    async def eval(self, expression, events=None):
        result = await self.call("Runtime.evaluate", {
            "expression": expression,
            "awaitPromise": True,
            "returnByValue": True,
        }, events)
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
  const q=s=>document.querySelectorAll(s).length;
  return {
    view:__PC.VIEW, explorers:q('.fx-explorer'), folderTiles:q('.fx-home-tile'),
    folderChips:q('.folder-chip'), syncedRoots:q('.syncroot'),
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

    wrap=document.createElement('div'); wrap.style='position:fixed;left:-10000px;width:800px;height:600px';
    const frame=document.createElement('iframe'); frame.name='pc-office-installed-smoke'; wrap.appendChild(frame);
    const form=document.createElement('form'); form.method='post'; form.action=s.editor_url; form.target=frame.name;
    for(const [n,v] of [['access_token',s.token],['access_token_ttl',String(s.expires*1000)]]){
      const i=document.createElement('input'); i.name=n; i.value=v; form.appendChild(i);
    }
    wrap.appendChild(form); document.body.appendChild(wrap);
    const loaded=new Promise((resolve,reject)=>{const t=setTimeout(()=>reject(new Error('editor iframe timeout')),20000);
      frame.onload=()=>{clearTimeout(t);resolve(true)};});
    form.submit(); await loaded; await new Promise(r=>setTimeout(r,1500)); out.frameLoaded=true;
    return out;
  }catch(e){out.error=String(e&&e.message||e); return out;}
  finally{
    if(wrap) wrap.remove();
    if(s){const B=String(window.__PC_API_BASE__||'').replace(/\/$/,'');
      await fetch(B+'/client/office/session/'+s.id+'?access_token='+encodeURIComponent(s.token),
        {method:'DELETE'}).catch(()=>{});}
  }
})()"""


async def main():
    page = await choose_authenticated_page()
    async with CDP(page["webSocketDebuggerUrl"]) as cdp:
        files = await cdp.eval(FILES_CHECK)
        assert files["view"] == "blossom", files
        assert files["explorers"] == 1 and files["folderTiles"] > 0 and files["folderChips"] > 0, files
        assert files["syncedRoots"] > 0, files
        assert not files["overflow"] and not files["errors"], files

        await cdp.call("Network.enable")
        events = []
        office = await cdp.eval(OFFICE_CHECK, events)
        responses = [int(e["params"]["response"].get("status", 0)) for e in events
                     if e.get("method") == "Network.responseReceived"
                     and "/office-code/browser/" in e["params"]["response"].get("url", "")]
        assert not office.get("error"), office
        assert all(office.get(k) for k in ("name", "initial", "updated", "frameLoaded")), office
        assert all(office.get(k) == 200 for k in ("create", "info", "read1", "put", "read2")), office
        assert 200 in responses, {"office": office, "editorResponses": responses}

    print("OK installed authenticated Files/Blossom and Office/WOPI/editor checks")
    print(json.dumps({"folders": files["folderTiles"], "folderEntries": files["folderChips"],
                      "syncedRoots": files["syncedRoots"], "officeEditorHTTP": 200}))


if __name__ == "__main__":
    asyncio.run(main())
