/* PosterChan Passwords — the popup.
 *
 * The list for the current tab, one-time codes with their countdown, and the generator. Sized for a
 * PHONE as well as a desktop: on Firefox for Android this is a full-screen sheet, which is why
 * nothing here is laid out in columns and every control is a real tap target.
 */
'use strict';

const B = (typeof browser !== 'undefined') ? browser : chrome;
const V = PCVaultCore;
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

let tabUrl = '', matches = [], everything = [], ticker = null, vaultCount = 0;

const send = (msg) => B.runtime.sendMessage(msg).catch(() => null);

/* EVERY pane in the document, not a hardcoded list of three.
 *
 * Adding a pane to the HTML and a button that calls show() for it LOOKED complete and did nothing:
 * this hid the three ids it knew about and never revealed the new one, so Bookmarks and Relays each
 * opened onto a blank popup. Sites had been doing the same for longer — its button passes 'sites',
 * which was never in the list either — which is how a broken tab survived unnoticed: the failure is
 * a panel that shows nothing, and nothing in the console. */
function show(pane){
  for(const p of document.querySelectorAll('.pane')) p.classList.toggle('hidden', p.id !== pane);
}

/* The build, in the footer.
 *
 * "It works on my laptop but not my tablet" is almost never the site — it is two different builds,
 * and until the popup says which one it is there is no way to tell from the outside. */
function showVersion(){
  try{
    const el = document.getElementById('ver');
    if(el) el.textContent = 'v' + B.runtime.getManifest().version;
  }catch(_){ }
}

/* Declared HERE, above boot(), not beside the bookmark UI at the bottom of the file: boot() assigns
 * these, boot() is called during script evaluation, and a `let` below that call is in its temporal
 * dead zone until evaluation reaches it. It happens to survive today only because the assignment sits
 * after an await — one edit moving it earlier turns the whole popup into a ReferenceError. */
let _mode = null, _bmOn = false, _bmCount = 0;
let _confirmRemovals = false;      // armed only by the "looks like a restore" prompt below

async function boot(){
  try{
    const tabs = await B.tabs.query({ active:true, currentWindow:true });
    tabUrl = (tabs && tabs[0] && tabs[0].url) || '';
  }catch(_){ }
  const st = await send({ type:'state' });
  if(!st || !st.paired){ show('pane-pair'); $('#status').textContent = ''; return; }
  vaultCount = st.count || 0;
  $('#status').textContent = `${st.count} · ${st.status}${st.mode === 'ro' ? ' · read-only' : ''}`;
  _mode = st.mode; _bmOn = !!st.bmOn; _bmCount = st.bmCount || 0;
  show('pane-list');
  await paint();
}

async function paint(){
  // BOTH: what matches this page, and the whole vault so the search box can actually find things.
  const [m, a] = await Promise.all([ send({ type:'matches', url: tabUrl }), send({ type:'all' }) ]);
  matches = (m && m.items) || [];
  everything = (a && a.items) || [];
  render();
}

/* Firefox MV3 does NOT grant host permissions at install — the user has to allow them, and until
 * they do, tabs.query hands back no URL and the content script never injects. The popup then knows
 * nothing about the page it is sitting on, and saying "no saved logins for this site" would be a
 * lie: it cannot see the site. Ask for the grant instead. */
async function hostGranted(){
  try{ return await B.permissions.contains({ origins:['<all_urls>'] }); }
  catch(_){ return true; }        // no permissions API → nothing to ask for
}

function render(){
  const q = ($('#q').value || '').trim().toLowerCase();
  const host = V.hostOf(tabUrl);
  // With text in the box, search the WHOLE vault — that is what a search box means, and the reason
  // an entry called "nostr" saved for poster.place could not be found from a poster.place tab.
  // Empty box: what matches this page, which is what you want the moment the popup opens.
  const list = q
    ? everything.filter(i => (i.title||'').toLowerCase().includes(q) ||
                             (i.username||'').toLowerCase().includes(q) ||
                             (i.host||'').toLowerCase().includes(q))
    : matches;
  $('#list').innerHTML = list.length ? list.map(i => `
    <div class="item" data-id="${esc(i.id)}">
      <div class="it-t">
        <b>${esc(i.title || i.host || host || 'Untitled')}</b>
        <span class="muted">${esc(i.username || '')}${i._match === 'domain' ? ' · same domain'
          : (q && i.host ? ' · ' + esc(i.host) : '')}</span>
      </div>
      <div class="it-a">
        <button data-a="fill" title="Fill this page">Fill</button>
        <button data-a="user" title="Copy username">User</button>
        <button data-a="pass" title="Copy password">Pass</button>
        <button data-a="edit" title="Edit this entry">Edit</button>
        ${i.hasTotp ? '<button data-a="totp" title="Copy the one-time code">2FA</button>' : ''}
      </div>
      <div class="otp" data-otp="${esc(i.id)}"></div>
    </div>`).join('')
    : `<div class="muted pad">${emptyWhy(q, host)}</div>`;

  document.querySelectorAll('.item').forEach(el => {
    el.querySelectorAll('[data-a]').forEach(b => b.onclick = () => act(el.dataset.id, b.dataset.a));
  });
  const grant = $('#grant');
  if(grant) grant.onclick = async () => {
    try{ await B.permissions.request({ origins:['<all_urls>'] }); }catch(_){ }
    boot();
  };
  startTicker(list);
}

