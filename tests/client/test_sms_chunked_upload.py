"""Actual shared uploader: bounded file slices, encrypted chunks, failure before SMS."""
import base64
import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from tests.client.test_encrypted_attachment import _fn, APP, NODE

@pytest.mark.parametrize("failure", ["", "second", "server_changed"])
def test_real_shared_uploader_chunks_and_keeps_key_in_fragment(tmp_path, failure):
    src = Path(APP).read_text()
    functions = [_fn(src, name, opener) for name, opener in (
        ("_masterKeyInput", "function _masterKeyInput(mk){"),
        ("_masterCryptoKey", "async function _masterCryptoKey(mk, usage){"),
        ("_masterEncrypt", "async function _masterEncrypt(mk, plain, iv){"),
        ("uploadSharedEnc", "async function uploadSharedEnc("),
    )]
    code = r'''
const {webcrypto:crypto,createHash}=require('crypto');
const {File}=require('buffer');
const fs=require('fs');
const location={href:'https://node.example/client'};
const _ENC_MARK='#pcenc1=';
const _b64u=b=>Buffer.from(b).toString('base64url');
const failure=process.argv[2], dir=process.argv[3];
let uploads=0;
async function uploadBlob(file,options){
  if(!options.noMirror||!options.keep||!options.noCompress)throw Error('lost privacy/retention flags');
  if(file.size>4*1024*1024)throw Error('413 upload limit');
  uploads++;
  if(failure==='second' && uploads===2)throw Error('connection lost');
  const bytes=Buffer.from(await file.arrayBuffer()),sha=createHash('sha256').update(bytes).digest('hex');
  fs.writeFileSync(dir+'/'+sha,bytes);
  return (failure==='server_changed'&&uploads>1?'https://other.example':'https://node.example')+'/blossom/'+sha+'.enc';
}
''' + '\n'.join(functions) + r'''
const size=25*1024*1024;
const file={size,name:'movie.mp4',type:'video/mp4',
  arrayBuffer(){throw Error('whole-file read would exhaust phone memory');},
  slice(start,end){const bytes=Buffer.alloc(Math.min(end,size)-start);for(let i=0;i<bytes.length;i++)bytes[i]=((start+i)*31+7)&255;return new Blob([bytes]);}
};
(async()=>{try{const link=await uploadSharedEnc(file,null,{chunked:true});console.log(JSON.stringify({link,uploads}));}
catch(e){console.log(JSON.stringify({error:e.message,uploads}));}})();
'''
    path = tmp_path / 'uploader.js'
    path.write_text(code)
    proc = subprocess.run([NODE, str(path), failure, str(tmp_path)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)
    if failure:
        assert 'link' not in got
        assert ('connection lost' if failure == 'second' else 'server changed') in got['error']
        return
    assert 'error' not in got, got
    url, token = got['link'].split('#pcenc1=')
    meta = json.loads(base64.urlsafe_b64decode(token + '=' * (-len(token) % 4)))
    assert meta['c'] == 1
    cipher = AESGCM(base64.urlsafe_b64decode(meta['k'] + '='))
    def decrypt(sha):
        data = (tmp_path / sha).read_bytes()
        assert len(data) <= 4 * 1024 * 1024
        assert hashlib.sha256(data).hexdigest() == sha
        return cipher.decrypt(data[:12], data[12:], None)
    manifest = json.loads(decrypt(url.split('/')[-1].split('.')[0]))
    digest = hashlib.sha256()
    for chunk in manifest['chunks']:
        data = decrypt(chunk['sha'])
        assert len(data) == chunk['size']
        digest.update(data)
    expected = hashlib.sha256()
    block = bytes((i * 31 + 7) & 255 for i in range(65536))
    for _ in range(400): expected.update(block)
    assert manifest['size'] == 25 * 1024 * 1024
    assert digest.digest() == expected.digest()
