"""A post's own `imeta … m image/jpeg` makes an extension-less URL a picture.

"images from this fediverse server is not showing , just link": outerheaven.club serves attachments from
object storage as ".../mediaheaven/<hash>.C117AB17-3C90-…" -- no file extension -- and mediaParts judged
media by extension alone, so both pictures rendered as bare links even though the event declared them
image/jpeg. Runs the SHIPPED mediaParts under node against that post's real content and tags.
"""
import json
import shutil
import subprocess

import pytest

from tests.client_source import client_source

APP = client_source()
NODE = shutil.which("node")


def _function(name):
    start = APP.index(f"function {name}(")
    depth, quote, esc = 0, None, False
    for pos in range(APP.index("{", start), len(APP)):
        c = APP[pos]
        if quote:
            if esc: esc = False
            elif c == "\\": esc = True
            elif c == quote: quote = None
            continue
        if c in "'\"`": quote = c
        elif c == "{": depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return APP[start:pos + 1]
    raise AssertionError(name)


U1 = "https://nbg1.your-objectstorage.com/mediaheaven/0a1b2c3d4e5f.C117AB17-3C90-4E4B-9A8D-7F2E1A6B5C4D"
U2 = "https://nbg1.your-objectstorage.com/mediaheaven/9f8e7d6c5b4a.0D3E2F1A-4B5C-4D6E-8F7A-9B0C1D2E3F4A"
PLAIN = "https://example.com/some/page"
VID = "https://cdn.example.net/v/abcdef"


_m = APP.index("const _isMediaUrl=")
_IS_MEDIA = APP[_m:APP.index(";", _m) + 1]


def _run(content, tags):
    helpers = _function("_imetaKinds") if "function _imetaKinds(" in APP else "function _imetaKinds(){return new Map()}"
    js = f"""
      const S={{NO_IMAGES:false}}, BLOBF='', enc=x=>String(x);
      const MediaDims={{seed(){{}}}};
      const _media=(e,kind)=>kind==='video'?'<video src="'+e+'"></video>':'<img src="'+e+'">';
      {_IS_MEDIA}
      {helpers}
      {_function('mediaParts')}
      const r=mediaParts({json.dumps(content)}, {{tags:{json.dumps(tags)}}});
      console.log(JSON.stringify({{text:r.text, items:r.items, first:r.mediaFirst}}));
    """
    out = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(not NODE, reason="node required")
def test_extensionless_images_declared_by_imeta_render_as_pictures():
    content = f"look at this\n{U1}\n{U2}\nand {PLAIN}"
    tags = [["imeta", f"url {U1}", "m image/jpeg"], ["imeta", f"url {U2}", "m image/jpeg"]]
    r = _run(content, tags)
    assert r["items"] == [f'<img src="{U1}">', f'<img src="{U2}">'], r
    assert U1 not in r["text"] and U2 not in r["text"], ("still shown as links", r["text"])
    assert PLAIN in r["text"], "an ordinary link must stay a link"


@pytest.mark.skipif(not NODE, reason="node required")
def test_imeta_video_and_media_first_and_no_imeta_stays_a_link():
    r = _run(f"{VID} clip", [["imeta", f"url {VID}", "m video/mp4"]])
    assert r["items"] == [f'<video src="{VID}"></video>'] and r["first"] is True, r
    # No imeta, or an imeta that is not image/video: nothing changes.
    r = _run(f"see {U1}", [])
    assert r["items"] == [] and U1 in r["text"], r
    r = _run(f"see {U1}", [["imeta", f"url {U1}", "m application/pdf"]])
    assert r["items"] == [] and U1 in r["text"], r
