/* Wayfire implementation of PosterChan's compositor-neutral window contract.
 * Wayfire IPC is uint32-le JSON (no i3 magic/type header). Keep this backend separate so an
 * installed machine can select Sway simply by retaining SWAYSOCK: rollback never changes UI code. */
'use strict';
const net=require('net');
const fs=require('fs');
const path=require('path');
const {spawn}=require('child_process');
const {clampRectToOutputs,pidFamily}=require('./wm.js');

function wayfireSockets(explicit){
  if(explicit)return [String(explicit)];
  if(process.env.WAYFIRE_SOCKET)return [process.env.WAYFIRE_SOCKET];
  if(process.platform!=='linux')return [];
  const uid=typeof process.getuid==='function'?process.getuid():null;
  const runtime=process.env.XDG_RUNTIME_DIR||(uid==null?'':'/run/user/'+uid);
  const dirs=[runtime,'/tmp'].filter(Boolean),out=[];
  for(const dir of dirs)try{
    for(const name of fs.readdirSync(dir))if(/^wayfire-wayland-.*\.socket$/.test(name)){
      const p=path.join(dir,name);let at=0;try{const st=fs.statSync(p);if(!st.isSocket())continue;at=st.mtimeMs||0;}catch(_){continue;}out.push({p,at});
    }
  }catch(_){}
  return out.sort((a,b)=>b.at-a.at).map(x=>x.p);
}
function wfFrame(value){const body=Buffer.from(JSON.stringify(value),'utf8'),head=Buffer.alloc(4);head.writeUInt32LE(body.length);return Buffer.concat([head,body]);}
function wfDecoder(onMessage){let buf=Buffer.alloc(0);return chunk=>{buf=buf.length?Buffer.concat([buf,chunk]):chunk;for(;;){if(buf.length<4)return;const n=buf.readUInt32LE(0);if(n>16*1024*1024)throw new Error('invalid Wayfire IPC frame');if(buf.length<4+n)return;let value=null;try{value=JSON.parse(buf.subarray(4,4+n).toString('utf8'));}catch(_){}buf=buf.subarray(4+n);onMessage(value);}};}
function geometryOf(v){const g=v&&v.geometry||{};return {x:Number(g.x)||0,y:Number(g.y)||0,width:Number(g.width)||0,height:Number(g.height)||0};}
function normalizeView(v){
  const g=geometryOf(v);return {id:Number(v&&v.id),pid:Number(v&&v.pid)>0?Number(v.pid):0,
    app:String(v&&((v['app-id']||v.app_id||v.app)||'')),title:String(v&&v.title||''),
    workspace:String(v&&((v['wset-index']??v['output-id']??v.output_id)??'')),
    focused:!!(v&&(v.activated||v.focused)),fullscreen:!!(v&&v.fullscreen),floating:!(Number(v&&v['tiled-edges'])>0),
    xwayland:!!(v&&(v.type==='xwayland'||v['app-id']==null&&v.app_id==null)),stashed:!!(v&&v.minimized),rect:g};
}
function normalizeOutput(o){const g=o&&((o.geometry||o.workarea)||{});return {name:String(o&&o.name||o&&o.id||''),active:o&&o.active!==false,
  primary:!!(o&&o.focused),focused:!!(o&&o.focused),current_workspace:String(o&&((o['wset-index']??o.id)??'')),scale:Number(o&&o.scale)||1,
  transform:String(o&&o.transform||'normal'),make:String(o&&o.make||''),model:String(o&&o.model||''),serial:String(o&&o.serial||''),
  rect:{x:Number(g.x)||0,y:Number(g.y)||0,width:Number(g.width)||0,height:Number(g.height)||0},id:Number(o&&o.id)};}
function procParents(){const out=[];if(process.platform!=='linux')return out;try{for(const n of fs.readdirSync('/proc'))if(/^\d+$/.test(n))try{const s=fs.readFileSync('/proc/'+n+'/stat','utf8'),i=s.lastIndexOf(')'),f=s.slice(i+1).trim().split(/\s+/);out.push({pid:Number(n),ppid:Number(f[1])});}catch(_){}}catch(_){}return out;}

