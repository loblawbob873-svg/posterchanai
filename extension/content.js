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

  let badge = null, panel = null, activeField = null;

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

  function otpFieldFor(root){
    return Array.from((root || document).querySelectorAll('input[type="text"],input[type="tel"],input[type="number"],input:not([type])'))
      .filter(visible)
      .find(el => OTP_HINT.test((el.name||'') + ' ' + (el.id||'') + ' ' + (el.autocomplete||'') + ' ' +
                                (el.placeholder||'') + ' ' + (el.getAttribute('aria-label')||'')));
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
    activeField = field;
    if(!badge){
      badge = document.createElement('div');
      badge.className = 'pcpw-badge';
      badge.title = 'PosterChan Passwords';
      badge.textContent = '🔑';
      badge.addEventListener('mousedown', (e) => { e.preventDefault(); e.stopPropagation(); togglePanel(); });
      document.documentElement.appendChild(badge);
    }
    place();
  }
  function place(){
    if(!badge || !activeField || !visible(activeField)) { hideAll(); return; }
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
    if(badge) badge.style.display = 'none';
    if(panel) panel.style.display = 'none';
  }

  async function togglePanel(){
    if(panel && panel.style.display === 'block'){ panel.style.display = 'none'; return; }
    if(!panel){
      panel = document.createElement('div');
      panel.className = 'pcpw-panel';
      document.documentElement.appendChild(panel);
    }
    panel.innerHTML = '<div class="pcpw-row pcpw-muted">looking…</div>';
    panel.style.display = 'block';
    place();
    let res;
    try{ res = await B.runtime.sendMessage({ type:'matches', url: location.href }); }
    catch(_){ res = null; }
    const list = (res && res.items) || [];
    if(!list.length){
      panel.innerHTML = '<div class="pcpw-row pcpw-muted">No saved logins for this site.</div>';
      return;
    }
    panel.innerHTML = list.map(i =>
      `<button class="pcpw-item" data-id="${esc(i.id)}">
         <b>${esc(i.username || i.title || 'no username')}</b>
         <span>${esc(i.title || '')}${i._match === 'domain' ? ' · same domain' : ''}${i.hasTotp ? ' · 2FA' : ''}</span>
       </button>`).join('');
    panel.querySelectorAll('.pcpw-item').forEach(b => {
      b.addEventListener('mousedown', async (e) => {
        e.preventDefault(); e.stopPropagation();
        await fill(b.dataset.id);
        panel.style.display = 'none';
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
    if(r.totp){
      const otp = otpFieldFor(pw && pw.form);
      if(otp) setValue(otp, r.totp);
    }
  }

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
    if(el.type === 'password' || (el.form && el.form.querySelector(PW_SEL))) ensureBadge(el);
  }, true);
  document.addEventListener('focusout', (e) => {
    // Let a click on the panel land before it disappears.
    setTimeout(() => {
      const a = document.activeElement;
      if(a && (a === badge || (panel && panel.contains(a)))) return;
      if(panel && panel.matches(':hover')) return;
      hideAll();
    }, 180);
  }, true);
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
