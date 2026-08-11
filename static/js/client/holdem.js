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
    let _botWatch=null;
    let _timer = null, _seatPicks = [], _holeCache = {}, _announced = {}, _raiseDraft = {};

    // Big, deliberate win/loss announcement that holds for a few seconds so the result of a hand is
    // unmissable before the bot deals the next one (which otherwise resets the board almost instantly).
    function showHandResult(g, lr){
      const me = PC.ME.pubkey, won = (lr.winners && lr.winners[me]) || 0;
      const prev = document.getElementById('pk-bigresult'); if(prev) prev.remove();
      const el = document.createElement('div');
      el.id = 'pk-bigresult'; el.className = 'pk-bigresult ' + (won>0?'win':'loss');
      el.innerHTML = `<div class="pk-br-card">
        <div class="pk-br-emoji">${won>0?'🏆':'🪦'}</div>
        <div class="pk-br-title">${won>0?'YOU WON':'YOU LOST'}</div>
        ${won>0?`<div class="pk-br-amt">+${won} chips</div>`:''}
        <div class="pk-br-sum">${enc(lr.summary||'')}${lr.hand_no?` · hand #${lr.hand_no}`:''}</div>
        <div class="pk-br-tap">tap to continue</div></div>`;
      document.body.appendChild(el);
      let done=false; const close=()=>{ if(done)return; done=true; el.classList.add('out'); setTimeout(()=>el.remove(),300); };
      el.onclick=close; setTimeout(close, 5200);
    }

    const RANKS='23456789TJQKA', SUITS=['♠','♥','♦','♣'];
    function cardHtml(c, big){
      if(c===null||c===undefined) return `<span class="pk-card back${big?' big':''}">?</span>`;
      let r=RANKS[c%13]; if(r==='T') r='10'; const si=Math.floor(c/13), s=SUITS[si], red=(si===1||si===2);
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
      _timer = setInterval(()=>{ if(PC.VIEW==='holdem'){ _load(); } else clearInterval(_timer); }, 30000);
      /* The board repaints when the BOT PUBLISHES, not when a timer next comes round. The interval
       * above is only a backstop now (a missed event, a socket that dropped and came back), which is
       * why it went from 6s to 30. See PC.watchBot. */
      if(_botWatch){ _botWatch(); _botWatch=null; }
      const _bpk = safePk(PC.CFG.holdem_bot_npub);   // botNpub is the npub; the pool filters on hex
      if(_bpk && PC.watchBot) _botWatch = PC.watchBot(_bpk, ()=>{ if(PC.VIEW==='holdem') _load(); else { _botWatch && _botWatch(); _botWatch=null; } });
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
    // Reliable, OFF-TIMELINE command channel: a signed kind-30078 (no nip44 encryption to fail, not a
    // kind-1 note) published to the local relay. The bot polls #t=holdemcmd. Used for solo start + all
    // moves so nothing hits your public timeline and moves don't depend on flaky DM encryption.
    async function _cmd(payload){
      const botPk=safePk(PC.CFG.holdem_bot_npub); if(!botPk) throw new Error('no bot');
      const tags=[['d',`pcai:holdem:cmd:${PC.ME.pubkey}`],['t','holdemcmd'],['p',botPk],['nofederate','1']];
      const r = await PC.publish(30078, JSON.stringify({...payload, ts:Date.now()}), tags);
      if(r && r.ok===false) throw new Error(r.msg||'rejected');
      return r;
    }
    async function startTable(friends){
      const botPk=safePk(PC.CFG.holdem_bot_npub); if(!botPk){ toast('no bot'); return; }
      friends=(friends||[]).filter(pk=>pk&&pk!==PC.ME.pubkey&&pk!==botPk);
      if(!friends.length){
        // SOLO: private command — no public timeline post at all, and reliable (no encryption).
        try{ await _cmd({action:'start'}); toast('dealing… 🃏'); setTimeout(()=>{ if(PC.VIEW==='holdem') _load(); }, 4500); }
        catch(e){ toast('could not start — try again'); }
        return;
      }
      // MULTIPLAYER: a public note to seat + notify the friends you tagged (they need to see it).
      const tags=[['p',botPk]]; friends.forEach(pk=>tags.push(['p',pk]));
      tags.push(['t','holdem'],['t','poker'],['t','nostr'],['t','gamestr']);
      const body = `🃏 Dealing a #holdem table — ${friends.map(pk=>{let n;try{n=NT().nip19.npubEncode(pk);}catch(_){n=pk;} return 'nostr:'+n;}).join(' ')} you're seated! Check your DMs for your hole cards.`;
      try{ await PC.publish(1, body+`\n\n#holdem #poker #nostr #gamestr`, tags); toast('dealing… 🃏'); setTimeout(()=>{ if(PC.VIEW==='holdem') render(); }, 4500); }
      catch(e){ toast('could not start'); }
    }
    async function _load(){
      const list=$('#hm-games'); if(!list) return;
      // Don't yank the table out from under a raise the user is mid-typing: a full innerHTML rebuild
      // wipes the bet field. While that field is focused it's their turn (everyone's waiting on them),
      // so no external state can change — safe to skip this refresh tick entirely.
      const _ae=document.activeElement;
      if(_ae && _ae.classList && _ae.classList.contains('pk-amt')) return;
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
        const gid=s.root||d.slice('pcai:holdem:'.length); s._gid=gid;   // remember the EXACT key so removal matches (solo/rootless games have no s.root)
        if(hidden.has(gid)) continue;
        if(!byGame[gid] || (e.created_at||0) > byGame[gid]._t){ s._t=e.created_at||0; byGame[gid]=s; }
      }
      const games=Object.values(byGame).filter(g=>['betting'].includes(g.status))/* show only in-progress games; finished/left/resigned drop out (left holdem/bj tables also caught by s.left above) */.sort((a,b)=>(a.status==='betting'?0:1)-(b.status==='betting'?0:1)||(b._t||0)-(a._t||0));
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
      const seated = seats.includes(me), myAv=(profOf(me)||{}).picture||LOGO;
      // YOU are shown in the dedicated "Your hand" card below; the seat list is the dealer + opponents
      // only (otherwise your avatar appears twice — sandwiched between the table and the dealer).
      const seatRows = seats.filter(pk=>pk!==me).map(pk=>{
        const mine=pk===me, isTurn=!over&&g.to_act===pk, won=winners[pk]||0;
        const status = folded.has(pk)?'folded':(allin.has(pk)?'ALL-IN':(sbet[pk]?('bet '+sbet[pk]):''));
        const btn = seats.indexOf(pk)===g.button?' 🔘':'';
        const av=(profOf(pk)||{}).picture||LOGO;
        return `<div class="pk-seat${mine?' me':''}${isTurn?' turn':''}${folded.has(pk)?' out':''}">
          <span class="pk-who"><img class="pk-av" src="${enc(av)}" onerror="this.onerror=null;this.src='${LOGO}'"><span class="pk-nm">${mine?'You':enc(nameOf(pk, names[pk]))}${btn}</span></span>
          <span class="pk-stk">${stacks[pk]||0}${won?` <b style="color:#ffd25a">+${won}</b>`:''} <span class="muted small">${enc(status)}</span></span>
          ${over&&!folded.has(pk)&&Array.isArray((g.hole||{})[pk])?`<span class="pk-hole">${g.hole[pk].map(c=>cardHtml(c)).join('')}</span>`:''}
        </div>`;
      }).join('');
      const myHand = seated ? `<div class="pk-myhand">
          <div class="pk-myinfo">
            <img class="pk-myav" src="${enc(myAv)}" onerror="this.onerror=null;this.src='${LOGO}'">
            <div class="pk-mymeta"><span class="pk-myname">You</span>
              <span class="pk-mychipline">💰 <b>${stacks[me]||0}</b> chips${!over&&call>0?` · to call <b>${call}</b>`:''}${over&&winners[me]?` · <b style="color:#5dffb0">won +${winners[me]}</b>`:''}</span></div>
          </div>
          <div class="pk-mycards">${(g._myhole||[]).map(c=>cardHtml(c,true)).join('')||'<span class="muted small">cards in your DMs…</span>'}</div>
        </div>` : '';
      let controls='';
      if(myTurn){
        controls = `<div class="pk-controls">
          <button class="btn btn-red small pk-fold">Fold</button>
          ${call===0?`<button class="btn btn-cyan small pk-check">Check</button>`:`<button class="btn btn-cyan small pk-call">Call ${call}</button>`}
          <span class="pk-raise"><input class="input pk-amt" type="number" inputmode="numeric" value="${enc(_raiseDraft[g._gid]||'')}" placeholder="${(g.to_call||0)+(g.min_raise||g.bb||10)}" style="width:5em"><button class="btn btn-neon small pk-raisebtn">Raise</button></span>
          <button class="btn btn-magenta small pk-allin">All-in</button>
        </div>`;
      } else if(!over){ controls = `<div class="muted small" style="padding:6px 2px">Waiting on ${enc(g.to_act===me?'you':nameOf(g.to_act, names[g.to_act]))}…</div>`; }
      const lr = g.last_result;
      // Pop the big win/loss overlay the moment a hand resolves. Seed on first sight (don't pop a stale
      // result when you just opened the view) — only announce when the hand number ADVANCES afterward.
      if(lr && lr.hand_no){ const gid=g._gid, seen=_announced[gid];
        if(seen===undefined) _announced[gid]=lr.hand_no;
        else if(lr.hand_no>seen){ _announced[gid]=lr.hand_no; showHandResult(g, lr); } }
      const lhn = (lr && lr.hand_no) ? ` #${lr.hand_no}` : '';   // show which hand it was, so consecutive results don't look frozen
      const lastBanner = (lr && lr.summary) ? `<div class="pk-last${(lr.winners&&lr.winners[me])?' win':''}">${(lr.winners&&lr.winners[me])?`🏆 You won ${lr.winners[me]} — hand${lhn} done`:`Hand${lhn} done`}<span class="muted small"> · ${enc(lr.summary)}</span></div>` : '';
      // Action since MY last turn (across streets) — same recap the DM gives, so I always see what the
      // bot/others did when it comes around to me (not just the current street, which is often empty).
      const _SL={preflop:'PRE-FLOP',flop:'FLOP',turn:'TURN',river:'RIVER'};
      const _log=g.log||[]; let _li=-1; for(let i=0;i<_log.length;i++){ if(_log[i]&&_log[i].pk===me) _li=i; }
      const _rl=[]; let _cs=null;
      for(const e of _log.slice(_li+1)){ if(!e) continue; const s=e.s||''; if(_cs!==null&&s!==_cs) _rl.push('— '+(_SL[s]||s.toUpperCase())+' —'); _cs=s; if(e.t) _rl.push(e.t); }
      const _recap=_rl.slice(-8);
      const logRows = _recap.length ? `<div class="pk-log">${_recap.map(t=>`<span class="pk-logln">${enc(t)}</span>`).join('')}</div>` : '';
      card.innerHTML = `<div class="chess-card-hd">
          <div class="cc-meta"><b>Hold'em · ${seats.length} seats</b><span class="muted small">blinds ${g.sb}/${g.bb} · hand #${g.hand_no||1}</span></div>
          <span class="cc-badge ${myTurn?'you':(over?'done':'wait')}">${myTurn?'Your move':(over?'hand over':'in play')}</span>
          <button class="chess-quit" title="Leave table">✕</button></div>
        ${banner}
        ${lastBanner}
        <div class="pk-felt">
          <div class="pk-pot">POT <b>${pot}</b>${call>0&&myTurn?` · to call ${call}`:''}</div>
          ${boardRow}
          ${logRows}
        </div>
        <div class="pk-seats">${seatRows}</div>
        ${myHand}
        ${controls}`;
      { const q=card.querySelector('.chess-quit'); if(q) q.onclick=(e)=>{ e.stopPropagation(); leaveTable(g); }; }
      if(myTurn){
        let busy=false; const lock=()=>{ busy=true; card.querySelectorAll('.pk-controls button').forEach(b=>b.disabled=true); };
        const bind=(sel,act,amtFn)=>{ const b=card.querySelector(sel); if(b) b.onclick=()=>{ if(busy)return; const amt=amtFn?amtFn():null; if(amtFn&&!amt){ toast('enter a raise amount'); return; } lock(); move(g, act, amt); }; };
        bind('.pk-fold','fold'); bind('.pk-check','check'); bind('.pk-call','call'); bind('.pk-allin','allin');
        bind('.pk-raisebtn','raise', ()=>{ const v=parseInt((card.querySelector('.pk-amt')||{}).value,10); return v>0?v:0; });
        // keep the typed amount through refresh ticks (the focus guard covers active typing; this
        // covers the case where the field was typed then blurred before the next rebuild)
        { const _amt=card.querySelector('.pk-amt'); if(_amt) _amt.oninput=()=>{ _raiseDraft[g._gid]=_amt.value; }; }
      }
    }
    async function leaveTable(g){
      if(!await PC.uiConfirm('Leave this table? The hand continues with the others.', { ok: 'Leave' })) return;
      try{ await move(g,'leave'); }catch(_){}
      _hide(g._gid||g.root); _load();
    }
    async function move(game, action, amount){
      // Moves go through the reliable kind-30078 command channel (not a flaky NIP-17 DM). Retry a few
      // times in case the local relay momentarily rejects.
      let ok=false;
      for(let i=0;i<3 && !ok;i++){
        try{ await _cmd({action, gameid:game.root, amount: action==='raise'?amount:undefined}); ok=true; }
        catch(e){ await new Promise(r=>setTimeout(r, 400*(i+1))); }
      }
      if(!ok){ toast('move failed — tap again'); return; }
      if(action==='raise') delete _raiseDraft[game._gid];   // submitted — don't carry it into the next hand
      toast(action+' sent 🃏');
      // refresh the table a couple times to catch the bot's update (it polls every ~4s)
      [2500, 5000, 8000].forEach(d=>setTimeout(()=>{ if(PC.VIEW==='holdem') _load(); }, d));
    }

    (window.PCGames = window.PCGames || {}).holdem = render;
  }
  init();
})();
