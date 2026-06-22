/* #blackjack — Blackjack (21) vs the bot dealer, solo or at a multi-seat table. Separate file; uses
 * window.__PC + registers in window.PCGames. Each player plays their OWN hand vs the shared dealer;
 * play in the app or by replying "hit"/"stand" to the bot's DM. */
(function(){
  function init(){
    const PC = window.__PC;
    if(!PC){ return setTimeout(init, 50); }
    const { $, $$, enc, publish, sendDm, safePk, nip05Resolve, profOf, niceNip05, LOGO, toast, ensureProfile, NT } = PC;
    const Relay = window.Relay, Store = window.Store;
    let _timer = null;
    let _seatPicks = [];   // pubkeys invited to the next table

    const _SUIT = { S:'♠', H:'♥', D:'♦', C:'♣' };
    function handVal(h){ let t=0,a=0; for(const c of (h||[])){ const r=c.slice(0,-1); if(r==='A'){t+=11;a++;} else if('TJQK'.includes(r)) t+=10; else t+=(+r||0); } while(t>21&&a){t-=10;a--;} return t; }
    function cardHtml(c){ if(!c) return '<span class="bj-card back">?</span>'; const r=c.slice(0,-1), s=c.slice(-1); const red=(s==='H'||s==='D'); return `<span class="bj-card${red?' red':''}">${enc(r)}${enc(_SUIT[s]||s)}</span>`; }
    function _hidden(){ try{ return new Set(JSON.parse(localStorage.getItem('pc_bj_hidden')||'[]')); }catch(_){ return new Set(); } }
    function _hide(gid){ const s=_hidden(); s.add(gid); try{ localStorage.setItem('pc_bj_hidden', JSON.stringify([...s])); }catch(_){} }

    async function render(){
      const feed=$('#feed');
      const botNpub = PC.CFG.blackjack_bot_npub;
      _seatPicks = [];
      const start = botNpub ? `
        <div class="chess-invite">
          <button class="btn btn-cyan" id="bj-solo">🃏 New hand (solo vs dealer)</button>
          <div class="chess-or">— or seat a table with friends —</div>
          <input id="bj-inv" class="input" placeholder="search name / npub / name@domain…" autocomplete="off">
          <div id="bj-inv-res" class="chess-inv-res"></div>
          <div id="bj-seats" class="bj-seatchips"></div>
          <button class="btn btn-neon" id="bj-deal" style="display:none">Deal table ▶</button>
        </div>` : `<div class="empty">No #blackjack bot is configured on this server yet — ask the admin to enable one in Admin → Bots.</div>`;
      feed.innerHTML = `<div class="chess-hub">
          <div class="chess-splash glass">
            <h2>🃏 Blackjack</h2>
            <p class="muted">Beat the dealer to 21 without busting. Play solo or seat friends — everyone plays their own hand vs the same dealer (DM 'hit'/'stand', or tap below). Dealer stands on 17.</p>
            ${start}
          </div>
          <div class="chess-games"><h3>🃏 Your tables</h3><div id="bj-games"><div class="spinner"></div></div></div>
        </div>`;
      if(botNpub){ const sb=$('#bj-solo'); if(sb) sb.onclick=()=>startTable([]); _bindInvite(); }
      _load();
      clearInterval(_timer);
      _timer = setInterval(()=>{ if(PC.VIEW==='blackjack'){ _load(); } else clearInterval(_timer); }, 12000);
    }
    function _drawSeats(){
      const box=$('#bj-seats'), deal=$('#bj-deal'); if(!box) return;
      box.innerHTML = _seatPicks.map(pk=>{ const m=profOf(pk)||{}; return `<span class="bj-chip">${enc(m.name||m.display_name||'anon')}<button data-rm="${pk}">✕</button></span>`; }).join('');
      $$('[data-rm]',box).forEach(b=> b.onclick=()=>{ _seatPicks=_seatPicks.filter(p=>p!==b.dataset.rm); _drawSeats(); });
      if(deal) deal.style.display = _seatPicks.length ? '' : 'none';
      if(deal) deal.onclick=()=>startTable(_seatPicks.slice());
    }
    function _bindInvite(){
      const inp=$('#bj-inv'), res=$('#bj-inv-res'); if(!inp) return; let t=null;
      const draw=rows=>{ res.innerHTML = rows.length ? rows.map(p=>`<div class="chess-inv-row"><img src="${enc((p.meta||{}).picture||LOGO)}" onerror="this.src='${LOGO}'">
          <div class="ci-meta"><b>${enc((p.meta||{}).name||(p.meta||{}).display_name||'anon')}</b><span class="muted small">${enc(niceNip05((p.meta||{}).nip05)||'')}</span></div>
          <button class="btn btn-neon small" data-add="${p.pubkey}">Seat</button></div>`).join('')
        : '<div class="muted small" style="padding:6px 2px">No match. Paste an npub or name@domain.</div>';
        $$('[data-add]',res).forEach(b=> b.onclick=()=>{ const pk=b.dataset.add; if(pk!==PC.ME.pubkey && !_seatPicks.includes(pk)) _seatPicks.push(pk); res.innerHTML=''; inp.value=''; _drawSeats(); }); };
      inp.oninput=()=>{ clearTimeout(t); const q=inp.value.trim(); if(!q){ res.innerHTML=''; return; }
        t=setTimeout(async()=>{
          const pk=safePk(q); if(pk){ await ensureProfile(pk); draw([{pubkey:pk, meta:(profOf(pk)||{})}]); return; }
          if(/^[\w.\-+]+@[\w.\-]+\.[a-z]{2,}$/i.test(q)){ const rp=await nip05Resolve(q.toLowerCase()); if(rp){ await ensureProfile(rp); draw([{pubkey:rp, meta:(profOf(rp)||{})}]); return; } }
          const ql=q.toLowerCase();
          draw(Store.profileList().filter(p=>(((p.meta.name||'')+(p.meta.display_name||'')+(p.meta.nip05||'')).toLowerCase().includes(ql))).slice(0,8));
        }, 250); };
    }
    async function startTable(friends){
      const botPk=safePk(PC.CFG.blackjack_bot_npub); if(!botPk){ toast('no bot'); return; }
      const tags=[['p',botPk]]; (friends||[]).forEach(pk=>{ if(pk&&pk!==PC.ME.pubkey&&pk!==botPk) tags.push(['p',pk]); });
      tags.push(['t','blackjack'],['t','nostr'],['t','gamestr']);
      const body = (friends&&friends.length)
        ? `🃏 Dealing a #blackjack table — ${friends.map(pk=>{let n;try{n=NT().nip19.npubEncode(pk);}catch(_){n=pk;} return 'nostr:'+n;}).join(' ')} you're seated! Check your DMs.`
        : `🃏 Dealing a #blackjack hand vs the bot. I'll play from my DMs.`;
      try{ await publish(1, body+`\n\n#blackjack #nostr #gamestr`, tags); toast('dealing… 🃏'); setTimeout(()=>{ if(PC.VIEW==='blackjack') render(); }, 4500); }
      catch(e){ toast('could not start'); }
    }
    async function _load(){
      const list=$('#bj-games'); if(!list) return;
      const botPk=safePk(PC.CFG.blackjack_bot_npub);
      if(!botPk){ list.innerHTML='<div class="empty">No bot configured.</div>'; return; }
      let evs=[]; try{ evs=await Relay.query([{ authors:[botPk], kinds:[30078], limit:500 }]); }catch(_){}
      const hidden=_hidden(), byGame={};
      for(const e of evs){
        const d=((e.tags.find(t=>t[0]==='d')||[])[1])||'';
        if(!d.startsWith('pcai:blackjack:') || d.indexOf('player:')>=0) continue;
        let s; try{ s=JSON.parse(e.content||'{}'); }catch(_){ continue; }
        if(!s || !Array.isArray(s.seats) || !s.seats.includes(PC.ME.pubkey)) continue;
        const gid=s.root||d.slice('pcai:blackjack:'.length);
        if(hidden.has(gid)) continue;
        if(!byGame[gid] || (e.created_at||0) > byGame[gid]._t){ s._t=e.created_at||0; byGame[gid]=s; }
      }
      const games=Object.values(byGame).sort((a,b)=>(a.status==='playing'?0:1)-(b.status==='playing'?0:1)||(b.started||0)-(a.started||0));
      if(!games.length){ list.innerHTML='<div class="empty">No tables yet. Deal one above.</div>'; return; }
      list.innerHTML = games.map((g,i)=>`<div class="chess-game-card glass" data-gi="${i}"></div>`).join('');
      games.forEach((g,i)=>_card(g, $(`.chess-game-card[data-gi="${i}"]`, list)));
    }
    async function quitGame(g){
      const me=PC.ME.pubkey, myTurn = g.status==='playing' && !((g.done||{})[me]);
      if(!confirm(myTurn?'Stand and remove this table from your list?':'Remove this table?')) return;
      if(myTurn){ try{ await move(g,'stand'); }catch(_){} }
      _hide(g.root); _load();
    }
    function _card(g, card){
      if(!card) return;
      const me=PC.ME.pubkey, over=g.status!=='playing';
      const seats=g.seats||[], names=g.names||{}, hands=g.hands||{}, done=g.done||{}, results=g.results||{};
      const myDone = !!done[me];
      const dv=handVal(g.dhand);
      const dealer=(g.dhand||[]).map((c,i)=> !over && i>0 ? cardHtml(null) : cardHtml(c)).join('');
      let banner='', badge, statusLine;
      if(over){
        const o=results[me]||'lose', win=(o==='win'||o==='blackjack'), push=(o==='push');
        badge=push?'wait':(win?'you':'done'); statusLine=push?'Push':(win?'You won! 🎉':'You lost');
        banner=`<div class="chess-result ${push?'draw':(win?'win':'loss')}">${push?'🤝 Push':(win?'🏆 You won!':'💀 You lost')}<span class="muted small"> · ${enc(g.result||'')}</span></div>`;
      } else if(myDone){ badge='wait'; statusLine='Locked in — waiting on the table'; }
      else { badge='you'; statusLine='Your move — hit or stand'; }
      const seatRows = seats.map(pk=>{
        const h=hands[pk]||[], v=handVal(h), mine=pk===me;
        const out = over ? (results[pk]||'') : (done[pk]?'stand':'…');
        const oc = out==='blackjack'||out==='win'?'win':(out==='lose'?'loss':'');
        return `<div class="bj-hand${mine?' mine':''}"><span class="bj-lbl">${mine?'You':enc(names[pk]||'player')} · ${v} ${oc?`<span class="bj-out ${oc}">${enc((out||'').toUpperCase())}</span>`:(over?'':`<span class="muted small">${enc(out)}</span>`)}</span><div class="bj-cards">${h.map(cardHtml).join('')}</div></div>`;
      }).join('');
      const controls = (!over && !myDone) ? `<div class="bj-controls"><button class="btn btn-cyan small bj-hit">Hit</button> <button class="btn btn-neon small bj-stand">Stand</button></div>` : '';
      card.innerHTML = `<div class="chess-card-hd">
          <div class="cc-meta"><b>Blackjack ${seats.length>1?`· ${seats.length} seats`:'vs dealer'}</b><span class="muted small">Dealer stands on 17</span></div>
          <span class="cc-badge ${badge}">${enc(statusLine)}</span>
          <button class="chess-quit" title="Remove">✕</button></div>
        ${banner}
        <div class="bj-table">
          <div class="bj-hand dealer"><span class="bj-lbl">Dealer ${over?('· '+dv):''}</span><div class="bj-cards">${dealer}</div></div>
          ${seatRows}
        </div>
        ${controls}`;
      { const q=card.querySelector('.chess-quit'); if(q) q.onclick=(e)=>{ e.stopPropagation(); quitGame(g); }; }
      if(!over && !myDone){
        let busy=false; const lock=()=>{ busy=true; card.querySelectorAll('.bj-hit,.bj-stand').forEach(b=>b.disabled=true); };
        const hit=card.querySelector('.bj-hit'), st=card.querySelector('.bj-stand');
        if(hit) hit.onclick=()=>{ if(busy)return; lock(); move(g,'hit'); };
        if(st) st.onclick=()=>{ if(busy)return; lock(); move(g,'stand'); };
      }
    }
    async function move(game, action){
      const botPk=safePk(PC.CFG.blackjack_bot_npub); if(!botPk){ toast('no bot'); return; }
      try{ await sendDm(botPk, `${action}\n\ng:${game.root}`); toast(action+' sent 🃏'); }
      catch(e){ toast('move failed'); return; }
      setTimeout(()=>{ if(PC.VIEW==='blackjack') render(); }, 4500);
    }

    (window.PCGames = window.PCGames || {}).blackjack = render;
  }
  init();
})();
