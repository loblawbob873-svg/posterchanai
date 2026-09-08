"""Room delivery drives the shipped relay implementation, including pooled URL overlap."""
import subprocess
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2]

@pytest.mark.parametrize('scenario', ['managed', 'mixed', 'legacy', 'rejected', 'unknown', 'offline', 'wrong-id', 'retry'])
def test_room_delivery_stays_scoped_and_reports_real_acknowledgement(tmp_path, scenario):
    script = tmp_path / 'probe.cjs'
    script.write_text(r'''
const assert=require('node:assert/strict');
global.window=global;global.self=global;
global.Worker=class{postMessage(){}terminate(){}};
global.document={hidden:false,addEventListener(){}};
global.location={origin:'https://fixture.test',protocol:'https:'};
global.navigator={onLine:true};
const mode=process.argv[3],sockets=[],sent=[];
class Socket{
 constructor(url){this.url=url;this.readyState=0;sockets.push(this);if(mode!=='offline')setTimeout(()=>{this.readyState=1;this.onopen?.()},1);}
 send(raw){const m=JSON.parse(raw);sent.push({url:this.url,event:m[1]});
  if(mode==='unknown')return;
  const reply=['OK',mode==='wrong-id'?'other':m[1].id,mode!=='rejected',mode==='rejected'?'blocked: fixture':''];
  setTimeout(()=>this.onmessage?.({data:JSON.stringify(reply)}),1);
 }
 close(){this.readyState=3;}
}
global.WebSocket=Socket;
require(process.argv[2]);
(async()=>{try{
 const room='wss://room.fixture',external='wss://external.fixture',unrelated='wss://private-account.fixture';
 const pooled={ws:{readyState:1,send(){throw new Error('must not broadcast to account pool');}}};
 Relay._conns.set(room,pooled);Relay._conns.set(unrelated,pooled);
 const event={id:'a'.repeat(64),pubkey:'b'.repeat(64),sig:'c'.repeat(128),kind:1059,tags:[],created_at:1,content:'encrypted fixture'};
 const urls=mode==='mixed'?[room,external,room]:[room];
 const options={timeout:30,includeManaged:mode!=='legacy',detailed:mode!=='legacy'};
 const result=await Relay.publishTo(urls,event,options);
 if(mode==='legacy'){
  assert.equal(result,0);assert.equal(sockets.length,0);
  assert.equal(await Relay.publishTo([external],event,{timeout:30}),1,'legacy return remains numeric');
 }else{
  assert.deepEqual(sockets.map(s=>s.url),mode==='mixed'?[room,external]:[room]);
  assert.equal(result.ok,['managed','mixed','retry'].includes(mode));
  assert.equal(result.accepted,mode==='mixed'?2:result.ok?1:0);
  assert.equal(result.uncertain,['unknown','wrong-id'].includes(mode));
  if(mode==='rejected')assert.match(result.msg,/blocked: fixture/);
  if(mode==='offline')assert.equal(sent.length,0);
  if(mode==='retry'){
   assert.equal((await Relay.publishTo(urls,event,options)).ok,true);
   assert.deepEqual(sent[0].event,sent[1].event,'explicit retry sends exact signed bytes');
  }
 }
 for(const entry of sent){assert.notEqual(entry.url,unrelated);assert.deepEqual(entry.event,event);}
 assert(sockets.every(s=>s.readyState===3),'bounded ephemeral sockets cleaned up');
 assert.equal(Relay._conns.get(room),pooled,'managed connection ownership unchanged');
 console.log('PASS');process.exit(0);
}catch(e){console.error(e);process.exit(1)}})();
''')
    result = subprocess.run(['node', str(script), str(ROOT/'static/js/client/relay.js'), scenario],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'PASS'
