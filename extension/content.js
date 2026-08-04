/* PosterChan Passwords — the page half.
 *
 * Finds login fields, offers to fill them, and offers to save a new one after a successful submit.
 *
 * IT NEVER ASKS FOR A PASSWORD UNPROMPTED. The background hands this script a password only in
 * response to a click the user made on the PosterChan badge or in the popup — never on page load,
 * never on a timer. A content script shares a world with the page it runs in, so anything it holds
 * is one XSS away from the site; holding nothing until asked is the difference between "a bug on
 * that site leaked the password you used there" and "…leaked your vault".
 *
 * IT DOES NOT FILL ON A DOMAIN MATCH ALONE. matchesFor ranks an exact host above a same-registrable-
 * domain one, and this only auto-offers the exact ones; a domain match has to be picked by hand from
 * the list. `paypal.com.evil.com` is not a match at all — see the tests in tests/test_vault_core.py.
 */
'use strict';

(function(){
  const B = (typeof browser !== 'undefined') ? browser : chrome;
  if(window.__pcPwLoaded) return;
  window.__pcPwLoaded = true;

  const PW_SEL = 'input[type="password"]:not([disabled]):not([readonly])';
  const USER_HINT = /user|email|login|account|identifier|phone|mobile/i;
  const OTP_HINT = /otp|totp|2fa|two.?factor|one.?time|auth.*code|verification/i;

  let badge = null, panel = null, activeField = null, panelGen = 0;
  /* The pending hide, and whether the panel is open.
   *
   * THE DISAPPEARING BUG: focusout scheduled a hide on a timer, and clicking the badge inside that
   * window opened the panel which the stale timer then closed a moment later — so the panel flashed
   * and vanished, seemingly at random, depending on how quickly you clicked. Worse on touch, where
   * the `:hover` escape hatch the old guard relied on does not exist at all, so every tap on Firefox
   * for Android raced it.
   *
   * Now: any interaction with our own UI cancels a pending hide, and once the panel is OPEN it is
   * closed only by something deliberate — a click outside it, Escape, a fill, or the field going
   * away. Never by a timer. */
  let hideT = null, panelOpen = false;
  const cancelHide = () => { clearTimeout(hideT); hideT = null; };

  const visible = (el) => {
    if(!el || !el.isConnected) return false;
    const r = el.getBoundingClientRect();
    return r.width > 20 && r.height > 8 && getComputedStyle(el).visibility !== 'hidden';
  };

  /* The username field for a password field: the nearest preceding text-ish input inside the same
   * form. "The first text input on the page" is wrong on any page with a search box above the form,
   * which is most of them. */
  function userFieldFor(pw){
    const scope = pw.form || document;
    const cands = Array.from(scope.querySelectorAll(
      'input[type="text"],input[type="email"],input[type="tel"],input:not([type])'))
      .filter(visible)
      .filter(el => el.compareDocumentPosition(pw) & Node.DOCUMENT_POSITION_FOLLOWING);
    if(!cands.length) return null;
    const hinted = cands.filter(el =>
      USER_HINT.test((el.name||'') + ' ' + (el.id||'') + ' ' + (el.autocomplete||'') + ' ' +
                     (el.placeholder||'') + ' ' + (el.getAttribute('aria-label')||'')));
    // The CLOSEST one above the password field: last in document order among the candidates. A
    // hinted field wins over an unhinted one, but only among fields that are already above it.
    const pick = hinted.length ? hinted : cands;
    return pick[pick.length - 1] || null;
  }

  const OTP_TYPES = 'input[type="text"],input[type="tel"],input[type="number"],input:not([type])';

  /* Is this THE one-time-code box?
   *
   * `autocomplete="one-time-code"` is the standard answer and is trusted outright. Failing that,
   * the usual words in a name/id/label — and finally the shape every 2FA form has and nothing else
   * does: a short numeric field. maxlength 1 is the six-separate-boxes pattern; 4-8 is the single
   * box. The shape rule is last because on its own it would also match a postcode or a PIN. */
  function isOtpField(el){
    if(!el || el.tagName !== 'INPUT' || el.type === 'password') return false;
    if(!el.matches(OTP_TYPES)) return false;
    const ac = (el.autocomplete || '').toLowerCase();
    if(ac.includes('one-time-code')) return true;
    const hay = (el.name||'') + ' ' + (el.id||'') + ' ' + ac + ' ' + (el.placeholder||'') + ' ' +
                (el.getAttribute('aria-label')||'') + ' ' + (el.className||'');
    if(OTP_HINT.test(hay)) return true;
    const len = parseInt(el.getAttribute('maxlength') || '0', 10);
    const numeric = (el.inputMode || '').toLowerCase() === 'numeric' || el.type === 'tel' ||
                    el.type === 'number' || /^[\d\s*]*$/.test(el.pattern || '');
    return numeric && (len === 1 || (len >= 4 && len <= 8));
  }

  function otpFieldFor(root){
    return Array.from((root || document).querySelectorAll(OTP_TYPES)).filter(visible).find(isOtpField);
  }

  /* Every box of a split code entry, in order. Sites that render six single-character inputs are
   * common enough that filling only the first — which is what writing to one field does — looks
   * exactly like the feature being broken. */
  function otpGroupFor(el){
    if(!el) return [];
    const scope = el.form || el.closest('div,section,fieldset,form') || document;
    const all = Array.from(scope.querySelectorAll(OTP_TYPES)).filter(visible).filter(isOtpField);
    const singles = all.filter(x => parseInt(x.getAttribute('maxlength') || '0', 10) === 1);
    return (singles.length >= 4 && singles.includes(el)) ? singles : [el];
  }

  /* Put a code in. One box, or spread a digit per box. */
  function fillCodeInto(field, code){
    const group = otpGroupFor(field);
    if(group.length > 1){
      const digits = String(code || '').replace(/\D/g, '');
      group.forEach((box, i) => setValue(box, digits[i] || ''));
      const last = group[Math.min(digits.length, group.length) - 1];
      if(last) try{ last.focus(); }catch(_){ }
      return true;
    }
    setValue(field, code);
    return true;
  }

  /* Set a value the way a human would, not the way a script does. React and friends listen for
   * `input`/`change` and re-render from their own state, so a bare `el.value = x` is visibly
   * reverted the moment you click Submit — the classic "it filled and then it didn't" bug. The
   * native setter plus the two events is what actually sticks. */
  function setValue(el, value){
    if(!el) return;
    const proto = Object.getPrototypeOf(el);
    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
    if(desc && desc.set) desc.set.call(el, value); else el.value = value;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }

  // ---------------------------------------------------------------- badge

  function ensureBadge(field){
    cancelHide();
    activeField = field;
    if(!badge){
      badge = document.createElement('div');
      badge.className = 'pcpw-badge';
      badge.title = 'PosterChan Passwords';
      badge.textContent = '🔑';
      // pointerdown, not mousedown: on a touch screen mousedown is synthesised late (or not at all
      // before the tap is treated as a scroll), which is half of why this felt unreliable on a phone.
      badge.addEventListener('pointerdown', (e) => {
        e.preventDefault(); e.stopPropagation(); cancelHide(); togglePanel();
      });
      document.documentElement.appendChild(badge);
    }
    place();
  }
  function place(){
    if(!badge) return;
    // Re-acquire a field that was replaced under us. Login screens re-render constantly — the app's
    // own nsec box lives inside a <details> that is toggled, and a framework can swap the input on
    // any keystroke — and treating "the element I remembered is gone" as "the user is finished"
    // is what made the panel vanish mid-use.
    if(activeField && !activeField.isConnected){
      const again = document.querySelector(PW_SEL);
      if(again) activeField = again;
    }
    if(!activeField || !visible(activeField)){
      // An OPEN panel is not taken away because a measurement failed for a frame; only the badge is.
      if(panelOpen) return;
      hideAll(); return;
    }
    const r = activeField.getBoundingClientRect();
    badge.style.top = (window.scrollY + r.top + (r.height - 20) / 2) + 'px';
    badge.style.left = (window.scrollX + r.right - 26) + 'px';
    badge.style.display = 'block';
    if(panel && panel.style.display === 'block'){
      panel.style.top = (window.scrollY + r.bottom + 6) + 'px';
      panel.style.left = (window.scrollX + Math.max(4, r.right - 300)) + 'px';
    }
  }
  function hideAll(){
    cancelHide();
    panelOpen = false;
    if(badge) badge.style.display = 'none';
    if(panel) panel.style.display = 'none';
  }
  function closePanel(){
    panelOpen = false;
    if(panel) panel.style.display = 'none';
  }

  async function togglePanel(){
    cancelHide();
    if(panelOpen){ closePanel(); return; }
    panelOpen = true;
    if(!panel){
      panel = document.createElement('div');
      panel.className = 'pcpw-panel';
      document.documentElement.appendChild(panel);
    }
    panel.innerHTML = '<div class="pcpw-row pcpw-muted">looking…</div>';
    panel.style.display = 'block';
    place();
    // The lookup is async; the user may have closed it again by the time it lands.
    const gen = ++panelGen;
    let res;
    try{ res = await B.runtime.sendMessage({ type:'matches', url: location.href }); }
    catch(_){ res = null; }
    if(gen !== panelGen || !panelOpen) return;          // closed while we were asking
    let list = (res && res.items) || [];
    // The entry whose password was just filled goes first on the code step: it is nearly always the
    // one wanted, and on a site with several accounts it is the only way to know which.
    const recent = recentlyUsed();
    if(recent) list = list.slice().sort((a, b) => (b.id === recent) - (a.id === recent));
    if(!list.length){
      panel.innerHTML = '<div class="pcpw-row pcpw-muted">No saved logins for this site — ' +
                        'open the PosterChan button to search your vault.</div>';
      return;
    }
    // On a code field, only entries that HAVE a code are any use, and the action is the code.
    const codeMode = isOtpField(activeField);
    const usable = codeMode ? list.filter(i => i.hasTotp) : list;
    if(codeMode && !usable.length){
      panel.innerHTML = '<div class="pcpw-row pcpw-muted">No one-time code saved for this site.</div>';
      return;
    }
    panel.innerHTML = usable.map(i =>
      `<button class="pcpw-item" data-id="${esc(i.id)}">
         <b>${codeMode ? 'Fill the code' : esc(i.username || i.title || 'no username')}</b>
         <span>${esc(i.title || '')}${!codeMode && i._match === 'domain' ? ' · same domain' : ''}${
           !codeMode && i.hasTotp ? ' · 2FA' : ''}${codeMode ? ' · ' + esc(i.username || '') : ''}</span>
       </button>`).join('');
    panel.querySelectorAll('.pcpw-item').forEach(b => {
      b.addEventListener('pointerdown', async (e) => {
        e.preventDefault(); e.stopPropagation(); cancelHide();
        if(codeMode) await fillCode(b.dataset.id); else await fill(b.dataset.id);
        closePanel();
      });
    });
  }

  async function fill(id){
    let r;
    try{ r = await B.runtime.sendMessage({ type:'fill', id }); }catch(_){ return; }
    if(!r || !r.ok) return;
    const pw = activeField && activeField.type === 'password'
      ? activeField
      : document.querySelector(PW_SEL);
    if(pw){
      setValue(pw, r.password);
      const u = userFieldFor(pw);
      if(u && r.username) setValue(u, r.username);
    } else if(activeField){
      setValue(activeField, r.username || '');
    }
    // A code on the SAME page (some sites ask for all three at once) goes in now. A code on the
    // next page is handled when we get there — see fillCode and recentlyUsed.
    if(r.totp){
      const otp = otpFieldFor((pw && pw.form) || document);
      if(otp) fillCodeInto(otp, r.totp);
    }
    lastUsed = { id, at: Date.now() };
  }

  /* The code for an entry, into the field the user is standing in. Fetched at the moment of the
   * click and never before: a TOTP is only valid for its window, so a code obtained when the page
   * loaded is one that may already have expired by the time it is used. */
  async function fillCode(id){
    let r;
    try{ r = await B.runtime.sendMessage({ type:'fill', id }); }catch(_){ return; }
    if(!r || !r.ok || !r.totp) return;
    const field = (activeField && isOtpField(activeField)) ? activeField : otpFieldFor(document);
    if(field) fillCodeInto(field, r.totp);
    lastUsed = { id, at: Date.now() };
  }

  /* Which entry filled this page's password, and when. The code step is a different page, so the
   * only way to offer the right entry there without asking again is to remember the choice — for a
   * few minutes, which is far longer than a login takes and far shorter than a session. */
  let lastUsed = null;
  const recentlyUsed = () => (lastUsed && (Date.now() - lastUsed.at) < 5 * 60000) ? lastUsed.id : null;

  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  // ---------------------------------------------------------------- wiring

  /* The popup drives the page through here: it has the tab, this has the fields. Both entry points
   * are the result of a click the user just made in the popup — nothing fills on its own. */
  B.runtime.onMessage.addListener((msg) => {
    if(!msg) return;
    if(msg.type === 'pcpw-fill'){ fill(msg.id); return; }
    if(msg.type === 'pcpw-set-password'){
      const pw = (activeField && activeField.type === 'password') ? activeField : document.querySelector(PW_SEL);
      if(pw && msg.value){
        setValue(pw, msg.value);
        // Sites that ask twice ("confirm password") get both, or the form fails validation and the
        // generated password is lost with it.
        const all = Array.from((pw.form || document).querySelectorAll(PW_SEL)).filter(visible);
        for(const f of all) if(f !== pw && !f.value) setValue(f, msg.value);
      }
    }
  });

  document.addEventListener('focusin', (e) => {
    const el = e.target;
    if(!el || el.tagName !== 'INPUT') return;
    // A password field, anything sharing a form with one, or a one-time-code box on its own — the
    // SECOND step of a login, which has no password field anywhere and so never got a badge at all.
    // That is where a code is actually needed, and it was the one place the add-on was silent.
    if(el.type === 'password' || (el.form && el.form.querySelector(PW_SEL)) || isOtpField(el))
      ensureBadge(el);
  }, true);
  document.addEventListener('focusout', () => {
    // Only ever hides the BADGE, and only while the panel is shut. An open panel is a thing the user
    // asked for and is looking at; taking it away on a timer is what made this feel broken.
    if(panelOpen) return;
    cancelHide();
    hideT = setTimeout(() => { hideT = null; if(!panelOpen) hideAll(); }, 250);
  }, true);
  // Deliberate ways to close it.
  document.addEventListener('pointerdown', (e) => {
    if(!panelOpen) return;
    if(e.target === badge || (panel && panel.contains(e.target))) return;
    closePanel();
  }, true);
  document.addEventListener('keydown', (e) => { if(e.key === 'Escape' && panelOpen) closePanel(); }, true);
  window.addEventListener('scroll', place, true);
  window.addEventListener('resize', place);

  /* Offer to save what was just typed. On submit, not on every keystroke: the value at submit time
   * is the one that was actually used, and a manager that saves drafts fills your vault with typos.
   * The offer is a bar, never an automatic write — this extension does not put things in your vault
   * without being told to. */
  document.addEventListener('submit', (e) => {
    try{
      const form = e.target;
      if(!form || !form.querySelector) return;
      const pw = form.querySelector('input[type="password"]');
      if(!pw || !pw.value) return;
      const u = userFieldFor(pw);
      offerSave({ username: (u && u.value) || '', password: pw.value });
    }catch(_){ }
  }, true);

  async function offerSave(cred){
    /* Ask the background whether it already holds this, sending the credential UP rather than
     * pulling one down: this script shares a world with the page, and a password it never receives
     * is one the page can never take. Same username AND password → nothing to ask. Same username,
     * different password → a rotation, which is the most important thing of all to capture: miss it
     * and the vault quietly fills the old one on every later visit. */
    let res;
    try{ res = await B.runtime.sendMessage({ type:'known', url: location.href,
                                             username: cred.username, password: cred.password }); }
    catch(_){ return; }
    if(!res || res.known) return;
    const rotating = !!res.rotating;
    const same = res.id ? [{ id: res.id }] : [];
    const bar = document.createElement('div');
    bar.className = 'pcpw-savebar';
    bar.innerHTML = `<span>${rotating ? 'Update this password in PosterChan?'
                                       : 'Save this login to PosterChan?'}</span>
      <button class="pcpw-yes">${rotating ? 'Update' : 'Save'}</button><button class="pcpw-no">No</button>`;
    document.documentElement.appendChild(bar);
    const close = () => bar.remove();
    bar.querySelector('.pcpw-no').onclick = close;
    bar.querySelector('.pcpw-yes').onclick = async () => {
      const r = await B.runtime.sendMessage({ type:'save', item: {
        id: rotating ? same[0].id : undefined,     // update in place, never a second copy
        kind:'login', title: location.hostname.replace(/^www\./,''),
        username: cred.username, password: cred.password,
        uris: [location.origin], totp:'', notes:'', tags:[], folder:'' } });
      bar.innerHTML = `<span>${r && r.queued
        ? 'Saved here — it will sync when you next open the PosterChan app.'
        : 'Saved.'}</span><button class="pcpw-no">OK</button>`;
      bar.querySelector('.pcpw-no').onclick = close;
      setTimeout(close, 6000);
    };
    setTimeout(() => { if(bar.isConnected) close(); }, 20000);
  }
})();
