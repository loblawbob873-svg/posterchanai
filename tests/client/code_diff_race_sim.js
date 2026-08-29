/* Runtime regression: a stale Source Control response must not reclaim the editor pane. */
'use strict';
const assert=require('assert');

global.window=global;
global.localStorage={getItem(){return null},setItem(){}};
global.document={querySelectorAll(){return []},addEventListener(){},hidden:false};
global.addEventListener=()=>{};
global.__PC={VIEW:'files',ME:null,$(){return null},enc:String,toast(){},authFetch(){throw new Error('network')},
  ensureAiSession(){},uiPrompt(){},uiConfirm(){return true}};
const waits={};
global.pcHost={pickDirectory(){},gitDiff(_root,path){return new Promise(resolve=>{waits[path]=resolve})}};
require('../../static/js/client/code.js');

(async()=>{
  PCCode._state.hostRoot='/project';PCCode._state.gitOpen=true;
  const a=PCCode._loadGitDiff('a.js'),b=PCCode._loadGitDiff('b.js');
  waits['b.js']({diff:'+ newest'});await b;
  assert.equal(PCCode._state.gitDiff.path,'b.js');
  waits['a.js']({diff:'+ stale'});assert.equal(await a,false);
  assert.equal(PCCode._state.gitDiff.path,'b.js','late A replaced the newer B diff');

  const c=PCCode._loadGitDiff('c.js');PCCode._state.gitOpen=false;PCCode._cancelGitDiff();
  waits['c.js']({diff:'+ should stay closed'});assert.equal(await c,false);
  assert.equal(PCCode._state.gitDiff,null,'late diff reopened after Explorer was selected');
  console.log('code diff request ownership runtime: ok');
})().catch(e=>{console.error(e);process.exitCode=1});
