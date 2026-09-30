/* ✨ IN TELEGRAM AND DIRECT MESSAGES -- one menu, three things the node's own AI can do with the chat
 * that is open: Generate reply, Summarize chat, Summarize YouTube & links.
 *
 * Asked for: "AI replies to Telegram and DM's also, make sure mobile UI looks good" and "For Telegram,
 * maybe the AI sparkle button should have a Generate Reply Summarize youtube and links".
 *
 * ONE MODULE FOR BOTH SCREENS, so they cannot drift: each screen hands over what only it knows (the
 * conversation's messages, how to put text in its composer) and this does the rest.
 *
 *   * A REPLY IS NEVER SENT. The drafts open as a menu (the Texts ✨ popup); a pick fills the composer,
 *     and asks first before replacing something the person typed.
 *   * A SUMMARY IS READ, in a sheet with Copy -- it never goes into the composer, and nothing is posted.
 *   * NO BUTTON WITHOUT AI: the server's own probe (the one AI gate) decides, per account, once. A
 *     Nostr-only node or a bundle with no instance never draws it and never asks.
 *   * BUSY IS KEYED ON THE CONVERSATION, on module state -- both screens repaint on every incoming
 *     message, and a latch in the DOM would let a second tap start a second request.
 */
(function(){
  const PC = () => window.__PC || {};
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const URL_RE = /https?:\/\/[^\s<>"'`]+/i;
  let _allowed = null, _for = '', _probe = null, _probeAt = 0;
  const busy = new Set();

  function me(){ const p = PC(); return ((p.me ? p.me() : p.ME) || {}).pubkey || ''; }
  function blocked(){ const p = PC(); return !!window.PC_NOSTR_ONLY || !!(p.standalone && p.standalone()); }
  function shown(){ return !blocked() && _allowed === true && _for === me(); }

  async function call(body){
    const p = PC();
    try{ if(p.ensureAiSession) await p.ensureAiSession(); }catch(_){ }
    const r = await (p.authFetch ? p.authFetch('/api/chat-assist', { method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) })
      : fetch('/api/chat-assist', { method:'POST', credentials:'include',
          headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) }));
    let j = null; try{ j = await r.json(); }catch(_){ }
    return { r, j: j || {} };
  }

  /** Ask once per account whether this node will answer; `onChange` repaints the button. */
  function probe(onChange){
    const who = me();
    if(blocked() || !who){ _allowed = false; _for = who; return Promise.resolve(false); }
    if(_for === who && _allowed !== null) return Promise.resolve(_allowed);
    if(_probe || (_for === who && Date.now() - _probeAt < 30000)) return _probe || Promise.resolve(null);
    _probeAt = Date.now();
    _probe = (async () => {
      try{
        const { r, j } = await call({ probe:true });
        if(me() !== who) return null;
        if(r.ok && typeof j.allowed === 'boolean'){ _allowed = j.allowed; _for = who; }
        else if(r.status === 401 || r.status === 403){ _allowed = false; _for = who; }
        else _for = who;
      }catch(_){ _for = who; }
      finally{ _probe = null; }
      try{ if(onChange) onChange(); }catch(_){ }
      return _allowed;
    })();
    return _probe;
  }

  /* A menu as a promise: the picked key, or '' when it was dismissed (outside tap, Escape, scroll)
     -- openMenuPopover calls back only on a pick, and a promise waiting for that would hold the
     conversation "busy" for ever after a dismissal. The Texts ✨ menu does the same. */
  function menu(anchor, items, cls, label){
    const p = PC();
    return new Promise(resolve => {
      let done = false;
      Promise.resolve(p.openMenuPopover(anchor, items.map(([k, t]) => [k, t, cls]), k => { done = true; resolve(k); }))
        .then(() => {
          const pop = document.querySelector('.menu-pop');
          if(pop){ pop.setAttribute('aria-label', label); pop.classList.add('ca-pop'); }
          const watch = setInterval(() => { if(done) return clearInterval(watch);
            if(!pop || !pop.isConnected){ clearInterval(watch); done = true; resolve(''); } }, 150);
        }, () => { done = true; resolve(''); });
    });
  }

  function sheet(title, bodyHtml, copyText){
    const p = PC();
    if(!p.modal){ if(p.toast) p.toast(copyText || title); return null; }
    let box = null;
    p.modal(`<div class="ca-sheet" role="dialog" aria-label="${esc(title)}">
        <div class="ca-title">✨ ${esc(title)}</div>
        <div class="ca-body">${bodyHtml}</div>
        <div class="ca-actions">
          <button class="btn small ca-copy"${copyText ? '' : ' hidden'}>Copy</button>
          <button class="btn btn-neon small ca-close">Close</button></div></div>`, b => { box = b; });
    if(box){
      const c = box.querySelector('.ca-copy');
      if(c) c.onclick = () => { if(p.copyValue) p.copyValue(box.dataset.copy || '', 'Copied'); };
      box.dataset.copy = copyText || '';
      box.querySelector('.ca-close').onclick = () => { if(p.closeModal) p.closeModal(); };
    }
    return box;
  }
  function fillSheet(box, bodyHtml, copyText){
    if(!box || !box.isConnected) return;
    box.querySelector('.ca-body').innerHTML = bodyHtml;
    box.dataset.copy = copyText || '';
    const c = box.querySelector('.ca-copy'); if(c) c.hidden = !copyText;
  }
  const bullets = text => String(text || '').split(/\n+/).map(l => l.trim()).filter(Boolean)
    .map(l => `<p class="ca-line">${esc(l)}</p>`).join('');
  const safeHref = u => /^https?:\/\//i.test(String(u || '')) ? String(u) : '';

  async function reply(anchor, ctx, key){
    const { r, j } = await call({ action:'reply', medium:ctx.medium, messages:ctx.messages().slice(-10), count:3 });
    if(!r.ok || !j.ok) throw new Error(j.error || ('HTTP ' + r.status));
    const choices = (Array.isArray(j.choices) && j.choices.length ? j.choices : [j.content]).filter(Boolean);
    if(!choices.length) throw new Error('The AI did not come up with a reply — try again.');
    const p = PC();
    // Dismissing the menu picks nothing -- the composer is left exactly as it was.
    const picked = (!p.openMenuPopover || !anchor || !anchor.isConnected) ? choices[0]
      : choices[Number(await menu(anchor, choices.map((c, i) => [String(i), c]), 'ca-choice', 'Suggested replies') || NaN)];
    if(!picked || ctx.key() !== key) return;
    const had = String(ctx.current() || '').trim();
    if(had && had !== picked && p.uiConfirm && !await p.uiConfirm('Replace what you typed with this reply?')) return;
    ctx.fill(picked);
  }

  async function summarize(ctx){
    const box = sheet('Chat summary', '<p class="muted ca-wait">Reading the conversation…</p>', '');
    const { r, j } = await call({ action:'summarize', medium:ctx.medium, messages:ctx.messages() });
    if(!r.ok || !j.ok) return fillSheet(box, `<p class="ca-err">${esc(j.error || ('HTTP ' + r.status))}</p>`, '');
    fillSheet(box, bullets(j.summary), j.summary);
  }

  async function links(ctx){
    const box = sheet('Links & videos', '<p class="muted ca-wait">Reading the links…</p>', '');
    const { r, j } = await call({ action:'links', medium:ctx.medium, messages:ctx.messages() });
    if(!r.ok || !j.ok) return fillSheet(box, `<p class="ca-err">${esc(j.error || ('HTTP ' + r.status))}</p>`, '');
    const list = j.links || [];
    const html = list.map(l => {
      // Only a link that was READ is offered as a link: one the node refused (a private address) or
      // could not open is named with the reason, not made one tap away.
      const href = l.summary ? safeHref(l.url) : '';
      const head = href ? `<a class="ca-link" href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(l.title || l.url)}</a>`
                        : `<b>${esc(l.title || l.url)}</b>`;
      return `<div class="ca-item">${head}${l.summary ? `<p class="ca-line">${esc(l.summary)}</p>`
                                                     : `<p class="ca-err">${esc(l.error || 'Could not read it.')}</p>`}</div>`;
    }).join('');
    const copy = list.filter(l => l.summary).map(l => (l.title || l.url) + '\n' + l.url + '\n' + l.summary).join('\n\n');
    fillSheet(box, html || '<p class="muted">Nothing to show.</p>', copy);
  }

  /**
   * Open the ✨ menu. ctx = { medium:'telegram'|'dm', key:()=>conversationId,
   *   messages:()=>[{me,text,who?}] oldest first, current:()=>composer text, fill:text=>void }
   */
  async function open(anchor, ctx){
    const p = PC(), key = ctx.key();
    if(busy.has(key)) return;
    const msgs = ctx.messages();
    if(!msgs.length){ if(p.toast) p.toast('There is nothing in this chat yet.'); return; }
    const items = [['reply', '✍️ Generate reply'], ['summarize', '📝 Summarize chat']];
    if(msgs.slice(-40).some(m => URL_RE.test(String(m.text || '')))) items.push(['links', '▶️ Summarize YouTube & links']);
    const what = p.openMenuPopover ? await menu(anchor, items, 'ca-item-menu', 'AI') : 'reply';
    if(!what || ctx.key() !== key) return;
    busy.add(key);
    if(anchor){ anchor.disabled = true; anchor.setAttribute('aria-busy', 'true'); }
    try{
      if(what === 'reply') await reply(anchor, ctx, key);
      else if(what === 'summarize') await summarize(ctx);
      else if(what === 'links') await links(ctx);
    }catch(e){ if(p.toast) p.toast((e && e.message) || 'The AI did not answer — try again in a moment.'); }
    finally{
      busy.delete(key);
      if(anchor && anchor.isConnected){ anchor.disabled = false; anchor.setAttribute('aria-busy', 'false'); }
    }
  }

  window.PCChatAssist = { probe, shown, open, isBusy: k => busy.has(k),
                          _reset(){ _allowed = null; _for = ''; _probe = null; _probeAt = 0; busy.clear(); } };
})();
