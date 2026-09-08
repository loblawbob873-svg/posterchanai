"""Execute the shipped connection handlers with deferred signing and socket replacement."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_auth_completion_belongs_to_the_socket_that_requested_it():
    result = subprocess.run(['node', '--input-type=module', '-'], input=r'''
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const source=fs.readFileSync('extension/background.js','utf8');
const sockets=[],signatures=[];
class Socket {
 constructor(){this.readyState=1;this.sent=[];sockets.push(this);}
 send(s){this.sent.push(JSON.parse(s));}
 close(){this.readyState=3;}
}
const conns=new Map();
const scope={conns,WebSocket:Socket,ws:null,cfg:{pubkey:'owner'},KIND:30078,L_TAG:'vault',L_BM:'bookmarks',
 clearTimeout,Date,JSON,okWaiters:new Map(),refreshStatus(){},retry(){},_anyOpen(){return null;},
 finalize(event){return new Promise((resolve,reject)=>signatures.push({event,resolve,reject}));}};
vm.createContext(scope);
vm.runInContext(source.slice(source.indexOf('function closeConn('),source.indexOf('function _anyOpen(')),scope);
const url='wss://relay.example';
const message=(socket,data)=>socket.onmessage({data:JSON.stringify(data)});
const drain=async()=>{await new Promise(resolve=>setImmediate(resolve));};
scope.openConn(url);const old=sockets.at(-1);message(old,['AUTH','old-challenge']);
const lateMessage=old.onmessage,lateClose=old.onclose;
scope.openConn(url);const replacement=sockets.at(-1);message(replacement,['AUTH','new-challenge']);
const pending=conns.get(url).authing;
signatures[0].resolve({id:'old-auth'});await drain();
assert.equal(replacement.sent.length,0,'obsolete challenge must not be sent on replacement');
assert.equal(conns.get(url).authing,pending,'old finally must not release new signing lock');
lateMessage({data:JSON.stringify(['OK','old-auth',true])});lateClose();
assert.equal(conns.get(url).authed,false);
signatures[1].resolve({id:'new-auth'});await drain();
assert.deepEqual(replacement.sent.map(m=>m[0]),['AUTH']);
message(replacement,['OK','new-auth',true]);
assert.deepEqual(replacement.sent.map(m=>m[0]),['AUTH','REQ'],'protected request waits for positive current AUTH acknowledgement');
// Rejection from an obsolete signer must not undo authenticated replacement state.
scope.openConn(url);const second=sockets.at(-1);message(second,['AUTH','obsolete-reject']);
scope.openConn(url);const third=sockets.at(-1);message(third,['AUTH','third']);
signatures[3].resolve({id:'third-auth'});await drain();message(third,['OK','third-auth',true]);
signatures[2].reject(Error('old signer failure'));await drain();assert.equal(conns.get(url).authed,true);
// Removing a relay while signing must not publish the signed AUTH to the retired socket.
scope.openConn(url);const removed=sockets.at(-1);message(removed,['AUTH','removed']);
scope.closeConn(conns.get(url));conns.delete(url);signatures[4].resolve({id:'removed-auth'});await drain();
assert.equal(removed.sent.length,0);
''', text=True, cwd=ROOT, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
