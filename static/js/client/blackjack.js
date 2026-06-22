/* #blackjack — Blackjack (21) vs the bot dealer. Separate file; uses window.__PC + registers in
 * window.PCGames. You play in the app or by replying "hit"/"stand" to the bot's DM. */
(function(){
  function init(){
    const PC = window.__PC;
    if(!PC){ return setTimeout(init, 50); }
    const { $, $$, enc, publish, sendDm, safePk, toast } = PC;
    const Relay = window.Relay;
    let _timer = null;

    const _SUIT = { S:'♠', H:'♥', D:'♦', C:'♣' };
    function handVal(h){ let t=0,a=0; for(const c of (h||[])){ const r=c.slice(0,-1); if(r==='A'){t+=11;a++;} else if('TJQK'.includes(r)) t+=10; else t+=(+r||0); } while(t>21&&a){t-=10;a--;} return t; }
    function cardHtml(c){ if(!c) return '<span class="bj-card back">?</span>'; const r=c.slice(0,-1), s=c.slice(-1); const red=(s==='H'||s==='D'); return `<span class="bj-card${red?' red':''}">${enc(r)}${enc(_SUIT[s]||s)}</span>`; }
    function _hidden(){ try{ return new Set(JSON.parse(localStorage.getItem('pc_bj_hidden')||'[]')); }catch(_){ return new Set(); } }
    function _hide(gid){ const s=_hidden(); s.add(gid); try{ localStorage.setItem('pc_bj_hidden', JSON.stringify([...s])); }catch(_){} }

    async function render(){
      const feed=$('#feed');
      const botNpub = PC.CFG.blackjack_bot_npub;
      const start = botNpub
        ? `<button class="btn btn-cyan" id="bj-play">🃏 New hand (vs dealer)</button>`
        : `<div class="empty">No #blackjack bot is configured on this server yet — ask the admin to enable one in Admin → Bots.</div>`;
      feed.innerHTML = `<div class="chess-hub">
          <div class="chess-splash glass">
            <h2>🃏 Blackjack</h2>
            <p class="muted">Beat the dealer to 21 without busting. The bot deals + DMs you your hand; reply <b>hit</b> or <b>stand</b> (or tap below). Dealer stands on 17.</p>
            ${start}
          </div>
          <div class="chess-games"><h3>🃏 Your hands</h3><div id="bj-games"><div class="spinner"></div></div></div>
        </div>`;
      const pb=$('#bj-play'); if(pb) pb.onclick=startHand;
      _load();
      clearInterval(_timer);
      _timer = setInterval(()=>{ if(PC.VIEW==='blackjack'){ _load(); } else clearInterval(_timer); }, 12000);
    }
    async function startHand(){
      const botPk=safePk(PC.CFG.blackjack_bot_npub); if(!botPk){ toast('no bot'); return; }
      try{ await publish(1, `🃏 Dealing a #blackjack hand vs the bot. I'll play from my DMs.\n\n#blackjack #nostr #gamestr`, [['p',botPk],['t','blackjack'],['t','nostr'],['t','gamestr']]);
        toast('dealing… 🃏'); setTimeout(()=>{ if(PC.VIEW==='blackjack') render(); }, 4500); }
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
        if(!s || s.player!==PC.ME.pubkey) continue;
        const gid=s.root||d.slice('pcai:blackjack:'.length);
        if(hidden.has(gid)) continue;
        if(!byGame[gid] || (e.created_at||0) > byGame[gid]._t){ s._t=e.created_at||0; byGame[gid]=s; }
      }
      const games=Object.values(byGame).sort((a,b)=>(a.status==='player'?0:1)-(b.status==='player'?0:1)||(b.started||0)-(a.started||0));
      if(!games.length){ list.innerHTML='<div class="empty">No hands yet. Deal one above.</div>'; return; }
      list.innerHTML = games.map((g,i)=>`<div class="chess-game-card glass" data-gi="${i}"></div>`).join('');
      games.forEach((g,i)=>_card(g, $(`.chess-game-card[data-gi="${i}"]`, list)));
    }
    async function quitGame(g){
      const active=g.status==='player';
      if(!confirm(active?'Fold and remove this hand?':'Remove this hand?')) return;
      if(active){ try{ await move(g,'stand'); }catch(_){} }   // stand it out so it resolves
      _hide(g.root); _load();
    }
    function _card(g, card){
      if(!card) return;
      const active = g.status==='player';
      const pv=handVal(g.phand), dv=handVal(g.dhand);
      const dealer = (g.dhand||[]).map((c,i)=> active && i>0 ? cardHtml(null) : cardHtml(c)).join('');
      const player = (g.phand||[]).map(cardHtml).join('');
      let banner='', badge, statusLine;
      if(!active){
        const win = g.outcome==='win'||g.outcome==='blackjack', push=g.outcome==='push';
        badge = push?'wait':(win?'you':'done'); statusLine = push?'Push':(win?'You won! 🎉':'You lost');
        banner = `<div class="chess-result ${push?'draw':(win?'win':'loss')}">${push?'🤝 Push':(win?'🏆 You won!':'💀 You lost')}<span class="muted small"> · ${enc(g.result||'')}</span></div>`;
      } else { badge='you'; statusLine='Your move — hit or stand'; }
      const controls = active ? `<div class="bj-controls"><button class="btn btn-cyan small bj-hit">Hit</button> <button class="btn btn-neon small bj-stand">Stand</button></div>` : '';
      card.innerHTML = `<div class="chess-card-hd">
          <div class="cc-meta"><b>Blackjack vs dealer</b><span class="muted small">Dealer stands on 17</span></div>
          <span class="cc-badge ${badge}">${enc(statusLine)}</span>
          <button class="chess-quit" title="${active?'Fold &amp; remove':'Remove'}">✕</button></div>
        ${banner}
        <div class="bj-table">
          <div class="bj-hand"><span class="bj-lbl">Dealer ${active?'':('· '+dv)}</span><div class="bj-cards">${dealer}</div></div>
          <div class="bj-hand"><span class="bj-lbl">You · ${pv}</span><div class="bj-cards">${player}</div></div>
        </div>
        ${controls}`;
      { const q=card.querySelector('.chess-quit'); if(q) q.onclick=(e)=>{ e.stopPropagation(); quitGame(g); }; }
      if(active){
        let busy=false;
        const lock=()=>{ busy=true; card.querySelectorAll('.bj-hit,.bj-stand').forEach(b=>b.disabled=true); };
        const hit=card.querySelector('.bj-hit'), st=card.querySelector('.bj-stand');
        if(hit) hit.onclick=()=>{ if(busy)return; lock(); move(g,'hit'); };
        if(st) st.onclick=()=>{ if(busy)return; lock(); move(g,'stand'); };
      }
    }
    async function move(game, action){
      const botPk=safePk(PC.CFG.blackjack_bot_npub); if(!botPk){ toast('no bot'); return; }
      // Gameplay is private: send the action as a NIP-17 DM with the hidden 'g:<root>' game marker.
      try{ await sendDm(botPk, `${action}\n\ng:${game.root}`); toast(action+' sent 🃏'); }
      catch(e){ toast('move failed'); return; }
      setTimeout(()=>{ if(PC.VIEW==='blackjack') render(); }, 4500);
    }

    (window.PCGames = window.PCGames || {}).blackjack = render;
  }
  init();
})();
