"""OFFLINE (OR ON A FLAKY LINK) THE DM CACHE KEY IS NEVER REPLACED.

`DmCache._loadKey` asked the relays for the `pcai:dmkey` doc and minted a new key when none came back.
But `Relay.query` resolves `[]` when it TIMES OUT (`complete === false`), so a silent network read as
"you have no key": a new key was minted and — on a link where the publish still gets out — stored
over the real one, making every other device's encrypted message cache unreadable. It also asked the
relays before the local Store, which already holds that doc (it is pinned), so offline the screen
waited ~12s for an answer it had all along. Found by the 2026-10-09 offline audit.

Rules: the local copy first; a query only counts as an answer when it `complete`d or returned the
doc; mint only on a complete answer with no doc.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "static/js/client/dms.js").read_text(encoding="utf-8")


def _method(name):
    i = SRC.index("    async " + name + "(){")
    depth, j = 0, SRC.index("{", i)
    while True:
        if SRC[j] == "{": depth += 1
        elif SRC[j] == "}":
            depth -= 1
            if depth == 0: return SRC[i:j + 1].strip()
        j += 1


RUN = r"""
const ME='a'.repeat(64), KEY='b'.repeat(64);
const calls={query:0,publish:0};
let mode='timeout', local=null;
const S={ ME:{pubkey:ME}, signer:{ nip44dec: async(pk,ct)=>ct==='enc:'+KEY?KEY:'', nip44enc: async(pk,pt)=>'enc:'+pt } };
const Relay={ query: async()=>{ calls.query++; const r = mode==='has'?[{kind:30078,created_at:5,content:'enc:'+KEY,tags:[['d','pcai:dmkey']]}]:[];
   Object.defineProperty(r,'complete',{value: mode!=='timeout'}); return r; } };
const Store={ query: (f)=> local ? [local] : [] };
async function publish(){ calls.publish++; return {ok:true}; }
const obj={ D:'pcai:dmkey', __M__ };
(async()=>{
  const out={};
  // 1. offline: relays time out, nothing local -> must refuse, never mint/publish
  mode='timeout'; local=null; calls.query=0; calls.publish=0; let e1=null;
  try{ await obj._loadKey(); }catch(e){ e1=String(e.message||e); }
  out.offline={err:e1, published:calls.publish};
  // 2. the doc is in the local Store -> used, relay not needed
  mode='timeout'; local={kind:30078,pubkey:ME,created_at:5,content:'enc:'+KEY,tags:[['d','pcai:dmkey']]}; calls.publish=0; let e2=null;
  try{ await obj._loadKey(); }catch(e){ e2=String(e.message||e); }
  out.local={err:e2, published:calls.publish};
  // 3. a complete answer with no doc -> a genuinely new account mints
  mode='empty'; local=null; calls.publish=0; let e3=null;
  try{ await obj._loadKey(); }catch(e){ e3=String(e.message||e); }
  out.fresh={err:e3, published:calls.publish};
  console.log(JSON.stringify(out));
})();
""".replace("__M__", _method("_loadKey"))


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_offline_never_mints_the_local_copy_is_used_and_a_new_account_still_mints():
    done = subprocess.run(["node", "-e", RUN], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-1500:]
    got = json.loads(done.stdout.strip().splitlines()[-1])
    assert got["offline"]["published"] == 0 and got["offline"]["err"], ("offline minted a new DM key", got)
    assert got["local"]["err"] is None and got["local"]["published"] == 0, ("the stored key was not used offline", got)
    assert got["fresh"]["err"] is None and got["fresh"]["published"] == 1, ("a new account got no key", got)
