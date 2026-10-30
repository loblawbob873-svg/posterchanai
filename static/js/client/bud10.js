/* BUD-10 `blossom:` URIs in posts (cards.js draws an empty <img data-bud10="<uri>">; app.js loads this on first use).
 * Loaded lazily: it is only needed by a post that holds one. */
window.PCBud10Factory = function(){
  'use strict';
  /* BUD-10: `blossom:<sha256>.<ext>?xs=<server>&as=<author pubkey>&sz=<bytes>` -- a file named by its hash, with the
   * servers that should hold it ("looks like we are missing bud-10 support", 2026-10-30; it drew as text). Tried in
   * order: each `xs` server, then the author's own Blossom list (kind 10063). An image is shown only once its bytes
   * hash to the sha256 -- any server can answer, so the hash is the proof it is the right file. */
  function _bud10Base(x){
    x=String(x||'').trim(); if(!x) return '';
    if(!/^https?:\/\//i.test(x)) x='https://'+x;
    try{ const u=new URL(x); return /^https?:$/.test(u.protocol) ? u.origin+u.pathname.replace(/\/+$/,'') : ''; }catch(_){ return ''; }
  }
  function _bud10(u){
    const m=/^blossom:([0-9a-fA-F]{64})(\.[A-Za-z0-9]{1,10})?(\?[^\s<#]*)?$/.exec(u); if(!m) return null;
    const q=new URLSearchParams((m[3]||'').slice(1)), as=(q.get('as')||'').toLowerCase();
    return { sha:m[1].toLowerCase(), ext:(m[2]||'').toLowerCase(), hosts:[...new Set(q.getAll('xs').map(_bud10Base).filter(Boolean))].slice(0,8),
             as:/^[0-9a-f]{64}$/.test(as) ? as : '', sz:Math.max(0, parseInt(q.get('sz'),10)||0) };
  }
  async function resolve(el){
    if(!el || el.dataset.bud10State) return; el.dataset.bud10State='busy';
    const b=_bud10(el.dataset.bud10||''); if(!b) return;
    if(/^\.(mp4|webm|mov|m4v)$/.test(b.ext) && b.hosts[0]){   // a video is streamed from its first server, not hashed
      const v=document.createElement('video'); v.src=b.hosts[0]+'/'+b.sha+b.ext; v.controls=true; v.playsInline=true; v.preload='metadata';
      v.className=el.className; el.dataset.bud10State='video'; el.replaceWith(v); return;
    }
    const tried=new Set(), unread=[];   // unread: answered nothing we could READ (no CORS), never wrong bytes
    const tryBase=async base=>{
      const url=base+'/'+b.sha+b.ext; if(!base || tried.has(url)) return false; tried.add(url);
      try{
        const ctl=new AbortController(), t=setTimeout(()=>ctl.abort(), 20000);
        const r=await fetch(url, { credentials:'omit', signal:ctl.signal }); clearTimeout(t);
        if(!r.ok) return false;
        const buf=await r.arrayBuffer();
        const h=[...new Uint8Array(await crypto.subtle.digest('SHA-256', buf))].map(x=>x.toString(16).padStart(2,'0')).join('');
        if(h!==b.sha){ b.bad=true; return false; }
        el.dataset.bud10State='ok'; el.dataset.osrc=url; el.removeAttribute('onload');
        el.src=URL.createObjectURL(new Blob([buf], { type:r.headers.get('content-type')||'' }));
        return true;
      }catch(_){ unread.push(url); return false; }
    };
    for(const h of b.hosts) if(await tryBase(h)) return;
    if(b.as){
      let ev=null; try{ ev=(await Relay.query([{ kinds:[10063], authors:[b.as], limit:1 }])).sort((x,y)=>y.created_at-x.created_at)[0]; }catch(_){}
      for(const t of ((ev&&ev.tags)||[]).slice(0,10)) if(t[0]==='server' && await tryBase(_bud10Base(t[1]))) return;
    }
    /* Nothing READABLE had it. A server without CORS can still show it as an ordinary image -- which is all any
     * https image link is -- unless one of them served the WRONG bytes, which rules unverified bytes out. */
    if(unread.length && !b.bad){
      el.removeAttribute('onload'); let i=0;
      el.onerror=()=>{ if(++i<unread.length) el.src=unread[i]; else el.replaceWith(Object.assign(document.createElement('span'),
        { className:'bud10-missing muted small', textContent:'Image not found on its servers ('+b.sha.slice(0,12)+'…)' })); };
      el.dataset.bud10State='unverified'; el.dataset.osrc=unread[0]; el.src=unread[0]; return;
    }
    el.dataset.bud10State='failed';
    const a=document.createElement('span'); a.className='bud10-missing muted small';
    a.textContent='Image not found on its servers ('+b.sha.slice(0,12)+'…)'; el.replaceWith(a);
  }
  return { resolve, parse: _bud10 };
};
