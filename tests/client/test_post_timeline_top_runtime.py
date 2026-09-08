"""Live insertion after posting must keep the composer visible, while preserving reading anchors."""
import json
import re
import subprocess
import tempfile
from html import unescape
from pathlib import Path
import pytest
from .test_notes_new_draft_runtime import CHROME, ROOT


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome is not installed')
def test_new_post_keeps_top_and_preserves_scrolled_reading_position():
    source = (ROOT/'static/js/client/app.js').read_text()
    code = source[source.index('  function _tlNotes(feed){'):source.index('  function _putAnchor(')]
    exercise = '''
const feed=document.getElementById('feed'), notes=document.getElementById('tl-notes');
const check=(ok,msg)=>{if(!ok)throw Error(msg)};
const insert=()=>{const p=document.createElement('article');p.dataset.key='new';p.style.height='180px';notes.prepend(p);return p;};
try{
 let place=_tlAnchor(feed), before=feed.scrollTop;
 const added=insert();
 if(!_restoreTlAnchor(feed,place))feed.scrollTop=before;
 check(feed.scrollTop===0,'new post scrolled the composer out of view');
 added.remove();feed.scrollTop=350;
 const reading=_tlAnchor(feed), card=notes.querySelector('[data-key="'+reading.key+'"]');
 const top=card.getBoundingClientRect().top;
 insert();check(_restoreTlAnchor(feed,reading),'reading anchor was lost');
 check(Math.abs(card.getBoundingClientRect().top-top)<1,'post insertion moved the card being read');
 document.getElementById('result').textContent=JSON.stringify({passed:true});
}catch(e){document.getElementById('result').textContent=JSON.stringify({error:e.message})}
'''
    html = '''<!doctype html><style>#feed{height:400px;width:500px;overflow:auto;overflow-anchor:none}article{height:200px}</style>
<div id="feed"><div style="height:120px">Composer</div><div id="tl-notes">'''
    html += ''.join(f'<article data-key="{i}">Post {i}</article>' for i in range(12))
    html += '</div></div><pre id="result"></pre><script>'+code+exercise+'</script>'
    with tempfile.TemporaryDirectory(prefix='pc-post-scroll-') as directory:
        page=Path(directory)/'test.html';page.write_text(html)
        run=subprocess.run([CHROME,'--headless=new','--no-sandbox','--disable-gpu','--dump-dom',page.as_uri()],capture_output=True,text=True,timeout=30)
    assert run.returncode==0,run.stderr[-1000:]
    result=re.search(r'<pre id="result">(.*?)</pre>',run.stdout,re.S)
    assert result,run.stdout[-1000:]
    assert json.loads(unescape(result.group(1)))=={'passed':True}