/* One-time codes tick in place, with the seconds left. A code you have to re-open a menu to refresh
 * is a code you mistype — the countdown is the part people actually use. */
function startTicker(list){
  clearInterval(ticker);
  const withTotp = list.filter(i => i.hasTotp);
  if(!withTotp.length) return;
  const paintCodes = async () => {
    for(const i of withTotp){
      const box = document.querySelector(`[data-otp="${CSS.escape(i.id)}"]`);
      if(!box) continue;
      const r = await send({ type:'reveal', id:i.id });
      if(!r || !r.ok || !r.totp){ box.textContent = ''; continue; }
      box.innerHTML = `<code>${esc(r.totp.replace(/(\d{3})(?=\d)/, '$1 '))}</code>
                       <span class="${r.left <= 5 ? 'low' : 'muted'}">${r.left}s</span>`;
    }
  };
  paintCodes();
  ticker = setInterval(paintCodes, 1000);
}

async function act(id, what){
  if(what === 'edit'){ return openEdit(id); }
  if(what === 'fill'){
    try{
      const tabs = await B.tabs.query({ active:true, currentWindow:true });
      // frameId 0 — the TOP frame only. Without it the message goes to every frame in the tab,
      // and content.js runs in all of them, so a third-party ad or widget iframe received the
      // password and put it into its own DOM. (The background refuses a frame the item does not
      // match as well; this is the half that stops the message being sent at all.)
      await B.tabs.sendMessage(tabs[0].id, { type:'pcpw-fill', id }, { frameId: 0 });
    }catch(_){ }
    window.close();
    return;
  }
  const r = await send({ type:'reveal', id });
  if(!r || !r.ok) return;
  const text = what === 'user' ? r.username : what === 'pass' ? r.password : r.totp;
  if(!text) return;
  try{ await navigator.clipboard.writeText(text); }catch(_){ return; }
  // No auto-clear here, and no pretending: a popup is destroyed as soon as it loses focus, so a
  // timer set in it never fires, and reading the clipboard back to check would need `clipboardRead`
  // — a permission a password manager should not hold. The app's own copy button does clear (its
  // page stays alive); this one tells you it did not.
  $('#status').textContent = what === 'pass' ? 'password copied — clear your clipboard when done'
                           : what === 'totp' ? 'code copied' : 'username copied';
}

/* Why the list is empty — four different situations that all rendered as "no saved logins for this
 * site", which is only ever true for one of them. */
function emptyWhy(q, host){
  if(q) return `Nothing in your vault matches “${esc(q)}”. ${everything.length} entr` +
               `${everything.length === 1 ? 'y' : 'ies'} searched.`;
  if(!vaultCount) return 'Your vault is empty here — it may still be syncing, or this pairing is ' +
                         'for a different account. Try Sync below.';
  if(!tabUrl) return 'This add-on can’t see which page you’re on, so it can’t match a login. ' +
                     'Firefox doesn’t grant that at install.<br>' +
                     '<button id="grant" class="primary">Allow on all sites</button>' +
                     '<br>Or type a name above to search all ' + everything.length + ' entries.';
  return `No saved login for ${esc(host || 'this page')} — search above to look through all ` +
         `${everything.length} entries.`;
}

// ---------------------------------------------------------------- generator

function genOpts(){
  return { length:+$('#gen-range').value, lower:$('#g-lower').checked, upper:$('#g-upper').checked,
           digits:$('#g-digits').checked, symbols:$('#g-symbols').checked, avoidAmbiguous:$('#g-amb').checked };
}
function drawGen(){
  const o = genOpts();
  $('#gen-len').textContent = o.length;
  try{
    $('#gen-out').textContent = V.generate(o);
    $('#gen-bits').textContent = `about ${V.entropyBits(o)} bits of entropy`;
  }catch(_){
    $('#gen-out').textContent = '—';
    $('#gen-bits').textContent = 'pick at least one kind of character';
  }
  try{ localStorage.setItem('pcpwGen', JSON.stringify(o)); }catch(_){ }
}