class WayfireWM{
  constructor(sockPath){this.backend='wayfire';this.paths=wayfireSockets(sockPath);this.path=this.paths[0]||'';this.sock=null;this.connecting=null;this.pending=[];this.listeners=new Map();this.moves=new Map();this.subscribed=false;this.actionServer=null;}
  available(){return this.paths.length>0;}
  _connect(){if(this.sock)return Promise.resolve(this.sock);if(this.connecting)return this.connecting;if(!this.paths.length)return Promise.reject(new Error('no compositor socket — WAYFIRE_SOCKET is not set'));this.connecting=(async()=>{let last;for(const p of this.paths)try{return await this._connectPath(p);}catch(e){last=e;}throw last||new Error('no live Wayfire socket');})().finally(()=>{this.connecting=null;});return this.connecting;}
  _connectPath(p){return new Promise((resolve,reject)=>{const s=net.createConnection(p);let settled=false;const feed=wfDecoder(msg=>{if(msg&&msg.event){this._event(msg);return;}const q=this.pending.shift();if(q){if(msg&&msg.error)q.reject(new Error(msg.error));else q.resolve(msg);}});s.on('data',c=>{try{feed(c);}catch(e){s.destroy(e);}});s.on('error',e=>{if(!settled){settled=true;reject(e);}this._fail(e);});s.on('close',()=>{if(this.sock===s)this.sock=null;this._fail(new Error('Wayfire IPC closed'));});s.on('connect',()=>{if(settled)return;settled=true;this.sock=s;this.path=p;resolve(s);});});}
  _fail(e){for(const q of this.pending.splice(0))q.reject(e);}
  _send(method,data){return this._connect().then(s=>new Promise((resolve,reject)=>{this.pending.push({resolve,reject});s.write(wfFrame({method,data:data||{}}));}));}
  _event(msg){const event=String(msg.event||'');let name=event.startsWith('view-')?'window':event.startsWith('output-')?'output':event.includes('workspace')?'workspace':event==='posterchan-tick'?'tick':'';if(!name)return;const raw=msg.view||msg.data&&msg.data.view;const ev={change:event,payload:msg.payload||msg.data&&msg.data.payload};if(raw)ev.wayfireView=normalizeView(raw);for(const fn of(this.listeners.get(name)||[]))try{fn(ev);}catch(_){}}
  version(){return this._send('list-methods').then(r=>({human_readable:'Wayfire IPC',methods:r&&r.methods||[]}));}
  async outputs(){const r=await this._send('window-rules/list-outputs');return (Array.isArray(r)?r:r&&r.outputs||[]).map(normalizeOutput);}
  workspaces(){return this.outputs().then(xs=>xs.map(x=>({name:x.current_workspace,focused:x.primary,output:x.name})));}
  async assignShell(id,assignment){const outs=await this.outputs(),o=outs.find(x=>x.name===String(assignment&&assignment.output));if(!o)throw new Error('Wayfire output not found');const b=assignment.rect||o.rect;await this.fullscreen(id,false);
    /* configure-view geometry is local to output_id. shell-displays deliberately carries global
     * Electron display bounds, so applying b.x/b.y directly displaced every non-origin output a
     * second time (DP-2 at x=3840 was configured at global x=7680). */
    return this._viewConfig(id,{x:b.x-o.rect.x,y:b.y-o.rect.y,w:b.width,h:b.height},{output_id:o.id});}
  async moveToAssignment(id,assignment){const outs=await this.outputs(),o=outs.find(x=>x.name===String(assignment&&assignment.output));if(!o)throw new Error('Wayfire output not found');return this._viewConfig(id,null,{output_id:o.id});}
  decorate(){return Promise.resolve(true);} // UI chrome is theme-owned; Wayfire rules exclude it.
  async windows(){const r=await this._send('window-rules/list-views');return (Array.isArray(r)?r:r&&r.views||[]).filter(v=>v&&v.mapped!==false&&v.role!=='desktop-environment').map(normalizeView);}
  tree(){return this.windows().then(rows=>({type:'root',nodes:rows}));}
  focus(id){return this._send('window-rules/focus-view',{id:Number(id)});}
  close(id){return this._send('window-rules/close-view',{id:Number(id)}).catch(e=>/not found|unknown view/i.test(String(e&&e.message||''))?{}:Promise.reject(e));}
  fullscreen(id,on){return this._send('wm-actions/set-fullscreen',{'view_id':Number(id),state:on!==false});}
  floating(id,on){return this._viewConfig(id,null,{'tiled-edges':on===false?15:0});}
  hide(id){return this._send('wm-actions/set-minimized',{'view_id':Number(id),state:true});}
  show(id){return this._send('wm-actions/set-minimized',{'view_id':Number(id),state:false});}
  async _viewConfig(id,rect,extra){const data=Object.assign({id:Number(id)},extra||{});if(rect)data.geometry={x:Math.round(rect.x),y:Math.round(rect.y),width:Math.round(rect.w),height:Math.round(rect.h)};return this._send('window-rules/configure-view',data);}
  async place(id,x,y,w,h){
    let at={x,y,w,h},extra={};
    try{
      const outs=await this.outputs();at=clampRectToOutputs(at,outs);
      /* Wayfire configure-view geometry is OUTPUT-LOCAL. Passing Electron's global coordinates
       * happened to work on the left display and detached Start by a whole monitor width on the
       * right. Select the output from the requested global rectangle, move there atomically, then
       * translate to its local coordinate space. */
      const cx=at.x+Math.max(1,at.w)/2,cy=at.y+Math.max(1,at.h)/2;
      const o=outs.find(v=>cx>=v.rect.x&&cx<v.rect.x+v.rect.width&&cy>=v.rect.y&&cy<v.rect.y+v.rect.height);
      if(o){extra.output_id=o.id;at={x:at.x-o.rect.x,y:at.y-o.rect.y,w:at.w,h:at.h};}
    }catch(_){}
    return this._viewConfig(id,at,extra);
  }
  async placeAndReveal(id,x,y,w,h){return this.place(id,x,y,w,h);}
  async restore(id,x,y,w,h){await this.show(id);return this.place(id,x,y,w,h);}
  async placeOnOutput(id,b,direction){const l=Number(b&&b.x)||0,t=Number(b&&b.y)||0,ow=Math.max(1,Number(b&&b.width)||1),oh=Math.max(1,Number(b&&b.height)||1),gap=12;const row=(await this.windows()).find(x=>x.id===Number(id)),r=row&&row.rect||{};const w=Math.min(Math.max(320,Number(r.width)||ow*.72),Math.max(1,ow-gap*2)),h=Math.min(Math.max(220,Number(r.height)||oh*.72),Math.max(1,oh-gap*2));let x=Math.min(Math.max(Number(r.x)||l+gap,l+gap),l+ow-w-gap),y=Math.min(Math.max(Number(r.y)||t+gap,t+gap),t+oh-h-gap);if(direction==='right')x=l+gap;else if(direction==='left')x=l+ow-w-gap;else if(direction==='down')y=t+gap;else if(direction==='up')y=t+oh-h-gap;await this.place(id,x,y,w,h);return{x:Math.round(x),y:Math.round(y),w:Math.round(w),h:Math.round(h)};}
  async snap(id,zone){const row=(await this.windows()).find(x=>x.id===Number(id));if(!row)return false;const outs=await this.outputs(),cx=row.rect.x+row.rect.width/2,cy=row.rect.y+row.rect.height/2,o=outs.find(o=>cx>=o.rect.x&&cx<o.rect.x+o.rect.width&&cy>=o.rect.y&&cy<o.rect.y+o.rect.height)||outs[0];if(!o)return false;const b=o.rect,h=Math.max(1,b.height-72),half=Math.floor(b.width/2);let x=b.x,y=b.y,w=b.width,hh=h;if(zone==='left')w=half;else if(zone==='right'){x+=half;w=b.width-half;}else if(/^(top|bottom)-(left|right)$/.test(zone)){const p=zone.split('-'),halfH=Math.floor(h/2);w=p[1]==='left'?half:b.width-half;if(p[1]==='right')x+=half;hh=p[0]==='top'?halfH:h-halfH;if(p[0]==='bottom')y+=halfH;}else if(zone!=='max')return false;return this.place(id,x,y,w,hh);}
  move(id,x,y){const key=Number(id);let state=this.moves.get(key);const at={x:Math.round(x),y:Math.round(y)};if(state){state.next=at;return state.promise;}state={next:at,promise:null};state.promise=(async()=>{while(state.next){const p=state.next;state.next=null;const row=(await this.windows()).find(v=>v.id===key);if(row)await this.place(key,p.x,p.y,row.rect.width,row.rect.height);}})().finally(()=>{if(this.moves.get(key)===state)this.moves.delete(key);});this.moves.set(key,state);return state.promise;}
  finishMove(id){const s=this.moves.get(Number(id));if(!s)return Promise.resolve();s.next=null;return s.promise||Promise.resolve();}
  applyChrome(){return Promise.resolve(true);} // PosterChanUI owns both macOS and Windows chrome.
  _openActionSocket(){
    if(this.actionServer||process.platform!=='linux')return;
    const runtime=process.env.XDG_RUNTIME_DIR||(typeof process.getuid==='function'?'/run/user/'+process.getuid():'');
    if(!runtime)return;const socketPath=path.join(runtime,'posterchan-action.sock');
    try{const st=fs.lstatSync(socketPath);if(st.isSocket())fs.unlinkSync(socketPath);else return;}catch(e){if(e&&e.code!=='ENOENT')return;}
    const server=net.createServer(c=>{let data='';c.setEncoding('utf8');c.on('data',x=>{data+=x;if(data.length>256)c.destroy();});c.on('end',()=>{const payload=data.trim();if(/^pc:[a-z0-9:_-]{1,220}$/i.test(payload))this._event({event:'posterchan-tick',payload});});});
    server.on('error',()=>{if(this.actionServer===server)this.actionServer=null;});
    server.listen(socketPath,()=>{try{fs.chmodSync(socketPath,0o600);}catch(_){}});this.actionServer=server;
  }
  async subscribe(){
    if(this.subscribed)return;
    /* 0.10 rejects the entire watch request when ONE event belongs to a newer release. Negotiate
     * down one named event at a time; losing an output notification only means the existing display
     * poll performs reconciliation, while losing every event made focus/minimise look single-window. */
    const events=['view-mapped','view-unmapped','view-focused','view-title-changed','view-app-id-changed',
      'view-set-output','view-geometry-changed','view-minimized','view-fullscreen','output-layout-changed','workspace-activated'];
    while(events.length){
      try{await this._send('window-rules/events/watch',{events});break;}
      catch(e){const match=/Event not found:\s*["']([^"']+)["']/i.exec(String(e&&e.message||e));
        if(!match)throw e;const at=events.indexOf(match[1]);if(at<0)throw e;events.splice(at,1);}
    }
    if(!events.length)throw new Error('Wayfire exposes no usable window events');
    this._openActionSocket();this.subscribed=true;
  }
  on(name,fn){if(!this.listeners.has(name))this.listeners.set(name,new Set());this.listeners.get(name).add(fn);return()=>this.listeners.get(name).delete(fn);}
  launch(argv,opts){const o=opts||{},child=spawn(argv[0],argv.slice(1),{detached:true,stdio:'ignore',cwd:o.cwd||undefined,env:Object.assign({},process.env,o.env||{})});let fail;const failed=new Promise(r=>{fail=r;});child.on('error',e=>fail(e&&e.code==='ENOENT'?argv[0]+' is not installed':String(e&&e.message||e)));child.unref();return{pid:child.pid,failed};}
  async waitForWindow(pid,ms,kin){const end=Date.now()+(ms||15000),roots=[Number(pid),...(kin||[]).map(Number)];let family=pidFamily(roots,[]);for(;;){family=pidFamily([...family],procParents());const hit=(await this.windows().catch(()=>[])).find(w=>family.has(w.pid));if(hit)return hit;if(Date.now()>end)return null;await new Promise(r=>setTimeout(r,250));}}
  async waitForNewWindow(before,ms,accept){const old=new Set((before||[]).map(Number)),end=Date.now()+(ms||15000);for(;;){const hit=(await this.windows().catch(()=>[])).find(w=>!old.has(w.id)&&(!accept||accept(w)));if(hit)return hit;if(Date.now()>end)return null;await new Promise(r=>setTimeout(r,250));}}
}
module.exports={WayfireWM,wfFrame,wfDecoder,wayfireSockets,normalizeView,normalizeOutput};
