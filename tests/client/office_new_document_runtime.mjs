import fs from 'node:fs';
import vm from 'node:vm';

const app=fs.readFileSync(new URL('../../static/js/client/app.js',import.meta.url),'utf8');
const start=app.indexOf('  const _DOC_KINDS =');
const end=app.indexOf('  async function openOfficeFile',start);
if(start<0||end<0)throw new Error('could not lift the shipped new-document flow');

const controls=new Map();
const node=id=>{if(!controls.has(id))controls.set(id,{id,value:'',disabled:false,focus(){},addEventListener(){}});return controls.get(id);};
globalThis.$=(selector)=>{
  if(selector==='input[name="nd-kind"]:checked')return {value:'text'};
  return node(selector.replace(/^#/,'').split(/[ ,]/)[0]);
};
globalThis.modal=(_html,mount)=>mount({});
globalThis.enc=String;
globalThis._instanceBase=()=> 'https://instance.example';
globalThis._filesFolder='Documents';
globalThis.fileFromBytes=(bytes,name,type)=>({bytes,name,type,size:bytes.byteLength});
globalThis._shaFromUrl=()=>'';
globalThis._rememberUploadedBlob=()=>{};
globalThis.uploadEncFile=async()=>{throw new Error('unexpected encrypted upload');};
const indexed=[],opened=[],toasts=[],requests=[];
globalThis.FilesIdx={isEncFolder:()=>false,setFile:(sha,meta)=>indexed.push({sha,meta})};
globalThis.uploadBlob=async(file,opts)=>{opts.hashOut.sha='a'.repeat(64);globalThis.uploaded=file;return 'https://blob.example/'+opts.hashOut.sha;};
globalThis.closeModal=()=>{globalThis.closed=(globalThis.closed||0)+1;};
globalThis.renderBlossom=()=>{globalThis.rendered=(globalThis.rendered||0)+1;};
globalThis.openOfficeFile=async d=>opened.push(d);
globalThis.toast=s=>toasts.push(String(s));
globalThis.fetch=async url=>{requests.push(String(url));return new Response(new Uint8Array([80,75,3,4]),{status:200,headers:{'content-type':'application/vnd.oasis.opendocument.text','X-Document-Extension':'odt'}});};

vm.runInThisContext(app.slice(start,end)+'\nglobalThis.__officeTest={_newDocumentModal};',{filename:'app-office-new-document.js'});
__officeTest._newDocumentModal('text');
node('nd-name').value='Quarterly/Plan';
await node('nd-create').onclick();
if(requests[0]!=='https://instance.example/client/office/blank/text')throw new Error('Create requested the wrong blank-document endpoint');
if(uploaded.name!=='Quarterly-Plan.odt'||uploaded.type!=='application/vnd.oasis.opendocument.text')throw new Error('Create lost the safe name, extension, or MIME type');
if(indexed.length!==1||indexed[0].sha!=='a'.repeat(64)||indexed[0].meta.folder!=='Documents')throw new Error('Create did not index the uploaded document in its folder');
if(globalThis.closed!==1||globalThis.rendered!==1||opened.length!==1||opened[0].sha!=='a'.repeat(64))throw new Error('Create did not close, repaint Files, and open the new document');

globalThis.fetch=async()=>new Response('down',{status:503});
node('nd-create').disabled=false;
__officeTest._newDocumentModal('text');
await node('nd-create').onclick();
if(node('nd-create').disabled)throw new Error('failed Create left its button permanently disabled');
if(node('nd-create').textContent!=='Create')throw new Error('failed Create did not restore its label');
if(!toasts.some(x=>x.includes('could not create it:')&&x.includes('HTTP 503')))throw new Error('failed Create was silent');
console.log('office new-document click flow ok');