function bindGen(){
  try{
    const o = JSON.parse(localStorage.getItem('pcpwGen') || 'null');
    if(o){ $('#gen-range').value = o.length; $('#g-lower').checked = o.lower;
           $('#g-upper').checked = o.upper; $('#g-digits').checked = o.digits;
           $('#g-symbols').checked = o.symbols; $('#g-amb').checked = o.avoidAmbiguous; }
  }catch(_){ }
  document.querySelectorAll('#pane-gen input').forEach(i => i.oninput = drawGen);
  $('#gen-again').onclick = drawGen;
  $('#gen-copy').onclick = async () => {
    try{ await navigator.clipboard.writeText($('#gen-out').textContent); $('#status').textContent = 'copied'; }catch(_){ }
  };
  // Straight into the page's password field — the reason to generate one in a browser at all.
  $('#gen-fill').onclick = async () => {
    try{
      const tabs = await B.tabs.query({ active:true, currentWindow:true });
      await B.tabs.sendMessage(tabs[0].id, { type:'pcpw-set-password', value: $('#gen-out').textContent },
                               { frameId: 0 });
      window.close();
    }catch(_){ $('#status').textContent = 'couldn’t reach this page'; }
  };
}

// ---------------------------------------------------------------- wiring

$('#q').oninput = render;
$('#tab-gen').onclick = () => {
  const on = !$('#pane-gen').classList.contains('hidden');
  if(on){ boot(); } else { show('pane-gen'); drawGen(); }
};
$('#pair-go').onclick = async () => {
  const r = await send({ type:'pair', code: $('#pair-code').value });
  if(r && r.ok){ $('#pair-err').textContent = ''; boot(); }
  else $('#pair-err').textContent = (r && r.error) || 'pairing failed';
};
/* What the signer has been allowed to do, and how to take it back.
 *
 * A remembered "allow" means a site signs with the user's key from then on with no window at all.
 * A store like that with no way to read or revoke it leaves unpairing as the only escape hatch,
 * which throws away the vault to withdraw one permission. */
const _KINDNAMES = { 0:'profile', 1:'notes', 3:'contact list', 4:'legacy DMs', 5:'deletions',
                     6:'reposts', 7:'reactions', 1059:'private messages', 9734:'zap requests',
                     10002:'relay list', 22242:'relay logins', 30023:'articles' };
function _permLabel(method, kind){
  if(method !== 'signEvent') return method.replace('nip04.', '').replace('nip44.', '');
  return 'sign ' + (_KINDNAMES[kind] || ('kind ' + kind));
}
async function paintSites(){
  const r = await send({ type:'nostr-perms' });
  const perms = (r && r.perms) || {};
  const bySite = new Map();
  for(const k of Object.keys(perms)){
    const [origin, method, kind] = k.split('|');
    if(!bySite.has(origin)) bySite.set(origin, []);
    bySite.get(origin).push({ k, label: _permLabel(method, kind|0), how: perms[k] });
  }
  const box = $('#sites');
  if(!bySite.size){ box.innerHTML = '<div class="muted small">No site has asked yet.</div>'; return; }
  box.innerHTML = '';
  for(const [origin, rows] of bySite){
    const el = document.createElement('div');
    el.className = 'item';
    el.innerHTML = '<div class="t"></div><div class="s"></div>' +
                   '<button class="danger" data-o="">Forget</button>';
    el.querySelector('.t').textContent = origin;
    el.querySelector('.s').textContent = rows.map(x => (x.how === 'deny' ? 'blocked: ' : '') + x.label)
                                             .join(', ');
    el.querySelector('button').onclick = async () => {
      await send({ type:'nostr-forget', origin });
      paintSites();
    };
    box.appendChild(el);
  }
}
$('#sites-tab').onclick = () => { show('pane-sites'); paintSites(); };
$('#sync').onclick = async () => { await send({ type:'sync' }); $('#status').textContent = 'syncing…'; setTimeout(boot, 1200); };
/* Two taps, in-page. A native confirm() can dismiss a Firefox action popup outright — the await
 * would then never resume and Unpair would silently do nothing — and this project's rule against
 * native dialogs exists for exactly that class of wedge. */
