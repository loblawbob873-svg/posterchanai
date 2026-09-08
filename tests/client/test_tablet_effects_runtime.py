"""Computed browser styles must remove GPU blur without changing input or window geometry."""
import json
from html import unescape
from pathlib import Path
import re
import subprocess
import tempfile

import pytest
from .test_notes_new_draft_runtime import CHROME, ROOT


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome is not installed')
@pytest.mark.parametrize('style', ['', 'os-style-mac'])
def test_touch_low_power_removes_nested_blur_and_preserves_editor(style):
    source = (ROOT / 'static/js/client/os.js').read_text()
    functions = source[source.index('function desktopEffectsMode()'):source.index('function applyDesktopStyle()')]
    css = (ROOT / 'static/css/client.css').as_uri()
    script = """
const root=document.querySelector('.os-root'), FX_KEY='osCompositing';
let stored,coarse=true;
const settings=()=>({get:(_key,fallback)=>stored??fallback});
window.matchMedia=()=>({matches:coarse});
""" + functions + """
window.addEventListener('load',()=>{
 const input=document.querySelector('textarea');input.focus();input.setSelectionRange(2,4);
 const windowEl=document.querySelector('.osw');
 const rect=()=>{const r=windowEl.getBoundingClientRect();return [r.x,r.y,r.width,r.height]};
 const before=rect();
 applyDesktopEffects();
 const all=[root,...root.querySelectorAll('*')];
 const filters=all.flatMap(el=>[null,'::before','::after'].map(p=>getComputedStyle(el,p).backdropFilter));
 const off={filters,mode:root.classList.contains('os-fx-off'),rect:rect(),
   focus:document.activeElement===input,selection:[input.selectionStart,input.selectionEnd],
   sensitive:getComputedStyle(document.querySelector('.sensitive-test')).filter};
 stored='full';applyDesktopEffects();
 const modern=getComputedStyle(windowEl).backdropFilter;
 stored='off';coarse=false;applyDesktopEffects();
 document.querySelector('#result').textContent=JSON.stringify({before,off,modern,
   explicitOff:root.classList.contains('os-fx-off'),finalRect:rect(),value:input.value});
});
"""
    html = f'''<!doctype html><link rel="stylesheet" href="{css}">
    <div class="os-root {style}" style="width:1200px;height:800px">
      <section class="osw" style="left:30px;top:50px;width:500px;height:400px">
        <header class="osw-bar">Notes</header><div class="osw-body">
        <textarea>Tablet draft stays intact</textarea>
        <div class="mail-read-hd">Nested header</div>
        <div class="sensitive-test" style="filter:blur(8px)">Hidden sensitive content</div></div>
      </section><div class="os-noti">Notifications</div><div class="os-pop">Menu</div>
    </div><pre id="result"></pre><script>{script}</script>'''
    with tempfile.TemporaryDirectory(prefix='pc-tablet-effects-') as td:
        page = Path(td) / 'test.html';page.write_text(html)
        run = subprocess.run([CHROME, '--headless=new', '--no-sandbox', '--disable-gpu',
            '--virtual-time-budget=1000', '--dump-dom', page.as_uri()], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr[-1000:]
    match = re.search(r'<pre id="result">(.*?)</pre>', run.stdout, re.S)
    assert match, run.stdout[-1000:]
    got = json.loads(unescape(match.group(1)))
    assert got['off']['mode'] and got['explicitOff'], got
    assert set(got['off']['filters']) == {'none'}, got
    assert got['modern'] != 'none', got
    assert got['off']['sensitive'] == 'blur(8px)', got
    assert got['before'] == got['off']['rect'] == got['finalRect'], got
    assert got['off']['focus'] and got['off']['selection'] == [2,4], got
    assert got['value'] == 'Tablet draft stays intact'
