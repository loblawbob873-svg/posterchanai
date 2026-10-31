"""PBS's saved setup and its two measurements, run from the SHIPPED pbs.js under node (no browser).

Run: venv-unified/bin/python -m pytest tests/client/test_pbs_doc_rules.py

  * The document `d=pcai:pbs` (PCPbsDoc.store): a read nobody answered is "could not read" -- the device copy is
    offered and NOTHING is published over the account's setup; a read that answered with no document is an empty
    studio and a first save goes out; a document that will not decrypt is "could not read" too; a change made on
    this device while the relay could not be read beats an older account copy.
  * normalize(): an overlay image is a URL or this device's `idb:` id -- never a data: URI inlined into the doc.
  * encoderKind(): what the status bar says about the encoder, from getStats' own fields.
  * dbOf(): the meter scale.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(shutil.which('node') is None, reason='node not installed')

RUN = r'''
const vm=require('node:vm'),fs=require('node:fs');
const ctx={console,setTimeout,clearTimeout,Promise,JSON,Math,Date,Object,Array,String,Number,Map,Set,Error,RegExp};
ctx.window=ctx;vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
const P=ctx.PCPbsDoc,ME='a'.repeat(64);
const answer=(evs,answered)=>{const a=[...evs];Object.defineProperty(a,'complete',{value:!!answered});return a;};
function mk(evs,answered,{dec,local}={}){let pubs=[],loc=local||null;
  const st=P.store({owner:ME,query:async()=>answer(evs,answered),homeAnswered:()=>false,
    publish:async(k,c,t)=>{pubs.push({k,c,t});return {ok:true}},enc:async(pk,t)=>'ENC:'+t,
    dec:dec||(async(pk,c)=>c.slice(4)),local:{get:()=>loc,set:d=>{loc=d}}});
  return {st,pubs,loc:()=>loc};}
(async()=>{const out={};
 const saved={sources:[{type:'text',text:'saved one',x:.5,y:.5}],t:1000,meta:{title:'Saved show'}};
 const ev={kind:30078,created_at:5,content:'ENC:'+JSON.stringify(saved),tags:[['d','pcai:pbs']]};
 // nobody answered
 {const w=mk([],false);const r=await w.st.load();await w.st.save({sources:[{type:'text',text:'device'}]});
  out.silent={status:r.status,pubs:w.pubs.length,kept:!!w.loc()};}
 // answered, no document
 {const w=mk([],true);const r=await w.st.load();const s=await w.st.save({sources:[{type:'text',text:'first'}]});
  out.empty={status:r.status,pubs:w.pubs.length,d:w.pubs[0]&&w.pubs[0].t,synced:s.synced};}
 // answered with the document
 {const w=mk([ev],true);const r=await w.st.load();out.ok={status:r.status,title:r.doc.meta.title,text:r.doc.sources[0].text};}
 // does not decrypt
 {const w=mk([ev],true,{dec:async()=>{throw new Error('bad key')}});const r=await w.st.load();await w.st.save({});
  out.undecryptable={status:r.status,pubs:w.pubs.length};}
 // a newer device copy wins over an older account copy
 {const w=mk([ev],true,{local:{sources:[{type:'text',text:'newer here'}],t:9e12}});const r=await w.st.load();
  out.newer={text:r.doc.sources[0].text};}
 // normalize
 const n=P.normalize({sources:[{type:'image',url:'data:image/png;base64,AAAA'},{type:'image',url:'idb:img-abcdef12'},
   {type:'image',url:'https://blossom.example/x.png'},{type:'bogus'},{type:'text',text:'x'.repeat(900),color:'red'}],
   out:{res:'4k',kbps:99999,fps:144}});
 out.norm={urls:n.sources.filter(s=>s.type==='image').map(s=>s.url),text:n.sources.find(s=>s.type==='text').text.length,
   color:n.sources.find(s=>s.type==='text').color,out:n.out,count:n.sources.length};
 out.enc=[{powerEfficientEncoder:true,encoderImplementation:'ExternalEncoder'},{encoderImplementation:'VaapiVideoEncodeAccelerator'},
   {encoderImplementation:'OpenH264',powerEfficientEncoder:false},{encoderImplementation:'libvpx'},{}].map(r=>P.encoderKind(r).kind);
 out.db=[P.dbOf(1),P.dbOf(0.5),P.dbOf(0),P.dbOf(2)].map(x=>Math.round(x));
 process.stdout.write(JSON.stringify(out));})().catch(e=>{console.error(e);process.exit(1)});
'''


@pytest.fixture(scope='module')
def got():
    r = subprocess.run(['node', '-e', RUN, os.environ.get('PC_PBS_JS', str(ROOT / 'static/js/client/pbs.js'))], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_a_read_nobody_answered_never_writes_over_the_saved_setup(got):
    assert got['silent'] == {'status': 'unreadable', 'pubs': 0, 'kept': True}, got['silent']


def test_a_read_that_answered_with_no_document_is_an_empty_studio_and_saves(got):
    assert got['empty']['status'] == 'empty' and got['empty']['pubs'] == 1 and got['empty']['synced'], got['empty']
    assert got['empty']['d'] == [['d', 'pcai:pbs']]


def test_the_saved_setup_loads(got):
    assert got['ok'] == {'status': 'ok', 'title': 'Saved show', 'text': 'saved one'}


def test_a_document_that_will_not_decrypt_is_could_not_read(got):
    assert got['undecryptable'] == {'status': 'unreadable', 'pubs': 0}


def test_a_newer_change_on_this_device_beats_an_older_account_copy(got):
    assert got['newer']['text'] == 'newer here'


def test_images_are_referenced_never_inlined_and_fields_are_bounded(got):
    n = got['norm']
    assert n['urls'] == ['idb:img-abcdef12', 'https://blossom.example/x.png'], n
    assert n['text'] == 500 and n['color'] == '#3ce8ff' and n['count'] == 3, n
    assert n['out'] == {'res': '720', 'fps': 30, 'kbps': 1500}, n


def test_encoder_reporting_reads_getstats(got):
    assert got['enc'] == ['hardware', 'hardware', 'software', 'software', 'unknown']


def test_meter_scale(got):
    assert got['db'] == [0, -6, -60, 0]
