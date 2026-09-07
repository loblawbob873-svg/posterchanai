"""A qualified user keeps the requested repository while membership loads."""
import json
import subprocess
from pathlib import Path


def test_repo_open_waits_for_membership_without_losing_latest_target():
    source=(Path(__file__).resolve().parents[2]/'static/js/client/git.js').read_text()
    start=source.index('  let _repoOpenEpoch=0;')
    end=source.index('    if(!e) return;',start)
    code=source[start:end]+'opened.push(e);}'
    js=r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
let allowed=false,waiters=[],opened=[],switched=[];
const S={VIEW:'home',ME:{pubkey:'alice'}};
const window={PCInstanceAccess:{allowed:()=>allowed,require:()=>new Promise((resolve,reject)=>waiters.push({resolve,reject}))}};
const switchView=v=>switched.push(v);
vm.runInThisContext(SOURCE);
(async()=>{
 openRepo('first');openRepo('second');allowed=true;waiters.forEach(x=>x.resolve());await Promise.resolve();
 assert.deepEqual(opened,['second']);assert.deepEqual(switched,[]);
 allowed=false;openRepo('third');S.VIEW='messages';waiters.at(-1).resolve();await Promise.resolve();
 assert.deepEqual(opened,['second']);
 S.VIEW='home';openRepo('fourth');S.ME.pubkey='bob';waiters.at(-1).resolve();await Promise.resolve();
 assert.deepEqual(opened,['second']);
 openRepo('denied');waiters.at(-1).reject(Error('not qualified'));await Promise.resolve();
 assert.deepEqual(switched,['repos']);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''.replace('SOURCE',json.dumps(code))
    result=subprocess.run(['node','-e',js],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stdout+result.stderr
