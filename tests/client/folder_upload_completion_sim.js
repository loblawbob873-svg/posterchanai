/* Execute packaged Files folder-upload completion against an in-memory Blossom/index boundary. */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const app = fs.readFileSync(process.env.PC_INSTALLED_APP_JS ||
  path.resolve(__dirname, '../../static/js/client/app.js'), 'utf8');

function fn(head) {
  const i=app.indexOf(head), begin=app.indexOf('{',i);if(i<0||begin<0)throw new Error('missing '+head);
  let depth=0,quote='',escaped=false;
  for(let p=begin;p<app.length;p++){const c=app[p];if(quote){if(escaped)escaped=false;else if(c==='\\')escaped=true;
    else if(c===quote)quote='';continue;}if(c==="'"||c==='"'||c==='`'){quote=c;continue;}
    if(c==='{')depth++;if(c==='}'&&--depth===0)return app.slice(i,p+1);}
  throw new Error('unterminated '+head);
}
const calls={folders:[],files:[],batch:[],toasts:[],badges:[],renders:0,uploads:[]};
const context={console,Set,Promise,Math,Date,saveOK:true,
  FilesIdx:{_pullDone:true,folders:()=>[],isEncFolder:()=>false,beginBatch:()=>calls.batch.push('begin'),
    endBatch:async()=>{calls.batch.push('end');return context.saveOK;},addFolder:f=>calls.folders.push(f),
    setFile:(sha,d)=>calls.files.push([sha,d])},
  toast:x=>calls.toasts.push(x),_uploadBadge:x=>calls.badges.push(x),
  uploadBlob:async f=>{calls.uploads.push(f.name);return 'https://blossom.test/'+
    (f.name==='a.jpg'?'a':'b').repeat(64);},_shaFromUrl:u=>u.split('/').pop(),
  _signUploadBatch:async()=>null,_blossomDenied:()=>false,requestBlossomAccess:()=>{},
  uploadEncFile:async()=>{},uploadMusicTrack:async()=>{},_refreshBlobHave:async()=>{},
  _looksAudio:()=>false,_musicHasSrc:()=>false,enc:String,$:()=>null,mediaServer:()=>"https://blossom.test",
  setTimeout:f=>{f();return 1},VIEW:'blossom',renderBlossom:()=>calls.renders++};
vm.createContext(context);
vm.runInContext(`let _filesFolder=null,_uploadCancel=false,_uploading=0,_uploadBatchAuth=null;
let _filesGridList=null,_blobHave=new Set(),_blobSizes=new Map();
${fn('function _uploadTargetFolder(')}
${fn('function _rememberUploadedBlob(')}
${fn('async function uploadFilesSeq(')}
globalThis.run=uploadFilesSeq;globalThis.uploading=()=>_uploading;
globalThis.visible=()=>_filesGridList;globalThis.have=()=>_blobHave;globalThis.sizes=()=>_blobSizes;
globalThis.failIndex=()=>{globalThis.saveOK=false;};`,context);

(async()=>{
  const files=[{name:'a.jpg',type:'image/jpeg',size:10,webkitRelativePath:'Pictures/a.jpg'},
    {name:'b.jpg',type:'image/jpeg',size:20,webkitRelativePath:'Pictures/Trips/b.jpg'}];
  await context.run(files);
  const folders=JSON.stringify(calls.folders),placed=calls.files.map(x=>x[1].folder);
  if(folders!==JSON.stringify(['Pictures','Pictures/Trips']))throw new Error('folders not registered: '+folders);
  if(JSON.stringify(placed)!==JSON.stringify(['Pictures','Pictures/Trips']))throw new Error('wrong indexed folders: '+placed);
  if(JSON.stringify(calls.batch)!==JSON.stringify(['begin','end']))throw new Error('batch did not commit: '+calls.batch);
  if(context.uploading()!==0)throw new Error('upload remained busy after completion');
  if(calls.renders!==1)throw new Error('Blossom did not refresh after completion');
  const visible=context.visible();
  if(visible.length!==2||visible.some(b=>!b.url||!b.name||!b.type))
    throw new Error('completed uploads were not added to the visible listing: '+JSON.stringify(visible));
  if(visible.some(b=>!context.have().has(b.sha256)||context.sizes().get(b.sha256)!==b.size))
    throw new Error('blob presence/size caches were not updated with the visible listing');
  if(!calls.toasts.some(x=>/Done.*2 added/.test(x)))throw new Error('no completion summary: '+calls.toasts);
  context.failIndex();
  await context.run([{name:'c.jpg',type:'image/jpeg',size:30,webkitRelativePath:'More/c.jpg'}]);
  if(!calls.toasts.some(x=>/Uploaded — folder list waiting to save.*1 added/.test(x)))
    throw new Error('failed index was announced as complete: '+calls.toasts);
  if(context.uploading()!==0)throw new Error('failed index save left upload busy');
  console.log('installed folder upload completion holds');
})().catch(e=>{console.error(e&&e.stack||e);process.exitCode=1});
