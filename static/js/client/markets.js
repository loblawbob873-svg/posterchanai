/* #markets — the daily crypto price+news digest (Discover → Markets). Kept OUT of app.js (own file, like
 * news.js / the games). The report is generated SERVER-SIDE at 08:00 (search + AI summary per coin) and
 * served from /api/markets as ONE shared document, so the client just fetches + renders. Each coin card
 * has a Share button that drops the briefing into the composer as a new note.
 * app.js dispatches the view via window.PCMarkets.render(). */
(function(){
  function init(){
    const PC = window.__PC;
    if(!PC){ return setTimeout(init, 50); }
    const { $, $$, enc, compose, authFetch } = PC;
    const inView = () => window.__PC.VIEW === 'markets';
    const safeUrl = u => /^https?:\/\//i.test(u||'') ? u : '';

    let _gen = 0, _report = null, _retryT = null, _tries = 0;

    async function fetchReport(){
      try{ const r = await authFetch('/api/markets'); return await r.json(); }
      catch(_){ return null; }
    }
    function fmtWhen(ts){ if(!ts) return ''; try{ return new Date(ts*1000).toLocaleString(); }catch(_){ return ''; } }

    function _card(c){
      const arts = (c.articles||[]).slice(0,4).map(a=>{
        const u = safeUrl(a.url); if(!u) return '';
        return `<a class="mkts-art" href="${enc(u)}" target="_blank" rel="noopener noreferrer">${enc(a.title||u)}</a>`;
      }).join('');
      return `<div class="mkts-card" data-sym="${enc(c.sym)}">
        <div class="mkts-head">
          <span class="mkts-sym">${enc(c.sym)}</span>
          <span class="mkts-name">${enc(c.name)}</span>
          <button class="mkts-post btn btn-cyan" title="Share as a post">Share</button>
        </div>
        <div class="mkts-sum${c.summary?'':' mkts-muted'}">${c.summary ? enc(c.summary) : 'No data available right now.'}</div>
        ${arts ? `<div class="mkts-arts">${arts}</div>` : ''}
      </div>`;
    }

    function paint(){
      const feed = $('#feed'); if(!feed) return;
      const r = _report;
      if(r && r.disabled){
        feed.innerHTML = `<div class="mkts-wrap"><div class="empty">Markets needs the AI backend, which isn't enabled on this server.</div></div>`;
        return;
      }
      if(r && r.unavailable){   // backend down — no dead Retry button; tick() auto-retries (server retries too)
        feed.innerHTML = `<div class="mkts-wrap"><div class="mkts-note">📈 Markets is temporarily unavailable — the AI backend isn't responding. Retrying automatically…</div></div>`;
        return;
      }
      if(!r){                   // transient fetch/connection error (502 during a restart, network blip)
        feed.innerHTML = `<div class="mkts-wrap"><div class="mkts-note">📈 Can't reach the server — retrying…</div><div class="spinner"></div></div>`;
        return;
      }
      const coins = r.coins || [];
      if(r.generating || !coins.length){   // building, or an unexpected/empty body → spinner; tick() keeps polling
        feed.innerHTML = `<div class="mkts-wrap"><div class="mkts-note">📈 Building today's market digest — this can take a minute…</div><div class="spinner"></div></div>`;
        return;
      }
      feed.innerHTML = `<div class="mkts-wrap">
        <div class="mkts-top">📈 Daily crypto digest${r.generated_at?` · <span class="mkts-muted">${enc(fmtWhen(r.generated_at))}</span>`:''}</div>
        ${coins.map(_card).join('')}
      </div>`;
      $$('.mkts-post', feed).forEach(b=> b.onclick=()=>{
        const el = b.closest('.mkts-card'); const c = coins.find(x=>x.sym===el.dataset.sym);
        if(c) share(c);
      });
    }

    function share(c){
      let t = `${c.name} (${c.sym})`;
      if(c.summary) t += `\n\n${c.summary}`;
      const a = (c.articles||[])[0];
      if(a && safeUrl(a.url)) t += `\n\n${a.url}`;
      try{ compose({ text: t }); }catch(_){}
    }

    // Schedule the next poll from the current state. ready/disabled → stop. generating → fast (slowing after
    // ~8min so a wedged server doesn't hammer). unavailable / transient error → slow auto-retry so the view
    // self-heals once the server's populate loop produces a report (no manual Retry needed).
    function _next(gen){
      if(gen!==_gen || !inView()) return;
      const r = _report;
      if(r && r.disabled) return;                      // terminal
      if(r && r.coins && r.coins.length) return;       // ready → terminal
      // everything else (generating / unavailable / null / unexpected / empty) keeps polling and self-heals.
      // Only the active 'generating' state polls fast (slowing after ~8min); the rest poll slowly at 30s.
      const fast = r && r.generating && _tries < 80;
      _tries++;
      clearTimeout(_retryT); _retryT = setTimeout(()=>tick(gen), fast ? 6000 : 30000);
    }
    async function tick(gen){
      if(gen!==_gen || !inView()) return;
      const rep = await fetchReport();
      if(gen!==_gen || !inView()) return;
      _report = rep;                                   // may be null on a transient error → paint() shows spinner
      paint();
      _next(gen);
    }
    async function render(){
      const gen = ++_gen; _tries = 0; clearTimeout(_retryT);
      const feed = $('#feed'); if(!feed) return;
      feed.innerHTML = `<div class="mkts-wrap"><div class="spinner"></div></div>`;
      const rep = await fetchReport();
      if(gen!==_gen || !inView()) return;
      _report = rep;
      paint();
      _next(gen);
    }

    window.PCMarkets = { render };
  }
  init();
})();
