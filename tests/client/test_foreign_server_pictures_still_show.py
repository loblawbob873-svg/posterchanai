"""A PICTURE ON ANOTHER APP'S SERVER STILL SHOWS IN FILES.

Josephus, 2026-10-13: "no thumbnails in his Files, no idea what picture is what". A tile asks for
`<server>/thumb/<sha>`, a route only THIS app's Blossom server has; a picture uploaded by another Nostr app
lives on a server that answers that with 404, and the tile fell straight back to a file icon. Every Blossom
server serves the blob itself, so a failed preview now loads the ORIGINAL (scaled by the tile), and only a
picture that cannot be loaded at all becomes the icon. Runs the shipped blobThumb + _bindThumbFallback."""
import subprocess

from tests.client_source import client_source


def test_a_failed_preview_loads_the_original_before_giving_up():
    src = client_source()
    code = src[src.index("  function _bindThumbFallback("):src.index("  // What a downloaded blob should be SAVED as.")]
    harness = r'''
const assert=require('node:assert/strict');
const enc=s=>String(s).replace(/"/g,'&quot;');
const thumbUrl=u=>u.replace(/\/([0-9a-f]+)$/,'/thumb/$1');
const extOfBlob=b=>'jpg', mimeForName=n=>/\.jpe?g$/.test(n)?'image/jpeg':'', _fxFileGlyph=k=>'['+k+']';
let imgs=[];
function parse(html){ const m=html.match(/<img[^>]*>/); const attrs={}; m[0].replace(/([a-z-]+)="([^"]*)"/g,(_,k,v)=>{attrs[k]=v;});
  return {className:attrs['class'],src:attrs.src,dataset:{ext:attrs['data-ext'],full:attrs['data-full']},
          replaceWith(d){this.replaced=d;}}; }
const document={createElement:()=>({className:'',innerHTML:''})};
const $$=(sel,root)=>root.imgs.filter(i=>sel==='.'+i.className);
CODE
const b={url:'https://nostr.build/abc123',type:'image/jpeg',sha256:'abc123'};
const im=parse(blobThumb(b,'jpg'));
assert.equal(im.src,'https://nostr.build/thumb/abc123');
_bindThumbFallback({imgs:[im]});
im.onerror();
assert.equal(im.src,'https://nostr.build/abc123','a failed preview did not load the original picture');
assert.ok(!im.replaced,'the picture was replaced by an icon before the original was tried');
im.onerror();
assert.ok(im.replaced,'a picture that cannot load at all must become the icon');
'''.replace('CODE', code)
    r = subprocess.run(['node', '-e', harness], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr[-1500:]
