"""POSTERCHANOS: THE MEME BUILDER RENDERS ON THE MACHINE (desktop/meme-local.js).

"for PosterChanOS, most of memebuilder should process on that machine (minus the AI features that need
network)". The desktop app runs the server's OWN renderer (meme_builder_service.render, from
/opt/posterchan-server) in a child python, and keeps layer media on disk when it cannot be uploaded.
These run the shipped module under node against the real renderer in this tree.
"""
import base64
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")

CLI = r"""
const m = require(process.argv[1]);
let raw = ''; process.stdin.on('data', d => raw += d).on('end', async () => {
  const j = JSON.parse(raw);
  const b = s => Buffer.from(s, 'base64');
  let out;
  if (j.op === 'store') { try { out = { ok: true, path: m.store(j.dir, b(j.b64), j.name, j.type) }; } catch (e) { out = { ok: false, error: e.message }; } }
  else if (j.op === 'local') out = { file: m.localFile(j.dir, j.ref) };
  else if (j.op === 'available') out = { ok: m.available(j.app) };
  else {
    const r = await m.render({ edit: j.edit, sources: (j.sources || []).map(s => ({ key: s.key, bytes: b(s.b64) })) },
                             { appDir: j.app, storeDir: j.dir, python: j.python });
    out = r.ok ? { ok: true, mime: r.mime, b64: r.bytes.toString('base64') } : r;
  }
  process.stdout.write(JSON.stringify(out));
});
"""


def call(**job):
    job.setdefault("app", str(ROOT))
    job.setdefault("python", sys.executable)
    done = subprocess.run([NODE, "-e", CLI, str(ROOT / "desktop/meme-local.js")], input=json.dumps(job),
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout)


def png(rgb, size=(64, 64)):
    buf = io.BytesIO()
    Image.new("RGB", size, rgb).save(buf, "PNG")
    return buf.getvalue()


pytestmark = pytest.mark.skipif(NODE is None, reason="needs node")


def test_a_kept_file_is_content_addressed_and_nothing_else_resolves(tmp_path):
    a = call(op="store", dir=str(tmp_path), b64=base64.b64encode(png((1, 2, 3))).decode(), name="x.png", type="image/png")
    again = call(op="store", dir=str(tmp_path), b64=base64.b64encode(png((1, 2, 3))).decode(), name="y.PNG", type="")
    assert a["ok"] and a["path"] == again["path"] and a["path"].startswith("/__memelocal/") and a["path"].endswith(".png")
    assert len(list(tmp_path.iterdir())) == 1
    assert call(op="local", dir=str(tmp_path), ref="app://posterchan" + a["path"])["file"].startswith(str(tmp_path))
    (tmp_path.parent / "secret.png").write_bytes(b"x")
    for ref in ("/__memelocal/../secret.png", "/__memelocal/%2e%2e/secret.png", "/__hostfile/etc/passwd",
                "/__memelocal/" + "0" * 64 + ".png"):
        assert call(op="local", dir=str(tmp_path), ref=ref)["file"] == "", ref


def test_the_machine_renders_with_the_servers_own_renderer(tmp_path):
    kept = call(op="store", dir=str(tmp_path), b64=base64.b64encode(png((200, 30, 30))).decode(), name="a.png", type="image/png")
    online = "https://blossom.example/" + "b" * 64 + ".png"
    edit = {"w": 200, "h": 100, "fmt": "png", "still": 0, "bg": "#000000", "layers": [
        {"type": "image", "src": "app://posterchan" + kept["path"], "start": 0, "dur": 3, "x": 0, "y": 0, "w": 100, "h": 100, "opacity": 1},
        {"type": "image", "src": online, "start": 0, "dur": 3, "x": 100, "y": 0, "w": 100, "h": 100, "opacity": 1},
    ]}
    r = call(op="render", dir=str(tmp_path), edit=edit,
             sources=[{"key": online, "b64": base64.b64encode(png((30, 30, 200))).decode()}])
    assert r.get("ok"), r
    assert r["mime"] == "image/png"
    im = Image.open(io.BytesIO(base64.b64decode(r["b64"]))).convert("RGB")
    assert im.size == (200, 100)
    left, right = im.getpixel((50, 50)), im.getpixel((150, 50))
    assert left[0] > 150 and left[2] < 80, ("the layer kept on this machine is missing", left)
    assert right[2] > 150 and right[0] < 80, ("the layer handed over as bytes is missing", right)


def test_a_refused_edit_says_why_and_a_missing_renderer_says_so(tmp_path):
    r = call(op="render", dir=str(tmp_path), edit={"layers": [], "fmt": "png"})
    assert r["ok"] is False and "layer" in r["error"], r
    assert call(op="available", app=str(tmp_path))["ok"] is False
    r = call(op="render", dir=str(tmp_path), app=str(tmp_path), edit={"layers": [{"type": "text", "text": "x"}]})
    assert r["ok"] is False and "not installed" in r["error"], r
