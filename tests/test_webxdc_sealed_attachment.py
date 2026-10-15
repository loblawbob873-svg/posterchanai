"""A .xdc that Armada/Vector posted ENCRYPTED in a Concord room is decrypted before it is opened.

Reported 2026-10-14: Josephus's DC20 Dice would not play in the PosterChan room -- "not a zip file (no
end-of-central-directory record)". His client seals every room attachment, the game included: the imeta reads
`m application/x-webxdc` AND `encryption-algorithm aes-gcm` + `decryption-key` + `decryption-nonce`, with `x`
the hash of the CIPHERTEXT and `ox` the original's. Measured on his real post: the download hashes to `x`,
starts with random bytes, and decrypts (16-byte nonce) to a valid 99 KB zip whose hash is `ox`. Our card handed
the player the ciphertext.

Both shipped halves run under node against a game sealed exactly that way: concord.js's `webxdcOf` must carry the
key to the card, and webxdc.js's `load` must decrypt, check `ox`, and open the archive. A tampered ciphertext or a
wrong `ox` must refuse rather than run.
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROOT = Path(__file__).resolve().parents[1]
WEBXDC_JS = ROOT / "static/js/client/webxdc.js"
ZIP_JS = ROOT / "static/js/client/zip.js"
CONCORD_JS = ROOT / "static/js/client/concord.js"

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")


def _sealed():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("index.html", "<!doctype html><script src=webxdc.js></script><p>roll d20")
        z.writestr("manifest.toml", 'name = "DC20 Dice"\n')
    plain = buf.getvalue()
    key, nonce = os.urandom(32), os.urandom(16)          # Armada's nonce is 16 bytes, not 12
    cipher = AESGCM(key).encrypt(nonce, plain, None)
    return {"cipher": cipher.hex(), "key": key.hex(), "nonce": nonce.hex(),
            "x": hashlib.sha256(cipher).hexdigest(), "ox": hashlib.sha256(plain).hexdigest()}


def _node(script):
    out = subprocess.run(["node", "-e", script], capture_output=True, timeout=90)
    assert out.returncode == 0, out.stderr.decode()[-3000:]
    return json.loads(out.stdout.decode().strip().splitlines()[-1])


def _load(s, app):
    return _node(f"""
      const S = {json.dumps(s)}, APP = {json.dumps(app)};
      const hex = h => Uint8Array.from(h.match(/../g).map(x => parseInt(x, 16)));
      global.window = {{ addEventListener(){{}}, __PC: {{ apiBase:()=>'' , toast(){{}} }} }};
      global.document = {{ addEventListener(){{}}, querySelectorAll:()=>[], getElementById:()=>null,
                          createElement:()=>({{ setAttribute(){{}}, classList:{{add(){{}}}}, appendChild(){{}}, remove(){{}}, style:{{}} }}) }};
      global.location = {{ hostname:'example.com', href:'https://example.com/' }};
      global.fetch = async u => new Response(hex(S.cipher), {{ status:200 }});
      require({json.dumps(str(ZIP_JS))});
      require({json.dumps(str(WEBXDC_JS))});
      (async () => {{
        try{{
          const got = await window.PCWebxdc.__load(APP);
          console.log(JSON.stringify({{ ok:true, files:[...got.index.keys()].sort() }}));
        }}catch(e){{ console.log(JSON.stringify({{ ok:false, error:String(e && e.message || e) }})); }}
      }})();
    """)


def _app(s, **enc):
    e = {"key": s["key"], "nonce": s["nonce"], "ox": s["ox"]}
    e.update(enc)
    return {"url": "https://blossom.example/" + s["x"] + ".xdc", "sha": s["x"], "uuid": "u-1", "name": "DC20 Dice",
            "enc": e}


def test_a_sealed_game_is_decrypted_checked_and_opened():
    s = _sealed()
    got = _load(s, _app(s))
    assert got == {"ok": True, "files": ["index.html", "manifest.toml"]}, got


def test_without_the_key_it_is_the_reported_error_and_with_a_wrong_ox_it_refuses():
    s = _sealed()
    plain = dict(_app(s))
    plain.pop("enc")
    keyless = _load(s, plain).get("error", "")
    assert "looks encrypted" in keyless and "key" in keyless, ("a keyless sealed game must say why, not 'not a zip'", keyless)
    bad = _load(s, _app(s, ox="0" * 64))
    assert bad.get("ok") is False and "does not match" in bad.get("error", ""), bad


def _cards(tag, text=""):
    got = _node(f"""
      const fs = require('fs'), vm = require('vm');
      const src = fs.readFileSync({json.dumps(str(CONCORD_JS))}, 'utf8'), seen = [], noop = () => {{}};
      const msg = {{ id:'m1', text:{json.dumps(text)}, tags:[{json.dumps(tag)}], at:1, remote:true }};
      const rows = [{{ dataset:{{messageId:'m1'}}, querySelector:()=>({{ querySelector:()=>null, insertAdjacentHTML:noop }}) }}];
      const document = {{ querySelectorAll:s=>s==='.cc-message[data-message-id]'?rows:[], querySelector:()=>null,
                         createElement:()=>({{dataset:{{}}}}), head:{{appendChild:noop}}, documentElement:{{appendChild:noop}}, addEventListener:noop }};
      const PCWebxdc = {{ cardHtml: app => {{ seen.push(app); return '<div class="xdc-card"></div>'; }} }}, q = [];
      const window = {{ document, PCWebxdc, addEventListener:noop, requestAnimationFrame:fn=>q.push(fn) }};
      const localStorage = {{ getItem:k=>k==='pc.concord.test.room'?JSON.stringify([msg]):null, setItem:noop, removeItem:noop }};
      vm.runInNewContext(src, {{ window, document, PCWebxdc, console, setTimeout:fn=>q.push(fn), clearTimeout:noop, URL, atob,
                                crypto:{{}}, localStorage, sessionStorage:{{getItem:()=>null,setItem:noop}} }});
      window.PCConcord.hydrateWebxdcCards({{ naddr:'room', channels:[{{name:'general'}}] }});
      while(q.length) q.shift()();
      console.log(JSON.stringify(seen.map(a => ({{ sha:a.sha, enc:a.enc||null, uuid:a.uuid }}))));
    """)
    return got


def _seal_fields(s):
    return ["encryption-algorithm aes-gcm", "decryption-key " + s["key"], "decryption-nonce " + s["nonce"], "ox " + s["ox"]]


def test_concord_hands_the_key_to_the_card():
    """concord.js's webxdcOf reads the imeta the way Josephus's client wrote it (fields from his real post)."""
    s = _sealed()
    url = "https://blossom.example/" + s["x"] + ".xdc"
    tag = ["imeta", "url " + url, "m application/x-webxdc", "x " + s["x"], "size 99311", "name dc20-dice (15).xdc",
           "webxdc ccede1cf-73a4-4e26-a54c-147033e64d2c", "summary DC20 Dice"] + _seal_fields(s)
    got = _cards(tag, text=url)          # his client ALSO put the URL in the message text
    assert len(got) == 1 and got[0]["enc"] == {"key": s["key"], "nonce": s["nonce"], "ox": s["ox"]}, got
    assert got[0]["sha"] == s["x"], got


def test_a_sealed_xdc_with_a_generic_type_is_still_a_game_with_its_key():
    """Firefox, 2026-10-14, after the first fix: still "not a zip". A sealed attachment whose type is generic was
    not recognised as a game at all, so the only card was the bare URL in the text -- with no key."""
    s = _sealed()
    url = "https://blossom.example/" + s["x"]
    for meta in (["m application/octet-stream", "name dc20-dice.xdc"], ["m application/zip", "name DC20 Dice.xdc"]):
        tag = ["imeta", "url " + url, "x " + s["x"]] + meta + _seal_fields(s)
        got = _cards(tag, text=url + ".xdc")
        assert got and got[0]["enc"] and got[0]["enc"]["key"] == s["key"], (meta, got)


def test_a_bare_link_that_is_a_sealed_attachment_borrows_its_key():
    s = _sealed()
    url = "https://blossom.example/" + s["x"] + ".xdc"
    tag = ["imeta", "url " + url, "m application/octet-stream", "x " + s["x"]] + _seal_fields(s)
    got = _cards(tag, text="play this " + url)
    assert got and got[0]["enc"] and got[0]["enc"]["key"] == s["key"], got


def _josephus_tag(s, url):
    """Josephus's real post shape: an Armada session UUID in `webxdc` (not an Iroh topic), sealed."""
    return ["imeta", "url " + url, "m application/x-webxdc", "x " + s["x"], "name dc20-dice (15).xdc",
            "webxdc ccede1cf-73a4-4e26-a54c-147033e64d2c", "summary DC20 Dice"] + _seal_fields(s)