let _unpairArmed = false;
$('#unpair').onclick = async () => {
  if(!_unpairArmed){
    _unpairArmed = true;
    $('#unpair').textContent = 'Tap again to remove';
    $('#status').textContent = 'your passwords stay in PosterChan';
    setTimeout(() => { _unpairArmed = false; $('#unpair').textContent = 'Unpair'; }, 5000);
    return;
  }
  await send({ type:'unpair' });
  location.reload();
};

bindGen();
showVersion();
boot();


/* ---- bookmark sync -------------------------------------------------------------------------
 * The toggle writes into the browser's bookmark tree, so it says what it will do BEFORE it is on,
 * and what it cannot do when the pairing is read-only. "Merge now" is the union: it gains bookmarks
 * on both sides and deletes nothing, which is also what happens the first time it is switched on.
 */

function paintBm(){
  const box = $('#bm-on'); if(!box) return;
  box.checked = _bmOn;
  const ro = _mode === 'ro';
  $('#bm-note').innerHTML = _bmOn
    ? (ro ? `${_bmCount} synced · <b>receive only</b> — this pairing has no signing key, so bookmarks
             saved here stay here. Pair with full access to publish them.`
          : `${_bmCount} synced · two-way. Encrypted per bookmark, like your passwords.`)
    : `Off. Turning it on merges this browser's bookmarks with your other devices' — it adds on both
       sides and deletes nothing.${ro ? ' This pairing is read-only, so it can receive but not send.' : ''}`;
}

{ const t = $('#bm-tab'); if(t) t.onclick = () => { show('pane-bm'); paintBm(); }; }
{ const b = $('#bm-on');
  if(b) b.onchange = async () => {
    b.disabled = true;
    const r = await send({ type:'bm-enable', on: b.checked });
    _bmOn = !!(r && r.on); _bmCount = (r && r.count) || 0;
    b.disabled = false; paintBm();
    // An enable that FAILED used to leave the checkbox snapping back with no explanation, which is
    // indistinguishable from a toggle that does not work.
    if(r && r.ok === false) $('#bm-note').innerHTML = '<b>Could not turn it on:</b> ' +
      String(r.error || 'unknown reason');
    else if(!r) $('#bm-note').innerHTML =
      '<b>No answer from the extension.</b> It may be disabled pending a new permission — check the ' +
      'browser\'s extensions page.';
  }; }
{ const b = $('#bm-sync');
  if(b) b.onclick = async () => {
    b.disabled = true; b.textContent = 'merging…';
    const r = await send({ type:'bm-sync', confirmRemovals: _confirmRemovals });
    _confirmRemovals = false;
    b.textContent = r && r.ok ? `+${r.created} here, +${r.published} sent` : (r && r.error) || 'failed';
    // A merge that sent nothing states the reason under the button — the alternative is a bare 0 that
    // looks identical whether the pairing cannot sign, the relay is unreachable, or there was simply
    // nothing new to send.
    /* "That looks like a restore, not a decision." Deleting everything and merging is a legitimate
     * thing to do — and so is restoring a backup, and they are indistinguishable from here. So the
     * merge stops and asks, rather than removing the same bookmarks from every other device. */
    if(r && r.ok && r.pendingRemovals){
      _confirmRemovals = true;
      $('#bm-note').innerHTML = `<b>${r.pendingRemovals} bookmarks are missing here</b> that this browser
        had synced. If you deleted them on purpose, press Merge now again to remove them from your
        other devices. If this browser was restored or re-paired, turn sync off instead — merging
        would delete them everywhere.`;
      return;
    }
    if(r && r.ok && r.blocked) $('#bm-note').innerHTML =
      `<b>Sent nothing:</b> ${r.blocked}. ${r.wanted} bookmark${r.wanted === 1 ? '' : 's'} waiting.`;
    setTimeout(() => { b.textContent = 'Merge now'; b.disabled = false; }, 3500);
    const st = await send({ type:'state' });
    _bmOn = !!(st && st.bmOn); _bmCount = (st && st.bmCount) || 0; paintBm();
  }; }


