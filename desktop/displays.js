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

function validate(layout, actual){
  if(!Array.isArray(layout) || !layout.length) throw new Error('no displays were supplied');
  const byName = new Map(actual.map(o => [String(o.name), o]));
  const seen = new Set(), out=[];
  for(const raw of layout){
    const name=String(raw && raw.name || '');
    const live=byName.get(name);
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
      if(!found) throw new Error('unsupported mode '+raw.mode+' for '+name);
      mode=modeText(found);
    }
    out.push({name,enabled,x,y,scale,transform,mode,primary:!!raw.primary});
  }
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
  constructor(wm, opts){ this.wm=wm; this.file=(opts&&opts.file)||path.join(process.env.HOME||'', '.config/sway/outputs.conf');
    this.ms=(opts&&opts.revertMs)||15000; this.pending=null; }
  async status(){ return (await this.wm.outputs()).map(publicOutput); }
  async _run(rows){ for(const cmd of commands(rows)) await this.wm.command(cmd); }
  async preview(layout){
    if(this.pending) await this.revert(this.pending.token);
    const actual=await this.wm.outputs();
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
    fs.writeFileSync(tmp,'# PosterChanOS System Settings — generated; edit through Displays.\n'+commands(p.rows).join('\n')+'\n',{mode:0o600});
    fs.renameSync(tmp,this.file);
    return {ok:true};
  }
  async revert(token){
    const p=this.pending; if(!p || (token && p.token!==String(token))) return {ok:true};
    clearTimeout(p.timer); this.pending=null; await this._run(p.before); return {ok:true};
  }
}

module.exports={Displays,publicOutput,modeText,validate,commands,snapshot};