def test_the_generic_card_carries_the_key_too():
    """"Webxdc opens and plays now. I keep getting these banners though" -- 'looks encrypted and the post did not
    include its key'. The generic link pass (linkcards.js, DMs, the gallery) builds its card with appOf, which
    dropped the seal: a second card for the same game whose Play could only fail."""
    s = _sealed()
    url = "https://blossom.example/" + s["x"] + ".xdc"
    want = {"key": s["key"], "nonce": s["nonce"], "ox": s["ox"]}
    got = _node(f"""
      global.window = {{ addEventListener(){{}}, __PC: {{ apiBase:()=>'' , toast(){{}} }} }};
      global.document = {{ addEventListener(){{}}, querySelectorAll:()=>[], getElementById:()=>null }};
      global.location = {{ hostname:'example.com', href:'https://example.com/' }};
      require({json.dumps(str(ZIP_JS))}); require({json.dumps(str(WEBXDC_JS))});
      const of = ev => window.PCWebxdc.appOf(ev);
      console.log(JSON.stringify({{
        imeta: of({{ id:'e1', kind:1, content:{json.dumps(url)}, tags:[{json.dumps(_josephus_tag(s, url))}] }}),
        bare: of({{ id:'e2', kind:1, content:'play ' + {json.dumps(url)},
                    tags:[["imeta","url "+{json.dumps(url)},"m application/octet-stream"].concat({json.dumps(_seal_fields(s))})] }}),
        foreign: of({{ id:'e3', kind:1, content:'', tags:[["imeta","url "+{json.dumps(url)},"m application/x-webxdc","encryption-algorithm chacha"]] }}),
      }}));
    """)
    assert got["imeta"].get("enc") == want, got
    assert got["imeta"]["uuid"] == "ccede1cf-73a4-4e26-a54c-147033e64d2c", got
    assert got["bare"].get("enc") == want, got
    assert got["foreign"] is None, ("a seal we cannot open must not be offered as a game", got)


