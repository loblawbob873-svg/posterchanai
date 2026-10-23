"""THE CONCORD CALL MODULES ARE LOADED UNDER THE BUILD'S VERSION, NOT AS A BARE URL.

concord.js loads cord-voice.js and cord-call.js on demand (`callModule`), resolving the file name against its own
script URL -- and `new URL('cord-voice.js', '/static/js/client/concord.js?v=<build>')` DROPS the query. Every other
client script carries `?v=<build>` because Cloudflare rewrites /static assets to a 31-day max-age (app/routers/client.py
`_static_version`): a bare URL is one an edge or a browser may answer from a copy weeks old, so after a deploy the new
concord.js ran with an old call module. Found by tests/client/test_upgrade_from_old_builds.py, whose upgraded page
loaded `/static/js/client/cord-voice.js` with no version while every other script was today's.

Runs the shipped `callModule` (cut from concord.js) in node against a recording document.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / 'static/js/client/concord.js'


def _call_module_source(src):
    start = src.index('  async function callModule(file, name) {')
    return src[start:src.index('\n  }\n', start) + 4]


@pytest.mark.skipif(not shutil.which('node'), reason='node required')
def test_a_call_module_is_loaded_under_concord_js_own_version():
    src = SRC.read_text()
    assert re.search(r"const concordScriptUrl=document\.currentScript&&document\.currentScript\.src;", src)
    fn = _call_module_source(src)
    prog = r'''
const loaded=[];
global.window={};
global.document={createElement:()=>({}),head:{appendChild:s=>{loaded.push(s.src);window[s.__name]={};s.onload();}}};
const callModules=new Map();
const concordScriptUrl='https://poster.place/static/js/client/concord.js?v=1761234567';
%s
(async()=>{
  const orig=document.createElement;
  for(const [file,name] of [['cord-voice.js','PCCordVoice'],['cord-call.js','PCCordCall']]){
    document.createElement=()=>({__name:name});
    await callModule(file,name);
  }
  console.log(JSON.stringify(loaded));
})();
''' % fn
    out = subprocess.run(['node', '-e', prog], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    loaded = json.loads(out.stdout.strip())
    assert loaded == ['https://poster.place/static/js/client/cord-voice.js?v=1761234567',
                      'https://poster.place/static/js/client/cord-call.js?v=1761234567'], loaded
