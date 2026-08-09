/* #tictactoe — Tic-Tac-Toe game UI for the Nostr client. Separate file (no app.js bloat); uses the
 * shared window.__PC surface and registers itself in window.PCGames so app.js dispatches it. */
(function(){
  function init(){
    const PC = window.__PC;
    if(!PC){ return setTimeout(init, 50); }
    const { $, $$, enc, publish, sendDm, safePk, nip05Resolve, profOf, needProfile, niceNip05, LOGO, toast, ensureProfile, NT } = PC;
    const Relay = window.Relay, Store = window.Store;
    let _timer = null;

    function _hidden(){ try{ return new Set(JSON.parse(localStorage.getItem('pc_ttt_hidden')||'[]')); }catch(_){ return new Set(); } }
    function _hide(gid){ const s=_hidden(); s.add(gid); try{ localStorage.setItem('pc_ttt_hidden', JSON.stringify([...s])); }catch(_){} }

    async function render(){
      const feed=$('#feed');
      const botNpub = PC.CFG.ttt_bot_npub;
      const invite = botNpub ? `
        <div class="chess-invite">
          <label class="chess-lbl">⚔️ Challenge a player</label>
          <input id="ttt-inv" class="input" placeholder="search name / npub / name@domain…" autocomplete="off">
          <div id="ttt-inv-res" class="chess-inv-res"></div>
          <div class="chess-or">— or —</div>
          <button class="btn btn-cyan" id="ttt-play-bot">🤖 Play the bot</button>
        </div>` : `<div class="empty">No #tictactoe bot is configured on this server yet — ask the admin to enable one in Admin → Bots.</div>`;
      feed.innerHTML = `<div class="chess-hub">
          <div class="chess-splash glass">
            <h2>⭕ Tic-Tac-Toe</h2>
            <p class="muted">Play over Nostr — the bot is the board &amp; referee. The bot <b>DMs each of you the board privately</b>; moves are private, the result is public. Move here on this tab, or reply to the bot's DM with a cell number 1-9. Or post <code>tictactoe @friend</code> (or just <code>tictactoe</code>) tagging the bot.</p>
            ${invite}
          </div>
          <div class="chess-games"><h3>⭕ Your games</h3><div id="ttt-games"><div class="spinner"></div></div></div>
        </div>`;
      if(botNpub){ _bindInvite(); const pb=$('#ttt-play-bot'); if(pb) pb.onclick=startBot; }
      _load();
      clearInterval(_timer);
      _timer = setInterval(()=>{ if(PC.VIEW==='ttt'){ _load(); } else clearInterval(_timer); }, 12000);
    }
    function _bindInvite(){
      const inp=$('#ttt-inv'), res=$('#ttt-inv-res'); if(!inp) return; let t=null;
      const draw=rows=>{ res.innerHTML = rows.length ? rows.map(p=>{ const m=p.meta||{};
        return `<div class="chess-inv-row"><img src="${enc(m.picture||LOGO)}" onerror="this.src='${LOGO}'">
          <div class="ci-meta"><b>${enc(m.name||m.display_name||'anon')}</b><span class="muted small">${enc(niceNip05(m.nip05)||'')}</span></div>
          <button class="btn btn-neon small" data-ch="${p.pubkey}">Challenge</button></div>`; }).join('')
        : '<div class="muted small" style="padding:6px 2px">No match. Paste an npub or name@domain.</div>';
        $$('[data-ch]',res).forEach(b=> b.onclick=()=>startGame(b.dataset.ch)); };
      inp.oninput=()=>{ clearTimeout(t); const q=inp.value.trim(); if(!q){ res.innerHTML=''; return; }
        t=setTimeout(async()=>{
          const pk=safePk(q); if(pk){ await ensureProfile(pk); draw([{pubkey:pk, meta:(profOf(pk)||{})}]); return; }
          if(/^[\w.\-+]+@[\w.\-]+\.[a-z]{2,}$/i.test(q)){ const rp=await nip05Resolve(q.toLowerCase()); if(rp){ await ensureProfile(rp); draw([{pubkey:rp, meta:(profOf(rp)||{})}]); return; } }
          const ql=q.toLowerCase();
          draw(Store.profileList().filter(p=>(((p.meta.name||'')+(p.meta.display_name||'')+(p.meta.nip05||'')).toLowerCase().includes(ql))).slice(0,8));
        }, 250); };
    }
    async function startBot(){
      const botPk=safePk(PC.CFG.ttt_bot_npub); if(!botPk){ toast('no bot'); return; }
      try{ await publish(1, `🤖 start tictactoe vs the bot — I'll be X. The bot will DM me the board; I'll play my moves privately.\n\n#tictactoe #nostr #gamestr`, [['p',botPk],['t','tictactoe'],['t','nostr'],['t','gamestr']]);
        toast('starting vs the bot ⭕'); setTimeout(()=>{ if(PC.VIEW==='ttt') render(); }, 4500);
      }catch(e){ toast('could not start'); }
    }
    async function startGame(pk){
      const botPk=safePk(PC.CFG.ttt_bot_npub); if(!botPk){ toast('no bot'); return; }
      if(pk===PC.ME.pubkey){ toast("you can't challenge yourself"); return; }
      let npub; try{ npub=NT().nip19.npubEncode(pk); }catch(_){ npub=pk; }
      const meName=(profOf(PC.ME.pubkey)||{}).name||'A player';
      try{ await publish(1, `⭕ ${meName} challenged you to Tic-Tac-Toe! nostr:${npub}\nYou're X — the bot will DM you the board to make the first move. Play your moves privately in DMs (or on the Tic-Tac-Toe tab) with a cell number 1-9. The bot referees and posts the result publicly.\n\n#tictactoe #nostr #gamestr`, [['p',botPk],['p',pk],['t','tictactoe'],['t','nostr'],['t','gamestr']]);
        toast('challenge sent ⭕ — the bot will DM the board'); setTimeout(()=>{ if(PC.VIEW==='ttt') render(); }, 4500);
      }catch(e){ toast('challenge failed'); }
    }
    async function _load(){
      const list=$('#ttt-games'); if(!list) return;
      const botPk=safePk(PC.CFG.ttt_bot_npub);
      if(!botPk){ list.innerHTML='<div class="empty">No bot configured.</div>'; return; }
      let evs=[]; try{ evs=await Relay.query([{ authors:[botPk], kinds:[30078], limit:500 }]); }catch(_){}
      const hidden=_hidden(), byGame={};
      for(const e of evs){
        const d=((e.tags.find(t=>t[0]==='d')||[])[1])||'';
        if(!d.startsWith('pcai:ttt:')) continue;
        let s; try{ s=JSON.parse(e.content||'{}'); }catch(_){ continue; }
        if(!s || (s.x!==PC.ME.pubkey && s.o!==PC.ME.pubkey)) continue;
        const gid=s.root||d.slice('pcai:ttt:'.length); s._gid=gid;   // remember the EXACT key so removal matches (solo/rootless games have no s.root)
        if(hidden.has(gid)) continue;
        if(!byGame[gid] || (e.created_at||0) > byGame[gid]._t){ s._t=e.created_at||0; byGame[gid]=s; }
      }
      // Show in-progress games AND recently-finished ones (last 2 days) so the win/loss/draw banner is
      // actually visible — otherwise a finished game just vanished and you never saw who won. The ✕ button
      // dismisses a finished game; old ones age out on their own.
      const _recent = Math.floor(Date.now()/1000) - 2*86400;
      const games=Object.values(byGame).filter(g=> g.status==='active' || (g._t||0) > _recent).sort((a,b)=>(a.status==='active'?0:1)-(b.status==='active'?0:1)||(b._t||0)-(a._t||0));
      if(!games.length){ list.innerHTML='<div class="empty">No games yet. Challenge someone or play the bot.</div>'; return; }
      list.innerHTML = games.map((g,i)=>`<div class="chess-game-card glass" data-gi="${i}"></div>`).join('');
      games.forEach((g,i)=>_card(g, $(`.chess-game-card[data-gi="${i}"]`, list)));
    }
    async function quitGame(g){
      const active=g.status==='active';
      if(!await PC.uiConfirm(active?'Resign and remove this game?':'Remove this game?',
                             { ok: active?'Resign':'Remove', danger: true })) return;
      if(active){ try{ await move(g,'resign'); }catch(_){} }
      _hide(g._gid||g.root); _load();
    }
    function _boardHtml(cells){
      let h='<div class="ttt-board">';
      for(let i=0;i<9;i++){ const m=cells[i]||'';
        h+=`<div class="ttt-cell${m?'':' empty'}" data-i="${i}">${m?`<span class="ttt-mark ${m==='X'?'tx':'to'}">${m}</span>`:`<span class="ttt-num">${i+1}</span>`}</div>`; }
      return h+'</div>';
    }
    function _card(g, card){
      if(!card) return;
      const iAmX = g.x===PC.ME.pubkey;
      const oppPk = iAmX ? g.o : g.x;
      needProfile(oppPk); const op=profOf(oppPk)||{};
      const oppName = op.name||op.display_name||(g[iAmX?'o_name':'x_name'])||'opponent';
      const filled=(g.cells||[]).filter(Boolean).length;
      const stm = filled%2===0 ? 'X' : 'O';
      const myTurn = g.status==='active' && ((stm==='X')===iAmX);
      let statusLine, badge, banner='';
      if(g.status!=='active'){
        const iWon = g.winner_pk && g.winner_pk===PC.ME.pubkey;
        const draw = !g.winner_pk;
        statusLine = draw ? 'Draw' : (iWon ? 'You won! 🎉' : 'You lost');
        badge = iWon ? 'you' : (draw ? 'wait' : 'done');
        banner = `<div class="chess-result ${draw?'draw':(iWon?'win':'loss')}">${draw?'🤝 Draw':(iWon?'🏆 You won!':'💀 You lost')}<span class="muted small"> · ${enc(g.result||'Game over')}</span></div>`;
      }
      else if(myTurn){ statusLine=filled===0?'Your move — tap a cell':'Your move'; badge='you'; }
      else { statusLine=`Waiting on ${enc(oppName)}`; badge='wait'; }
      card.innerHTML = `<div class="chess-card-hd">
          <img class="cc-av" src="${enc(op.picture||LOGO)}" onerror="this.src='${LOGO}'">
          <div class="cc-meta"><b>vs ${enc(oppName)}</b><span class="muted small">${enc(iAmX?'You: X (cyan)':'You: O (magenta)')}</span></div>
          <span class="cc-badge ${badge}">${enc(statusLine)}</span>
          <button class="chess-quit" title="${g.status==='active'?'Resign &amp; remove':'Remove'}">✕</button></div>
        ${banner}
        <div class="ttt-wrap">${_boardHtml(g.cells||[])}</div>`;
      { const q=card.querySelector('.chess-quit'); if(q) q.onclick=(e)=>{ e.stopPropagation(); quitGame(g); }; }
      if(myTurn){
        let busy=false;
        card.querySelectorAll('.ttt-cell.empty').forEach(c=> c.onclick=()=>{
          if(busy) return; busy=true; c.classList.add('pending');
          // flip the badge immediately so you can SEE the turn hand off — vs the bot it replies in a
          // second or two, so without this the badge looks stuck on "Your move" the whole game.
          const bdg=card.querySelector('.cc-badge'); if(bdg){ bdg.textContent="Opponent's turn…"; bdg.className='cc-badge wait'; }
          move(g, String(parseInt(c.dataset.i,10)+1));
        });
      }
    }
    async function move(game, text){
      const botPk=safePk(PC.CFG.ttt_bot_npub); if(!botPk){ toast('no bot'); return; }
      // Gameplay is PRIVATE: moves go to the bot as a NIP-17 DM. The 'g:<root>' marker (on its own
      // line so it's hidden from the visible message) tells the bot which game this move belongs to.
      try{ await sendDm(botPk, `${text.trim()}\n\ng:${game.root}`); toast('move sent ⭕'); }
      catch(e){ toast('move failed'); return; }
      setTimeout(()=>{ if(PC.VIEW==='ttt') render(); }, 4500);   // give the bot time to validate + DM back
    }

    (window.PCGames = window.PCGames || {}).ttt = render;
  }
  init();
})();
