'use strict';
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const TRANSFORMS = new Set(['normal','90','180','270','flipped','flipped-90','flipped-180','flipped-270']);
const quote = s => '"' + String(s).replace(/(["\\])/g, '\\$1') + '"';

function publicOutput(o){
  const modes = (o.modes || []).map(m => ({ width:+m.width||0, height:+m.height||0,
    refresh:+m.refresh||0, current:!!m.current, preferred:!!m.preferred }));
  return { name:String(o.name||''), make:String(o.make||''), model:String(o.model||''),
    serial:String(o.serial||''), active:!!o.active, focused:!!o.focused,
    primary:!!o.primary, rect:o.rect||{x:0,y:0,width:0,height:0}, scale:+o.scale||1,
    transform:String(o.transform||'normal'), modes };
}

function modeText(m){
  const hz = Math.round((+m.refresh || 0) / 1000 * 1000) / 1000;
  return `${+m.width}x${+m.height}${hz ? '@'+hz+'Hz' : ''}`;
}

/* `prune` IS FOR REPLAYING A SAVED LAYOUT, NEVER FOR A REQUEST SOMEBODY JUST MADE.
 *
 * Strict is right for preview(): the layout came from the Displays page, so a monitor it does not
 * know about or a mode it cannot run is a bug worth refusing loudly. It is exactly wrong at
 * startup, where the same function replays a file written weeks ago. Unplug one of two monitors —
 * or let a docking station renegotiate a mode away — and a saved layout naming it threw, so the
 * restore aborted ENTIRELY and the remaining monitor came up in whatever arrangement the
 * compositor guessed, with nothing on screen or in any log to say a saved layout existed.
 * Pruning drops the outputs that are gone and falls back to the display's preferred mode when the
 * saved one is: the rest of the arrangement is still the arrangement the user confirmed. */
function validate(layout, actual, opts){
  const prune=!!(opts&&opts.prune);
  if(!Array.isArray(layout) || !layout.length) throw new Error('no displays were supplied');
  const byName = new Map(actual.map(o => [String(o.name), o]));
  const seen = new Set(), out=[];
  for(const raw of layout){
    const name=String(raw && raw.name || '');
    const live=byName.get(name);
    if((!live || seen.has(name)) && prune) continue;
    if(!live || seen.has(name)) throw new Error('unknown or duplicate display: '+name);
    seen.add(name);
    const enabled=raw.enabled !== false;
    const transform=String(raw.transform||'normal');
    if(!TRANSFORMS.has(transform)) throw new Error('invalid rotation for '+name);
    const scale=Number(raw.scale == null ? (live.scale||1) : raw.scale);
    if(!Number.isFinite(scale) || scale < .5 || scale > 3) throw new Error('invalid scale for '+name);
    const x=Math.round(Number(raw.x)||0), y=Math.round(Number(raw.y)||0);
    let mode='';
    if(enabled && raw.mode){
      const want=String(raw.mode).replace(/Hz$/,'');
      const found=(live.modes||[]).find(m => modeText(m).replace(/Hz$/,'')===want);
      if(!found && !prune) throw new Error('unsupported mode '+raw.mode+' for '+name);
      if(found) mode=modeText(found);
    }
    out.push({name,enabled,x,y,scale,transform,mode,primary:!!raw.primary});
  }
  if(prune && !out.length) throw new Error('none of the saved displays are connected');
  if(!out.some(o=>o.enabled)) throw new Error('at least one display must remain enabled');
  if(out.filter(o=>o.primary && o.enabled).length>1) throw new Error('choose only one primary display');
  /* Keep the compositor's global coordinate space non-negative. XWayland translates a client's
   * surface through its legacy root window while Sway decorates it in compositor coordinates; a
   * negative output origin makes those two disagree by exactly the origin offset. The visible
   * result is Telegram/Steam content detached from its title bar, pointer walls, and windows that
   * snap back while crossing monitors. Shifting every enabled output together preserves the user's
   * arrangement and gaps while making (0,0) the desktop's top-left corner. */
  const enabled=out.filter(o=>o.enabled);
  const minX=Math.min(...enabled.map(o=>o.x));
  const minY=Math.min(...enabled.map(o=>o.y));
  if(minX || minY) for(const o of enabled){ o.x-=minX; o.y-=minY; }
  /* A small empty strip between otherwise-adjacent displays is an unreachable part of Sway's
   * coordinate space, not useful spacing. The pointer stops there and a dragged native window can
   * lose its destination and snap back. Snap nearby overlapping edges together; retain deliberate
   * large arrangements and perpendicular offsets. */
  const size=o=>{
    const live=byName.get(o.name)||{}, rect=live.rect||{};
    let w=Number(rect.width)||1, h=Number(rect.height)||1;
    if(o.mode){ const m=/^(\d+)x(\d+)/.exec(o.mode); if(m){w=+m[1]/o.scale;h=+m[2]/o.scale;} }
    return {w:Math.round(w),h:Math.round(h)};
  };
  const overlap=(a0,a1,b0,b1)=>Math.min(a1,b1)>Math.max(a0,b0);
  /* Scaling changes an output's logical width/height. Sway does not move its neighbours with it:
   * two displays that touched at x=1920 become separated by a pointer-blocking gap when the first
   * display is changed to 125% (its logical edge moves to 1536). Preserve an existing seam when
   * the user changed scale without explicitly rearranging either monitor. */
  const liveActive=actual.filter(o=>o&&o.active), liveMinX=Math.min(...liveActive.map(o=>(o.rect&&o.rect.x)||0));
  const liveMinY=Math.min(...liveActive.map(o=>(o.rect&&o.rect.y)||0));
  for(const cur of enabled){
    const curLive=byName.get(cur.name)||{}, cr=curLive.rect||{};
    for(const other of enabled){
      if(other===cur) continue;
      const otherLive=byName.get(other.name)||{}, or=otherLive.rect||{}, os=size(other);
      const oldCx=(Number(cr.x)||0)-liveMinX, oldCy=(Number(cr.y)||0)-liveMinY;
      const oldOx=(Number(or.x)||0)-liveMinX, oldOy=(Number(or.y)||0)-liveMinY;
      if(cur.x===oldCx && other.x===oldOx && oldCx===oldOx+(Number(or.width)||0) &&
          overlap(oldCy,oldCy+(Number(cr.height)||0),oldOy,oldOy+(Number(or.height)||0)))
        cur.x=other.x+os.w;
      if(cur.y===oldCy && other.y===oldOy && oldCy===oldOy+(Number(or.height)||0) &&
          overlap(oldCx,oldCx+(Number(cr.width)||0),oldOx,oldOx+(Number(or.width)||0)))
        cur.y=other.y+os.h;
    }
  }
  for(const cur of enabled){
    const cs=size(cur); let bestX=null,bestY=null;
    for(const other of enabled){
      if(other===cur) continue;
      const os=size(other);
      if(overlap(cur.y,cur.y+cs.h,other.y,other.y+os.h)){
        const gap=cur.x-(other.x+os.w);
        if(gap>0 && gap<=256 && (bestX===null || gap<bestX)) bestX=gap;
      }
      if(overlap(cur.x,cur.x+cs.w,other.x,other.x+os.w)){
        const gap=cur.y-(other.y+os.h);
        if(gap>0 && gap<=256 && (bestY===null || gap<bestY)) bestY=gap;
      }
    }
    if(bestX!==null) cur.x-=bestX;
    if(bestY!==null) cur.y-=bestY;
  }
  return out;
}

function commands(rows){
  const out=rows.map(o => {
    let s='output '+quote(o.name)+' '+(o.enabled?'enable':'disable');
    if(o.enabled){
      if(o.mode) s+=' mode '+o.mode;
      s+=' pos '+o.x+' '+o.y+' scale '+o.scale+' transform '+o.transform;
    }
    return s;
  });
  /* Sway has no freestanding `primary` flag. The meaningful desktop equivalent is where workspace
   * 1 (and therefore the PosterChan shell on a normal session) belongs. Assign it persistently and
   * focus that output for the live preview; otherwise the radio button is saved-looking decoration. */
  const primary=rows.find(o=>o.enabled&&o.primary);
  if(primary){
    out.push('workspace 1 output '+quote(primary.name));
    out.push('focus output '+quote(primary.name));
  }
  return out;
}

function snapshot(actual){
  return actual.map(o => { const cur=(o.modes||[]).find(m=>m.current);
    return {name:String(o.name),enabled:!!o.active,x:(o.rect&&o.rect.x)||0,y:(o.rect&&o.rect.y)||0,
      scale:+o.scale||1,transform:String(o.transform||'normal'),mode:cur?modeText(cur):'',primary:!!o.focused}; });
}

class Displays {
  constructor(wm, opts){ this.wm=wm; this.file=(opts&&opts.file)||path.join(process.env.HOME||'',
      wm&&wm.backend==='wayfire'?'.config/posterchanos/displays.json':'.config/sway/outputs.conf');
    this.ms=(opts&&opts.revertMs)||15000; this.pending=null; }
  /* Sway's own IPC already answers modes/scale/transform, so it needs no second method and does
   * not have one. Wayfire's does not, and the settings page is unusable without them. */
  _outputs(){return typeof this.wm.outputsDetailed==='function'?this.wm.outputsDetailed():this.wm.outputs();}
  _saved(){
    if(!(this.wm&&this.wm.backend==='wayfire'))return null;
    try{const value=JSON.parse(fs.readFileSync(this.file,'utf8'));return Array.isArray(value&&value.outputs)?value.outputs:null;}
    catch(_){return null;}
  }
  async status(){
    const rows=(await this._outputs()).map(publicOutput),saved=this._saved();
    const primary=saved&&saved.find(x=>x&&x.primary&&x.enabled!==false);
    if(primary)for(const row of rows)row.primary=row.name===primary.name;
    else if(rows.length&&!rows.some(row=>row.primary)){
      const first=rows.filter(row=>row.active).sort((a,b)=>(a.rect.x-b.rect.x)||(a.rect.y-b.rect.y))[0];
      if(first)first.primary=true;
    }
    return rows;
  }
  async repairPointerGaps(){
    const actual=await this._outputs();
    /* On Wayfire this is also the early-session persistence hook. Do not replace the saved layout
     * with the compositor's automatic arrangement before applying it: that made every reboot undo
     * precisely the monitor layout System Settings had confirmed. */
    const persisted=this._saved();
    if(persisted){const rows=validate(persisted,actual,{prune:true});await this._run(rows);return {ok:true,changed:true,persisted:true};}
    const before=snapshot(actual), rows=validate(before,actual);
    const changed=rows.some((o,i)=>o.x!==before[i].x || o.y!==before[i].y);
    const body='# PosterChanOS System Settings — generated; edit through Displays.\n'+commands(rows).join('\n')+'\n';
    let saved=''; try{ saved=fs.readFileSync(this.file,'utf8'); }catch(_){}
    const fileChanged=saved!==body;
    if(!changed && !fileChanged) return {ok:true,changed:false};
    if(changed) await this._run(rows);
    fs.mkdirSync(path.dirname(this.file),{recursive:true});
    const tmp=this.file+'.new';
    fs.writeFileSync(tmp,body,{mode:0o600});
    fs.renameSync(tmp,this.file);
    return {ok:true,changed:true};
  }
  async _run(rows){
    if(this.wm&&typeof this.wm.configureOutputs==='function')return this.wm.configureOutputs(rows);
    for(const cmd of commands(rows)) await this.wm.command(cmd);
  }
  async preview(layout){
    if(this.pending) await this.revert(this.pending.token);
    const actual=await this._outputs();
    const rows=validate(layout,actual), before=snapshot(actual);
    await this._run(rows);
    const token=crypto.randomBytes(12).toString('hex');
    const timer=setTimeout(()=>{ this.revert(token).catch(()=>{}); },this.ms);
    this.pending={token,rows,before,timer};
    return {ok:true,token,revertMs:this.ms};
  }
  async confirm(token){
    const p=this.pending; if(!p || p.token!==String(token||'')) throw new Error('display preview expired');
    clearTimeout(p.timer); this.pending=null;
    fs.mkdirSync(path.dirname(this.file),{recursive:true});
    const tmp=this.file+'.new';
    const body=this.wm&&this.wm.backend==='wayfire'
      ?JSON.stringify({version:1,outputs:p.rows},null,2)+'\n'
      :'# PosterChanOS System Settings — generated; edit through Displays.\n'+commands(p.rows).join('\n')+'\n';
    fs.writeFileSync(tmp,body,{mode:0o600});
    fs.renameSync(tmp,this.file);
    return {ok:true};
  }
  async revert(token){
    const p=this.pending; if(!p || (token && p.token!==String(token))) return {ok:true};
    clearTimeout(p.timer); this.pending=null; await this._run(p.before); return {ok:true};
  }
}

module.exports={Displays,publicOutput,modeText,validate,commands,snapshot};
