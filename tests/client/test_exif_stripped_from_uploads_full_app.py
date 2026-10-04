"""The bytes that actually leave the device for Blossom carry no camera metadata -- the shipped upload path.

"make sure we remove exif data on all blossom uploads". The byte-level rules are in
test_exif_stripped_from_uploads.py; this drives the REAL client's uploadBlob in the bundled app, with the
Blossom server stubbed at the network boundary, and reads the body that was PUT:

  small photo    under the size the compressor touches -- the case that used to go out with its GPS
  noCompress     the archival/Shared path that keeps the original size -- still no metadata
  rotated        a sideways phone photo comes out upright (pixels turned), not sideways and not with EXIF
"""
import asyncio
import base64
import io
import json
from pathlib import Path

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_exif_stripped_from_uploads import _exif, _img, jpeg


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


INIT = r"""
window.__puts=[];
{const prev=window.fetch;window.fetch=async function(url,opts={}){
  const u=String(url), m=String(opts.method||'GET').toUpperCase();
  if(u.includes('/client/config')){const r=await prev.apply(this,arguments);const d=await r.json();
    return new Response(JSON.stringify({...d,blossom_enabled:true}),{status:200,headers:{'Content-Type':'application/json'}});}
  if(u.includes('/client/blossom-access'))return new Response(JSON.stringify({allowed:true,whitelisted:true}),{status:200,headers:{'Content-Type':'application/json'}});
  if(m==='PUT'&&/\/upload$/.test(u)){
    const b=new Uint8Array(await opts.body.arrayBuffer());let s='';for(const x of b)s+=String.fromCharCode(x);
    __puts.push({type:(opts.headers||{})['Content-Type'],b64:btoa(s)});
    const sha=(__puts.length).toString(16).padStart(64,'0');
    return new Response(JSON.stringify({url:u.replace(/\/upload$/,'/')+sha,sha256:sha}),{status:200,headers:{'Content-Type':'application/json'}});}
  return prev.apply(this,arguments);};}
"""


def _rotated_jpeg():
    buf = io.BytesIO()
    _img().save(buf, "JPEG", exif=_exif(orientation=6).tobytes(), quality=95)     # 64x48 stored, shown 48x64
    return buf.getvalue()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_bytes_put_to_blossom_carry_no_camera_metadata():
    photos = {"small": jpeg(), "archival": jpeg(), "rotated": _rotated_jpeg()}
    res = {}

    async def check(b):
        await desktop.login(b)
        await b.until("!!window.PCExifStrip")
        for name, data, opts in (("small", photos["small"], "{}"), ("archival", photos["archival"], "{noCompress:true}"),
                                 ("rotated", photos["rotated"], "{}")):
            await b.js(f"""(async()=>{{const bin=Uint8Array.from(atob({json.dumps(base64.b64encode(data).decode())}),c=>c.charCodeAt(0));
                await __PC.uploadBlob(new File([bin],'{name}.jpg',{{type:'image/jpeg'}}),{opts});return true;}})()""")
        await b.until("__puts.length===3")
        res["puts"] = await b.js("__puts")

    asyncio.run(desktop.with_browser("online", "", check, INIT))
    out = {n: base64.b64decode(p["b64"]) for n, p in zip(("small", "archival", "rotated"), res["puts"])}
    for name, data in out.items():
        with Image.open(io.BytesIO(data)) as im:
            assert not dict(im.getexif()) and not im.getexif().get_ifd(0x8825), f"{name}: EXIF/GPS went to Blossom"
            assert b"PhoneModel" not in data and b"taken at home" not in data, name
    assert len(out["archival"]) > len(out["small"]) * 0.8, "the archival copy was re-encoded, not just stripped"
    with Image.open(io.BytesIO(out["rotated"])) as im:
        assert im.size == (48, 64), ("a sideways photo must be stored upright", im.size)


def test_encrypted_shares_are_cleaned_before_they_are_encrypted():
    """A DM attachment and a Concord attachment are encrypted for somebody ELSE to open, so no server can
    clean them: the strip must run on the plaintext, before the encryption call."""
    root = Path(__file__).resolve().parents[2] / "static/js/client"
    lib = (root / "musiclib.js").read_text()
    fn = lib[lib.index("async function uploadSharedEnc("):]
    assert fn.index("cleanFile(file)") < fn.index("_masterEncrypt("), "uploadSharedEnc encrypts before cleaning"
    cc = (root / "concord.js").read_text()
    up = cc[cc.index("const uploadAttachments=async"):]
    assert up.index("cleanFile(f)") < up.index("sealAttachment(clean)"), "Concord seals before cleaning"
