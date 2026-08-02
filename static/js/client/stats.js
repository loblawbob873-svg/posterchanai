/* #stats — public Server Stats + Uptime. Anyone (including a logged-out guest) can read them.
 *
 * Two tabs, two cached endpoints: /client/server-stats (activity, recomputed at most once a minute
 * server-side) and /client/uptime (endpoint monitors, whose checks run in the background worker —
 * this page only reads the state the worker publishes). Opening either costs the server a dictionary
 * lookup, not a query.
 *
 * Rendering is deliberately dumb: every chart is a static SVG string built once per refresh — no
 * canvas, no animation loop, no requestAnimationFrame, no charting library. The only motion is a
 * CSS transition on hover. That keeps a page full of graphs at ~0% CPU when idle, which matters on
 * a phone and on the shared GPU box this runs on.
 */
(function(){
  function init(){
    const PC = window.__PC;
    if(!PC){ return setTimeout(init, 50); }
    const { $, enc } = PC;
    const inView = () => window.__PC.VIEW === 'stats';

    const RANGES = [['minute','60 min'],['hour','24 hours'],['day','30 days']];
    const TABS = [['stats','📊 Activity'],['uptime','📡 Uptime']];
    let _range = 'hour', _data = null, _timer = null, _busy = false;
    let _tab = 'stats', _up = null, _upBusy = false;

    // Per-metric accent + label. Colours are the client's own neon palette so the page matches the
    // rest of the app rather than inventing a second one.
    const M = {
      notes:     ['#22d3ee','🖊️','Notes'],
      reactions: ['#f472b6','❤️','Reactions'],
      replies:   ['#a78bfa','💬','Replies'],
      reposts:   ['#34d399','🔁','Reposts'],
      zaps:      ['#fbbf24','⚡','Zaps'],
      dms:       ['#60a5fa','✉️','DMs'],
      articles:  ['#f97316','📰','Articles'],
      files:     ['#2dd4bf','📎','Files'],
      profiles:  ['#c084fc','👤','Profile updates'],
      streams:   ['#ef4444','📺','Live streams'],
    };
    const ORDER = ['notes','reactions','replies','reposts','zaps','dms','articles','files','profiles','streams'];

    const nf = n => (n==null?'—':Number(n).toLocaleString());
    const bytes = b => { if(!b) return '—'; const u=['B','KB','MB','GB','TB']; let i=0,v=Number(b);
      while(v>=1024 && i<u.length-1){ v/=1024; i++; } return (v>=10?v.toFixed(0):v.toFixed(1))+' '+u[i]; };
    const rateLabel = () => ({minute:'per minute', hour:'per hour', day:'per day'})[_range];
    // Plain-English name for the selected range, used to label the summary sections so it's obvious
    // WHAT the number covers (they used to show all-time figures that never moved when you switched
    // range, which read as broken).
    const rangeWord = () => ({minute:'last hour', hour:'last 24h', day:'last 30 days'})[_range];
    // The COUNTER cards get their own range word, because for the first 24h after hourly counting
    // starts they still answer from the day bucket (see inRange). Saying "last 24h" over a
    // today-so-far number is the exact defect being fixed, so the label follows the data.
    const counterRangeWord = () => {
      const rolling = !!(_data && _data.counters && _data.counters.rolling);
      if(rolling || _range === 'day') return rangeWord();
      return _range === 'minute' ? 'today (UTC)' : 'today (UTC so far)';
    };
    // Counters are now bucketed per HOUR as well as per day, so each range is answered with the window
    // it actually names. It used to serve "last hour" AND "last 24h" from the current UTC day bucket,
    // which is today-so-far: in UTC-6 that means every evening after 18:00 local the numbers collapse
    // and read as broken (8 memes shown as a day's worth against a 51/day average).
    //
    // `rolling` is the server's flag that it sends the hourly windows. Without it — an older node
    // behind a newer page — fall back to the old day-bucket behaviour rather than showing 0, which
    // would be a confident lie where the old number was merely mislabelled.
    const inRange = (m)=>{
      const a = (m && m.series) || [];
      if(_range === 'day') return sum(a);
      if(_data && _data.counters && _data.counters.rolling){
        return (_range === 'minute' ? (m && m.last1h) : (m && m.last24)) || 0;
      }
      return a.length ? a[a.length - 1] : 0;
    };

    // ---- SVG chart builders -----------------------------------------------------------------
    // Both take an already-bounded array (60/24/30 points) and return a string. No DOM, no state.

    /* Area + line. `id` must be unique per chart on the page: the gradient is referenced by id. */
    function areaChart(vals, colour, id, h){
      const W = 300, H = h || 80, n = vals.length;
      if(!n) return '';
      const max = Math.max(1, ...vals);
      const x = i => (n===1 ? 0 : (i/(n-1))*W);
      const y = v => H - 2 - (v/max)*(H-8);
      const pts = vals.map((v,i)=>`${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
      return `<svg class="st-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">
        <defs><linearGradient id="g${id}" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="${colour}" stop-opacity=".38"/>
          <stop offset="100%" stop-color="${colour}" stop-opacity="0"/></linearGradient></defs>
        <polygon points="0,${H} ${pts} ${W},${H}" fill="url(#g${id})"/>
        <polyline points="${pts}" fill="none" stroke="${colour}" stroke-width="2"
          stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>
      </svg>`;
    }

    /* Bars — used for the big hero chart, where individual buckets are worth reading. */
    function barChart(vals, colour, h){
      const W = 300, H = h || 140, n = vals.length;
      if(!n) return '';
      const max = Math.max(1, ...vals);
      const gap = n > 40 ? 0.4 : 1.2;
      const bw = Math.max(0.6, W/n - gap);
      return `<svg class="st-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">
        ${vals.map((v,i)=>{ const bh=Math.max(v>0?1.5:0, (v/max)*(H-6));
          return `<rect x="${(i*(W/n)).toFixed(2)}" y="${(H-bh).toFixed(2)}" width="${bw.toFixed(2)}"
            height="${bh.toFixed(2)}" fill="${colour}" opacity="${0.55 + 0.45*(v/max)}" rx="1"/>`; }).join('')}
      </svg>`;
    }

    function sum(a){ return a.reduce((x,y)=>x+y,0); }

    // ---- page ---------------------------------------------------------------------------------
    function card(metric, vals){
      const [colour, icon, label] = M[metric];
      const total = sum(vals), last = vals[vals.length-1]||0;
      return `<div class="st-card" style="--acc:${colour}">
        <div class="st-cardhd"><span class="st-ic">${icon}</span><span class="st-lbl">${enc(label)}</span></div>
        <div class="st-num">${nf(total)}</div>
        <div class="st-sub muted small">${nf(last)} in the last ${_range==='minute'?'minute':_range==='hour'?'hour':'day'}</div>
        <div class="st-spark">${areaChart(vals, colour, metric+'_'+_range, 46)}</div>
      </div>`;
    }

    /* A card for a counted metric (30 daily points). `note` marks series that only start when the
       feature shipped, so a low number isn't mistaken for low usage. */
    function seriesCard(id, icon, label, colour, vals, ranged, allTime, note){
      return `<div class="st-card" style="--acc:${colour}">
        <div class="st-cardhd"><span class="st-ic">${icon}</span><span class="st-lbl">${enc(label)}</span>
          ${note?'<span class="st-new" title="Counted from when this feature shipped">new</span>':''}</div>
        <div class="st-num">${nf(ranged)}</div>
        <div class="st-sub muted small">${enc(counterRangeWord())} · ${nf(allTime)} all time</div>
        <div class="st-spark">${areaChart(vals, colour, 'c_'+id, 46)}</div>
      </div>`;
    }

    function tile(label, value, title){
      return `<div class="st-tile"${title?` title="${enc(title)}"`:''}>
        <div class="st-tval">${enc(String(value))}</div><div class="st-tlbl muted small">${enc(label)}</div></div>`;
    }

    /* The Activity tab's body (everything below the shared header/tabs). Returns a string; the
       caller owns the wrapper, so switching tabs never re-paints the header. */
    function activityBody(){
      if(!_data) return `<div class="spinner"></div>`;
      // Say so out loud rather than rendering an empty page: a payload without `windows` means the
      // endpoint isn't the stats one (this is exactly what a route-name collision looked like — the
      // page silently drew nothing while the fetch returned 200).
      if(!_data.windows){
        return `<p class="muted">Stats aren't available from this server yet — it may need a restart
          to pick up the stats endpoint.</p>`;
      }
      const w = (_data.windows||{})[_range] || {series:{}, n:0};
      const S = w.series || {};
      const W = w.totals || {events:0, people:0, games:0, by_game:{}};   // per-window figures (see stats_service)
      const notes = S.notes || [];
      const T = _data.totals || {}, G = _data.games || {by_game:{}};
      const CT = _data.counters || {metrics:{}}, isNew = !!CT.since_deploy;
      const blank = {series:[], total:0, today:0, last24:0, last1h:0};
      // Defaults for every counter, so a node running an older server (whose payload has no `meme`
      // key yet) renders an empty card instead of throwing on cm.meme.series.
      const cm = Object.assign({calls:blank, image:blank, music:blank, video:blank, meme:blank}, CT.metrics||{});
      const chat = (_data.chat||{}).series || [];
      // Per-game bars follow the selected range too (the last section that didn't). Fall back to the
      // all-time breakdown only if an older server hasn't got per-window figures.
      const gm = (W.by_game && Object.keys(W.by_game).length) ? W.by_game : (G.by_game || {});
      const gmax = Math.max(1, ...Object.values(gm));
      const gAny = Object.values(gm).some(v=>v>0);
      const GAME_LBL = {chess:'♟️ Chess', tictactoe:'⭕ Tic-Tac-Toe', hangman:'🎯 Hangman',
                        connect4:'🔴 Connect Four', blackjack:'🃏 Blackjack', holdem:'🂡 Hold’em'};

      return `
        <div class="st-ranges">${RANGES.map(([k,l])=>
          `<button class="st-range${_range===k?' on':''}" data-range="${k}">${enc(l)}</button>`).join('')}</div>

        <div class="st-hero" style="--acc:${M.notes[0]}">
          <div class="st-herohd">
            <div><div class="st-lbl">🖊️ Notes ${enc(rateLabel())}</div>
              <div class="st-hnum">${nf(sum(notes))}</div>
              <div class="muted small">in the last ${_range==='minute'?'hour':_range==='hour'?'24 hours':'30 days'}</div></div>
            <div class="st-peak muted small">peak ${nf(Math.max(0,...notes))}</div>
          </div>
          <div class="st-herochart">${barChart(notes, M.notes[0], 150)}</div>
        </div>

        <div class="st-grid">${ORDER.filter(m=>m!=='notes' && (S[m]||[]).some(v=>v>0)).map(m=>card(m, S[m])).join('')}</div>

        <h3 class="st-sec">🌐 This server <span class="st-rangelbl">${enc(rangeWord())}</span></h3>
        <div class="st-tiles">
          ${tile(`events ${rangeWord()}`, nf(W.events), 'Events published directly to this server in the selected range (not federated-in)')}
          ${tile(`people active ${rangeWord()}`, nf(W.people), 'Distinct pubkeys that published to this server in the selected range')}
          ${tile('notes '+rangeWord(), nf(sum(S.notes||[])))}
          ${tile('zaps '+rangeWord(), nf(sum(S.zaps||[])))}
        </div>
        <div class="muted small st-hint">All time:</div>
        <div class="st-tiles">
          ${tile('events posted here', nf(T.events), 'Events published directly to this server (origin), not synced from other relays')}
          ${tile('notes', nf(T.notes))}
          ${tile('profiles here', nf(T.profiles), 'Profiles published directly to this server')}
          ${tile('relay database', bytes(T.db_bytes), 'On-disk size of the relay database — includes everything it stores and serves, local and federated')}
        </div>

        <h3 class="st-sec">🤖 AI &amp; media <span class="st-rangelbl">${enc(counterRangeWord())}</span></h3>
        <div class="st-grid">
          ${seriesCard('chat','💬','AI chat','#22d3ee', chat, inRange(chat), T.ai_requests, false)}
          ${seriesCard('image','🎨','Images','#f472b6', cm.image.series, inRange(cm.image), cm.image.total, isNew)}
          ${seriesCard('music','🎵','Music','#a78bfa', cm.music.series, inRange(cm.music), cm.music.total, isNew)}
          ${seriesCard('video','🎬','Video','#34d399', cm.video.series, inRange(cm.video), cm.video.total, isNew)}
          ${seriesCard('meme','😂','Memes','#fb923c', cm.meme.series, inRange(cm.meme), cm.meme.total, isNew)}
          ${seriesCard('calls','📞','Calls','#fbbf24', cm.calls.series, inRange(cm.calls), cm.calls.total, isNew)}
        </div>
        <div class="muted small st-hint">Counted on this node as they happen. The 30-day range is bucketed per day; shorter ranges are a rolling window ending now.</div>

        <h3 class="st-sec">🎮 Games &amp; streams <span class="st-rangelbl">${enc(rangeWord())}</span></h3>
        <div class="st-tiles">
          ${tile('games played '+rangeWord(), nf(W.games))}
          ${tile('streams '+rangeWord(), nf(sum(S.streams||[])))}
          ${tile('games all time', nf(G.total))}
          ${tile('streams all time', nf(T.streams))}
        </div>

        <div class="st-games">${gAny ? Object.keys(gm).map(k=>`
          <div class="st-grow"><span class="st-glbl">${enc(GAME_LBL[k]||k)}</span>
            <span class="st-gbar"><i style="width:${Math.round((gm[k]/gmax)*100)}%"></i></span>
            <span class="st-gnum">${nf(gm[k])}</span></div>`).join('')
          : `<div class="muted small">No games in the ${enc(rangeWord())} — try a longer range.</div>`}</div>

        ${isNew ? `<div class="st-note muted small">Items marked <span class="st-new">new</span> are counted
          from when this feature shipped: generated media isn't stored server-side, and call signaling is
          ephemeral (kind&nbsp;25050), so there is no past to read. AI chat, and everything above, is
          full history.</div>` : ''}

        <div class="st-foot muted small">Refreshes every ${enc(String(_data.ttl||60))}s ·
          computed in ${enc(String(_data.ms||0))}ms · shared by every viewer</div>`;
    }

    // ---- Uptime tab ---------------------------------------------------------------------------
    // The server does the arithmetic (uptime %, averages) so this stays a renderer, same as above.

    const ago = ts => {
      if(!ts) return 'never';
      const s = Math.max(0, Math.floor(Date.now()/1000) - ts);
      if(s < 60) return s + 's ago';
      if(s < 3600) return Math.floor(s/60) + 'm ago';
      if(s < 86400) return Math.floor(s/3600) + 'h ago';
      return Math.floor(s/86400) + 'd ago';
    };
    const dur = ts => {
      if(!ts) return '—';
      const s = Math.max(0, Math.floor(Date.now()/1000) - ts);
      if(s < 3600) return Math.floor(s/60) + 'm';
      if(s < 86400) return Math.floor(s/3600) + 'h';
      return Math.floor(s/86400) + 'd';
    };
    const pct = v => (v==null ? '—' : (Number(v).toFixed(Number(v) >= 99.95 ? 0 : 2) + '%'));
    const plural = (n, w) => nf(n) + ' ' + w + (n === 1 ? '' : 's');

    /* Kuma-style heartbeat bar: one bar per check, oldest → newest, right-aligned. Titles carry the
       timestamp so hovering a red bar tells you WHEN, without any tooltip machinery. */
    function beats(checks){
      const c = (checks||[]).slice(-60);
      if(!c.length) return `<div class="up-beats"><span class="muted small">no checks yet</span></div>`;
      return `<div class="up-beats">${c.map(([ts, ok, ms])=>
        `<i class="up-beat${ok?'':' bad'}" title="${enc(new Date(ts*1000).toLocaleString())} · ${
          ok ? enc(String(ms)) + ' ms' : 'failed'}"></i>`).join('')}</div>`;
    }

    function monitorCard(m){
      const st = m.status === 'up' ? 'up' : (m.status === 'down' ? 'down' : 'pending');
      const lbl = st === 'up' ? 'Up' : (st === 'down' ? 'Down' : 'Pending');
      return `<div class="up-card ${st}">
        <div class="up-hd">
          <div class="up-name">
            <span class="up-dot"></span>
            <a href="${enc(m.url||'#')}" target="_blank" rel="noopener noreferrer nofollow">${enc(m.name||m.url||'')}</a>
          </div>
          <span class="up-pill ${st}">${lbl}</span>
        </div>
        <div class="up-url muted small">${enc(m.url||'')}</div>
        ${beats(m.checks)}
        <div class="up-meta muted small">
          ${st === 'pending' ? '<span>awaiting first check</span>'
            : `<span>${st} for <b>${enc(dur(m.since))}</b></span>`}
          <span>24h <b>${enc(pct(m.uptime_24h))}</b></span>
          <span>30d <b>${enc(pct(m.uptime_30d))}</b></span>
          <span>${enc(String(m.ms||0))} ms <span class="muted">(avg ${enc(String(m.avg_ms||0))})</span></span>
          <span>checked ${enc(ago(m.last))}</span>
        </div>
        ${m.err ? `<div class="up-err small">${enc(m.err)}</div>` : ''}
      </div>`;
    }

    function uptimeBody(){
      if(!_up) return `<div class="spinner"></div>`;
      if(!_up.enabled){
        return `<p class="muted">Uptime monitoring is off on this server. An admin can turn it on and
          add endpoints in <b>Admin → Nodes → Uptime Monitoring</b>.</p>`;
      }
      if(!(_up.monitors||[]).length){
        return `<p class="muted">No endpoints are being monitored yet. An admin can add them in
          <b>Admin → Nodes → Uptime Monitoring</b>.</p>`;
      }
      const all = _up.monitors, down = _up.down|0;
      const overall = all.map(m=>m.uptime_24h).filter(v=>v!=null);
      const avg = overall.length ? overall.reduce((a,b)=>a+b,0)/overall.length : null;
      return `
        <div class="up-banner ${down?'down':'ok'}">${down
          ? `🔴 ${nf(down)} of ${plural(_up.total,'endpoint')} down`
          : `🟢 All ${plural(_up.total,'endpoint')} up`}</div>
        <div class="st-tiles">
          ${tile('endpoints', nf(_up.total))}
          ${tile('up now', nf(_up.up))}
          ${tile('down now', nf(down))}
          ${tile('average uptime 24h', pct(avg))}
        </div>
        <div class="up-list">${all.map(monitorCard).join('')}</div>
        <div class="st-foot muted small">Checked in the background · page refreshes every 30s ·
          last update ${enc(ago(_up.updated))}<br>
          Shareable public status page: <a href="/status" target="_blank" rel="noopener">/status</a>
          · machine-readable: <a href="/status.json" target="_blank" rel="noopener">/status.json</a></div>`;
    }

    // ---- shell --------------------------------------------------------------------------------

    function render(){
      const feed = $('#feed'); if(!feed) return;
      const sub = _tab === 'uptime'
        ? 'Endpoints this server watches. Public, no account needed.'
        : 'Activity published to <b>this server</b> — not the wider Nostr network it syncs. Public, no account needed.';
      feed.innerHTML = `<div class="st-wrap">
        <div class="st-head">
          <div><h2 class="st-h1">📊 Server Stats</h2>
            <div class="muted small">${sub}</div></div>
        </div>
        <div class="st-tabs">${TABS.map(([k,l])=>
          `<button class="st-tab${_tab===k?' on':''}" data-tab="${k}">${enc(l)}</button>`).join('')}</div>
        <div class="st-body">${_tab === 'uptime' ? uptimeBody() : activityBody()}</div>
      </div>`;

      feed.querySelectorAll('.st-range').forEach(b=> b.onclick = ()=>{ _range = b.dataset.range; render(); });
      feed.querySelectorAll('.st-tab').forEach(b=> b.onclick = ()=>{
        if(_tab === b.dataset.tab) return;
        _tab = b.dataset.tab;
        render();               // paint the tab's last snapshot (or a spinner) immediately…
        load();                 // …then refresh whichever endpoint it needs
        schedule();             // the two tabs poll at different rates
      });
    }

    /* One loader for both tabs — it only ever fetches the endpoint the OPEN tab needs, so sitting on
       Activity costs nothing on the uptime side and vice versa. */
    async function load(){
      if(_tab === 'uptime'){
        if(_upBusy) return; _upBusy = true;
        try{
          const r = await fetch('/client/uptime', {credentials:'include'});
          if(r.ok){ const d = await r.json(); if(!d.error) _up = d; }
        }catch(_){ }
        finally{ _upBusy = false; }
      }else{
        if(_busy) return; _busy = true;
        try{
          const r = await fetch('/client/server-stats', {credentials:'include'});
          if(r.ok){ const d = await r.json(); if(!d.error) _data = d; }
        }catch(_){ }
        finally{ _busy = false; }
      }
      if(inView()) render();
    }

    /* Poll only while the page is actually open AND the tab is visible — a backgrounded phone must
       not keep waking to fetch. Uptime moves faster than the once-a-minute stats snapshot, so it
       polls at 30s; the server caches both, so neither is a per-viewer cost. */
    function schedule(){
      if(_timer) clearInterval(_timer);
      const every = _tab === 'uptime' ? 30000 : 60000;
      _timer = setInterval(()=>{
        if(!inView()){ clearInterval(_timer); _timer=null; return; }
        if(document.visibilityState === 'visible') load();
      }, every);
    }

    async function renderStats(){
      render();                 // paint the last snapshot (or a spinner) immediately
      await load();
      schedule();
    }

    window.PCStats = { render: renderStats };
  }
  init();
})();
