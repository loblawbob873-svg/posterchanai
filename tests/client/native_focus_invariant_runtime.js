'use strict';
const fs=require('fs'),path=require('path');
const src=fs.readFileSync(path.resolve(__dirname,'../../static/js/client/os.js'),'utf8');
function fn(signature){
  const start=src.indexOf(signature);if(start<0)throw Error('missing '+signature);
  const brace=src.indexOf('{',start);let depth=0;
  for(let i=brace;i<src.length;i++){
    if(src[i]==='{')depth++;else if(src[i]==='}'&&!--depth)return src.slice(start,i+1);
  }
  throw Error('unterminated '+signature);
}
(async()=>{
  const calls=[];
  global.window=global;
  global.pcWM={
    decorate:async id=>{calls.push('decorate:'+id);return true;},
    focus:async id=>{calls.push('focus:'+id);return true;}
  };
  let nativeTasks=[{id:73,focused:true,stashed:false}],nativeMenuHidden=[];
  const nsync=async()=>{calls.push('sync');};
  // Same lexical scope as the production declarations, so this exercises their real control flow.
  const _focusNativeDecorated=eval('('+fn('function _focusNativeDecorated')+')');
  const _nativeMenuLayer=eval('('+fn('async function _nativeMenuLayer')+')');
  await _nativeMenuLayer(true);
  await _nativeMenuLayer(false);
  if(calls.join(',')!=='sync,decorate:73,focus:73')
    throw Error('native overlay return bypassed decoration/order: '+calls.join(','));
  if(nativeMenuHidden.length)throw Error('overlay focus memory was not consumed');
  console.log('Native focus invariant runtime: ok');
})().catch(e=>{console.error(e&&e.stack||e);process.exitCode=1;});
