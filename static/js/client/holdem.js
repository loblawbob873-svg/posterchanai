/* #holdem — multiplayer Texas Hold'em, dealt + refereed by the bot. Mobile-first cyberpunk UI.
 * Reads the bot's kind-30078 table doc; your OWN hole cards are NIP-44-encrypted in it (only you can
 * decrypt them — PC.nip44dec). Act in the app or by DMing the bot. Persistent table: it re-deals
 * until everyone leaves. Registers in window.PCGames. */
(function(){
  function init(){
    const PC = window.__PC;
    if(!PC){ return setTimeout(init, 50); }
    const { $, $$, enc, sendDm, safePk, nip05Resolve, profOf, niceNip05, LOGO, toast, ensureProfile, NT } = PC;
    const Relay = window.Relay, Store = window.Store;
    let _timer = null, _seatPicks = [], _holeCache = {};

    const RANKS='23456789TJQKA', SUITS=['♠','♥','♦','♣'];
    function cardHtml(c, big){
      if(c===null||c===undefined) return `<span class="pk-card back${big?' big':''}">?</span>`;
      const r=RANKS[c%13], si=Math.floor(c/13), s=SUITS[si], red=(si===1||si===2);
      return `<span class="pk-card${red?' red':''}${big?' big':''}">${r}${s}</span>`;
    }
    function nameOf(pk, fb){ const m=profOf(pk)||{}; return m.name||m.display_name||niceNip05(m.nip05)||fb||'player'; }
    function _hidden(){ try{ return new Set(JSON.parse(localStorage.getItem('pc_hm_holdem')||'[]')); }catch(_){ return new Set(); } }
    function _hide(gid){ const s=_hidden(); s.add(gid); try{ localStorage.setItem('pc_hm_holdem', JSON.stringify([...s])); }catch(_){} }

    async function render(){
      const feed=$('#feed');
      const botNpub = PC.CFG.holdem_bot_npub;
      _seatPicks = [];
      const start = botNpub ? `
        <div class="chess-invite">
          <button class="btn btn-cyan" id="hm-solo" style="width:100%">🤖 New game — heads-up vs the bot</button>
          <div class="chess-or">— or seat a table with friends (up to 5) —</div>
          <input id="hm-inv" class="input" placeholder="search name / npub / name@domain…" autocomplete="off">
          <div id="hm-inv-res" class="chess-inv-res"></div>
          <div id="hm-seats" class="bj-seatchips"></div>
          <button class="btn btn-neon" id="hm-deal" style="display:none">Deal table ▶</button>
        </div>` : `<div class="empty">No #holdem bot is configured on this server yet — ask the admin to enable one in Admin → Bots.</div>`;
      feed.innerHTML = `<div class="chess-hub">
          <div class="chess-splash glass">
            <h2>🃏 Texas Hold'em</h2>
            <p class="muted">No-limit Hold'em with friends. The bot deals your hole cards privately, runs the betting (pre-flop → flop → turn → river), and posts each result. Act here or DM the bot <code>check</code> / <code>call</code> / <code>raise N</code> / <code>fold</code> / <code>allin</code> / <code>leave</code>. Play-money chips; the table keeps going until everyone leaves.</p>
            ${start}
          </div>
          <div class="chess-games"><h3>🃏 Your tables</h3><div id="hm-games"><div class="spinner"></div></div></div>
        </div>`;
      if(botNpub){ const sb=$('#hm-solo'); if(sb) sb.onclick=()=>startTable([]); _bindInvite(); }
      _load();
      clearInterval(_timer);
      _timer = setInterval(()=>{ if(PC.VIEW==='holdem'){ _load(); } else clearInterval(_timer); }, 8000);
    }
    function _drawSeats(){
      const box=$('#hm-seats'), deal=$('#hm-deal'); if(!box) return;
      box.innerHTML = _seatPicks.map(pk=>{ const m=profOf(pk)||{}; return `<span class="bj-chip">${enc(m.name||m.display_name||'anon')}<button data-rm="${pk}">✕</button></span>`; }).join('');
      $$('[data-rm]',box).forEach(b=> b.onclick=()=>{ _seatPicks=_seatPicks.filter(p=>p!==b.dataset.rm); _drawSeats(); });
      if(deal){ deal.style.display = _seatPicks.length ? '' : 'none'; deal.onclick=()=>startTable(_seatPicks.slice()); }
    }
    function _bindInvite(){
      const inp=$('#hm-inv'), res=$('#hm-inv-res'); if(!inp) return; let t=null;
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
      const botPk=safePk(PC.CFG.holdem_bot_npub); if(!botPk){ toast('no bot'); return; }
      friends=(friends||[]).filter(pk=>pk&&pk!==PC.ME.pubkey&&pk!==botPk);
      const solo=!friends.length;
      const tags=[['p',botPk]]; friends.forEach(pk=>tags.push(['p',pk]));
      tags.push(['t','holdem'],['t','poker'],['t','nostr'],['t','gamestr']);
      const body = solo
        ? `🃏 Dealing a #holdem game — heads-up vs the bot. Check my DMs for my hole cards.`
        : `🃏 Dealing a #holdem table — ${friends.map(pk=>{let n;try{n=NT().nip19.npubEncode(pk);}catch(_){n=pk;} return 'nostr:'+n;}).join(' ')} you're seated! Check your DMs for your hole cards.`;
      try{ await PC.publish(1, body+`\n\n#holdem #poker #nostr #gamestr`, tags); toast('dealing… 🃏'); setTimeout(()=>{ if(PC.VIEW==='holdem') render(); }, 4500); }
      catch(e){ toast('could not start'); }
    }
    async function _load(){
      const list=$('#hm-games'); if(!list) return;
      const botPk=safePk(PC.CFG.holdem_bot_npub);
      if(!botPk){ list.innerHTML='<div class="empty">No bot configured.</div>'; return; }
      let evs=[]; try{ evs=await Relay.query([{ authors:[botPk], kinds:[30078], limit:500 }]); }catch(_){}
      const hidden=_hidden(), byGame={};
      for(const e of evs){
        const d=((e.tags.find(t=>t[0]==='d')||[])[1])||'';
        if(!d.startsWith('pcai:holdem:') || d.indexOf('player:')>=0) continue;
        let s; try{ s=JSON.parse(e.content||'{}'); }catch(_){ continue; }
        if(!s || !Array.isArray(s.seats) || !s.seats.includes(PC.ME.pubkey)) continue;
        if(Array.isArray(s.left) && s.left.includes(PC.ME.pubkey)) continue;  // you left this table — don't resurrect it
        const gid=s.root||d.slice('pcai:holdem:'.length);
        if(hidden.has(gid)) continue;
        if(!byGame[gid] || (e.created_at||0) > byGame[gid]._t){ s._t=e.created_at||0; byGame[gid]=s; }
      }
      const games=Object.values(byGame).sort((a,b)=>(a.status==='betting'?0:1)-(b.status==='betting'?0:1)||(b._t||0)-(a._t||0));
      // decrypt MY hole cards for each game (cached by ciphertext)
      for(const g of games){
        const ct=(g.hole_enc||{})[PC.ME.pubkey];
        if(ct && g.bot_pub){
          if(_holeCache[ct]) g._myhole=_holeCache[ct];
          else { try{ const pt=await PC.nip44dec(g.bot_pub, ct); g._myhole=JSON.parse(pt); _holeCache[ct]=g._myhole; }catch(_){ g._myhole=null; } }
        }
      }
      // load profiles for every seat so names render as nip05/display name, not raw npubs
      const allpks=new Set(); games.forEach(g=>(g.seats||[]).forEach(pk=>allpks.add(pk)));
      await Promise.all([...allpks].map(pk=>ensureProfile(pk).catch(()=>{})));
      if(!games.length){ list.innerHTML='<div class="empty">No tables yet. Seat some friends above.</div>'; return; }
      list.innerHTML = games.map((g,i)=>`<div class="chess-game-card glass" data-gi="${i}"></div>`).join('');
      games.forEach((g,i)=>_card(g, $(`.chess-game-card[data-gi="${i}"]`, list)));
    }
    function _card(g, card){
      if(!card) return;
      const me=PC.ME.pubkey, over=g.status!=='betting';
      const seats=g.seats||[], names=g.names||{}, stacks=g.stacks||{}, sbet=g.street_bet||{}, contrib=g.contrib||{};
      const folded=new Set(g.folded||[]), allin=new Set(g.allin||[]), winners=g.winners||{};
      const pot=Object.values(contrib).reduce((a,b)=>a+(+b||0),0);
      const board=(g.board||[]);
      const myTurn = !over && g.to_act===me;
      const call = Math.max(0, (g.to_call||0)-(sbet[me]||0));
      let banner='';
      if(over && Object.keys(winners).length){
        const iWon=(winners[me]||0)>0;
        banner=`<div class="chess-result ${iWon?'win':'loss'}">${iWon?'🏆 You won '+winners[me]:'Hand over'}<span class="muted small"> · ${enc(g.result||'')}</span></div>`;
      }
      const street = board.length>=5?'RIVER':board.length===4?'TURN':board.length===3?'FLOP':'PRE-FLOP';
      // undealt board slots show an empty placeholder (not "?", which reads like a hidden card)
      const boardRow = `<div class="pk-street">${street}</div><div class="pk-board">${[0,1,2,3,4].map(i=> i<board.length?cardHtml(board[i]):'<span class="pk-card empty"></span>').join('')}</div>`;
      const seated = seats.includes(me);
      const myChips = seated ? `<div class="pk-mychips">💰 Your chips <b>${stacks[me]||0}</b>${!over&&call>0?` · to call <b>${call}</b>`:''}${over&&winners[me]?` · won <b style="color:#5dffb0">+${winners[me]}</b>`:''}</div>` : '';
      const seatRows = seats.map(pk=>{
        const mine=pk===me, isTurn=!over&&g.to_act===pk, won=winners[pk]||0;
        const status = folded.has(pk)?'folded':(allin.has(pk)?'ALL-IN':(sbet[pk]?('bet '+sbet[pk]):''));
        const btn = seats.indexOf(pk)===g.button?' 🔘':'';
        return `<div class="pk-seat${mine?' me':''}${isTurn?' turn':''}${folded.has(pk)?' out':''}">
          <span class="pk-nm">${mine?'You':enc(nameOf(pk, names[pk]))}${btn}</span>
          <span class="pk-stk">${stacks[pk]||0}${won?` <b style="color:#ffd25a">+${won}</b>`:''} <span class="muted small">${enc(status)}</span></span>
          ${over&&!folded.has(pk)&&Array.isArray((g.hole||{})[pk])?`<span class="pk-hole">${g.hole[pk].map(c=>cardHtml(c)).join('')}</span>`:''}
        </div>`;
      }).join('');
      const myHole = g._myhole ? `<div class="pk-myhand"><span class="muted small">Your hand</span> ${g._myhole.map(c=>cardHtml(c,true)).join('')}</div>` : '';
      let controls='';
      if(myTurn){
        controls = `<div class="pk-controls">
          <button class="btn small pk-fold">Fold</button>
          ${call===0?`<button class="btn btn-cyan small pk-check">Check</button>`:`<button class="btn btn-cyan small pk-call">Call ${call}</button>`}
          <span class="pk-raise"><input class="input pk-amt" type="number" inputmode="numeric" placeholder="${(g.to_call||0)+(g.min_raise||g.bb||10)}" style="width:5em"><button class="btn btn-neon small pk-raisebtn">Raise</button></span>
          <button class="btn btn-magenta small pk-allin">All-in</button>
        </div>`;
      } else if(!over){ controls = `<div class="muted small" style="padding:6px 2px">Waiting on ${enc(g.to_act===me?'you':nameOf(g.to_act, names[g.to_act]))}…</div>`; }
      const lr = g.last_result;
      const lastBanner = (lr && lr.summary) ? `<div class="pk-last${(lr.winners&&lr.winners[me])?' win':''}">${(lr.winners&&lr.winners[me])?`🏆 You won ${lr.winners[me]} last hand!`:'Last hand'}<span class="muted small"> · ${enc(lr.summary)}</span></div>` : '';
      card.innerHTML = `<div class="chess-card-hd">
          <div class="cc-meta"><b>Hold'em · ${seats.length} seats</b><span class="muted small">blinds ${g.sb}/${g.bb} · hand #${g.hand_no||1}</span></div>
          <span class="cc-badge ${myTurn?'you':(over?'done':'wait')}">${myTurn?'Your move':(over?'hand over':'in play')}</span>
          <button class="chess-quit" title="Leave table">✕</button></div>
        ${banner}
        ${lastBanner}
        <div class="pk-felt">
          <div class="pk-pot">POT <b>${pot}</b>${call>0&&myTurn?` · to call ${call}`:''}</div>
          ${boardRow}
        </div>
        ${myChips}
        <div class="pk-seats">${seatRows}</div>
        ${myHole}
        ${controls}`;
      { const q=card.querySelector('.chess-quit'); if(q) q.onclick=(e)=>{ e.stopPropagation(); leaveTable(g); }; }
      if(myTurn){
        let busy=false; const lock=()=>{ busy=true; card.querySelectorAll('.pk-controls button').forEach(b=>b.disabled=true); };
        const bind=(sel,act,amtFn)=>{ const b=card.querySelector(sel); if(b) b.onclick=()=>{ if(busy)return; const amt=amtFn?amtFn():null; if(amtFn&&!amt){ toast('enter a raise amount'); return; } lock(); move(g, act, amt); }; };
        bind('.pk-fold','fold'); bind('.pk-check','check'); bind('.pk-call','call'); bind('.pk-allin','allin');
        bind('.pk-raisebtn','raise', ()=>{ const v=parseInt((card.querySelector('.pk-amt')||{}).value,10); return v>0?v:0; });
      }
    }
    async function leaveTable(g){
      if(!confirm('Leave this table? The hand continues with the others.')) return;
      try{ await move(g,'leave'); }catch(_){}
      _hide(g.root); _load();
    }
    async function move(game, action, amount){
      const botPk=safePk(PC.CFG.holdem_bot_npub); if(!botPk){ toast('no bot'); return; }
      const txt = action==='raise' ? `raise ${amount}` : action;
      try{ await sendDm(botPk, `${txt}\n\ng:${game.root}`); toast(action+' sent 🃏'); }
      catch(e){ toast('move failed'); return; }
      setTimeout(()=>{ if(PC.VIEW==='holdem') render(); }, 4000);
    }

    (window.PCGames = window.PCGames || {}).holdem = render;
  }
  init();
})();
