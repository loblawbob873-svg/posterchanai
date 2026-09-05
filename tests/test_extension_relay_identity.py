"""Only extension-owned WebSockets may identify themselves as PosterChan."""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BG = (ROOT / 'extension/background.js').read_text()
SRC = BG[BG.index('function installRelayIdentity()'):BG.index('const relayIdentityReady=')]


def test_firefox_and_chrome_identity_is_scoped_to_extension():
    js = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
let handler,filter,options,rules;
const own='moz-extension://test-id/';
const B={runtime:{getURL:()=>own,getManifest:()=>({version:'1.4.8'}),id:'chrome-id'},
webRequest:{onBeforeSendHeaders:{addListener:(h,f,o)=>{handler=h;filter=f;options=o;}}}};
const ctx={B};vm.createContext(ctx);vm.runInContext(SRC,ctx);
(async()=>{
await ctx.installRelayIdentity();
assert.deepEqual(Array.from(filter.types),['websocket']);assert(options.includes('blocking'));
for(const originUrl of ['https://other.test/','moz-extension://someone-else/bg.html',own.slice(0,-1)+'.evil/bg.html','']){
 const h={name:'User-Agent',value:'Firefox'};handler({originUrl,requestHeaders:[h]});assert.equal(h.value,'Firefox');
}
const result=handler({originUrl:own+'background.html',requestHeaders:[{name:'User-Agent',value:'Firefox'}]});
assert.equal(result.requestHeaders[0].value,'Firefox PosterChan/1.4.8');
B.runtime.getURL=()=> 'chrome-extension://chrome-id/';
B.declarativeNetRequest={updateSessionRules:async r=>{rules=r;}};
await ctx.installRelayIdentity();
assert.deepEqual(Array.from(rules.addRules[0].condition.initiatorDomains),['chrome-id']);
assert.deepEqual(Array.from(rules.addRules[0].condition.resourceTypes),['websocket']);
assert.equal(rules.addRules[0].action.requestHeaders[0].value,'PosterChan/1.4.8');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''.replace('vm.runInContext(SRC,ctx)', 'vm.runInContext('+json.dumps(SRC)+',ctx)')
    subprocess.run(['node'], input=js, text=True, capture_output=True, check=True, timeout=10)
