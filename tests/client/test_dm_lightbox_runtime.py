"""A decrypted DM image must open as visible, decoded media in real Chromium."""
from pathlib import Path
import json, re, shutil, subprocess, tempfile
import pytest

ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text()
CSS = (ROOT / "static/css/client.css").read_text()
CHROME = shutil.which("google-chrome-stable") or shutil.which("chromium")

def extract_function(name):
    start = APP.index("function " + name + "(")
    brace = APP.index("{", start)
    depth = 0
    for i in range(brace, len(APP)):
        if APP[i] == "{": depth += 1
        elif APP[i] == "}":
            depth -= 1
            if depth == 0: return APP[start:i + 1]
    raise AssertionError("unbalanced " + name)

@pytest.mark.skipif(not CHROME, reason="Chrome unavailable")
def test_encrypted_dm_image_click_opens_visible_decoded_lightbox():
    open_box = extract_function("openLightbox")
    png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLZVwAAAABJRU5ErkJggg=="
    html = f'''<!doctype html><style>{CSS}</style><div id="dm-msgs"><img id="dm" src="data:image/png;base64,{png}"></div><pre id="out"></pre><script>
    const _trapFocus=()=>{{}},_lbZoom=()=>{{}},_lbCopyImg=()=>{{}},_lbSaveMedia=()=>{{}},_lbToBlossom=()=>{{}};
    {open_box}
    document.querySelector('#dm-msgs').addEventListener('click',e=>{{const im=e.target.closest('img:not(.emoji-inline)');if(im){{e.preventDefault();openLightbox(im.currentSrc||im.src)}}}});
    document.querySelector('#dm').click();
    const done=()=>{{const bg=document.querySelector(':root > .lightbox'),im=bg&&bg.querySelector('img'),r=bg&&bg.getBoundingClientRect(),s=bg&&getComputedStyle(bg);document.querySelector('#out').textContent=JSON.stringify({{root:bg&&bg.parentNode===document.documentElement,decoded:im&&im.naturalWidth>0&&im.naturalHeight>0,visible:!!(r&&r.width>0&&r.height>0&&r.bottom>0&&r.right>0&&s.display!=='none'&&s.visibility!=='hidden'),z:s&&Number(s.zIndex)}});document.title='done'}};
    const x=document.querySelector('.lightbox img');if(x.complete)done();else x.addEventListener('load',done);
    </script>'''
    with tempfile.TemporaryDirectory() as td:
        path=Path(td)/"dm.html"; path.write_text(html)
        run=subprocess.run([CHROME,"--headless=new","--no-sandbox","--disable-gpu","--allow-file-access-from-files","--virtual-time-budget=3000","--dump-dom",path.as_uri()],capture_output=True,text=True,timeout=30)
    assert run.returncode == 0, run.stderr[-2000:]
    match=re.search(r'<pre id="out">(.*?)</pre>',run.stdout,re.S)
    assert match, run.stdout[-2000:]
    got=json.loads(match.group(1).replace('&quot;','"'))
    assert got == {"root": True, "decoded": True, "visible": True, "z": 100000}

def test_dm_uses_the_tested_delegated_image_handler():
    assert "m.addEventListener('click', ce=>{ const im=ce.target.closest('img:not(.emoji-inline)')" in APP
