/* PCContactCard — one contact, shown (not edited). Loaded the first time a contact is tapped
 * (contacts.js openCard), so the list costs nothing more and the boot payload carries none of it.
 *
 * DISPLAY ONLY. It reads the card PCVcard parsed and never writes; Edit hands the same card to
 * contacts.js's editor, whose Save reloads and repaints this view with the stored values.
 * Fields this app has no editor for (URL, NICKNAME, ANNIVERSARY, IMPP, Apple's grouped labels) are
 * read from `card.other`, the lines vcard.js carries through verbatim.
 */
(function(){
  const CSS = `
.ctc a{text-decoration:none}
.ctc{max-width:640px;margin:0 auto;padding:0 8px calc(40px + env(safe-area-inset-bottom))}
.ctc-bar{position:sticky;top:-8px;z-index:5;background:var(--canvas);background-attachment:fixed;
  display:flex;align-items:center;gap:8px;padding:10px 2px 8px}
.ctc-bar .btn{display:inline-flex;align-items:center;gap:6px;min-height:44px;min-width:44px;justify-content:center;margin:0}
.ctc-bart{flex:1 1 auto;min-width:0;font-weight:700;color:var(--muted);font-size:13px;letter-spacing:.06em;text-transform:uppercase}
.ctc-hero{text-align:center;padding:14px 8px 18px}
.ctc-face{width:112px;height:112px;border-radius:999px;object-fit:cover;display:flex;align-items:center;
  justify-content:center;margin:0 auto 12px;font-style:normal;font-weight:800;font-size:40px;color:var(--text);
  background:var(--panel2);border:2px solid var(--neon);box-shadow:0 0 18px rgba(var(--accent-rgb),.45)}
.ctc-name{font-size:24px;font-weight:800;margin:0;overflow-wrap:anywhere}
.ctc-org{color:var(--muted);margin-top:4px;overflow-wrap:anywhere}
.ctc-quick{display:flex;justify-content:center;gap:14px;margin-top:16px;flex-wrap:wrap}
.ctc .ctc-quick .btn{display:inline-flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;min-width:76px;min-height:64px;font-size:12px;border-radius:14px;margin:0}
.ctc-quick .ic{width:22px;height:22px}
.ctc-sec{border:1px solid var(--line);border-radius:14px;background:var(--panel);margin:12px 0;overflow:hidden}
.ctc-sh{font-size:11px;font-weight:800;letter-spacing:.1em;text-transform:uppercase;color:var(--neon);padding:10px 14px 2px}
.ctc-row{display:flex;align-items:center;gap:6px;padding:6px 8px 6px 14px;min-height:56px}
.ctc-row+.ctc-row{border-top:1px solid var(--line)}
.ctc-val{flex:1 1 auto;min-width:0;text-align:start;background:none;border:0;color:inherit;padding:6px 0;
  cursor:pointer;display:flex;flex-direction:column;gap:2px;font:inherit}
.ctc-v{font-size:16px;overflow-wrap:anywhere;white-space:pre-line}
.ctc-l{font-size:12px;color:var(--muted);text-transform:capitalize}
.ctc-act{flex:0 0 auto;width:44px;height:44px;border-radius:999px;display:flex;align-items:center;justify-content:center;
  background:var(--panel2);border:1px solid var(--line);color:var(--neon);cursor:pointer;padding:0}
.ctc-act:hover,.ctc-act:focus-visible{border-color:var(--neon);box-shadow:0 0 10px rgba(var(--accent-rgb),.5)}
.ctc-act .ic{width:20px;height:20px}
@media(min-width:821px){.ctc-hero{display:grid;grid-template-columns:auto 1fr;gap:18px;text-align:start;align-items:center}
  .ctc-face{margin:0}.ctc-quick{justify-content:flex-start;grid-column:2}}`;

  const ic = n => `<svg class="ic" aria-hidden="true"><use href="#i-${n}"></use></svg>`;
  const WORDS = { cell:'mobile', mobile:'mobile', home:'home', work:'work', fax:'fax', pager:'pager',
                  main:'main', iphone:'iPhone', other:'other', text:'text', video:'video' };

  /* One preserved line → {group, name, params, value}. */
  function line(raw){
    const i = raw.indexOf(':'); if(i < 0) return null;
    const bits = raw.slice(0, i).split(';');
    let name = bits[0], group = '';
    const d = name.indexOf('.'); if(d > 0){ group = name.slice(0, d); name = name.slice(d + 1); }
    const params = {};
    for(const p of bits.slice(1)){ const j = p.indexOf('='); if(j > 0) params[p.slice(0, j).toUpperCase()] = p.slice(j + 1).replace(/^"|"$/g, ''); }
    return { group: group.toLowerCase(), name: name.toUpperCase(), params, value: raw.slice(i + 1) };
  }
  const unesc = s => String(s || '').replace(/\\n/gi, '\n').replace(/\\([,;\\])/g, '$1');

  /* A label: Apple's `itemN.X-ABLABEL` for that group wins (`_$!<Mobile>!$_` is Apple's spelling of a
   * stock one), then the vCard TYPE in words. */
  function labelOf(entry, abl, fallback){
    const g = String(entry.group || '').toLowerCase();
    if(g && abl[g]) return abl[g];
    const t = String(entry.type || '').split(/\s+/).filter(w => w && w !== 'pref' && w !== 'x-pref')
      .map(w => WORDS[w] || w.replace(/^x-/, ''));
    return t.join(' · ') || fallback;
  }

  function render(feed, c, D){
    const PC = window.__PC, { enc } = PC, V = window.PCVcard;
    if(!document.getElementById('pc-ctc-css')){
      const st = document.createElement('style'); st.id = 'pc-ctc-css'; st.textContent = CSS; document.head.appendChild(st);
    }
    const other = (c.other || []).map(line).filter(Boolean);
    const abl = {};
    for(const p of other) if(p.name === 'X-ABLABEL' && p.group)
      abl[p.group] = unesc(p.value).replace(/^_\$!<(.*)>!\$_$/, '$1');
    const pick = (...names) => other.filter(p => names.includes(p.name) && String(p.value).trim());

    const name = V.displayName(c);
    const n = c.n || {};
    const init = (((n.given || c.fn || '').trim()[0] || '') + ((n.family || '').trim()[0] || '')).toUpperCase() || (name.trim()[0] || '?').toUpperCase();
    const nick = pick('NICKNAME').map(p => unesc(p.value))[0] || '';
    const sub = [c.title, c.org].filter(Boolean).join(' · ');
    const tels = (c.tels || []).filter(t => String(t.value || '').trim());
    const emails = (c.emails || []).filter(e => String(e.value || '').trim());
    /* A real tel: link: the APK's WebView hands it to Android (Capacitor launches ACTION_VIEW -> the
     * dialer), a browser to whatever handles calls. Encoded, or a `#` service code is cut at the fragment. */
    const tel = v => enc('tel:' + encodeURIComponent(String(v).trim()));
    const mailOn = () => { const b = document.querySelector('.nav-item[data-view="mail"]'); return !!(b && !b.classList.contains('hidden') && PC.openMailto); };

    const row = (cls, i, value, label, acts) => `<div class="ctc-row">
        <button class="ctc-val ${cls}" data-i="${i}" title="Copy"><span class="ctc-v">${enc(value)}</span>${label ? `<span class="ctc-l">${enc(label)}</span>` : ''}</button>${acts || ''}</div>`;
    const sec = (title, body) => body ? `<div class="ctc-sec"><div class="ctc-sh">${title}</div>${body}</div>` : '';
    const adrText = a => [[a.po, a.ext, a.street].filter(Boolean).join(' '),
                          [a.city, a.region, a.code].filter(Boolean).join(' '), a.country].filter(s => String(s).trim()).join('\n');
    const adrs = (c.adrs || []).map(a => ({ a, t: adrText(a) })).filter(x => x.t);
    const urls = pick('URL', 'X-SOCIALPROFILE').map(p => unesc(p.value).trim());
    const dates = [];
    if(c.bday) dates.push(['birthday', c.bday]);
    for(const p of pick('ANNIVERSARY', 'X-ANNIVERSARY')) dates.push(['anniversary', p.value.trim()]);
    for(const p of pick('X-ABDATE')) dates.push([abl[p.group] || 'date', p.value.trim()]);
    const ims = pick('IMPP', 'X-JABBER', 'X-SKYPE', 'X-AIM').map(p => unesc(p.value).replace(/^[a-z]+:/i, ''));
    const related = pick('RELATED', 'X-ABRELATEDNAMES').map(p => [abl[p.group] || (p.params.TYPE || 'related').toLowerCase(), unesc(p.value)]);
    const fmtDate = s => { const m = /^(\d{4}|--)-?(\d{2})-?(\d{2})/.exec(String(s)); if(!m) return s;
      const d = new Date(Date.UTC(m[1] === '--' ? 2000 : +m[1], +m[2] - 1, +m[3]));
      try{ return d.toLocaleDateString(undefined, Object.assign({ month:'long', day:'numeric', timeZone:'UTC' }, m[1] === '--' ? {} : { year:'numeric' })); }catch(_){ return s; } };

    feed.innerHTML = `<div class="ctc" data-uid="${enc(c.uid)}">
      <div class="ctc-bar">
        <button class="btn btn-ghost small" id="ctc-back" aria-label="Back to contacts">${ic('arrow-left')}</button>
        <span class="ctc-bart">Contact</span>
        <button class="btn btn-cyan small" id="ctc-edit">${ic('pen')}Edit</button>
      </div>
      <div class="ctc-hero">
        ${c.photo ? `<img class="ctc-face" src="${enc(c.photo)}" alt="">` : `<i class="ctc-face ctc-init">${enc(init)}</i>`}
        <div><h2 class="ctc-name">${enc(name)}</h2>
          ${nick ? `<div class="ctc-org">“${enc(nick)}”</div>` : ''}
          ${sub ? `<div class="ctc-org">${enc(sub)}</div>` : ''}</div>
        ${(tels.length || emails.length) ? `<div class="ctc-quick">
          ${tels.length ? `<a class="btn btn-ghost" id="ctc-qcall" href="${tel(tels[0].value)}">${ic('phone')}Call</a><button class="btn btn-ghost" id="ctc-qtext">${ic('chat')}Text</button>` : ''}
          ${emails.length ? `<button class="btn btn-ghost" id="ctc-qmail">${ic('mail')}Email</button>` : ''}</div>` : ''}
      </div>
      ${sec('Phone', tels.map((t, i) => row('ctc-tel', i, t.value, labelOf(t, abl, 'phone'),
          `<a class="ctc-act ctc-call" href="${tel(t.value)}" aria-label="Call ${enc(t.value)}">${ic('phone')}</a><button class="ctc-act ctc-text" data-i="${i}" aria-label="Text ${enc(t.value)}">${ic('chat')}</button>`)).join(''))}
      ${sec('Email', emails.map((e, i) => row('ctc-email', i, e.value, labelOf(e, abl, ''),
          `<button class="ctc-act ctc-mail" data-i="${i}" aria-label="Email ${enc(e.value)}">${ic('mail')}</button>`)).join(''))}
      ${sec('Address', adrs.map((x, i) => row('ctc-adr', i, x.t, labelOf(x.a, abl, ''),
          `<button class="ctc-act ctc-map" data-i="${i}" aria-label="Open in maps">${ic('compass')}</button>`)).join(''))}
      ${sec('Dates', dates.map((d, i) => row('ctc-date', i, fmtDate(d[1]), d[0])).join(''))}
      ${sec('Web', urls.map((u, i) => row('ctc-url', i, u, '', /^https?:\/\//i.test(u)
          ? `<button class="ctc-act ctc-open" data-i="${i}" aria-label="Open ${enc(u)}">${ic('link')}</button>` : '')).join(''))}
      ${sec('Chat', ims.map((m, i) => row('ctc-im', i, m, '')).join(''))}
      ${sec('People', related.map((r, i) => row('ctc-rel', i, r[1], r[0])).join(''))}
      ${c.note ? sec('Notes', row('ctc-note', 0, c.note, '')) : ''}
    </div>`;
    try{ feed.scrollTop = 0; }catch(_){}

    const root = feed.querySelector('.ctc');
    const each = (sel, fn) => root.querySelectorAll(sel).forEach(b => b.onclick = () => fn(+b.dataset.i || 0, b));
    const copy = v => PC.copyValue(String(v), 'copied');
    const go = (href, blank) => { const a = document.createElement('a'); a.href = href;
      if(blank){ a.target = '_blank'; a.rel = 'noopener'; } root.appendChild(a); a.click(); a.remove(); };
    const mail = addr => mailOn() ? PC.openMailto('mailto:' + addr) : go('mailto:' + addr);
    const native = () => !!(window.Capacitor && Capacitor.isNativePlatform && Capacitor.isNativePlatform());
    const maps = t => { const q = encodeURIComponent(t.replace(/\n/g, ', '));
      native() ? go('geo:0,0?q=' + q) : go('https://www.openstreetmap.org/search?query=' + q, 1); };
    root.querySelector('#ctc-back').onclick = () => D.back();
    root.querySelector('#ctc-edit').onclick = () => D.edit(c);
    const q = id => root.querySelector(id);
    if(q('#ctc-qtext')) q('#ctc-qtext').onclick = () => D.text(tels[0].value);
    if(q('#ctc-qmail')) q('#ctc-qmail').onclick = () => mail(emails[0].value);
    each('.ctc-text', i => D.text(tels[i].value));
    each('.ctc-mail', i => mail(emails[i].value));
    each('.ctc-map', i => maps(adrs[i].t));
    each('.ctc-open', i => go(urls[i], 1));
    each('.ctc-tel', i => copy(tels[i].value));
    each('.ctc-email', i => copy(emails[i].value));
    each('.ctc-adr', i => copy(adrs[i].t));
    each('.ctc-date', (i, b) => copy(b.querySelector('.ctc-v').textContent));
    each('.ctc-url', i => copy(urls[i]));
    each('.ctc-im', i => copy(ims[i]));
    each('.ctc-rel', i => copy(related[i][1]));
    each('.ctc-note', () => copy(c.note));
  }

  window.PCContactCard = { render };
})();
