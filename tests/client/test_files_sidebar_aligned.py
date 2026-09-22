"""Files → the sidebar's folder rows line up ("you can see on Desktop that All and Music are not aligned").

Rendered with the SHIPPED client.css and icon sprite in headless Chrome, the rows shaped exactly as
app.js emits them (_fxSidebarHTML / _fxSyncedHTML / _fxHostHTML). The bug: `button:has(> svg.ic.b-ic:
only-child)` — meant for icon-only buttons — also matches an icon + NAME row (`:only-child` ignores
text) and outranked the sidebar's left alignment, so each row was centred by its own name length.
"""
import html
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT=Path(__file__).resolve().parents[2]
css=(ROOT/'static/css/client.css').read_text(); sprite=(ROOT/'static/js/client/sprite.js').read_text()
ic=lambda k:f'<svg class="ic b-ic fx-folder-ic fx-folder-{k}" aria-hidden="true"><use href="#i-{k}"></use></svg>'
side=f'''<div class="fx-tree"><section class="fx-tree-node"><button class="fx-tree-head active" data-fxtoggle="blossom"><span class="chev">▾</span><svg class="ic b-ic" aria-hidden="true"><use href="#i-folder"></use></svg><b>Files</b></button>
<div class="fx-tree-children" data-fxtree="blossom"><div class="folder-bar">
<button class="folder-chip active" data-folder=""><svg class="ic b-ic" aria-hidden="true"><use href="#i-folder"></use></svg>All</button>
<button class="folder-chip" data-folder="Music">{ic('music')}Music</button><button class="folder-chip" data-folder="Pictures">{ic('folder')}Pictures</button><button class="folder-chip" data-folder="Vault">{ic('lock')}Vault</button>
</div></div></section>
<section class="fx-tree-node"><button class="fx-tree-head" data-fxtoggle="synced"><span class="chev">▾</span><svg class="ic b-ic"><use href="#i-refresh"></use></svg><b>Synced Folders</b></button><div class="fx-tree-children" data-fxtree="synced"><span class="fx-syncwrap"><button class="folder-chip syncroot" data-synckey="Documents"><svg class="ic b-ic"><use href="#i-refresh"></use></svg>Documents<span class="fx-n">412</span></button><button class="fx-syncx">✕</button></span></div></section>
<section class="fx-tree-node"><button class="fx-tree-head" data-fxtoggle="computer"><span class="chev">▾</span><svg class="ic b-ic"><use href="#i-monitor"></use></svg><b>My Computer</b></button><div class="fx-tree-children" data-fxtree="computer"><button class="folder-chip" data-host="1"><svg class="ic b-ic"><use href="#i-folder"></use></svg>Home</button></div></section></div>'''
js='''const out=[];for(const b of document.querySelectorAll('.fx-side .folder-chip, .fx-side .fx-tree-head')){const r=b.getBoundingClientRect();const w=document.createTreeWalker(b,NodeFilter.SHOW_TEXT);let t,tx=null;while((t=w.nextNode())){if(t.textContent.trim()&&!t.parentElement.closest('.fx-n,.chev')){const g=document.createRange();const i=t.textContent.search(/\\S/);g.setStart(t,i);g.setEnd(t,i+1);tx=Math.round(g.getBoundingClientRect().x);break}}const svg=b.querySelector('svg');const cs=getComputedStyle(b);out.push({jc:cs.justifyContent,disp:cs.display,w:Math.round(r.width),row:b.textContent.trim().slice(0,14),left:Math.round(r.x),icon:svg?[Math.round(svg.getBoundingClientRect().x),Math.round(svg.getBoundingClientRect().width)]:null,text:tx})}document.getElementById('r').textContent=JSON.stringify(out)'''
page=f'<!doctype html><style>{css}</style><script>{sprite}</script><div class="fx-explorer" style="width:1000px"><div class="fx-side" style="width:280px">{side}</div></div><pre id="r"></pre><script>setTimeout(()=>{{{js}}},100)</script>'


def _rows():
    d=tempfile.mkdtemp(); p=Path(d)/'t.html'; p.write_text(page)
    chrome=next((shutil.which(c) for c in ('google-chrome-stable','google-chrome','chromium','chromium-browser') if shutil.which(c)),None)
    if chrome is None: pytest.skip('Chrome is required', allow_module_level=True)
    o=subprocess.run([chrome,'--headless=new','--no-sandbox','--disable-gpu','--virtual-time-budget=2000','--window-size=1200,800','--dump-dom',p.as_uri()],capture_output=True,text=True,timeout=60).stdout
    m=re.search(r'<pre id="r">(.*?)</pre>',o,re.S); rows=json.loads(html.unescape(m.group(1)))
    
    return rows


def test_every_folder_row_starts_its_icon_and_name_at_the_same_place():
    rows = [r for r in _rows() if not r["row"].startswith("▾")]
    assert len(rows) >= 6, rows
    icons = {r["row"]: r["icon"][0] for r in rows}
    texts = {r["row"]: r["text"] for r in rows}
    assert max(icons.values()) - min(icons.values()) <= 2, f"folder icons are not in one column: {icons}"
    assert max(texts.values()) - min(texts.values()) <= 3, f"folder names are not in one column: {texts}"


def test_the_rows_are_emitted_the_way_this_test_renders_them():
    app = (ROOT / "static/js/client/app.js").read_text()
    assert "${_fxFolderIcon(f)}${enc(f)}</button>" in app, "icon and name must not be separated by a space"
    assert '>🔄 ${enc(f.key)}' not in app, "synced rows use the same svg icon as every other row"
