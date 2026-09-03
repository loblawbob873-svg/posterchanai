/* #hangman — Hangman game UI for the Nostr client. Separate file; uses window.__PC + registers in
 * window.PCGames. The bot holds the secret word (encrypted); the client shows the masked display and
 * a tappable A-Z keyboard. */
(function(){
  function init(){
    const PC = window.__PC;
    if(!PC){ return setTimeout(init, 50); }
    const { $, $$, enc, publish, sendDm, safePk, nip05Resolve, profOf, needProfile, niceNip05, LOGO, toast, ensureProfile, NT } = PC;
    const Relay = window.Relay, Store = window.Store;
    let _botWatch=null;
    let _timer = null;

    function _hidden(){ try{ return new Set(JSON.parse(localStorage.getItem('pc_hm_hidden')||'[]')); }catch(_){ return new Set(); } }
    function _hide(gid){ const s=_hidden(); s.add(gid); try{ localStorage.setItem('pc_hm_hidden', JSON.stringify([...s])); }catch(_){} }

    async function render(){
      const feed=$('#feed');
      const botNpub = PC.CFG.hangman_bot_npub;
      const start = botNpub ? `
        <div class="chess-invite">
          <button class="btn btn-cyan" id="hm-play">🎯 New game (you guess)</button>
          <div class="chess-or">— or challenge someone to guess —</div>
          <input id="hm-inv" class="input" placeholder="search name / npub / name@domain…" autocomplete="off">
          <div id="hm-inv-res" class="chess-inv-res"></div>
        </div>` : `<div class="empty">No #hangman bot is configured on this server yet — ask the admin to enable one in Admin → Bots.</div>`;
      feed.innerHTML = `<div class="chess-hub">
          <div class="chess-splash glass">
            <h2>🎯 Hangman</h2>
            <p class="muted">The bot picks a secret word and <b>DMs it to you masked</b>; guess letters before the figure is complete (6 misses). Tap a letter here, or reply to the bot's DM. Only the start &amp; final result are public.</p>
            ${start}
          </div>
          <div class="chess-games"><h3>🎯 Your games</h3><div id="hm-games"><div class="spinner"></div></div></div>
        </div>`;
      if(botNpub){ const pb=$('#hm-play'); if(pb) pb.onclick=()=>startGame(null); _bindInvite(); }
      _load();
      clearInterval(_timer);
      _timer = setInterval(()=>{ if(PC.VIEW==='hangman'){ _load(); } else clearInterval(_timer); }, 30000);
      /* The board repaints when the BOT PUBLISHES, not when a timer next comes round. The interval
       * above is only a backstop now (a missed event, a socket that dropped and came back), which is
       * why it went from 12s to 30. See PC.watchBot. */
      if(_botWatch){ _botWatch(); _botWatch=null; }
      const _bpk = safePk(PC.CFG.hangman_bot_npub);   // botNpub is the npub; the pool filters on hex
      if(_bpk && PC.watchBot) _botWatch = PC.watchBot(_bpk, ()=>{ if(PC.VIEW==='hangman') _load(); else { _botWatch && _botWatch(); _botWatch=null; } });
    }
    function _bindInvite(){
      const inp=$('#hm-inv'), res=$('#hm-inv-res'); if(!inp) return; let t=null;
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
    async function startGame(pk){
      const botPk=safePk(PC.CFG.hangman_bot_npub); if(!botPk){ toast('no bot'); return; }
      let body, tags;
      if(pk){
        if(pk===PC.ME.pubkey){ toast("challenge someone else (or use New game)"); return; }
        let npub; try{ npub=NT().nip19.npubEncode(pk); }catch(_){ npub=pk; }
        const meName=(profOf(PC.ME.pubkey)||{}).name||'A player';
        body=`🎯 ${meName} challenged you to #hangman! nostr:${npub} — the bot will DM you a word to guess, a letter at a time. Guess here on the Hangman tab or by replying to the DM.`;
        tags=[['p',botPk],['p',pk],['t','hangman'],['t','nostr'],['t','gamestr']];
      } else {
        body=`🎯 start a #hangman game — the bot will DM me a word to guess privately, a letter at a time.`;
        tags=[['p',botPk],['t','hangman'],['t','nostr'],['t','gamestr']];
      }
      try{ await publish(1, body+`\n\nstart\n\n#hangman #nostr #gamestr`, tags); toast('starting hangman 🎯'); setTimeout(()=>{ if(PC.VIEW==='hangman') render(); }, 4500); }
      catch(e){ toast('could not start'); }
    }
    async function _load(){
      const list=$('#hm-games'); if(!list) return;
      const botPk=safePk(PC.CFG.hangman_bot_npub);
      if(!botPk){ list.innerHTML='<div class="empty">No bot configured.</div>'; return; }
      let evs=[]; try{ evs=await Relay.query([{ authors:[botPk], kinds:[30388], limit:500 }]); }catch(_){}
      const hidden=_hidden(), byGame={};
      for(const e of evs){
        const d=((e.tags.find(t=>t[0]==='d')||[])[1])||'';
        if(!d.startsWith('pcai:hangman:')) continue;
        let s; try{ s=JSON.parse(e.content||'{}'); }catch(_){ continue; }
        if(!s || (s.guesser!==PC.ME.pubkey && s.opponent!==PC.ME.pubkey)) continue;
        const gid=s.root||d.slice('pcai:hangman:'.length); s._gid=gid;   // remember the EXACT key so removal matches (solo/rootless games have no s.root)
        if(hidden.has(gid)) continue;
        if(!byGame[gid] || (e.created_at||0) > byGame[gid]._t){ s._t=e.created_at||0; byGame[gid]=s; }
      }
      const games=Object.values(byGame).filter(g=>['active','awaiting_word'].includes(g.status))/* show only in-progress games; finished/left/resigned drop out (left holdem/bj tables also caught by s.left above) */.sort((a,b)=>(a.status==='active'?0:1)-(b.status==='active'?0:1)||(b.started||0)-(a.started||0));
      if(!games.length){ list.innerHTML='<div class="empty">No games yet. Start one above.</div>'; return; }
      list.innerHTML = games.map((g,i)=>`<div class="chess-game-card glass" data-gi="${i}"></div>`).join('');
      games.forEach((g,i)=>_card(g, $(`.chess-game-card[data-gi="${i}"]`, list)));
    }
    async function quitGame(g){
      const active=g.status==='active';
      if(!await PC.uiConfirm(active?'Give up and remove this game?':'Remove this game?',
                             { ok: active?'Give up':'Remove', danger: true })) return;
      if(active && g.guesser===PC.ME.pubkey){ try{ await guess(g,'resign'); }catch(_){} }
      _hide(g._gid||g.root); _load();
    }
    // Cyberpunk gallows drawn client-side from the wrong-guess count (the bot doesn't post mid-game).
    function _gallows(w){
      const C='#3ce6ff', F='#ff3cd2';   // cyan frame, magenta figure
      const part=(n,svg)=> w>=n ? svg : '';
      return `<svg class="hm-svg" viewBox="0 0 170 200" aria-hidden="true">
        <g stroke="${C}" stroke-width="5" fill="none" stroke-linecap="round">
          <line x1="12" y1="192" x2="95" y2="192"/><line x1="28" y1="192" x2="28" y2="12"/>
          <line x1="28" y1="12" x2="120" y2="12"/><line x1="120" y1="12" x2="120" y2="32"/></g>
        <g stroke="${F}" stroke-width="5" fill="none" stroke-linecap="round">
          ${part(1,'<circle cx="120" cy="48" r="16"/>')}
          ${part(2,'<line x1="120" y1="64" x2="120" y2="128"/>')}
          ${part(3,'<line x1="120" y1="80" x2="92" y2="106"/>')}
          ${part(4,'<line x1="120" y1="80" x2="148" y2="106"/>')}
          ${part(5,'<line x1="120" y1="128" x2="96" y2="168"/>')}
          ${part(6,'<line x1="120" y1="128" x2="144" y2="168"/>')}
        </g></svg>`;
    }
    function _card(g, card){
      if(!card) return;
      const iGuess = g.guesser===PC.ME.pubkey;
      const tried = new Set([...(g.guessed||[]), ...(g.wrong_letters||[])].map(x=>x.toLowerCase()));
      const active = g.status==='active';
      let statusLine, badge, banner='';
      if(!active){
        const iWon = g.status==='won' || g.winner_pk===PC.ME.pubkey;
        statusLine = iWon ? 'You won! 🎉' : 'You lost';
        badge = iWon ? 'you' : 'done';
        banner = `<div class="chess-result ${iWon?'win':'loss'}">${iWon?'🏆 You won!':'💀 You lost'}<span class="muted small"> · ${enc(g.result||'')}</span></div>`;
      }
      else if(iGuess){ statusLine='Your turn — pick a letter'; badge='you'; }
      else { statusLine='Watching'; badge='wait'; }
      const display = enc(g.display||'');
      const kb = (iGuess && active) ? '<div class="hm-keys">'+'abcdefghijklmnopqrstuvwxyz'.split('').map(L=>
        `<button class="hm-key${tried.has(L)?' used':''}" data-l="${L}"${tried.has(L)?' disabled':''}>${L.toUpperCase()}</button>`).join('')+'</div>' : '';
      card.innerHTML = `<div class="chess-card-hd">
          <div class="cc-meta"><b>${iGuess?'You are guessing':enc(g.guesser_name||'someone')+' is guessing'}</b>
            <span class="muted small">Misses ${g.wrong||0}/6${(g.wrong_letters||[]).length?' · wrong: '+enc((g.wrong_letters||[]).join(' ').toUpperCase()):''}</span></div>
          <span class="cc-badge ${badge}">${enc(statusLine)}</span>
          <button class="chess-quit" title="${active?'Give up &amp; remove':'Remove'}">✕</button></div>
        ${banner}
        ${g.hint?`<div class="hm-hint muted small" style="text-align:center;margin:2px 0 6px">💡 Clue: ${enc(g.hint)}</div>`:''}
        <div class="hm-game">${_gallows(g.wrong||0)}<div class="hm-word">${display||'…'}</div></div>
        ${kb}`;
      { const q=card.querySelector('.chess-quit'); if(q) q.onclick=(e)=>{ e.stopPropagation(); quitGame(g); }; }
      if(iGuess && active){
        let busy=false;
        card.querySelectorAll('.hm-key:not(.used)').forEach(b=> b.onclick=()=>{
          if(busy) return; busy=true; card.querySelectorAll('.hm-key').forEach(k=>k.disabled=true);
          guess(g, b.dataset.l);
        });
      }
    }
    async function guess(game, letter){
      const botPk=safePk(PC.CFG.hangman_bot_npub); if(!botPk){ toast('no bot'); return; }
      // Gameplay is PRIVATE: guesses go to the bot as a NIP-17 DM. The 'g:<root>' marker (on its own
      // line so it's hidden from the visible message) tells the bot which game this guess belongs to.
      try{ await sendDm(botPk, `${letter.trim()}\n\ng:${game.root}`); toast('guess sent 🎯'); }
      catch(e){ toast('guess failed'); return; }
      setTimeout(()=>{ if(PC.VIEW==='hangman') render(); }, 4500);
    }

    (window.PCGames = window.PCGames || {}).hangman = render;
  }
  init();
})();
