/* Telegram — a client inside PosterChan (app/services/telegram_client). Sign in once; every window,
 * computer and tablet is the same conversation, because the session lives on the node.
 *
 * NOT ON PHONES. A phone already runs Telegram; a second live client there only doubles every
 * notification. `isPhone()` is a short screen side under 480 CSS px — Android's "large" screen class,
 * which no phone reaches (the biggest are ~430) — so a tablet (APK or browser) gets the full client and
 * a phone gets a sentence saying why not. The line was 600 (Android's sw600dp resource bucket) and hid
 * Telegram on real tablets: an 8" 800x1280 tablet at 240dpi is 533dp across ("i don't see telegram on
 * android tablet").
 *
 * NOTIFICATIONS without the view open: once the app is up (desktop page only — a popped-out window
 * must not notify a second time) a background socket listens and raises an OS notification per new
 * incoming message, tagged per message so several tabs collapse into one.
 */
(function(){
  'use strict';
  const PC = () => window.__PC || {};
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  const PHONE_MAX_SHORT_SIDE = 480;
  function isPhone(){
    try{ return Math.min(screen.width || 9999, screen.height || 9999) < PHONE_MAX_SHORT_SIDE; }catch(_){ return false; }
  }
  const st = { status:null, dialogs:[], open:null, msgs:new Map(), filter:'', ws:null, wsTimer:0, reply:null,
               loadingOlder:false, done:new Set(), pending:[], busy:false };
  let root = null;

  async function api(path, opts){
    const P = PC();
    try{ if(P.ensureAiSession) await P.ensureAiSession(); }catch(_){}
    const r = await (P.authFetch ? P.authFetch(path, opts || {}) : fetch(path, Object.assign({credentials:'include'}, opts || {})));
    let j = null; try{ j = await r.json(); }catch(_){}
    if(!r.ok || (j && j.ok === false)) throw new Error((j && (j.error || j.detail)) || ('HTTP ' + r.status));
    return j || {};
  }
  const post = (path, body) => api(path, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body||{}) });
  /* Media is addressed with a short-lived TICKET, never the session token: an <img src> cannot carry
   * a header, and a token in a URL ends up in logs and history (routers/telegram_client.py). */
  const tk = { t:'', at:0 };
  async function ticket(){
    if(tk.t && Date.now() - tk.at < 5 * 3600 * 1000) return tk.t;
    try{ const r = await api('/api/tgc/ticket'); tk.t = r.t || ''; tk.at = Date.now(); }catch(_){}
    return tk.t;
  }
  const base = () => { const P = PC(); return ((P.apiBase ? P.apiBase() : '') || '').replace(/\/+$/, ''); };
  function mediaUrl(chat, id, thumb){
    return base() + '/api/tgc/media/' + chat + '/' + id + '?thumb=' + (thumb ? 1 : 0) + (tk.t ? '&t=' + encodeURIComponent(tk.t) : '');
  }
  function avatarUrl(peer){
    return base() + '/api/tgc/avatar/' + peer + (tk.t ? '?t=' + encodeURIComponent(tk.t) : '');
  }
  const initials = t => (String(t || '?').trim().split(/\s+/).map(w => w[0]).join('').slice(0, 2) || '?').toUpperCase();
  const when = ts => { if(!ts) return ''; const d = new Date(ts * 1000), n = new Date();
    return d.toDateString() === n.toDateString() ? d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'})
                                                 : d.toLocaleDateString([], {month:'short', day:'numeric'}); };
  const size = n => n > 1e6 ? (n / 1e6).toFixed(1) + ' MB' : n > 1e3 ? Math.round(n / 1e3) + ' KB' : (n || 0) + ' B';

  // ---- live socket (shared by the view and the background notifier) -------------------------------
  function connect(){
    if(st.ws && st.ws.readyState <= 1) return;
    const P = PC(), base = (P.apiBase ? P.apiBase() : location.origin) || location.origin;
    let ws;
    try{ ws = new WebSocket(base.replace(/^http/, 'ws') + '/api/tgc/ws'); }catch(_){ return; }
    st.ws = ws;
    ws.onopen = async () => { try{ if(P.ensureAiSession) await P.ensureAiSession(); }catch(_){}
      ws.send(JSON.stringify({ token: P.aiToken ? P.aiToken() : '' })); };
    ws.onmessage = e => { let ev; try{ ev = JSON.parse(e.data); }catch(_){ return; } onEvent(ev); };
    ws.onclose = () => { if(st.ws === ws) st.ws = null;
      // Back off and come back: a restart of the node must not end notifications for the session.
      clearTimeout(st.wsTimer); st.wsTimer = setTimeout(() => { if(wanted()) connect(); }, 8000); };
  }
  function wanted(){ return !isPhone() && st.status && st.status.state === 'ready'; }

  function onEvent(ev){
    if(ev.type === 'state'){ const was = st.status && st.status.state; st.status = Object.assign({}, st.status || {}, ev);
      if(was !== ev.state && inView()) render(); return; }
    if(ev.type === 'message'){
      const m = ev.message, list = st.msgs.get(m.chat_id);
      if(list && !list.some(x => x.id === m.id)) list.push(m);
      const d = st.dialogs.find(x => x.id === m.chat_id);
      if(d){ d.last = { text:(m.text || '').slice(0, 200), date:m.date, out:m.out, media:m.media && m.media.kind };
             if(!m.out && st.open !== m.chat_id) d.unread = (d.unread || 0) + 1;
             st.dialogs = [d, ...st.dialogs.filter(x => x !== d)]; }
      else st.dialogs.unshift({ id:m.chat_id, title:(ev.chat && ev.chat.title) || m.sender, kind:'user', unread:m.out ? 0 : 1,
                                last:{ text:m.text, date:m.date, out:m.out } });
      const looking = inView() && st.open === m.chat_id && document.hasFocus && document.hasFocus();
      if(!m.out && !looking) notify(m, ev.chat);
      if(inView()){ paintDialogs(); if(st.open === m.chat_id){ paintMessages(true); markRead(); } }
      return;
    }
    if(ev.type === 'edited'){ const list = st.msgs.get(ev.message.chat_id), i = list ? list.findIndex(x => x.id === ev.message.id) : -1;
      if(i >= 0){ list[i] = ev.message; if(inView() && st.open === ev.message.chat_id) paintMessages(false); } return; }
    if(ev.type === 'deleted'){ for(const [cid, list] of st.msgs){ const n = list.filter(x => !ev.ids.includes(x.id));
      if(n.length !== list.length){ st.msgs.set(cid, n); if(inView() && st.open === cid) paintMessages(false); } } }
  }
  function notify(m, chat){
    const P = PC(), who = (chat && chat.title) || m.sender || 'Telegram';
    const body = m.text || (m.media ? ({photo:'📷 Photo', video:'🎬 Video', voice:'🎤 Voice message', video_note:'⏺ Video message',
                                          sticker:'Sticker', gif:'GIF', audio:'🎵 Audio'}[m.media.kind] || '📎 ' + (m.media.name || 'File')) : '');
    try{ if(P.osNotify) P.osNotify('Telegram · ' + who, body, { tag:'tg-' + m.chat_id + '-' + m.id, notificationType:'dm',
                                                               onClick:() => { openChatFromNotification(m.chat_id); } }); }catch(_){}
  }
  function openChatFromNotification(chatId){
    const P = PC(); st.open = chatId;
    try{ if(P.switchView) P.switchView('tg'); }catch(_){}
  }
  const inView = () => { try{ return PC().VIEW === 'tg' && root && root.isConnected; }catch(_){ return false; } };

  // ---- views ----------------------------------------------------------------------------------------
  async function render(){
    const feed = document.getElementById('feed'); if(!feed) return;
    try{ const t = document.getElementById('view-title'); if(t) t.textContent = 'Telegram'; }catch(_){}
    if(isPhone()){
      feed.innerHTML = `<div class="tg-app tg-empty"><div class="tg-card"><h3>Telegram lives on your phone</h3>
        <p>This client is for computers and tablets. On a phone, the Telegram app you already have keeps your
        notifications single and your account in one place.</p></div></div>`;
      return;
    }
    feed.innerHTML = '<div class="tg-app"><div class="tg-boot">CONNECTING…</div></div>';
    root = feed.querySelector('.tg-app');
    try{ st.status = await api('/api/tgc/status'); }
    catch(e){ root.innerHTML = `<div class="tg-card"><h3>Telegram is not available</h3><p>${esc(e.message)}</p></div>`; return; }
    if(!inView()) return;
    if(!st.status.configured) return renderSetup();
    if(st.status.state !== 'ready') return renderLogin();
    await ticket();
    connect();
    return renderMain();
  }

  /* NOT SET UP YET: say exactly what to do, and to whom. An admin gets the steps and a link straight to
   * the fields; anybody else is told it is a server setting and who can change it. */
  function renderSetup(){
    const admin = !!st.status.admin, adminUrl = base() + '/admin#tab-telegram';
    root.innerHTML = `<div class="tg-card tg-setup"><div class="tg-logo">TG//LINK · SETUP</div>
      <h3>Connect Telegram to this server</h3>
      ${admin ? `<p class="tg-muted">One-time, for everyone on this server. Telegram requires every app to have its own API key:</p>
      <ol class="tg-steps">
        <li>Open <a href="https://my.telegram.org/apps" target="_blank" rel="noopener noreferrer">my.telegram.org/apps</a> and log in with your phone number.</li>
        <li>Under <b>API development tools</b>, create an app — any title and short name, platform <b>Web</b>.</li>
        <li>Copy the <b>App api_id</b> (a number) and <b>App api_hash</b> (32 letters and digits).</li>
        <li>Paste them into <b>Admin → Telegram → API ID / API Hash</b> and press <b>Save Settings</b>.</li>
        <li>Come back here — the sign-in screen appears.</li>
      </ol>
      <div class="tg-form"><a class="tg-btn tg-primary tg-linkbtn" href="${esc(adminUrl)}" target="_blank" rel="noopener">Open Admin → Telegram</a>
        <button class="tg-btn" data-act="recheck">I've saved it — check again</button></div>`
      : `<p class="tg-muted">Telegram needs a one-time server setting before anyone can sign in here: an API key
        from my.telegram.org, entered by an admin of this server in <b>Admin → Telegram</b>.</p>
      <p class="tg-muted">Ask your server's admin to set it up, then come back — nothing else is needed from you.</p>
      <div class="tg-form"><button class="tg-btn" data-act="recheck">Check again</button></div>`}
      <p class="tg-fine">Your Telegram login itself is yours alone: it is kept on this server, encrypted to your account, and
      no other user can use it.</p></div>`;
    const re = root.querySelector('[data-act="recheck"]'); if(re) re.onclick = () => render();
  }

  function renderLogin(){
    const s = st.status.state;
    const step = s === 'code' ? 2 : s === 'password' ? 3 : 1;
    root.innerHTML = `<div class="tg-card tg-login"><div class="tg-logo">TG//LINK</div>
      <ol class="tg-progress" aria-label="Sign-in steps">
        <li class="${step >= 1 ? 'on' : ''}">Phone</li><li class="${step >= 2 ? 'on' : ''}">Code</li><li class="${step >= 3 ? 'on' : ''}">Password</li></ol>
      <h3>${s === 'code' ? 'Enter the code' : s === 'password' ? 'Your Telegram password' : 'Sign in to Telegram'}</h3>
      <p class="tg-muted">${s === 'code'
          ? 'Telegram just sent a login code <b>inside your Telegram app</b> — look for a message from <b>Telegram</b> on your phone or another device. If you have no other device signed in, it comes by SMS.'
        : s === 'password'
          ? 'Your account has <b>two-step verification</b> on. Enter the cloud password you set in Telegram → Settings → Privacy and Security → Two-Step Verification. (This is not the code.)'
          : 'Enter the phone number of your Telegram account, with its country code. You do this <b>once</b>: every computer and tablet you use PosterChan on shares the same login. It stays on this server, encrypted to your account.'}</p>
      <form class="tg-form"><input class="tg-in" name="v" ${s === 'password' ? 'type="password"' : s === 'code' ? 'inputmode="numeric" autocomplete="one-time-code"' : 'type="tel" autocomplete="tel"'}
        placeholder="${s === 'code' ? '12345' : s === 'password' ? 'Password' : '+1 555 010 4477'}" required>
      <button class="tg-btn tg-primary" type="submit">${s === 'none' ? 'Send code' : 'Continue'}</button></form>
      <p class="tg-err" role="alert"></p>
      ${s !== 'none' ? '<button class="tg-mini tg-restart" data-act="restart">Wrong number? Start over</button>' : ''}</div>`;
    const f = root.querySelector('form'), err = root.querySelector('.tg-err');
    const rs = root.querySelector('[data-act="restart"]');
    if(rs) rs.onclick = async () => { try{ await post('/api/tgc/logout'); }catch(_){} st.status = Object.assign({}, st.status, {state:'none'}); render(); };
    setTimeout(() => { const i = f.querySelector('input'); if(i) i.focus(); }, 0);
    f.onsubmit = async e => {
      e.preventDefault(); const v = f.v.value.trim(), b = f.querySelector('button'); b.disabled = true; err.textContent = '';
      try{
        const r = s === 'none' ? await post('/api/tgc/login/phone', {phone:v})
                : s === 'code' ? await post('/api/tgc/login/code', {code:v}) : await post('/api/tgc/login/password', {password:v});
        st.status = Object.assign({}, st.status, r); render();
      }catch(x){ err.textContent = x.message; b.disabled = false; }
    };
  }

  async function renderMain(){
    root.innerHTML = `<div class="tg-shell${st.open ? ' tg-chat-open' : ''}">
      <aside class="tg-side"><div class="tg-side-hd"><span class="tg-logo">TG//LINK</span>
          <button class="tg-icon" data-act="logout" title="Sign out of Telegram">⏻</button></div>
        <input class="tg-in tg-search" type="search" placeholder="Search chats" value="${esc(st.filter)}" aria-label="Search chats">
        <div class="tg-dialogs" role="list"><div class="tg-muted tg-pad">Loading chats…</div></div></aside>
      <section class="tg-chat" aria-live="polite"></section></div>`;
    root.querySelector('.tg-search').oninput = e => { st.filter = e.target.value; paintDialogs(); };
    root.querySelector('[data-act="logout"]').onclick = async () => {
      const P = PC(); if(P.uiConfirm && !(await P.uiConfirm('Sign out of Telegram on every device?'))) return;
      await post('/api/tgc/logout'); st.status = null; st.dialogs = []; st.msgs.clear(); st.open = null; render(); };
    try{ st.dialogs = (await api('/api/tgc/dialogs?limit=200')).dialogs || []; }
    catch(e){ root.querySelector('.tg-dialogs').innerHTML = `<div class="tg-err tg-pad">${esc(e.message)}</div>`; return; }
    if(!inView()) return;
    paintDialogs();
    if(st.open) openChat(st.open); else paintChat();
  }

  function paintDialogs(){
    const box = root && root.querySelector('.tg-dialogs'); if(!box) return;
    const q = st.filter.trim().toLowerCase();
    const rows = st.dialogs.filter(d => !q || String(d.title).toLowerCase().includes(q));
    box.innerHTML = rows.length ? rows.map(d => `<button class="tg-dialog${d.id === st.open ? ' on' : ''}" data-chat="${d.id}" role="listitem">
        <span class="tg-av" data-av="${d.id}"><b>${esc(initials(d.title))}</b></span>
        <span class="tg-dmain"><span class="tg-drow"><b class="tg-dtitle">${d.kind === 'channel' ? '📢 ' : d.kind === 'group' ? '👥 ' : ''}${esc(d.title)}</b>
          <small>${esc(when(d.last && d.last.date))}</small></span>
          <span class="tg-drow"><span class="tg-dlast">${d.last && d.last.out ? 'You: ' : ''}${esc((d.last && d.last.text) || (d.last && d.last.media ? '[' + d.last.media + ']' : ''))}</span>
          ${d.unread ? `<i class="tg-badge">${d.unread > 99 ? '99+' : d.unread}</i>` : ''}</span></span></button>`).join('')
      : '<div class="tg-muted tg-pad">No chats match.</div>';
    box.querySelectorAll('[data-chat]').forEach(b => b.onclick = () => openChat(Number(b.dataset.chat)));
    loadAvatars(box);
  }
  function loadAvatars(scope){
    scope.querySelectorAll('[data-av]:not([data-av-done])').forEach(el => {
      el.dataset.avDone = '1'; const img = new Image(); img.alt = '';
      img.onload = () => { if(img.naturalWidth){ el.innerHTML = ''; el.appendChild(img); } };
      img.src = avatarUrl(el.dataset.av);
    });
  }

  function paintChat(){
    const pane = root.querySelector('.tg-chat'); if(!pane) return;
    root.querySelector('.tg-shell').classList.toggle('tg-chat-open', !!st.open);
    if(!st.open){ pane.innerHTML = '<div class="tg-empty"><div class="tg-glyph">◢◤</div><p>Pick a chat.</p></div>'; return; }
    const d = st.dialogs.find(x => x.id === st.open) || { title:'Chat', id:st.open, kind:'user' };
    pane.innerHTML = `<header class="tg-chat-hd"><button class="tg-icon tg-back" data-act="back" aria-label="Back to chats">‹</button>
        <span class="tg-av" data-av="${d.id}"><b>${esc(initials(d.title))}</b></span><b class="tg-ctitle">${esc(d.title)}</b></header>
      <div class="tg-msgs" tabindex="0"></div>
      <div class="tg-replybar" hidden></div>
      <div class="tg-pending" hidden></div>
      <footer class="tg-composer">
        <button class="tg-icon" data-act="attach" title="Attach files" aria-label="Attach files">📎</button>
        <button class="tg-icon" data-act="camera" title="Camera: photo or video" aria-label="Camera">📷</button>
        <textarea class="tg-in tg-text" rows="1" placeholder="Message" aria-label="Message"></textarea>
        <button class="tg-btn tg-primary tg-send" data-act="send" aria-label="Send">➤</button>
        <input type="file" class="tg-file" multiple hidden></footer>`;
    loadAvatars(pane);
    pane.querySelector('[data-act="back"]').onclick = () => { st.open = null; paintDialogs(); paintChat(); };
    const ta = pane.querySelector('.tg-text');
    ta.addEventListener('keydown', e => { if(e.key === 'Enter' && !e.shiftKey && !e.isComposing){ e.preventDefault(); send(); } });
    ta.addEventListener('input', () => { ta.style.height = 'auto'; ta.style.height = Math.min(160, ta.scrollHeight) + 'px'; });
    pane.querySelector('[data-act="send"]').onclick = send;
    const file = pane.querySelector('.tg-file');
    pane.querySelector('[data-act="attach"]').onclick = () => file.click();
    file.onchange = () => { for(const f of file.files) st.pending.push({ file:f, name:f.name }); file.value = ''; paintPending(); };
    pane.querySelector('[data-act="camera"]').onclick = openCamera;
    const list = pane.querySelector('.tg-msgs');
    list.addEventListener('scroll', () => { if(list.scrollTop < 60) loadOlder(); });
    paintMessages(true); paintPending(); paintReply();
  }

  async function openChat(id){
    st.open = id; st.reply = null; st.pending = [];
    paintDialogs(); paintChat();
    if(!st.msgs.has(id)){
      try{ st.msgs.set(id, (await api('/api/tgc/messages/' + id + '?limit=50')).messages || []); }
      catch(e){ const l = root.querySelector('.tg-msgs'); if(l) l.innerHTML = `<div class="tg-err tg-pad">${esc(e.message)}</div>`; return; }
      if(st.open !== id) return;
      paintMessages(true);
    }
    const d = st.dialogs.find(x => x.id === id); if(d){ d.unread = 0; paintDialogs(); }
    markRead();
  }
  function markRead(){
    const list = st.msgs.get(st.open) || []; const last = list[list.length - 1];
    if(last) post('/api/tgc/read', { chat_id:st.open, max_id:last.id }).catch(() => {});
  }
  async function loadOlder(){
    const list = st.msgs.get(st.open); if(!list || !list.length || st.loadingOlder || st.done.has(st.open)) return;
    st.loadingOlder = true; const id = st.open, box = root.querySelector('.tg-msgs'), before = box.scrollHeight;
    try{
      const older = (await api('/api/tgc/messages/' + id + '?limit=50&before=' + list[0].id)).messages || [];
      if(!older.length) st.done.add(id);
      if(st.open === id){ st.msgs.set(id, [...older, ...list]); paintMessages(false); box.scrollTop = box.scrollHeight - before; }
    }catch(_){} finally{ st.loadingOlder = false; }
  }

  function mediaHtml(m){
    const md = m.media; if(!md) return '';
    const u = mediaUrl(m.chat_id, m.id, false), t = mediaUrl(m.chat_id, m.id, true);
    if(md.kind === 'photo' || md.kind === 'sticker') return `<a class="tg-media" href="${esc(u)}" target="_blank" rel="noopener" data-full="${esc(u)}"><img src="${esc(t)}" data-src="${esc(u)}" alt="Photo" loading="lazy"></a>`;
    if(md.kind === 'video' || md.kind === 'gif' || md.kind === 'video_note')
      return `<video class="tg-media${md.kind === 'video_note' ? ' tg-round' : ''}" controls preload="none" poster="${esc(t)}" src="${esc(u)}"${md.kind === 'gif' ? ' loop muted autoplay playsinline' : ''}></video>`;
    if(md.kind === 'voice' || md.kind === 'audio') return `<audio class="tg-audio" controls preload="none" src="${esc(u)}"></audio>`;
    return `<a class="tg-file-chip" href="${esc(u)}" download="${esc(md.name || 'file')}">📄 <span>${esc(md.name || 'File')}</span><small>${esc(size(md.size))}</small></a>`;
  }
  function paintMessages(toBottom){
    const box = root && root.querySelector('.tg-msgs'); if(!box) return;
    const list = st.msgs.get(st.open);
    if(!list){ box.innerHTML = '<div class="tg-muted tg-pad">Loading…</div>'; return; }
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    const byId = new Map(list.map(m => [m.id, m])); let day = '';
    box.innerHTML = list.map(m => {
      const d = new Date(m.date * 1000).toDateString(), sep = d !== day ? (day = d, `<div class="tg-day"><span>${esc(new Date(m.date*1000).toLocaleDateString([], {weekday:'short', month:'short', day:'numeric'}))}</span></div>`) : '';
      const r = m.reply_to && byId.get(m.reply_to);
      return sep + `<div class="tg-msg${m.out ? ' out' : ''}" data-id="${m.id}">
        ${!m.out && m.sender ? `<b class="tg-from">${esc(m.sender)}</b>` : ''}
        ${r ? `<div class="tg-quote">${esc((r.text || '[attachment]').slice(0, 140))}</div>` : ''}
        ${mediaHtml(m)}${m.text ? `<div class="tg-body">${linkify(m.text)}</div>` : ''}
        <span class="tg-meta">${m.edited ? 'edited · ' : ''}${esc(new Date(m.date * 1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}))}
          <button class="tg-mini" data-reply="${m.id}" aria-label="Reply">↩</button></span></div>`;
    }).join('') || '<div class="tg-muted tg-pad">No messages yet.</div>';
    box.querySelectorAll('[data-reply]').forEach(b => b.onclick = () => { st.reply = byId.get(Number(b.dataset.reply)); paintReply();
      const ta = root.querySelector('.tg-text'); if(ta) ta.focus(); });
    box.querySelectorAll('a.tg-media').forEach(a => a.onclick = e => { const P = PC(); if(P.openLightbox){ e.preventDefault(); P.openLightbox(a.dataset.full); } });
    if(toBottom || atBottom) box.scrollTop = box.scrollHeight;
  }
  function linkify(t){
    return esc(t).replace(/(https?:\/\/[^\s<]+)/g, u => `<a href="${u}" target="_blank" rel="noopener noreferrer">${u}</a>`).replace(/\n/g, '<br>');
  }
  function paintReply(){
    const bar = root && root.querySelector('.tg-replybar'); if(!bar) return;
    bar.hidden = !st.reply;
    if(st.reply){ bar.innerHTML = `<span>↩ ${esc((st.reply.text || '[attachment]').slice(0, 120))}</span><button class="tg-mini" aria-label="Cancel reply">✕</button>`;
      bar.querySelector('button').onclick = () => { st.reply = null; paintReply(); }; }
  }
  function paintPending(){
    const box = root && root.querySelector('.tg-pending'); if(!box) return;
    box.hidden = !st.pending.length;
    box.innerHTML = st.pending.map((p, i) => `<span class="tg-chip">${esc(p.name)} <small>${esc(size(p.file.size))}</small>
      <button class="tg-mini" data-drop="${i}" aria-label="Remove ${esc(p.name)}">✕</button></span>`).join('');
    box.querySelectorAll('[data-drop]').forEach(b => b.onclick = () => { st.pending.splice(Number(b.dataset.drop), 1); paintPending(); });
  }

  async function send(){
    if(st.busy) return;
    const ta = root.querySelector('.tg-text'), text = ta.value, chat = st.open, reply = st.reply ? st.reply.id : 0;
    const files = st.pending.slice();
    if(!text.trim() && !files.length) return;
    st.busy = true; const btn = root.querySelector('.tg-send'); if(btn) btn.disabled = true;
    try{
      if(files.length){
        for(let i = 0; i < files.length; i++){
          const fd = new FormData(); fd.append('chat_id', String(chat)); fd.append('file', files[i].file, files[i].name);
          fd.append('mode', files[i].mode || 'auto'); fd.append('caption', i === files.length - 1 ? text : ''); fd.append('reply_to', String(reply));
          const r = await api('/api/tgc/send-file', { method:'POST', body:fd }); addMine(r.message);
        }
      } else addMine((await post('/api/tgc/send', { chat_id:chat, text, reply_to:reply })).message);
      ta.value = ''; ta.style.height = 'auto'; st.pending = []; st.reply = null; paintPending(); paintReply();
    }catch(e){ const P = PC(); if(P.toast) P.toast('Telegram: ' + e.message); }
    finally{ st.busy = false; if(btn) btn.disabled = false; }
  }
  function addMine(m){
    if(!m) return; const list = st.msgs.get(m.chat_id) || []; if(!list.some(x => x.id === m.id)) list.push(m);
    st.msgs.set(m.chat_id, list); if(st.open === m.chat_id) paintMessages(true);
  }

  // ---- camera: a photo, or a video message ------------------------------------------------------------
  async function openCamera(){
    const P = PC(); if(!P.modal){ return; }
    let stream;
    try{ stream = await navigator.mediaDevices.getUserMedia({ video:{ width:{ideal:1280}, height:{ideal:720} }, audio:true }); }
    catch(e){ if(P.toast) P.toast('Camera unavailable: ' + (e && e.message || e)); return; }
    let rec = null, chunks = [];
    const stop = () => { try{ stream.getTracks().forEach(t => t.stop()); }catch(_){} };
    P.modal(`<div class="tg-cam"><video class="tg-cam-v" autoplay playsinline muted></video>
      <div class="tg-cam-acts"><button class="tg-btn" data-cam="photo">📸 Photo</button>
        <button class="tg-btn" data-cam="rec">⏺ Record video</button><button class="tg-btn" data-cam="close">Cancel</button></div></div>`, box => {
      const v = box.querySelector('video'); v.srcObject = stream;
      const done = () => { stop(); if(P.closeModal) P.closeModal(); };
      box.querySelector('[data-cam="close"]').onclick = () => { if(rec && rec.state === 'recording'){ rec.onstop = null; rec.stop(); } done(); };
      box.querySelector('[data-cam="photo"]').onclick = () => {
        const c = document.createElement('canvas'); c.width = v.videoWidth || 1280; c.height = v.videoHeight || 720;
        c.getContext('2d').drawImage(v, 0, 0, c.width, c.height);
        c.toBlob(b => { if(b){ st.pending.push({ file:new File([b], 'photo.jpg', {type:'image/jpeg'}), name:'photo.jpg' }); paintPending(); } done(); }, 'image/jpeg', 0.9);
      };
      const rb = box.querySelector('[data-cam="rec"]');
      rb.onclick = () => {
        if(rec && rec.state === 'recording'){ rec.stop(); return; }
        const type = ['video/webm;codecs=vp9,opus', 'video/webm', 'video/mp4'].find(t => window.MediaRecorder && MediaRecorder.isTypeSupported(t)) || '';
        try{ rec = new MediaRecorder(stream, type ? { mimeType:type } : undefined); }catch(e){ if(P.toast) P.toast('Recording is not supported here'); return; }
        chunks = []; rec.ondataavailable = e => { if(e.data && e.data.size) chunks.push(e.data); };
        rec.onstop = () => { const ext = (rec.mimeType || '').includes('mp4') ? 'mp4' : 'webm';
          const blob = new Blob(chunks, { type:rec.mimeType || 'video/webm' });
          st.pending.push({ file:new File([blob], 'video.' + ext, {type:blob.type}), name:'video.' + ext }); paintPending(); done(); };
        rec.start(); rb.textContent = '⏹ Stop'; rb.classList.add('tg-live');
      };
    });
  }

  // ---- background notifier ----------------------------------------------------------------------------
  async function background(){
    try{ if(window.PCOSWin && PCOSWin.isWindow()) return; }catch(_){}
    if(isPhone()) return;
    const P = PC();
    try{ if(P.standalone && P.standalone()) return; }catch(_){}
    try{ st.status = await api('/api/tgc/status'); }catch(_){ return; }
    if(wanted()) connect();
  }
  if(typeof document !== 'undefined'){
    // Phones: every entry point (sidebar, More sheet, the desktop's icons built from the sidebar)
    // is hidden by one root class, so none of them can drift out of step.
    const gate = () => { try{ document.documentElement.classList.toggle('pc-no-tg', isPhone()); }catch(_){} };
    gate();
    // Re-checked on resize: a desktop window dragged down to phone size, or a phone rotated, is
    // answered by the SAME rule rather than whatever it was when the page loaded.
    try{ window.addEventListener('resize', gate); }catch(_){}
    const go = () => setTimeout(background, 4000);
    if(window.__PC_BOOTED) go(); else document.addEventListener('pc-app-ready', go, { once:true });
  }

  const api_ = { render, isPhone, onEvent, _state:st };
  window.PCTelegram = api_;
  if(typeof module !== 'undefined') module.exports = api_;
})();