def test_play_on_a_keyless_card_takes_the_key_from_the_message():
    """resolveWebxdcCard trusted ANY card carrying a session id, so a card drawn without the key launched without it."""
    s = _sealed()
    url = "https://blossom.example/" + s["x"] + ".xdc"
    got = _node(f"""
      const fs = require('fs'), vm = require('vm');
      const src = fs.readFileSync({json.dumps(str(CONCORD_JS))}, 'utf8'), noop = () => {{}}, PK = 'a'.repeat(64);
      const msg = {{ id:'m1', text:{json.dumps(url)}, tags:[{json.dumps(_josephus_tag(s, url))}], at:1, remote:true }};
      const room = {{ naddr:'room', channels:[{{name:'general'}}] }};
      const store = {{ ['pc.concord.test.room']:JSON.stringify([msg]), ['pc.concord.rooms.v1.'+PK]:JSON.stringify([room]) }};
      const localStorage = {{ getItem:k=>store[k]??null, setItem:(k,v)=>{{store[k]=v;}}, removeItem:noop }};
      const document = {{ querySelectorAll:()=>[], querySelector:()=>null, createElement:()=>({{dataset:{{}}}}),
                         head:{{appendChild:noop}}, documentElement:{{appendChild:noop}}, addEventListener:noop }};
      const __PC = {{ viewer:()=>({{pubkey:PK}}), me:()=>({{pubkey:PK}}) }};
      const window = {{ document, __PC, addEventListener:noop, requestAnimationFrame:noop }};
      vm.runInNewContext(src, {{ window, document, console, setTimeout:noop, clearTimeout:noop, URL, atob,
                                crypto:{{}}, localStorage, sessionStorage:{{getItem:()=>null,setItem:noop}} }});
      const C = window.PCConcord;
      C.acceptHandoff({{ room:'room', channel:'general' }});
      const row = {{ dataset:{{messageId:'m1'}} }}, card = {{ closest:sel=>sel.includes('cc-message')?row:null }};
      const keyless = {{ url:{json.dumps(url)}, sha:{json.dumps(s["x"])}, uuid:'ccede1cf-73a4-4e26-a54c-147033e64d2c', name:'DC20 Dice' }};
      console.log(JSON.stringify(C.resolveWebxdcCard(card, keyless)));
    """)
    assert got.get("enc") == {"key": s["key"], "nonce": s["nonce"], "ox": s["ox"]}, got
    assert got["uuid"] == "ccede1cf-73a4-4e26-a54c-147033e64d2c", ("the card's lobby must be kept", got)
