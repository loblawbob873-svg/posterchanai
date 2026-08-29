/* Execute overlapping calls against the production folder-upload function. */
'use strict';
const fs=require('fs'),path=require('path'),vm=require('vm');
const app=fs.readFileSync(path.resolve(__dirname,'../../static/js/client/app.js'),'utf8');

function fn(head){
  const i=app.indexOf(head),begin=app.indexOf('{',i);if(i<0||begin<0)throw new Error('missing '+head);
  let depth=0,quote='',escaped=false;
  for(let p=begin;p<app.length;p++){const c=app[p];if(quote){if(escaped)escaped=false;else if(c==='\\')escaped=true;
    else if(c===quote)quote='';continue;}if(c==="'"||c==='"'||c==='`'){quote=c;continue;}
    if(c==='{')depth++;if(c==='}'&&--depth===0)return app.slice(i,p+1);}
  throw new Error('unterminated '+head);
}

let release;
const held=new Promise(resolve=>{release=resolve;});
const calls={uploads:[],toasts:[],begin:0,end:0,renders:0};
const context={console,Set,Map,Promise,Math,Date,
  FilesIdx:{_pullDone:true,folders:()=>[],isEncFolder:()=>false,addFolder:()=>{},setFile:()=>{},
    beginBatch:()=>calls.begin++,endBatch:async()=>{calls.end++;return true;}},
  toast:x=>calls.toasts.push(String(x)),_uploadBadge:()=>{},
  uploadBlob:async(f,opts)=>{calls.uploads.push(f.name);await held;const sha='a'.repeat(64);opts.hashOut.sha=sha;return 'https://blossom.test/'+sha;},
  _shaFromUrl:()=>'',_signUploadBatch:async()=>null,_blossomDenied:()=>false,requestBlossomAccess:()=>{},
  uploadEncFile:async()=>'',uploadMusicTrack:async()=>{},_refreshBlobHave:async()=>{},
  _looksAudio:()=>false,_musicHasSrc:()=>false,enc:String,$:()=>null,
  mediaServer:()=> 'https://blossom.test',VIEW:'blossom',renderBlossom:()=>calls.renders++};
vm.createContext(context);
vm.runInContext(`let _filesFolder=null,_uploadCancel=false,_uploading=0,_uploadBatchAuth=null;
let _filesGridList=null,_blobHave=new Set(),_blobSizes=new Map();
${fn('function _uploadTargetFolder(')}
${fn('function _rememberUploadedBlob(')}
${fn('async function uploadFilesSeq(')}
globalThis.run=uploadFilesSeq;globalThis.busy=()=>_uploading;`,context);

(async()=>{
  const file=name=>({name,type:'text/plain',size:1,webkitRelativePath:''});
  const first=context.run([file('first.txt')]);
  await new Promise(resolve=>setImmediate(resolve));
  if(context.busy()!==1||calls.uploads.length!==1)throw new Error('first batch did not enter upload');
  await context.run([file('second.txt')]);
  if(calls.uploads.length!==1)throw new Error('second batch overlapped the first: '+calls.uploads);
  if(calls.begin!==1)throw new Error('second batch entered shared index state');
  if(!calls.toasts.some(x=>/upload is still running/i.test(x)))throw new Error('overlap was not explained');
  release();await first;
  if(context.busy()!==0||calls.end!==1||calls.renders!==1)throw new Error('first batch did not finish cleanly');
  console.log('folder upload is single-flight');
})().catch(e=>{console.error(e&&e.stack||e);process.exitCode=1;});
