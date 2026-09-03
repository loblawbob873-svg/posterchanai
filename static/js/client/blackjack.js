/* #blackjack — Blackjack (21) vs the bot dealer with chips, betting + a persistent table. Reliable,
 * OFF-TIMELINE control via a dedicated kind-30388 command channel (#t=blackjackcmd) — solo start + every move.
 * Reads the bot's kind-30388 table doc (dealer hole card + deck hidden; player hands open). Registers
 * in window.PCGames. Mirrors holdem.js. */
(function(){
  function init(){
    const PC = window.__PC;
    if(!PC){ return setTimeout(init, 50); }
    const { $, $$, enc, publish, safePk, nip05Resolve, profOf, niceNip05, LOGO, toast, ensureProfile, NT } = PC;
    const Relay = window.Relay, Store = window.Store;
    let _botWatch=null;
    let _timer = null, _seatPicks = [];
    const SUIT = { S:'♠', H:'♥', D:'♦', C:'♣' };

    function handVal(h){ let t=0,a=0; for(const c of (h||[])){ const r=c.slice(0,-1); if(r==='A'){t+=11;a++;} else if('TJQK'.includes(r)) t+=10; else t+=(+r||0); } while(t>21&&a){t-=10;a--;} return t; }
    function cardHtml(c, big){ if(c===null||c===undefined) return `<span class="pk-card back${big?' big':''}">?</span>`; let r=c.slice(0,-1); if(r==='T') r='10'; const s=c.slice(-1), red=(s==='H'||s==='D'); return `<span class="pk-card${red?' red':''}${big?' big':''}">${enc(r)}${enc(SUIT[s]||s)}</span>`; }
    function nameOf(pk, fb){ const m=profOf(pk)||{}; return m.name||m.display_name||niceNip05(m.nip05)||fb||'player'; }
    function _hidden(){ try{ return new Set(JSON.parse(localStorage.getItem('pc_bj_hidden')||'[]')); }catch(_){ return new Set(); } }
    function _hide(gid){ const s=_hidden(); s.add(gid); try{ localStorage.setItem('pc_bj_hidden', JSON.stringify([...s])); }catch(_){} }
    function _unhideAll(){ try{ localStorage.removeItem('pc_bj_hidden'); }catch(_){} }
    function getBet(){ const b=parseInt(localStorage.getItem('pc_bj_bet')||'25',10); return (b>0?b:25); }
    function setBetLS(b){ try{ localStorage.setItem('pc_bj_bet', String(b)); }catch(_){} }
    // graphical poker-chip bet picker: clickable chips + a live bet amount + a typed-amount input.
    function chipRowHtml(bet, max){
      const chips = [5,25,100,500].filter(c=>c<=max).map(c=>`<button class="bj-pchip v${c}" data-bet="${c}">${c}</button>`).join('')
        + (max>5?`<button class="bj-pchip vmax" data-bet="${max}">MAX</button>`:'');
      return `<div class="bj-bethdr">🪙 <span class="muted small">YOUR BET</span> <b class="bj-betnum">${bet}</b></div>
        <div class="bj-chiprow">${chips}</div>
        <input class="input bj-betinp" type="number" inputmode="numeric" min="5" max="${max}" value="${bet}" style="width:6em">`;
    }
    function _wireChips(root){
      if(!root) return;
      const inp=root.querySelector('.bj-betinp'), disp=root.querySelector('.bj-betnum');
      const set=v=>{ v=Math.max(5, parseInt(v,10)||5); if(inp) inp.value=v; if(disp) disp.textContent=v; setBetLS(v); };
      root.querySelectorAll('.bj-pchip').forEach(b=> b.onclick=()=>set(b.dataset.bet));
      if(inp) inp.oninput=()=>{ if(disp) disp.textContent=inp.value||0; };
    }

    // Reliable, off-timeline command channel — solo start + all moves (no public note, no DM encryption).
    async function _cmd(payload){
      const botPk=safePk(PC.CFG.blackjack_bot_npub); if(!botPk) throw new Error('no bot');
      const tags=[['d',`pcai:blackjack:cmd:${PC.ME.pubkey}`],['t','blackjackcmd'],['p',botPk],['nofederate','1']];
      const r = await publish(30388, JSON.stringify({...payload, ts:Date.now()}), tags);
      if(r && r.ok===false) throw new Error(r.msg||'rejected');
      return r;
    }

    async function render(){
      const feed=$('#feed');
      const botNpub = PC.CFG.blackjack_bot_npub;
      _seatPicks = [];
      const start = botNpub ? `
        <div class="chess-invite">
          <button class="btn btn-cyan" id="bj-solo" style="width:100%">🃏 New game — vs the dealer</button>
          <div class="chess-or">— or seat a table with friends —</div>
          <input id="bj-inv" class="input" placeholder="search name / npub / name@domain…" autocomplete="off">
          <div id="bj-inv-res" class="chess-inv-res"></div>
          <div id="bj-seats" class="bj-seatchips"></div>
          <button class="btn btn-neon" id="bj-deal" style="display:none">Deal table ▶</button>
        </div>` : `<div class="empty">No #blackjack bot is configured on this server yet — ask the admin to enable one in Admin → Bots.</div>`;
      feed.innerHTML = `<div class="chess-hub">
          <div class="chess-splash glass">
            <h2>🃏 Blackjack</h2>
            <p class="muted">Beat the dealer to 21 without busting. Wager chips each hand (blackjack pays 3:2); the table keeps dealing until you leave or bust out. Hit/stand here or by replying to the bot's DM. Dealer stands on 17.</p>
            ${start}
          </div>
          <div class="chess-games"><h3>🃏 Your tables <button class="btn small" id="bj-clear" style="display:none;float:right;font-size:.8em">✕ Clear all</button></h3><div id="bj-games"><div class="spinner"></div></div></div>
        </div>`;
      if(botNpub){
        const sb=$('#bj-solo'); if(sb) sb.onclick=()=>startTable([]);
        _bindInvite();
      }
      _load();
      clearInterval(_timer);
      _timer = setInterval(()=>{ if(PC.VIEW==='blackjack'){ _load(); } else clearInterval(_timer); }, 30000);
      /* The board repaints when the BOT PUBLISHES, not when a timer next comes round. The interval
       * above is only a backstop now (a missed event, a socket that dropped and came back), which is
       * why it went from 6s to 30. See PC.watchBot. */
      if(_botWatch){ _botWatch(); _botWatch=null; }
      const _bpk = safePk(PC.CFG.blackjack_bot_npub);   // botNpub is the npub; the pool filters on hex
      if(_bpk && PC.watchBot) _botWatch = PC.watchBot(_bpk, ()=>{ if(PC.VIEW==='blackjack') _load(); else { _botWatch && _botWatch(); _botWatch=null; } });
    }
    function _drawSeats(){
      const box=$('#bj-seats'), deal=$('#bj-deal'); if(!box) return;
      box.innerHTML = _seatPicks.map(pk=>{ const m=profOf(pk)||{}; return `<span class="bj-chip">${enc(m.name||m.display_name||'anon')}<button data-rm="${pk}">✕</button></span>`; }).join('');
      $$('[data-rm]',box).forEach(b=> b.onclick=()=>{ _seatPicks=_seatPicks.filter(p=>p!==b.dataset.rm); _drawSeats(); });
      if(deal){ deal.style.display=_seatPicks.length?'':'none'; deal.onclick=()=>startTable(_seatPicks.slice(), getBet()); }
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
    async function startTable(friends, bet){
      const botPk=safePk(PC.CFG.blackjack_bot_npub); if(!botPk){ toast('no bot'); return; }
      friends=(friends||[]).filter(pk=>pk&&pk!==PC.ME.pubkey&&pk!==botPk);
      if(!friends.length){
        // SOLO: private command — starts a table in the "place your bet" state (bet is IN the game).
        // Un-hide first: the bot keeps a persistent table and won't deal a brand-new one while you still
        // have a live (even just-'over') table it can resume. If we'd hidden that table locally, 'start'
        // resumes it but you'd see nothing — the button looks dead. Surfacing hidden tables guarantees
        // you always land on a playable table (a 'left' table stays filtered out by _load).
        _unhideAll();
        try{ await _cmd({action:'start'}); toast('new game… 🃏'); [2000,4500,7000].forEach(d=>setTimeout(()=>{ if(PC.VIEW==='blackjack') _load(); }, d)); }
        catch(e){ toast('could not start — try again'); }
        return;
      }
      // MULTIPLAYER: public note to seat + notify friends.
      const tags=[['p',botPk]]; friends.forEach(pk=>tags.push(['p',pk]));
      tags.push(['t','blackjack'],['t','nostr'],['t','gamestr']);
      const body = `🃏 Dealing a #blackjack table — ${friends.map(pk=>{let n;try{n=NT().nip19.npubEncode(pk);}catch(_){n=pk;} return 'nostr:'+n;}).join(' ')} you're seated! Check your DMs.`;
      try{ await publish(1, body+`\n\n#blackjack #nostr #gamestr`, tags); toast('dealing… 🃏'); setTimeout(()=>{ if(PC.VIEW==='blackjack') render(); }, 4500); }
      catch(e){ toast('could not start'); }
    }
    async function _load(){
      const list=$('#bj-games'); if(!list) return;
      const botPk=safePk(PC.CFG.blackjack_bot_npub);
      if(!botPk){ list.innerHTML='<div class="empty">No bot configured.</div>'; return; }
      let evs=[]; try{ evs=await Relay.query([{ authors:[botPk], kinds:[30388], limit:500 }]); }catch(_){}
      const hidden=_hidden(), byGame={};
      for(const e of evs){
        const d=((e.tags.find(t=>t[0]==='d')||[])[1])||'';
        if(!d.startsWith('pcai:blackjack:') || d.indexOf('player:')>=0 || d.indexOf('cmd:')>=0) continue;
        let s; try{ s=JSON.parse(e.content||'{}'); }catch(_){ continue; }
        if(!s || !Array.isArray(s.seats) || !s.seats.includes(PC.ME.pubkey)) continue;
        if(Array.isArray(s.left) && s.left.includes(PC.ME.pubkey)) continue;
        const gid=s.root||d.slice('pcai:blackjack:'.length); s._gid=gid;   // remember the EXACT key so removal matches (solo/rootless games have no s.root)
        if(hidden.has(gid)) continue;
        if(!byGame[gid] || (e.created_at||0) > byGame[gid]._t){ s._t=e.created_at||0; byGame[gid]=s; }
      }
      const games=Object.values(byGame).filter(g=>['betting','playing','over'].includes(g.status))/* show only in-progress games; finished/left/resigned drop out (left holdem/bj tables also caught by s.left above) */.sort((a,b)=>(a.status==='playing'?0:1)-(b.status==='playing'?0:1)||(b._t||0)-(a._t||0));
      const allpks=new Set(); games.forEach(g=>(g.seats||[]).forEach(pk=>allpks.add(pk)));
      await Promise.all([...allpks].map(pk=>ensureProfile(pk).catch(()=>{})));
      const clearBtn=$('#bj-clear');
      if(clearBtn){ clearBtn.style.display = games.length>1 ? '' : 'none'; clearBtn.onclick=()=>clearAll(games); }
      if(!games.length){ list.innerHTML='<div class="empty">No tables yet. Deal one above.</div>'; return; }
      list.innerHTML = games.map((g,i)=>`<div class="chess-game-card glass" data-gi="${i}"></div>`).join('');
      games.forEach((g,i)=>_card(g, $(`.chess-game-card[data-gi="${i}"]`, list)));
    }
    function _card(g, card){
      if(!card) return;
      const me=PC.ME.pubkey, bot=g.bot, over=g.status!=='playing', betting=g.status==='betting';
      const seats=g.seats||[], names=g.names||{}, hands=g.hands||{}, stacks=g.stacks||{}, bet=g.bet||{}, done=g.done||{}, results=g.results||{}, payouts=g.payouts||{};
      const players=seats.filter(p=>p!==bot);
      const myHand=hands[me]||[], myVal=handVal(myHand), myDone=!!done[me], myStack=stacks[me]||0, myBet=bet[me]||0;
      // dealer: full hand at showdown, else up card + face-down placeholders
      const dh = g.dhand || (g.dealer_up ? [g.dealer_up].concat(Array(g.dealer_down||1).fill(null)) : []);
      const dealerCards = (dh.length?dh:[null]).map((c,i)=> (over && g.dhand) ? cardHtml(c) : (i===0?cardHtml(c):cardHtml(null))).join('');
      const dealerVal = (over && g.dhand) ? handVal(g.dhand) : '';
      const myOut=results[me], myNet=payouts[me]||0;
      const lr=g.last_result;
      const lastBanner = (lr && lr.summary && !over) ? `<div class="pk-last${(lr.payouts&&lr.payouts[me]>0)?' win':''}">${(lr.payouts&&lr.payouts[me]>0)?`🏆 You won ${lr.payouts[me]} last round!`:((lr.payouts&&lr.payouts[me]<0)?`Last round: lost ${-lr.payouts[me]}`:'Last round')}<span class="muted small"> · ${enc(lr.summary)}</span></div>` : '';
      let banner='';
      if(over && myOut){ const win=myNet>0, push=myOut==='push'; banner=`<div class="chess-result ${push?'draw':(win?'win':'loss')}">${push?'🤝 Push':(win?`🏆 You won ${myNet}!`:`💀 You lost ${-myNet}`)}<span class="muted small"> · ${myStack>0?'place your bet to deal again':'out of chips'}</span></div>`; }
      const dealerBlock = `<div class="pk-felt"><div class="pk-street">DEALER${dealerVal!==''?' · '+dealerVal+(dealerVal>21?' BUST':''):''}</div><div class="pk-board">${dealerCards}</div></div>`;
      // Live play-by-play for THIS round (who hit/stood/busted + how the dealer played) — same recap the
      // to-act player gets in their DM, so everyone watching knows what's happening. Server tags by round.
      const _recap = (g.log||[]).filter(e=>e&&e.r===(g.round_no||0)&&e.t).slice(-6);
      const logRows = (_recap.length && !betting) ? `<div class="pk-log">${_recap.map(e=>`<span class="pk-logln">${enc(e.t)}</span>`).join('')}</div>` : '';
      const seatRows = players.filter(pk=>pk!==me).map(pk=>{
        const h=hands[pk]||[], v=handVal(h), av=(profOf(pk)||{}).picture||LOGO, out=results[pk], net=payouts[pk]||0;
        const status=(g.left||[]).includes(pk)?'left':(done[pk]?(out?out.toUpperCase()+(net?` ${net>0?'+':''}${net}`:''):'stand'):'…');
        return `<div class="pk-seat${out==='win'||out==='blackjack'?' win':''}"><span class="pk-who"><img class="pk-av" src="${enc(av)}" onerror="this.onerror=null;this.src='${LOGO}'"><span class="pk-nm">${enc(nameOf(pk,names[pk]))}</span></span>
          <span class="pk-stk"><span class="bj-cards">${h.map(c=>cardHtml(c)).join('')}</span> <b>${v}</b> <span class="muted small">${enc(status)}</span> · ${stacks[pk]||0}c</span></div>`;
      }).join('');
      const myAv=(profOf(me)||{}).picture||LOGO;
      const myHandCard = `<div class="pk-myhand">
          <div class="pk-myinfo"><img class="pk-myav" src="${enc(myAv)}" onerror="this.onerror=null;this.src='${LOGO}'"><div class="pk-mymeta"><span class="pk-myname">You</span><span class="pk-mychipline">💰 <b>${myStack}</b> chips · bet ${myBet}</span></div></div>
          <div class="pk-mycards">${myHand.map(c=>cardHtml(c,true)).join('')||'<span class="muted small">…</span>'}</div>
          ${myHand.length?`<div class="bj-myval ${myVal>21?'bust':(myVal===21?'win':'')}">${myVal}${myVal>21?' · BUST':(myVal===21?' · 21!':'')}</div>`:''}
        </div>`;
      let controls='';
      if(!over && !myDone){
        controls = `<div class="pk-controls"><button class="btn btn-cyan small bj-hit">Hit</button><button class="btn btn-neon small bj-stand">Stand</button><button class="btn btn-red small bj-leave">Leave</button></div>`;
      } else if(!over && myDone){
        controls = `<div class="pk-controls" style="align-items:center"><span class="muted small" style="flex:1;padding:6px 2px">✋ Locked in — the dealer plays when the table's done.</span><button class="btn btn-red small bj-leave">Leave</button></div>`;
      } else {
        const nb = myStack>0 ? Math.min(myBet||getBet(), myStack) : 0;
        controls = myStack>0
          ? `<div class="bj-betbar">${chipRowHtml(nb, myStack)}<div class="pk-controls"><button class="btn btn-cyan bj-deal" style="flex:3 1 60%">${betting?'Deal hand ▶':'Deal next hand ▶'}</button><button class="btn btn-red small bj-leave" style="flex:1 1 28%">Leave</button></div></div>`
          : `<div class="muted small" style="padding:8px 2px">💀 You're out of chips. <button class="btn btn-red small bj-leave" style="margin-left:6px">Leave</button></div>`;
      }
      card.innerHTML = `<div class="chess-card-hd">
          <div class="cc-meta"><b>Blackjack ${players.length>1?`· ${players.length} seats`:'vs dealer'}</b><span class="muted small">round #${g.round_no||1} · dealer stands on 17</span></div>
          <span class="cc-badge ${(!over&&!myDone)?'you':(over?(myStack>0?'you':'done'):'wait')}">${(!over&&!myDone)?'Your move':(over?(myStack>0?'place your bet':'busted out'):'in play')}</span>
          <button class="chess-quit" title="Leave table">✕</button></div>
        ${banner}${lastBanner}
        ${betting?'':dealerBlock}
        ${logRows}
        ${seatRows?`<div class="pk-seats">${seatRows}</div>`:''}
        ${betting?'':myHandCard}
        ${controls}`;
      { const q=card.querySelector('.chess-quit'); if(q) q.onclick=(e)=>{ e.stopPropagation(); leaveTable(g); }; }
      const bind=(sel,fn)=>{ const b=card.querySelector(sel); if(b) b.onclick=fn; };
      bind('.bj-hit', ()=>move(g,'hit'));
      bind('.bj-stand', ()=>move(g,'stand'));
      bind('.bj-leave', ()=>leaveTable(g));
      bind('.bj-deal', ()=>{ const v=parseInt((card.querySelector('.bj-betinp')||{}).value,10)||getBet(); dealNext(g, v); });
      _wireChips(card);
    }
    async function move(game, action){
      let ok=false;
      for(let i=0;i<3 && !ok;i++){ try{ await _cmd({action, gameid:game.root}); ok=true; }catch(e){ await new Promise(r=>setTimeout(r, 400*(i+1))); } }
      if(!ok){ toast('move failed — tap again'); return; }
      toast(action+' sent 🃏');
      [2000, 4500, 7000].forEach(d=>setTimeout(()=>{ if(PC.VIEW==='blackjack') _load(); }, d));
    }
    async function dealNext(game, bet){
      // place this hand's bet + deal the next round (the persistent table waits between rounds).
      let ok=false;
      for(let i=0;i<3 && !ok;i++){ try{ await _cmd({action:'deal', gameid:game.root, bet}); ok=true; }catch(e){ await new Promise(r=>setTimeout(r, 400*(i+1))); } }
      if(!ok){ toast('could not deal — tap again'); return; }
      setBetLS(bet); toast('dealing… 🃏');
      [2000, 4500, 7000].forEach(d=>setTimeout(()=>{ if(PC.VIEW==='blackjack') _load(); }, d));
    }
    async function leaveTable(g){
      if(!await PC.uiConfirm('Leave this table? You keep your chips.', { ok: 'Leave' })) return;
      // Hide it locally FIRST so it disappears from your list immediately — even if the bot is slow or the
      // leave command doesn't land. (The command channel is a single replaceable event per user, so a rapid
      // second leave can clobber the first server-side; the local hide is what reliably clears the UI.)
      _hide(g._gid||g.root);
      const gid=g.root||g._gid;
      for(let i=0;i<3;i++){ try{ await _cmd({action:'leave', gameid:gid}); break; }catch(_){ await new Promise(r=>setTimeout(r, 400*(i+1))); } }
      toast('left the table 👋');
      _load();
    }
    async function clearAll(games){
      if(!await PC.uiConfirm(`Clear all ${games.length} tables from your list? You keep your chips.`,
                             { ok: 'Clear all', danger: true })) return;
      // The command channel is ONE replaceable event per user, so leaves fired back-to-back overwrite
      // each other before the bot (10s poll) reads them — only the last would register, and the rest
      // would resurface next time you start a game. Space them out so every LEAVE actually lands.
      for(let i=0;i<games.length;i++){
        const g=games[i]; _hide(g._gid||g.root);
        try{ await _cmd({action:'leave', gameid:g.root||g._gid}); }catch(_){}
        _load();
        if(i<games.length-1){ toast(`clearing ${i+1}/${games.length}…`); await new Promise(r=>setTimeout(r, 11000)); }
      }
      toast('cleared 🧹'); _load();
    }

    (window.PCGames = window.PCGames || {}).blackjack = render;
  }
  init();
})();
