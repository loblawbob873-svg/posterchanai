"""The instance welcome is shown ONCE per account on a device — a page rebuild must not bring it back.

Reported from Android: "I was in the settings taking a screen shot then when I hit the back button to go back it
just popped up". Android can rebuild the app's page when you leave and come back; the welcome remembered only in
MEMORY that it had asked, and "Maybe later" was not remembered at all, so every rebuild showed it again over
whatever you were doing. Runs the real module in Chrome; a "rebuild" is the module evaluated again against the
same device storage, which is exactly what a reload keeps.
"""
import json
import re
import shutil
import subprocess
from html import unescape
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHROME = shutil.which('google-chrome-stable') or shutil.which('chromium')
pytestmark = pytest.mark.skipif(not CHROME, reason='Chrome unavailable')


def run(tmp_path, scenario):
    css = (ROOT / 'static/css/client.css').read_text() + (ROOT / 'static/css/instance-welcome.css').read_text()
    access = (ROOT / 'static/js/client/instance-access.js').read_text()
    welcome = (ROOT / 'static/js/client/instance-welcome.js').read_text().replace('</script>', '<\\/script>')
    page = tmp_path / 'once.html'
    page.write_text(f'''<!doctype html><html><meta name="viewport" content="width=device-width,initial-scale=1">
<style>{css}</style><body><pre id="result"></pre>
<script id="welcome-src" type="text/plain">{welcome}</script><script>
let account='a'.repeat(64), statusCalls=0;
window.PCOS={{isOn:()=>false}}; window.PCOSWin={{isWindow:()=>false}};
window.__PC_BOOTED=true;
window.__PC={{viewer:()=>({{pubkey:account}}), standalone:()=>false, LOGO:'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg"/>',
  signTemplate:async t=>t}};
window.setInterval=()=>0;
window.fetch=async url=>({{ok:true,json:async()=>{{statusCalls++;return {{eligible:true,pending:false,site_name:'Example',domain:'example.test'}};}}}});
try{{localStorage.clear();}}catch(_){{}}
const boot=()=>(0,eval)(document.getElementById('welcome-src').textContent);   // a fresh page = a fresh module
const wait=ms=>new Promise(r=>setTimeout(r,ms));
const open=()=>!!document.querySelector('dialog.instance-welcome[open]');
</script><script>{access}</script><script>
(async()=>{{
  const out={{}};
  boot(); await wait(150);
  out.firstShown=open();
  const later=document.querySelector('dialog.instance-welcome .iw-later'); if(later) later.click();
  await wait(50); out.closedByLater=!open();
  if('{scenario}'==='aged'){{ for(const k of Object.keys(localStorage)) if(k.startsWith('pc_iw_seen_'))
      localStorage.setItem(k, String(Date.now()-8*86400e3)); }}
  if('{scenario}'==='other_account') account='b'.repeat(64);
  document.querySelectorAll('dialog').forEach(d=>d.remove());
  boot(); await wait(150);                       // the page was rebuilt
  out.afterRebuild=open();
  document.getElementById('result').textContent=JSON.stringify(out);
}})();
</script></body></html>''')
    done = subprocess.run([CHROME, '--headless=new', '--no-sandbox', '--disable-gpu', '--window-size=390,900',
                           '--allow-file-access-from-files', '--virtual-time-budget=3000', '--dump-dom', page.as_uri()],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr[-1500:]
    m = re.search(r'<pre id="result">(.*?)</pre>', done.stdout, re.S)
    assert m and m.group(1), done.stdout[-1500:]
    return json.loads(unescape(m.group(1)))


def test_a_rebuilt_page_does_not_show_the_welcome_again(tmp_path):
    got = run(tmp_path, 'same')
    assert got['firstShown'] and got['closedByLater'], got
    assert got['afterRebuild'] is False, "the welcome came back after the page was rebuilt"


def test_another_account_on_the_device_still_gets_its_welcome(tmp_path):
    assert run(tmp_path, 'other_account')['afterRebuild'] is True


def test_the_welcome_comes_back_after_a_week(tmp_path):
    assert run(tmp_path, 'aged')['afterRebuild'] is True
