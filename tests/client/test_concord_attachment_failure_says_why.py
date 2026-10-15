"""A Concord attachment that will not open says WHY -- unreachable, refused, or truly undecryptable -- with Retry.

"now getting 'Could not decrypt attachment' errors on laptop in concord" (2026-10-15). Every failure -- a host this
network cannot reach, a host that refused the download, a wrong key -- was the same "Could not decrypt attachment",
which points at keys when the cause was usually the host. The shipped decryptAttachment + attachmentFailText run
under node against a real AES-GCM attachment, with fetch stubbed per case.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / 'static/js/client/concord.js').read_text()
pytestmark = pytest.mark.skipif(not shutil.which('node'), reason='node not installed')


def _fn(name):
    m = re.search(r'\n  (?:async )?function ' + name + r'\(', SRC)
    assert m, name
    i = m.start() + 1
    depth, j = 0, SRC.index('{', i)
    while True:
        c = SRC[j]
        depth += c == '{'
        depth -= c == '}'
        j += 1
        if depth == 0:
            return SRC[i:j]


def test_each_failure_names_itself():
    code = '\n'.join(_fn(n) for n in ('hexBytes', 'bytesHex', 'attachmentFailText', 'decryptAttachment'))
    script = r'''
const {webcrypto}=require('node:crypto');globalThis.crypto=webcrypto;
const attachmentCache=new Map(),attachmentLoads=new Map();URL.createObjectURL=()=>'blob:x';
%s
(async()=>{
  const plain=new TextEncoder().encode('hello'),key=crypto.getRandomValues(new Uint8Array(32)),nonce=crypto.getRandomValues(new Uint8Array(16));
  const k=await crypto.subtle.importKey('raw',key,'AES-GCM',false,['encrypt']);
  const cipher=new Uint8Array(await crypto.subtle.encrypt({name:'AES-GCM',iv:nonce},k,plain));
  const file=u=>({url:u,key:bytesHex(key),nonce:bytesHex(nonce),hash:bytesHex(await_hash),mime:'text/plain',name:'a.txt'});
  var await_hash=await crypto.subtle.digest('SHA-256',plain);
  const out={};
  const run=async(name,fetchImpl,f)=>{globalThis.fetch=fetchImpl;try{await decryptAttachment(f);out[name]='ok';}catch(e){out[name]=attachmentFailText(e);}};
  await run('unreachable',async()=>{throw new TypeError('Failed to fetch');},file('https://blossom.example/1'));
  await run('refused',async()=>new Response('no',{status:403}),file('https://blossom.example/2'));
  const wrong={...file('https://blossom.example/3'),key:'00'.repeat(32)};
  await run('decrypt',async()=>new Response(cipher,{status:200}),wrong);
  await run('fine',async()=>new Response(cipher,{status:200}),file('https://blossom.example/4'));
  console.log(JSON.stringify(out));
})().catch(e=>{console.error(e);process.exit(1);});
''' % code
    r = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-2000:]
    got = json.loads(r.stdout.strip().splitlines()[-1])
    assert got['fine'] == 'ok', got
    assert 'reach blossom.example' in got['unreachable'], got
    assert 'HTTP 403' in got['refused'] and 'blossom.example' in got['refused'], got
    assert got['decrypt'] == 'Could not decrypt attachment', got
