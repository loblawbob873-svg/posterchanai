/* #chess — the Chess game UI for the Nostr client. Kept OUT of app.js so per-game code doesn't
 * bloat the core (future games — Tic-Tac-Toe, etc. — get their own file the same way). It uses the
 * small shared surface app.js publishes on window.__PC (helpers + live ME/CFG/VIEW getters); app.js
 * dispatches the Chess view via window.PCChess.render(). */
(function(){
  function init(){
    const PC = window.__PC;
    if(!PC){ return setTimeout(init, 50); }   // app.js not ready yet — retry
    const { $, $$, enc, publish, safePk, nip05Resolve, profOf, needProfile, niceNip05, LOGO, toast, ensureProfile, NT } = PC;
    const Relay = window.Relay, Store = window.Store;
    let _chessTimer = null;

    // Locally-hidden games (resigned/quit or just cleared from "Your games"). Persisted per browser.
    function _hiddenGames(){ try{ return new Set(JSON.parse(localStorage.getItem('pc_chess_hidden')||'[]')); }catch(_){ return new Set(); } }
    function _hideGame(gid){ const s=_hiddenGames(); s.add(gid); try{ localStorage.setItem('pc_chess_hidden', JSON.stringify([...s])); }catch(_){} }
    async function quitGame(g){
      const active = g.status==='active';
      if(!confirm(active ? 'Resign and remove this game?' : 'Remove this game from your list?')) return;
      if(active){ try{ await chessMove(g, 'resign'); }catch(_){} }   // tell the bot you resigned
      _hideGame(g.root);
      _loadMyChessGames();
    }

    async function renderChess(){
      const feed=$('#feed');
      const botNpub = PC.CFG.chess_bot_npub;
      const how = `
        <ol class="chess-how">
          <li>Invite someone below (or post <code>chess nostr:npub… </code> tagging them and the bot).</li>
          <li>The bot replies with a board; <b>your</b> pieces are <b>numbered</b>.</li>
          <li>Reply with <code>&lt;number&gt; &lt;square&gt;</code> — e.g. <code>1 d4</code>. <code>Nf3</code>, <code>e4</code>, <code>O-O</code> and <code>resign</code> work too.</li>
          <li>The bot validates every move, posts the updated board, and calls checkmate. Games never expire — play over days.</li>
        </ol>`;
      const invite = botNpub ? `
        <div class="chess-invite">
          <label class="chess-lbl">⚔️ Challenge a player</label>
          <input id="chess-inv" class="input" placeholder="search name / npub / name@domain…" autocomplete="off">
          <div id="chess-inv-res" class="chess-inv-res"></div>
          <div class="chess-or">— or —</div>
          <button class="btn btn-cyan" id="chess-play-bot">🤖 Play the bot</button>
        </div>` : `<div class="empty">No #chess bot is configured on this server yet — ask the admin to enable one in Admin → Bots.</div>`;
      feed.innerHTML = `<div class="chess-hub">
          <div class="chess-splash glass">
            <h2>♟️ #chess</h2>
            <p class="muted">Play chess with anyone on Nostr — the bot is the board &amp; referee.</p>
            ${how}
            ${invite}
          </div>
          <div class="chess-games">
            <h3>♟️ Your games</h3>
            <div id="chess-games-list"><div class="spinner"></div></div>
          </div>
        </div>`;
      if(botNpub){ _bindChessInvite(); const pb=$('#chess-play-bot'); if(pb) pb.onclick=startBotGame; }
      _loadMyChessGames();
      // poll for new games / opponent moves while the Chess view is open (turn-based, so gentle)
      clearInterval(_chessTimer);
      _chessTimer = setInterval(()=>{ if(PC.VIEW==='chess'){ if(!document.querySelector('.csq.sel')) _loadMyChessGames(); } else clearInterval(_chessTimer); }, 12000);
    }
    async function startBotGame(){
      const botPk=safePk(PC.CFG.chess_bot_npub); if(!botPk){ toast('no chess bot configured'); return; }
      try{
        await publish(1, `🤖 Playing #chess against the bot — I'll be White. Reply to the board with moves like "1 d4".\n\n#chess #nostr #gamestr`, [['p',botPk],['t','chess'],['t','nostr'],['t','gamestr']]);
        toast('starting game vs the bot ♟️');
        setTimeout(()=>{ if(PC.VIEW==='chess') renderChess(); }, 4500);
      }catch(e){ toast('could not start game'); }
    }
    function _bindChessInvite(){
      const inp=$('#chess-inv'), res=$('#chess-inv-res'); if(!inp) return; let t=null;
      const render=rows=>{ res.innerHTML = rows.length ? rows.map(p=>{
          const m=p.meta||{}; return `<div class="chess-inv-row"><img src="${enc(m.picture||LOGO)}" onerror="this.src='${LOGO}'">
            <div class="ci-meta"><b>${enc(m.name||m.display_name||'anon')}</b><span class="muted small">${enc(niceNip05(m.nip05)||'')}</span></div>
            <button class="btn btn-neon small" data-challenge="${p.pubkey}">Challenge</button></div>`; }).join('')
        : '<div class="muted small" style="padding:6px 2px">No match. Paste an npub or name@domain.</div>';
        $$('[data-challenge]',res).forEach(b=> b.onclick=()=>startChessGame(b.dataset.challenge)); };
      inp.oninput=()=>{ clearTimeout(t); const q=inp.value.trim(); if(!q){ res.innerHTML=''; return; }
        t=setTimeout(async()=>{
          const pk=safePk(q); if(pk){ await ensureProfile(pk); render([{pubkey:pk, meta:(profOf(pk)||{})}]); return; }
          if(/^[\w.\-+]+@[\w.\-]+\.[a-z]{2,}$/i.test(q)){ const rp=await nip05Resolve(q.toLowerCase()); if(rp){ await ensureProfile(rp); render([{pubkey:rp, meta:(profOf(rp)||{})}]); return; } }
          const ql=q.toLowerCase();
          render(Store.profileList().filter(p=>(((p.meta.name||'')+(p.meta.display_name||'')+(p.meta.nip05||'')).toLowerCase().includes(ql))).slice(0,8));
        }, 250); };
    }
    async function startChessGame(pk){
      const botPk=safePk(PC.CFG.chess_bot_npub); if(!botPk){ toast('no chess bot configured'); return; }
      if(pk===PC.ME.pubkey){ toast("you can't challenge yourself"); return; }
      let npub; try{ npub=NT().nip19.npubEncode(pk); }catch(_){ npub=pk; }
      const meName = (profOf(PC.ME.pubkey)||{}).name || (profOf(PC.ME.pubkey)||{}).display_name || 'A player';
      // chess_first = opponent → they're White and "accept" by making the first move.
      const tags=[['p',botPk],['p',pk],['t','chess'],['t','nostr'],['t','gamestr'],['chess_first',pk]];
      try{
        await publish(1, `♟️ ${meName} has invited you to play chess! nostr:${npub}\n`
          + `To accept, make the first move (you're White): reply to the board the bot posts with your move — e.g. "1 d4" (move piece #1 to d4). "Nf3", "e4" and "O-O" work too, or just tap a piece on the Chess tab.\n`
          + `The bot referees, validates every move, and calls checkmate. Games never expire — take your time.\n\n#chess #nostr #gamestr`, tags);
        toast('invite sent ♟️ — the bot will post the board');
        setTimeout(()=>{ if(PC.VIEW==='chess') renderChess(); }, 4500);
      }catch(e){ toast('challenge failed'); }
    }
    async function _loadMyChessGames(){
      const list=$('#chess-games-list'); if(!list) return;
      const botPk=safePk(PC.CFG.chess_bot_npub);
      if(!botPk){ list.innerHTML='<div class="empty">No chess bot configured.</div>'; return; }
      let evs=[]; try{ evs=await Relay.query([{ authors:[botPk], kinds:[30078], limit:500 }]); }catch(_){}
      const hidden=_hiddenGames();
      const byGame={};
      for(const e of evs){
        const d=((e.tags.find(t=>t[0]==='d')||[])[1])||'';
        if(!d.startsWith('pcai:chesstr:') || d.startsWith('pcai:chesstr:player:')) continue;
        let s; try{ s=JSON.parse(e.content||'{}'); }catch(_){ continue; }
        if(!s || (s.white!==PC.ME.pubkey && s.black!==PC.ME.pubkey)) continue;
        const gid=s.root||d.slice('pcai:chesstr:'.length);
        if(hidden.has(gid)) continue;   // resigned/quit or cleared from this device
        if(!byGame[gid] || (e.created_at||0) > byGame[gid]._t){ s._t=e.created_at||0; byGame[gid]=s; }
      }
      const games=Object.values(byGame).sort((a,b)=>(a.status==='active'?0:1)-(b.status==='active'?0:1) || (b.started||0)-(a.started||0));
      if(!games.length){ list.innerHTML='<div class="empty">No games yet. Challenge someone above to start one.</div>'; return; }
      list.innerHTML = games.map((g,i)=>`<div class="chess-game-card glass" data-gi="${i}"><div class="spinner"></div></div>`).join('');
      games.forEach((g,i)=>_fillChessCard(g, $(`.chess-game-card[data-gi="${i}"]`, list)));
    }
    // FEN → 8×8 grid (row 0 = rank 8 … row 7 = rank 1), each cell a piece char or ''.
    // All glyphs use the filled set + a text variation selector (︎) so the browser renders them
    // as monochrome TEXT (tinted cyan/magenta by CSS) — without it, ♟ renders as a color emoji and
    // looks a different colour from the other pieces.
    const _VS='︎';   // text-presentation selector — forces monochrome (no emoji ♟)
    const _UNI={K:'♚'+_VS,Q:'♛'+_VS,R:'♜'+_VS,B:'♝'+_VS,N:'♞'+_VS,P:'♟'+_VS,
                k:'♚'+_VS,q:'♛'+_VS,r:'♜'+_VS,b:'♝'+_VS,n:'♞'+_VS,p:'♟'+_VS};
    function _fenGrid(fen){
      const rows=(fen||'').split(' ')[0].split('/'); const g=[];
      for(const r of rows){ const row=[]; for(const ch of r){ if(/\d/.test(ch)){ for(let i=0;i<+ch;i++) row.push(''); } else row.push(ch); } while(row.length<8) row.push(''); g.push(row); }
      while(g.length<8) g.push(['','','','','','','','']);
      return g;
    }
    function _chessBoardHtml(fen){
      const grid=_fenGrid(fen); let h='<div class="chessboard">';
      for(let ri=0; ri<8; ri++) for(let fi=0; fi<8; fi++){
        const pc=grid[ri][fi]; const sq=String.fromCharCode(97+fi)+(8-ri);
        const light=(ri+fi)%2===0; const white = pc && pc===pc.toUpperCase();
        h+=`<div class="csq ${light?'lt':'dk'}" data-sq="${sq}" data-pc="${enc(pc)}">${pc?`<span class="cpc ${white?'cw':'cb'}">${_UNI[pc]}</span>`:''}</div>`;
      }
      return h+'</div>';
    }
    function _fillChessCard(g, card){
      if(!card) return;
      const iAmWhite = g.white===PC.ME.pubkey;
      const oppPk = iAmWhite ? g.black : g.white;
      needProfile(oppPk); const op=profOf(oppPk)||{};
      const oppName = op.name||op.display_name||(g[iAmWhite?'black_name':'white_name'])||'opponent';
      const turn = (g.fen||'').split(' ')[1]==='w' ? 'white' : 'black';
      const myTurn = g.status==='active' && ((turn==='white')===iAmWhite);
      const isInvite = g.status==='active' && (g.moves||[]).length===0;   // nobody has moved yet
      let statusLine, badge;
      if(g.status!=='active'){ statusLine = g.status==='abandoned' ? 'Abandoned' : 'Game over'; badge='done'; }
      else if(myTurn && isInvite){ statusLine='♟️ Invited — move to accept'; badge='you'; }
      else if(myTurn){ statusLine='Your move — tap a piece'; badge='you'; }
      else if(isInvite){ statusLine=`Invite sent to ${enc(oppName)}`; badge='wait'; }
      else { statusLine=`Waiting on ${enc(oppName)}`; badge='wait'; }
      const moveBox = myTurn ? `<div class="chess-move-row">
          <input class="input chess-move-in" placeholder="or type: 1 d4 / Nf3 / e4 / O-O / resign" autocomplete="off">
          <button class="btn btn-neon small chess-move-go">Move ▶</button></div>` : '';
      card.classList.add('glass');
      card.innerHTML = `<div class="chess-card-hd">
          <img class="cc-av" src="${enc(op.picture||LOGO)}" onerror="this.src='${LOGO}'">
          <div class="cc-meta"><b>vs ${enc(oppName)}</b><span class="muted small">${enc(iAmWhite?'You: cyan (White)':'You: magenta (Black)')} · ${(g.moves||[]).length} half-moves</span></div>
          <span class="cc-badge ${badge}">${enc(statusLine)}</span>
          <button class="chess-quit" title="${g.status==='active'?'Resign &amp; remove':'Remove from your games'}">✕</button></div>
        <div class="chess-board-wrap${iAmWhite?'':' flip'}">${_chessBoardHtml(g.fen)}</div>
        ${moveBox}`;
      { const q=card.querySelector('.chess-quit'); if(q) q.onclick=(e)=>{ e.stopPropagation(); quitGame(g); }; }
      // tap-to-move: tap your piece (1st click), then its destination (2nd click) → sent as UCI
      // (the bot validates legality). The card LOCKS after one move so you can't fire several.
      if(myTurn){
        let sel=null, busy=false;
        const board=card.querySelector('.chessboard');
        const cells=()=>board.querySelectorAll('.csq');
        const clearSel=()=>cells().forEach(c=>c.classList.remove('sel'));
        const inp=card.querySelector('.chess-move-in'), go=card.querySelector('.chess-move-go');
        const fire=(uci)=>{ if(busy) return; busy=true; board.classList.add('locked'); if(go) go.disabled=true; chessMove(g, uci); };
        cells().forEach(cell=> cell.onclick=()=>{
          if(busy) return;
          const sq=cell.dataset.sq, pc=cell.dataset.pc;
          const mine = pc && (pc===pc.toUpperCase())===iAmWhite;
          if(sel){
            if(sel===sq){ sel=null; clearSel(); return; }       // tap same square → deselect
            if(mine){ sel=sq; clearSel(); cell.classList.add('sel'); return; }   // pick a different piece
            const selCell=board.querySelector(`.csq[data-sq="${sel}"]`);
            const moving=selCell&&selCell.dataset.pc;
            const promo=(moving==='P'&&sq[1]==='8')||(moving==='p'&&sq[1]==='1')?'q':'';
            cell.classList.add('sel');
            fire(sel+sq+promo); return;                          // 2nd click = destination
          }
          if(mine){ sel=sq; cell.classList.add('sel'); }         // 1st click = pick your piece
        });
        const send=()=>{ const mv=inp.value.trim(); if(!mv||busy) return; fire(mv); };
        if(go) go.onclick=send;
        if(inp) inp.addEventListener('keydown', e=>{ if(e.key==='Enter'){ e.preventDefault(); send(); } });
      }
    }
    async function chessMove(game, moveText){
      const botPk=safePk(PC.CFG.chess_bot_npub); if(!botPk){ toast('no chess bot'); return; }
      const oppPk = game.white===PC.ME.pubkey ? game.black : game.white;
      const tags=[['e', game.root, '', 'root']];
      if(game.last_board_event && game.last_board_event!==game.root) tags.push(['e', game.last_board_event, '', 'reply']);
      tags.push(['p', botPk], ['p', oppPk], ['t','chess'],['t','nostr'],['t','gamestr']);
      try{ await publish(1, moveText.trim()+"\n\n#chess", tags); toast('move sent ♟️'); }
      catch(e){ toast('move failed'); return; }
      setTimeout(()=>{ if(PC.VIEW==='chess') renderChess(); }, 4500);   // give the bot time to validate + post
    }

    window.PCChess = { render: renderChess };
    (window.PCGames = window.PCGames || {}).chess = renderChess;
  }
  init();
})();
