"""PosterChanOS Monero widget lifecycle in a real Chromium DOM."""
from html import unescape
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
OSJS = (ROOT / "static/js/client/os.js").read_text()
CSS = (ROOT / "static/css/client.css").read_text()
CHROME = shutil.which("google-chrome-stable") or shutil.which("chromium") or shutil.which("chrome")


def shipped_widget_code():
    start = OSJS.index("function _xmrDisplay(")
    end = OSJS.index("\n  /* WMO weather codes", start)
    return OSJS[start:end]


@pytest.mark.skipif(not CHROME, reason="Chrome unavailable")
def test_widget_success_resize_actions_error_and_removed_response_runtime():
    js = shipped_widget_code()
    script = f'''
    const $=(s,r=document)=>r.querySelector(s),_wgtFeeds=new Map();let opened=[],received=0,sent=0,refreshes=0,mode='ok';
    const authFetch=async path=>{{
      if(mode==='delay')await new Promise(r=>setTimeout(r,60));
      if(mode==='error')return new Response(JSON.stringify({{detail:'Local Monero wallet is unavailable'}}),{{status:503}});
      if(mode==='disabled')return new Response(JSON.stringify({{detail:'Monero tipping wallet is disabled'}}),{{status:503}});
      const body=path.endsWith('/status')?{{network:mode==='mainnet'?'mainnet':'stagenet',mainnet:mode==='mainnet'}}:{{wallet_rpc_reachable:true,daemon_connected:true,network:mode==='mainnet'?'mainnet':'stagenet',balance:'1234567890123.456789012345',unlocked_balance:'42.5'}};
      return new Response(JSON.stringify(body),{{status:200,headers:{{'Content-Type':'application/json'}}}});
    }};
    const PC=()=>({{authFetch}}),_api=p=>p,openApp=v=>opened.push(v),_wgtRefreshOne=()=>refreshes++;
    window.PCMoneroWallet={{openReceive:()=>received++,openSend:()=>sent++}};
    let _wgtFeed=async(k,t,f)=>f();
    {js}
    (async()=>{{
      const def=_moneroWidget(),host=document.querySelector('.os-wgt'),body=host.querySelector('.os-wgt-body');def.mount(body);await def.refresh(body);
      const success={{balance:$('.wgt-xmr-bal strong',body).textContent,net:$('.wgt-xmr-net',body).textContent,rpc:$('[data-rpc]',body).textContent,node:$('[data-node]',body).textContent,at:$('[data-at]',body).textContent,addressLeaked:/[4578][1-9A-HJ-NP-Za-km-z]{{94}}/.test(body.textContent)}};
      for(const a of ['open','receive','send','refresh'])body.querySelector('[data-xmr="'+a+'"]').click();
      host.dataset.size='s';const compact={{overflow:host.scrollWidth-host.clientWidth,vertical:body.scrollHeight-body.clientHeight,buttons:[...body.querySelectorAll('button')].map(b=>b.getBoundingClientRect().width)}};
      mode='error';await def.refresh(body);const error={{net:$('.wgt-xmr-net',body).textContent,rpc:$('[data-rpc]',body).textContent,error:$('.wgt-xmr',body).classList.contains('is-error')}};
      mode='disabled';await def.refresh(body);const disabled={{net:$('.wgt-xmr-net',body).textContent,rpc:$('[data-rpc]',body).textContent}};
      mode='mainnet';await def.refresh(body);const mainnet={{net:$('.wgt-xmr-net',body).textContent,warn:$('.wgt-xmr-warn',body).textContent,hidden:$('.wgt-xmr-warn',body).hidden}};
      const lateHost=document.createElement('section');lateHost.className='os-wgt';lateHost.innerHTML='<div class="os-wgt-body"></div>';document.body.appendChild(lateHost);const lateBody=lateHost.firstElementChild;def.mount(lateBody);mode='delay';const pending=def.refresh(lateBody);lateHost.remove();await pending;
      out.textContent=JSON.stringify({{success,compact,error,disabled,mainnet,actions:{{opened,received,sent,refreshes}},late:$('.wgt-xmr-net',lateBody).textContent}});
    }})().catch(e=>out.textContent=JSON.stringify({{fatal:String(e),stack:e.stack}}));
    '''
    html = f'''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><style>{CSS}html,body{{margin:0}}.os-wgt{{position:relative;width:290px;height:176px}}.os-wgt[data-size=s]{{width:210px;height:118px}}</style><section class="os-wgt" data-type="monero" data-size="m"><div class="os-wgt-body"></div></section><pre id="out"></pre><script>{script}</script>'''
    with tempfile.TemporaryDirectory() as td:
        page = Path(td) / "widget.html"; page.write_text(html)
        done = subprocess.run([CHROME,"--headless=new","--no-sandbox","--disable-gpu","--window-size=900,700","--virtual-time-budget=1000","--dump-dom",page.as_uri()],text=True,capture_output=True,timeout=30)
    assert done.returncode == 0, done.stderr[-1000:]
    match = re.search(r'<pre id="out">(.*?)</pre>', done.stdout, re.S); assert match and match.group(1)
    got = json.loads(unescape(match.group(1))); assert "fatal" not in got, got
    assert got["success"]["balance"] == "1,234,567,890,123.456789012345"
    assert got["success"]["net"] == "STAGENET"
    assert got["success"]["rpc"] == "RPC · online" and got["success"]["node"] == "Node · connected"
    assert got["success"]["at"].startswith("Updated ") and got["success"]["addressLeaked"] is False
    assert got["compact"]["overflow"] <= 0 and got["compact"]["vertical"] <= 0 and min(got["compact"]["buttons"]) > 0
    assert got["actions"] == {"opened":["wallet"],"received":1,"sent":1,"refreshes":1}
    assert got["error"] == {"net":"OFFLINE","rpc":"RPC · unreachable","error":True}
    assert got["disabled"] == {"net":"DISABLED","rpc":"RPC · disabled"}
    assert got["mainnet"] == {"net":"MAINNET","warn":"MAINNET hot wallet · small tips only","hidden":False}
    assert got["late"] == "checking…"


def test_registry_and_picker_include_monero_without_sensitive_markup():
    assert "monero: _moneroWidget()" in OSJS
    body = shipped_widget_code().lower()
    for secret in ("password", "credential", "spend_key", "seed", "tx_hash", "address"):
        assert secret not in body
