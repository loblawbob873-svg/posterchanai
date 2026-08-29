"""Drive decryption accepts the lossless key shapes produced by browser/native/signer bridges."""
import json
from pathlib import Path
import re
import subprocess
import textwrap


ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "static/js/client/app.js"


def _fn(src, name):
    match = re.search(r"^\s*(?:async )?function " + re.escape(name) + r"\(", src, re.M)
    assert match
    depth = 0
    started = False
    for pos in range(match.start(), len(src)):
        if src[pos] == "{":
            depth += 1
            started = True
        elif src[pos] == "}":
            depth -= 1
            if started and depth == 0:
                return src[match.start():pos + 1]
    raise AssertionError("unterminated " + name)


def test_master_decrypt_accepts_every_lossless_bridge_shape():
    src = APP.read_text(encoding="utf-8")
    helpers = "\n".join(_fn(src, name) for name in
                        ("_b64u8", "_masterKeyInput", "_masterCryptoKey",
                         "_masterEncrypt", "_masterDecrypt"))
    program = textwrap.dedent(f"""
      const crypto=require('crypto').webcrypto;
      const atob=s=>Buffer.from(s,'base64').toString('binary');
      {helpers}
      (async()=>{{
        const raw=new Uint8Array(32).map((_,i)=>i+1), plain=new TextEncoder().encode('Texts archive');
        const sealed=await _masterEncrypt(raw,plain);
        const imported=await crypto.subtle.importKey('raw',raw,'AES-GCM',false,['decrypt']);
        const b64=Buffer.from(raw).toString('base64');
        const shapes=[raw,raw.buffer,new DataView(raw.buffer),Array.from(raw),
          {{type:'Buffer',data:Array.from(raw)}},Object.fromEntries(Array.from(raw,(v,i)=>[i,v])),
          b64,imported];
        const opened=[];
        for(const key of shapes) opened.push(new TextDecoder().decode(await _masterDecrypt(key,sealed)));
        let bad='';try{{await _masterDecrypt({{0:1}},sealed)}}catch(e){{bad=e.message+':'+!!e.badKey}}
        process.stdout.write(JSON.stringify({{opened,bad}}));
      }})().catch(e=>{{console.error(e.stack||e);process.exit(1)}});
    """)
    result = subprocess.run(["node", "-e", program], cwd=ROOT, capture_output=True, text=True,
                            timeout=30)
    assert result.returncode == 0, result.stderr
    got = json.loads(result.stdout)
    assert got["opened"] == ["Texts archive"] * 8
    assert got["bad"] == "drive master key is not 32 raw bytes:true"
