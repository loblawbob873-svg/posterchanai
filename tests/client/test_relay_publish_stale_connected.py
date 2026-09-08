"""Publish recovers a recently frozen OPEN socket without another signature/event."""
import json
from pathlib import Path
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('scenario',['silent','concurrent','healthy','refused','untrusted','permanent','replacement'])
def test_recently_connected_silent_socket_is_replaced_and_same_event_delivered(tmp_path,scenario):
    script = tmp_path / 'relay.js'
    script.write_text(r'''
const assert=require('node:assert/strict');
global.window=global;global.self=global;
global.Worker=class{postMessage(){} terminate(){}};
global.document={hidden:false,addEventListener(){}};
global.location={origin:'https://fixture.test',protocol:'https:'};
global.navigator={onLine:true};
const sockets=[],events=[];const scenario=process.argv[3];
class Socket {
 constructor(url){this.url=url;this.readyState=0;this.generation=sockets.length;sockets.push(this);setTimeout(()=>{this.readyState=1;this.onopen?.()},5)}
 send(raw){const m=JSON.parse(raw);
  if(m[0]==='EVENT'){
   events.push(m[1]);
   if(scenario==='refused')setTimeout(()=>this.reply(['OK',m[1].id,false,'blocked: fixture']),5);
   else if(scenario==='healthy')setTimeout(()=>this.reply(['OK',m[1].id,true,'']),3000);
   else if(this.generation>0&&scenario!=='permanent')setTimeout(()=>this.reply(['OK',m[1].id,true,'']),5);
  }
  if(m[0]==='REQ'&&scenario==='healthy')setTimeout(()=>this.reply(['EOSE',m[1]]),5);
 }
 reply(data){this.onmessage?.({data:JSON.stringify(data)})}
 // The original socket stays OPEN but never answers EVENT or exact-id confirmation REQ.
 close(){this.readyState=3}
}
global.WebSocket=Socket;
require(process.argv[2]);
(async()=>{try{
 Relay.configure({urls:['wss://fixture.test'],verify:scenario==='untrusted'});
 await new Promise(r=>setTimeout(r,20));
 assert.equal(Relay.status,'ok');
 const ev={id:'a'.repeat(64),pubkey:'b'.repeat(64),sig:'c'.repeat(128),kind:1,created_at:1,tags:[],content:'fixture'};
 const before=Date.now();
 if(scenario==='replacement')setTimeout(()=>Relay.wake(),1000);
 const pending=[Relay.publish(ev,4000)];
 if(scenario==='concurrent')pending.push(Relay.publish({...ev,id:'d'.repeat(64)},4000));
 const results=await Promise.all(pending),result=results[0];
 if(['silent','concurrent'].includes(scenario)){
  assert(results.every(r=>r.ok),JSON.stringify({results,sockets:sockets.length}));
  assert.equal(sockets.length,2,'exactly one recovery, no reconnect storm');
  assert.equal(sockets[0].readyState,3);
  assert.equal(events.length,pending.length*2);
  for(const event of events.slice(0,pending.length))assert.equal(events.filter(e=>JSON.stringify(e)===JSON.stringify(event)).length,2,'identical signed retry');
 }else{
  assert.equal(sockets.length,['permanent','replacement'].includes(scenario)?2:1,'do not churn healthy/refusing/external relays');
  assert.equal(result.ok,scenario==='healthy');
  if(['permanent','replacement'].includes(scenario))assert(Date.now()-before<4600,'original publish deadline grew');
  if(scenario==='refused')assert.match(result.msg,/blocked/);
 }

 console.log(JSON.stringify({ok:true,elapsed:Date.now()-before}));process.exit(0);
}catch(e){console.error(e);process.exit(1)}})();
''')
    result = subprocess.run(['node', str(script), str(ROOT / 'static/js/client/relay.js'),scenario],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['ok']
