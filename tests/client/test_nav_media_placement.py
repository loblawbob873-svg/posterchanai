"""Old bundled shells must recover Media Center outside collapsed Discover."""
import json
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[2] / 'static/js/client/app.js'


@pytest.mark.parametrize('existing', [True, False])
@pytest.mark.parametrize('anchor_location', ['nested', 'root', 'missing'])
def test_media_center_recovers_as_top_level(existing, anchor_location):
    source = APP.read_text()
    block = source[source.index('  const _NAV_REQUIRED ='):source.index('  function applyInstanceGating()')]
    script = r'''
const assert = require('node:assert/strict');
class Element {
  constructor() {
    this.children=[]; this.parentNode=null; this.dataset={}; this.classes=new Set(['nav-item','sub']);
    this.classList={remove: c=>this.classes.delete(c)};
  }
  appendChild(el) { return this.insertBefore(el,null); }
  insertBefore(el,before) {
    if(el.parentNode) el.parentNode.children.splice(el.parentNode.children.indexOf(el),1);
    const index=before ? this.children.indexOf(before) : this.children.length;
    assert.ok(index>=0); this.children.splice(index,0,el); el.parentNode=this; return el;
  }
  after(el) { this.parentNode.insertBefore(el,this.nextSibling); }
  get nextSibling() { return this.parentNode.children[this.parentNode.children.indexOf(this)+1] || null; }
  closest() { return group; }
  querySelector(sel) { return query(sel); }
}
const nav=new Element(), group=new Element(), sub=new Element(), toggle=new Element();
nav.appendChild(group); group.appendChild(toggle); group.appendChild(sub);
let media=EXISTING ? new Element() : null;
if(media) { media.dataset.view='media-center'; sub.appendChild(media); }
const repos=LOCATION==='missing' ? null : new Element();
if(repos) (LOCATION==='root' ? nav : sub).appendChild(repos);
function query(sel) {
  if(sel==='.sidebar .nav') return nav;
  if(sel==='#disc-toggle') return toggle;
  if(sel.includes('data-view="media-center"')) return media;
  if(sel.includes('data-view="repos"')) return repos;
  return null;
}
const document={querySelector:query,createElement:()=>new Element()};
const enc=s=>s;
BLOCK
ensureNavItems();
media=nav.children.find(el=>el.dataset.view==='media-center');
assert.ok(media, 'Media Center must be a direct child of the navigation');
assert.ok(!media.classes.has('sub') || media.className==='nav-item');
assert.equal(sub.children.includes(media),false,'Discover collapse must not hide Media Center');
const order=nav.children.slice();
ensureNavItems();
assert.deepEqual(nav.children,order,'repair must be idempotent');
'''.replace('EXISTING', json.dumps(existing)).replace('LOCATION', json.dumps(anchor_location)).replace('BLOCK', block)
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
