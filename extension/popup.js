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

let tabUrl = '', allItems = [], ticker = null;

const send = (msg) => B.runtime.sendMessage(msg).catch(() => null);

function show(pane){
  for(const p of ['pane-pair','pane-list','pane-gen']) $('#'+p).classList.toggle('hidden', p !== pane);
}

async function boot(){
  try{
    const tabs = await B.tabs.query({ active:true, currentWindow:true });
    tabUrl = (tabs && tabs[0] && tabs[0].url) || '';
  }catch(_){ }
  const st = await send({ type:'state' });
  if(!st || !st.paired){ show('pane-pair'); $('#status').textContent = ''; return; }
  $('#status').textContent = `${st.count} · ${st.status}${st.mode === 'ro' ? ' · read-only' : ''}`;
  show('pane-list');
  await paint();
}

async function paint(){
  const res = await send({ type:'matches', url: tabUrl });
  allItems = (res && res.items) || [];
  render();
}

function render(){
  const q = ($('#q').value || '').trim().toLowerCase();
  const list = allItems.filter(i => !q ||
    (i.title||'').toLowerCase().includes(q) || (i.username||'').toLowerCase().includes(q));
  const host = V.hostOf(tabUrl) || 'this page';
  $('#list').innerHTML = list.length ? list.map(i => `
    <div class="item" data-id="${esc(i.id)}">
      <div class="it-t">
        <b>${esc(i.title || host)}</b>
        <span class="muted">${esc(i.username || '')}${i._match === 'domain' ? ' · same domain' : ''}</span>
      </div>
      <div class="it-a">
        <button data-a="fill" title="Fill this page">Fill</button>
        <button data-a="user" title="Copy username">User</button>
        <button data-a="pass" title="Copy password">Pass</button>
        ${i.hasTotp ? '<button data-a="totp" title="Copy the one-time code">2FA</button>' : ''}
      </div>
      <div class="otp" data-otp="${esc(i.id)}"></div>
    </div>`).join('')
    : `<div class="muted pad">No saved logins for ${esc(host)}.
        ${allItems.length ? '' : 'Open a site you have a login for, or search above.'}</div>`;

  document.querySelectorAll('.item').forEach(el => {
    el.querySelectorAll('[data-a]').forEach(b => b.onclick = () => act(el.dataset.id, b.dataset.a));
  });
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
boot();
