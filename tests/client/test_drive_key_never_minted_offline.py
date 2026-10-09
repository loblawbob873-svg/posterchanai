"""OFFLINE, A DEVICE MUST NEVER INVENT A NEW DRIVE KEY.

`FilesIdx._ensureMK` asks the server for the drive's key before minting one, and minting is right
only when the server ANSWERED and had none (a brand-new drive). But `_pullDone` — the flag the guard
read — means "a pull attempt finished", and it is set in a `finally`, so a pull that FAILED (no
network) counted as an answer: the device minted a key that decrypts none of the person's files,
saved it locally, and tried to claim it at the server. Found by the 2026-10-09 offline audit of every
app ("we need to make sure ... users can access their notes, music ... without network").
The rule: mint only on `_pullOk` (the server proved it has no index) and never while `_pullBlocked`.

The method is EXTRACTED from the shipped filesindex.js and run under node.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "static/js/client/filesindex.js").read_text(encoding="utf-8")


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
const S = { ME:{pubkey:'a'.repeat(64)}, signer:{ nip44enc: async()=>{ minted.wrapped++; return 'wrapped'; } } };
const minted = { wrapped:0 };
const crypto = globalThis.crypto;
async function _unwrapMK(){ throw Object.assign(new Error('no'),{badKey:true}); }
function _u8b64(u){ return Buffer.from(u).toString('base64'); }
function make(pullResult){
  return { mk:null, _mkWrapped:null, _pullDone:false, _pullOk:false, _pullBlocked:false, _pulling:false, _dirty:false,
    saveLocal(){}, async _saveOnce(){},
    async pull(){ this._pulling=true; try{
        if(pullResult==='offline'){ throw new Error('Failed to fetch'); }
        if(pullResult==='empty'){ this._pullOk=true; }
        if(pullResult==='blocked'){ this._pullBlocked=true; }
      } finally { this._pulling=false; this._pullDone=true; } },
    __M__ };
}
(async()=>{
  const out={};
  for(const kind of ['offline','blocked','empty']){
    minted.wrapped=0;
    const idx=make(kind); let err=null;
    try{ await idx._ensureMK(); }catch(e){ err=String(e.message||e); }
    // and a SECOND call after the failed pull already set _pullDone
    let err2=null; try{ await idx._ensureMK(); }catch(e){ err2=String(e.message||e); }
    out[kind]={ minted: minted.wrapped, mk: !!idx.mk, err, err2 };
  }
  console.log(JSON.stringify(out));
})();
""".replace("__M__", _method("_ensureMK"))


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_offline_never_mints_and_a_new_drive_still_does():
    done = subprocess.run(["node", "-e", RUN], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr[-1500:]
    got = json.loads(done.stdout.strip().splitlines()[-1])
    for kind in ("offline", "blocked"):
        assert got[kind]["minted"] == 0 and not got[kind]["mk"], (kind, "a drive key was invented", got)
        assert got[kind]["err"] and got[kind]["err2"], (kind, "no error said why nothing happened", got)
    assert got["empty"]["mk"] and got["empty"]["minted"] >= 1, ("a genuinely new drive got no key", got)
