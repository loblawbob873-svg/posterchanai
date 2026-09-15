"""Execute shipped IPC handlers with the asynchronous Electron 44 contract."""
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_clipboard_async_results_permissions_and_image_bytes():
    subprocess.run(['node', '-e', r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync('desktop/main.js','utf8');
const handlers={};let allowed=true,wayland=false,fail=false,empty=false,nativeOk=true,stored,read='terminal text',resolveRead;
const clip={async writeText(s){if(fail)throw Error('denied');stored=s},async readText(){return resolveRead?new Promise(r=>resolveRead=r):read},async write(items){if(fail)throw Error('denied');stored=items[0].data['image/png'];}};
const native={isWayland:()=>wayland,async writeWaylandText(){return nativeOk},async writeWaylandImage(){return nativeOk},async readWaylandText(){return null}};
const ctx={ipcMain:{handle:(name,fn)=>handlers[name]=fn},fromOurPage:()=>allowed,clipboard:clip,console,Buffer,Blob,ArrayBuffer,require:p=>p==='electron'?{nativeImage:{createFromBuffer:()=>({isEmpty:()=>empty})},ClipboardItem:class{constructor(data){this.data=data}}}:native};
vm.runInNewContext(source.slice(source.indexOf("ipcMain.handle('pc:clip:write'"),source.indexOf('// Screen picker')),ctx);
(async()=>{
 assert.equal(await handlers['pc:clip:read']({}),'terminal text');
 assert.equal(await handlers['pc:clip:write']({},'copy'),true);assert.equal(stored,'copy');
 fail=true;assert.equal(await handlers['pc:clip:write']({},'copy'),false);
 const png=Buffer.from([137,80,78,71,13,10,26,10,1,2]);
 assert.equal(await handlers['pc:clip:write-image']({},png),false);
 wayland=true;assert.equal(await handlers['pc:clip:write']({},'copy'),true);assert.equal(await handlers['pc:clip:write-image']({},png),true);
 nativeOk=false;assert.equal(await handlers['pc:clip:write']({},'copy'),false);assert.equal(await handlers['pc:clip:write-image']({},png),false);
 fail=false;nativeOk=true;empty=true;assert.equal(await handlers['pc:clip:write-image']({},png),false);empty=false;
 wayland=false;fail=false;const backing=Buffer.concat([Buffer.from([9,9]),png,Buffer.from([8])]);
 assert.equal(await handlers['pc:clip:write-image']({},backing.subarray(2,-1)),true);
 assert.deepEqual(Buffer.from(await stored.arrayBuffer()),png);
 assert.equal(await handlers['pc:clip:write-image']({},Buffer.alloc(33*1024*1024)),false);
 resolveRead=true;const pending=handlers['pc:clip:read']({});await new Promise(r=>setImmediate(r));allowed=false;resolveRead('secret');assert.equal(await pending,'');
 assert.equal(await handlers['pc:clip:write']({},'denied'),false);
 allowed=true;resolveRead=null;clip.readText=async()=>{throw Error('unavailable')};assert.equal(await handlers['pc:clip:read']({}),'');
})().catch(e=>{console.error(e);process.exitCode=1});
'''], cwd=ROOT, check=True, timeout=15)


def test_screenshot_copy_awaits_write_and_menu_rejections_are_handled():
    subprocess.run(['node', '-e', r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync('desktop/main.js','utf8');
const end=source.indexOf('return Object.assign({}, r, { copied });');
assert(end>0);
const start=source.lastIndexOf('  let copied = false;',end);
const body=source.slice(start,end)+'return { ...r, copied };';
let release,reject=false,has=true,external=false,platform='win32';
const ctx={Blob,console:{warn(){}},r:{ok:true,path:'/saved.png'},process:{platform},clipboard:{write:()=>new Promise((yes,no)=>{release=()=>reject?no(Error('denied')):yes()}),has:async()=>has},require:p=>p==='electron'?{nativeImage:{createFromPath:()=>({isEmpty:()=>false,toPNG:()=>Buffer.from([1])})},ClipboardItem:class{}}:{clipboardHasImage:async()=>external}};
const run=()=>vm.runInNewContext('(async()=>{'+body+'})()',ctx);
(async()=>{
 let done=false,p=run().then(v=>{done=true;return v});await new Promise(r=>setImmediate(r));assert.equal(done,false);release();assert.equal((await p).copied,true);
 reject=true;p=run();release();assert.deepEqual(JSON.parse(JSON.stringify(await p)),{ok:true,path:'/saved.png',copied:false});
 reject=false;has=false;p=run();release();assert.equal((await p).copied,false);
 ctx.process.platform='linux';has=true;p=run();release();assert.equal((await p).copied,false);
 for(const label of ['Copy link address','Copy image address']){
  const line=source.split('\n').find(l=>l.includes("label: '"+label+"'"));assert(line);
  let item;vm.runInNewContext(line,{items:{push:v=>item=v},params:{linkURL:'https://example.test',srcURL:'https://example.test/i.png'},clipboard:{writeText:async()=>{throw Error('denied')}}});
  await item.click();
 }
})().catch(e=>{console.error(e);process.exitCode=1});
'''], cwd=ROOT, check=True, timeout=15)
