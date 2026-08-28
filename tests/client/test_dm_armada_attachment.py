"""Armada NIP-17 encrypted file metadata decrypts and verifies in Chromium."""
from pathlib import Path
import json, subprocess, tempfile

ROOT=Path(__file__).resolve().parents[2]
APP=(ROOT/'static/js/client/app.js').read_text()

def fn(name):
    start=APP.index('function '+name+'('); brace=APP.index('{',start); depth=0
    if APP[max(0,start-6):start]=='async ': start-=6
    for i in range(brace,len(APP)):
        if APP[i]=='{': depth+=1
        elif APP[i]=='}':
            depth-=1
            if depth==0:return APP[start:i+1]
    raise AssertionError(name)

def test_armada_kind14_and_kind15_aes_gcm_attachment_contract():
    js='\n'.join(fn(x) for x in ('_dmKeyBytes','_dmEncOf','_dmAttachmentMeta','_dmDecryptAttachment'))
    script=f'''{js}
    (async()=>{{
      const key=crypto.getRandomValues(new Uint8Array(32)),iv=crypto.getRandomValues(new Uint8Array(16));
      const plain=new TextEncoder().encode('verified armada image bytes');
      const ck=await crypto.subtle.importKey('raw',key,{{name:'AES-GCM'}},false,['encrypt']);
      const ct=await crypto.subtle.encrypt({{name:'AES-GCM',iv}},ck,plain);
      const hex=u=>[...u].map(x=>x.toString(16).padStart(2,'0')).join('');
      const ox=hex(new Uint8Array(await crypto.subtle.digest('SHA-256',plain)));
      const url='https://blossom.example/cipher.png';
      globalThis.fetch=async()=>new Response(ct,{{status:200,headers:{{'content-type':'image/png'}}}});
      const fields=['url '+url,'m image/png','name photo.png','encryption-algorithm aes-gcm','decryption-key '+hex(key),'decryption-nonce '+hex(iv),'ox '+ox];
      const a=_dmAttachmentMeta({{kind:14,tags:[['imeta',...fields]]}})[0];
      const b=_dmAttachmentMeta({{kind:15,content:url,tags:[['file-type','image/png'],['name','photo.png'],['encryption-algorithm','aes-gcm'],['decryption-key',hex(key)],['decryption-nonce',hex(iv)],['ox',ox]]}})[0];
      if(!a?.enc||!b?.enc) throw new Error('metadata '+JSON.stringify({{a,b}}));
      const pa=await (await _dmDecryptAttachment(a)).text(),pb=await (await _dmDecryptAttachment(b)).text();
      process.stdout.write(JSON.stringify({{pa,pb,mime:a.mime,key:a.enc.key.length,iv:a.enc.nonce.length}}));
    }})().catch(e=>{{process.stdout.write(JSON.stringify({{error:String(e)}}));process.exitCode=1}})
    '''
    with tempfile.TemporaryDirectory() as td:
        p=Path(td)/'x.js';p.write_text(script)
        r=subprocess.run(['node',p],text=True,capture_output=True,timeout=30)
    assert r.returncode==0,r.stdout+r.stderr
    got=json.loads(r.stdout)
    assert got=={'pa':'verified armada image bytes','pb':'verified armada image bytes','mime':'image/png','key':64,'iv':32}

def test_ingest_preserves_both_file_rumor_kinds_and_fails_closed():
    assert '(rumor.kind === 14 || rumor.kind === 15)' in APP
    assert '(rumor.kind!==14 && rumor.kind!==15)' in APP
    assert "if(a.enc.algorithm!=='aes-gcm'" in APP
    assert "if(hex!==a.enc.ox) throw new Error('attachment hash mismatch')" in APP

def test_plain_armada_attachment_bypasses_the_decryptor():
    block=APP[APP.index('async function _decorateDmFileAtts'):APP.index('async function ingestWrap')]
    assert 'let u=a.url;' in block
    assert 'if(a.enc){' in block
    assert 'target="_blank" rel="noopener noreferrer"' in block
    assert "if(!a || !a.enc) throw new Error('not encrypted')" not in block
