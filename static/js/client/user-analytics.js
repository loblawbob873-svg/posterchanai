/* Personal, relay-native post analytics. Nothing is sent to a separate analytics service: the view
 * asks the user's configured relays for their public notes and the public events referencing them. */
(function(){
  const DAY = 86400;

  function target(ev){
    const tags = Array.isArray(ev && ev.tags) ? ev.tags : [];
    const roots = tags.filter(t => t && t[0] === 'e' && t[1]);
    const marked = roots.find(t => t[3] === 'root' || t[3] === 'reply');
    return (marked || roots[roots.length - 1] || [])[1] || '';
  }
  /* AN ADDRESS TIP IS A KIND-1 NOTE, AND WAS THEREFORE BEING COUNTED AS A REPLY.
     A Monero tip is published as a kind 1 carrying `t:monerotip` and `amount_xmr` (Bitcoin Cash
     the same with `bchtip`/`amount_bch`), because there is no zap receipt for a chain the sender
     paid from their own wallet. So it arrives through exactly the same `#e` query as a reply, and
     the only thing telling them apart is the `t` tag — without this check every tip somebody sent
     inflated the reply count and appeared nowhere as support received. */
  const TIP_TAGS = { monerotip: 'xmr', bchtip: 'bch' };
  function tipOf(ev){
    for(const t of ((ev && ev.tags) || [])) if(t && t[0] === 't' && TIP_TAGS[t[1]]) return TIP_TAGS[t[1]];
    return '';
  }
  /* Amounts are decimal strings ("0.001"), never integers, and a tip with no amount tag is still a
     tip — it counts as support even though it contributes nothing to the total. */
  function tagAmount(ev, name){
    const t = ((ev && ev.tags) || []).find(x => x && x[0] === name);
    const n = Number(t && t[1]);
    return Number.isFinite(n) && n > 0 ? n : 0;
  }
  function dedupe(items){ const seen=new Set(); return (items||[]).filter(x=>x&&x.id&&!seen.has(x.id)&&seen.add(x.id)); }
  function compute(posts, events, now, days){
    posts=dedupe(posts).sort((a,b)=>b.created_at-a.created_at); events=dedupe(events); now=now||Math.floor(Date.now()/1000);
    const byId=new Map(posts.map(p=>[p.id,{post:p,replies:0,reactions:0,reposts:0,zaps:0,xmr:0,bch:0,tips:0,score:0}]));
    events.forEach(e=>{ const row=byId.get(target(e)); if(!row)return;
      if(e.kind===1){
        /* A tip note is a kind 1 too — count it as support, never as a reply. */
        const tk=tipOf(e);
        if(tk==='xmr'){ row.xmr+=tagAmount(e,'amount_xmr'); row.tips++; }
        else if(tk==='bch'){ row.bch+=tagAmount(e,'amount_bch'); row.tips++; }
        else row.replies++;
      }
      else if(e.kind===7)row.reactions++; else if(e.kind===6)row.reposts++;
      else if(e.kind===9735){ const amount=(e.tags||[]).find(t=>t[0]==='amount'); row.zaps+=Math.round((Number(amount&&amount[1])||0)/1000); }
    });
    byId.forEach(r=>{ r.score=r.reactions+r.replies*2+r.reposts*3+r.tips*3+(r.zaps?1:0); });
    days=Math.max(1,Math.min(90,Number(days)||30));
    const daily=Array.from({length:days},()=>({posts:0,engagement:0}));
    posts.forEach(p=>{ const i=days-1-Math.floor((now-p.created_at)/DAY); if(i>=0&&i<days)daily[i].posts++; });
    events.forEach(e=>{ const i=days-1-Math.floor((now-e.created_at)/DAY); if(i>=0&&i<days&&byId.has(target(e)))daily[i].engagement++; });
    const rows=[...byId.values()], totals=rows.reduce((a,r)=>({posts:a.posts+1,replies:a.replies+r.replies,reactions:a.reactions+r.reactions,reposts:a.reposts+r.reposts,zaps:a.zaps+r.zaps,xmr:a.xmr+r.xmr,bch:a.bch+r.bch,tips:a.tips+r.tips}),{posts:0,replies:0,reactions:0,reposts:0,zaps:0,xmr:0,bch:0,tips:0});
    /* Tips are engagement. They were invisible here because they were being tallied as replies. */
    totals.engagement=totals.replies+totals.reactions+totals.reposts+totals.tips; totals.rate=totals.posts?totals.engagement/totals.posts:0;
    return {totals,daily,top:rows.sort((a,b)=>b.score-a.score||b.post.created_at-a.post.created_at).slice(0,6)};
  }

  function init(){
    const PC=window.__PC; if(!PC)return setTimeout(init,40);
    let range=30, serial=0;
    const esc=PC.enc, nf=n=>Number(n||0).toLocaleString();
    /* Chain amounts are decimals, not integer sats: 0.0002 XMR must not render as "0". */
    const coin=n=>Number(n||0).toLocaleString(undefined,{minimumFractionDigits:0,maximumFractionDigits:8});
    function bars(values, key, colour){ const max=Math.max(1,...values.map(x=>x[key])); return `<div class="ua-bars" aria-label="${range} day ${key} chart">${values.map((v,i)=>`<i style="height:${Math.max(v[key]?5:1,v[key]/max*100)}%;--bar:${colour}" title="Day ${i+1}: ${v[key]}"></i>`).join('')}</div>`; }
    function empty(message){ return `<div class="ua-empty"><span>↗</span><h2>${esc(message)}</h2><p>Your numbers will appear here as your relays return public post activity.</p></div>`; }
    function paint(data){ if(!PC.isView('analytics'))return; const f=PC.$('#feed'),t=data.totals;
      f.innerHTML=`<section class="ua-page">
        <header class="ua-head"><div><span class="ua-kicker">CREATOR STUDIO</span><h1>Your signal, at a glance.</h1><p>Public engagement from your connected relays.</p></div><div class="ua-ranges">${[7,30,90].map(n=>`<button data-days="${n}" class="${range===n?'on':''}">${n}D</button>`).join('')}</div></header>
        ${!t.posts?empty('Publish your first post to light this up.'):`
        <div class="ua-metrics">
          <article class="ua-metric hero"><span>Total engagement</span><strong>${nf(t.engagement)}</strong><small>${t.rate.toFixed(1)} interactions per post</small></article>
          <article class="ua-metric"><span>Posts</span><strong>${nf(t.posts)}</strong><small>in this period</small></article>
          <article class="ua-metric"><span>Reactions</span><strong>${nf(t.reactions)}</strong><small>♡ likes</small></article>
          <article class="ua-metric"><span>Replies</span><strong>${nf(t.replies)}</strong><small>↳ conversations</small></article>
          <article class="ua-metric"><span>Reposts</span><strong>${nf(t.reposts)}</strong><small>↻ shares</small></article>
          <article class="ua-metric zap"><span>Zap volume</span><strong>${nf(t.zaps)} <em>sats</em></strong><small>⚡ support received</small></article>
          <article class="ua-metric xmr"><span>Monero tips</span><strong>${coin(t.xmr)} <em>XMR</em></strong><small>ɱ ${nf(t.tips)} tip${t.tips===1?'':'s'} received</small></article>${t.bch?`
          <article class="ua-metric"><span>Bitcoin Cash</span><strong>${coin(t.bch)} <em>BCH</em></strong><small>🟢 on-chain tips</small></article>`:''}
        </div>
        <div class="ua-chartgrid"><article class="ua-panel"><header><div><b>Publishing rhythm</b><small>Posts over the last ${range} days</small></div><strong>${nf(data.daily.reduce((s,x)=>s+x.posts,0))}</strong></header>${bars(data.daily,'posts','#8b5cf6')}</article>
        <article class="ua-panel"><header><div><b>Audience pulse</b><small>Interactions over the last ${range} days</small></div><strong>${nf(data.daily.reduce((s,x)=>s+x.engagement,0))}</strong></header>${bars(data.daily,'engagement','#22d3ee')}</article></div>
        <article class="ua-panel ua-top"><header><div><b>Top posts</b><small>Your strongest signals in this period</small></div></header><div class="ua-posts">${data.top.map((r,i)=>`<button data-note="${r.post.id}"><span class="ua-rank">${String(i+1).padStart(2,'0')}</span><span class="ua-copy">${esc((r.post.content||'Media post').replace(/https?:\/\/\S+/g,'').trim().slice(0,120)||'Media post')}<small>${new Date(r.post.created_at*1000).toLocaleDateString()}</small></span><span class="ua-postnums"><b>${nf(r.reactions+r.replies+r.reposts+r.tips)}</b><small>engagements</small></span><span class="ua-arrow">↗</span></button>`).join('')}</div></article>`}
        <footer class="ua-foot">Analytics are calculated locally from public Nostr events. Relay coverage can vary.</footer></section>`;
      f.querySelectorAll('[data-days]').forEach(b=>b.onclick=()=>{range=+b.dataset.days;load();});
      f.querySelectorAll('[data-note]').forEach(b=>b.onclick=()=>PC.openNote(b.dataset.note));
    }
    async function load(){ const mine=++serial,viewer=PC.viewer(),f=PC.$('#feed'); if(!viewer.pubkey){f.innerHTML=empty('Sign in to see your analytics.');return;}
      f.innerHTML='<div class="spinner"></div>'; const since=Math.floor(Date.now()/1000)-range*DAY;
      try{ const posts=(await PC.relayQuery([{kinds:[1],authors:[viewer.pubkey],since,limit:500}],9000)).filter(p=>!(p.tags||[]).some(t=>t[0]==='e')); if(mine!==serial||!PC.isView('analytics'))return;
        const ids=posts.map(p=>p.id), events=[]; for(let i=0;i<ids.length;i+=100){ const chunk=ids.slice(i,i+100); if(!chunk.length)continue; const got=await PC.relayQuery([{kinds:[1,6,7,9735],'#e':chunk,limit:1000}],9000); events.push(...got); }
        if(mine===serial)paint(compute(posts,events,0,range));
      }catch(e){ if(mine===serial&&PC.isView('analytics'))f.innerHTML=`<div class="empty">Couldn’t load analytics from your relays.<br><button class="btn btn-cyan small" id="ua-retry">Try again</button></div>`,PC.$('#ua-retry').onclick=load; }
    }
    window.PCUserAnalytics={render:load,_compute:compute,_target:target};
  }
  init();
})();
