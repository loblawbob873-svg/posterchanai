"""A kind-4 DM whose content is not NIP-04 (no `?iv=`) is never handed to nip04_decrypt (2026-10-10).

A user signing with a remote signer saw "nip04_decrypt_failed: invalid base64": the client asked the signer to
NIP-04-decrypt a payload that was not NIP-04. Runs the SHIPPED decryptMsg from dms.js under node."""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

DRIVER = r"""
const fs=require('fs');const src=fs.readFileSync(%s,'utf8');
const i=src.indexOf('async function decryptMsg(');let depth=0,j=src.indexOf('{',i);
for(let k=j;k<src.length;k++){if(src[k]==='{')depth++;else if(src[k]==='}'){depth--;if(!depth){j=k+1;break;}}}
const body=src.slice(i,j);
(async()=>{const asked=[];
 const S={signer:{nip04dec:async()=>{asked.push('04');throw new Error('nip04_decrypt_failed: invalid base64')},
                  nip44dec:async()=>{asked.push('44');return 'hi'}}};
 const decryptMsg=new Function('S','return '+body)(S);
 const a=await decryptMsg('p',{text:null,ev:{content:'AsomeNip44Payload=='}});
 const b=await decryptMsg('p',{text:null,ev:{content:'abc?iv=def'}});
 console.log(JSON.stringify({a,asked}));})();
"""


def test_a_dm_without_an_iv_is_never_sent_to_nip04():
    out = subprocess.run(["node", "-e", DRIVER % json.dumps(str(ROOT / "static/js/client/dms.js"))],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["asked"][0] == "44", ("a NIP-44 payload went to nip04_decrypt", got)
    assert got["a"] == "hi", got
    assert "04" in got["asked"][1:], ("real NIP-04 (?iv=) no longer goes to nip04_decrypt", got)