/* ---- relays ---------------------------------------------------------------------------------
 * Editable here because the pairing code's list is a snapshot of whatever the app had at that
 * moment, and a bad one is invisible: nothing syncs and nothing says why. The line under the box
 * shows what is ACTUALLY in use, which is the question being asked.
 */
{ const t = $('#relay-tab');
  if(t) t.onclick = async () => {
    show('pane-relays');
    const r = await send({ type:'relays-get' });
    if(!r) return;
    $('#relay-list').value = (r.relays || []).join('\n');
    $('#relay-note').textContent = 'In use: ' + ((r.paired || []).join(', ') || r.fallback);
  }; }
{ const b = $('#relay-save');
  if(b) b.onclick = async () => {
    b.disabled = true;
    const lines = $('#relay-list').value.split(/[\s,]+/).filter(Boolean);
    const r = await send({ type:'relays-set', relays: lines });
    b.disabled = false;
    if(!r || !r.ok){ $('#relay-note').textContent = 'could not save'; return; }
    $('#relay-list').value = (r.relays || []).join('\n');
    // Anything unusable was dropped by the normaliser rather than kept as decoration; say so, or a
    // typo silently becomes "I set it and it still does not work".
    const dropped = lines.length - (r.relays || []).length;
    $('#relay-note').textContent = 'In use: ' + ((r.using || []).join(', ') || '(none)')
      + (dropped > 0 ? `  ·  ${dropped} entr${dropped === 1 ? 'y' : 'ies'} ignored (not a relay address)` : '');
  }; }

/* Cleanup for the duplicate folders an earlier build created (folder creation was not serialised, so
 * a burst of arriving bookmarks each made their own copy). Explicit, because nothing can tell those
 * apart from two folders somebody named the same deliberately. */
{ const b = $('#bm-tidy');
  if(b) b.onclick = async () => {
    b.disabled = true; b.textContent = 'tidying…';
    const r = await send({ type:'bm-tidy' });
    b.textContent = 'Tidy duplicate folders';
    b.disabled = false;
    $('#bm-note').innerHTML = (r && r.ok)
      ? `Merged ${r.merged} duplicate folder${r.merged === 1 ? '' : 's'} (${r.removed} removed). No bookmarks were deleted.`
      : `<b>Could not tidy:</b> ${(r && r.error) || 'no answer'}`;
  }; }


/* ---- editing an entry -----------------------------------------------------------------------
 * The gap this closes: the popup could fill, generate and show a one-time code, but correcting a
 * username or a rotated password meant opening the app. It writes through the SAME save path as the
 * save bar — merge, publish, or queue when the pairing is read-only — so there is one writer and one
 * set of rules, not a second one that can disagree with it.
 *
 * `full: true` marks it authoritative: an emptied box means CLEAR. The save bar's backfill exists
 * because that bar knows only a username and a password, and applying it here would make deleting a
 * note impossible — you clear it, save, and it comes back.
 */
let _editId = null;

async function openEdit(id){
  const r = await send({ type:'item', id });
  if(!r || !r.ok){ return; }
  const it = r.item || {};
  _editId = id;
  $('#ed-title').value = it.title || '';
  $('#ed-user').value  = it.username || '';
  $('#ed-pass').value  = it.password || '';
  $('#ed-pass').type   = 'password';
  $('#ed-url').value   = (it.uris && it.uris[0]) || it.url || '';
  $('#ed-totp').value  = it.totp || '';
  $('#ed-notes').value = it.notes || '';
  $('#ed-note').textContent = _mode === 'ro'
    ? 'This pairing is read-only, so the change waits here until the app publishes it.'
    : '';
  show('pane-edit');
}

{ const b = $('#ed-show');
  if(b) b.onclick = () => { const p = $('#ed-pass');
    p.type = p.type === 'password' ? 'text' : 'password'; b.textContent = p.type === 'password' ? 'show' : 'hide'; }; }
{ const b = $('#ed-cancel'); if(b) b.onclick = () => { _editId = null; show('pane-list'); }; }
{ const b = $('#ed-save');
  if(b) b.onclick = async () => {
    if(!_editId) return;
    b.disabled = true; b.textContent = 'saving…';
    const url = $('#ed-url').value.trim();
    const item = { id: _editId, title: $('#ed-title').value.trim(),
                   username: $('#ed-user').value, password: $('#ed-pass').value,
                   uris: url ? [url] : [], totp: $('#ed-totp').value.trim(),
                   notes: $('#ed-notes').value };
    const r = await send({ type:'save', item, full:true });
    b.disabled = false; b.textContent = 'Save changes';
    if(!r || !r.ok){ $('#ed-note').textContent = (r && r.error) || 'could not save'; return; }
    // Queued is a real outcome, not a failure — say which happened.
    $('#ed-note').textContent = r.published ? 'saved and published' : 'saved — waiting for the app to publish it';
    _editId = null;
    setTimeout(() => { show('pane-list'); boot(); }, 900);
  }; }
