"""The injected NIP-07 provider enforces NIP-44's carrier boundary safely."""
from pathlib import Path
import subprocess, tempfile

ROOT=Path(__file__).resolve().parents[1]
SRC=(ROOT/'extension/inject.js').read_text()

def test_oversize_nip44_never_reaches_extension_or_becomes_unhandled():
    # Load the real provider in a tiny page-world shim. Node exits nonzero on an unhandled rejection.
    script=SRC+'''\nlet posted=0;
globalThis.window={addEventListener:()=>{},postMessage:()=>{posted++},nostr:null};
globalThis.document={documentElement:{setAttribute:()=>{}}};
__pcNostrProvider();
(async()=>{
  let error='';try{await window.nostr.nip44.encrypt('a'.repeat(64),'x'.repeat(65536));}catch(e){error=e.message}
  window.nostr.nip44.encrypt('a'.repeat(64),'x'.repeat(70000)); // deliberately fire-and-forget
  await new Promise(r=>setTimeout(r,20));
  if(posted!==0||!error.includes('1..65535')||!error.includes('attachment')) throw Error(JSON.stringify({posted,error}));
})().catch(e=>{console.error(e);process.exitCode=1});'''
    with tempfile.TemporaryDirectory() as td:
        p=Path(td)/'boundary.js';p.write_text(script)
        r=subprocess.run(['node','--unhandled-rejections=strict',p],text=True,capture_output=True,timeout=10)
    assert r.returncode==0,r.stdout+r.stderr

def test_empty_and_malformed_nip44_never_reach_extension_or_become_unhandled():
    script=SRC+'''\nlet posted=0;
globalThis.window={addEventListener:()=>{},postMessage:()=>{posted++},nostr:null};
globalThis.document={documentElement:{setAttribute:()=>{}}};
__pcNostrProvider();
(async()=>{
  const bad=['',null,undefined,{text:'not plaintext'}]; let errors=0;
  for(const value of bad){ try{await window.nostr.nip44.encrypt('a'.repeat(64),value);}catch(e){if(e.message.includes('1..65535')) errors++;} }
  window.nostr.nip44.encrypt('a'.repeat(64),undefined); // deliberately fire-and-forget
  await new Promise(r=>setTimeout(r,20));
  if(posted!==0||errors!==bad.length) throw Error(JSON.stringify({posted,errors}));
})().catch(e=>{console.error(e);process.exitCode=1});'''
    with tempfile.TemporaryDirectory() as td:
        p=Path(td)/'boundary-empty.js';p.write_text(script)
        r=subprocess.run(['node','--unhandled-rejections=strict',p],text=True,capture_output=True,timeout=10)
    assert r.returncode==0,r.stdout+r.stderr

def test_provider_does_not_chunk_nip44_events():
    assert 'Store large data as an attachment and encrypt only its pointer' in SRC
    assert 'promise.catch(() => {})' in SRC
    block=SRC[SRC.index('function nip44Encrypt'):SRC.index('const nostr =')]
    assert ".slice(" not in block and ".substring(" not in block


def test_empty_decrypt_is_rejected_locally_and_handled():
    script=SRC+'''\nlet posted=0;
globalThis.window={addEventListener:()=>{},postMessage:()=>{posted++},nostr:null};
globalThis.document={documentElement:{setAttribute:()=>{}}};
__pcNostrProvider();
(async()=>{
  let error='';try{await window.nostr.nip44.decrypt('a'.repeat(64),'');}catch(e){error=e.message}
  window.nostr.nip44.decrypt('a'.repeat(64),null);
  await new Promise(r=>setTimeout(r,20));
  if(posted!==0||!error.includes('ciphertext is empty'))throw Error(JSON.stringify({posted,error}));
})().catch(e=>{console.error(e);process.exitCode=1});'''
    with tempfile.TemporaryDirectory() as td:
        p=Path(td)/'decrypt-empty.js';p.write_text(script)
        r=subprocess.run(['node','--unhandled-rejections=strict',p],text=True,capture_output=True,timeout=10)
    assert r.returncode==0,r.stdout+r.stderr
