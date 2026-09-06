/* The instance welcome is independent of app.js so login and account switches share one flow. */
(function () {
  'use strict';
  let identity = '', checked = '', checking = false, dialog = null, retryAt = 0;
  const endpoint = path => (window.__PC_API_BASE__ || '') + '/api/instance-welcome/' + path;
  async function request(action, pk) {
    const pc = window.__PC;
    const event = await pc.signTemplate({kind: 27235, pubkey: pk,
      created_at: Math.floor(Date.now() / 1000), tags: [], content: 'instance-welcome-' + action});
    if (pc.viewer().pubkey !== pk) throw new Error('Account changed. Please try again.');
    const response = await fetch(endpoint(action), {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({pubkey: pk, auth: btoa(JSON.stringify(event))})});
    if (!response.ok) throw new Error('Could not reach the instance. Please try again.');
    return response.json();
  }
  function close() { if (dialog) { dialog.close(); dialog.remove(); dialog = null; } }
  function show(data, pk) {
    close();
    const pc = window.__PC;
    const node = document.createElement('dialog');
    node.className = 'instance-welcome';
    node.setAttribute('aria-labelledby', 'instance-welcome-title');
    node.innerHTML = '<form method="dialog"><button class="iw-close" aria-label="Close welcome">×</button></form>' +
      '<img class="iw-logo" alt="Instance logo"><p class="iw-eyebrow">YOUR COMMUNITY, ONE ADDRESS</p>' +
      '<h2 id="instance-welcome-title"></h2><p>Make yourself at home with a verified name on this instance.</p>' +
      '<div class="iw-benefits"><article><span aria-hidden="true"><svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M3 7h7l2-3h9v16H3z"/></svg></span><h3>File Storage</h3><p>A home for your files and uploads.</p></article>' +
      '<article><span aria-hidden="true"><svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="9"/><path d="m10 8 6 4-6 4z"/></svg></span><h3>Live Streaming</h3><p>Share live moments with your community.</p></article>' +
      '<article><span aria-hidden="true"><svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="1.7"><path d="m12 3 2.7 6.3L21 12l-6.3 2.7L12 21l-2.7-6.3L3 12l6.3-2.7z"/></svg></span><h3>AI Access</h3><p>Chat, create, and explore ideas.</p></article></div>' +
      '<p class="iw-note">Apply for a name in one click. An admin reviews your name and access permissions.</p>' +
      '<button type="button" class="btn btn-neon iw-apply">Apply for an instance name</button>' +
      '<p class="iw-status" role="status" aria-live="polite"></p>' +
      '<form method="dialog"><button class="iw-later">Maybe later</button></form>';
    node.querySelector('h2').textContent = 'Welcome to ' + data.site_name;
    node.querySelector('img').src = document.querySelector('.brand-logo,.logo-img')?.src || pc.LOGO;
    const button = node.querySelector('.iw-apply'), status = node.querySelector('.iw-status');
    button.onclick = async () => {
      button.disabled = true; status.textContent = 'Sending your application…';
      try {
        const result = await request('apply', pk);
        if (!node.isConnected || pc.viewer().pubkey !== pk) return;
        button.textContent = result.already ? 'Your name is ready' : 'Application submitted';
        status.textContent = result.already ? result.address : 'Your application is saved. We’ll DM you when your name is approved.';
      } catch (error) { button.disabled = false; status.textContent = error.message; }
    };
    node.addEventListener('close', () => { node.remove(); if (dialog === node) dialog = null; });
    document.body.append(node); dialog = node; node.showModal(); button.focus();
  }
  async function check() {
    const pc = window.__PC;
    if (!pc || !window.__PC_BOOTED || document.hidden || window.PCOSWin?.isWindow()) return;
    const pk = pc.viewer().pubkey || '';
    if (identity !== pk) { identity = pk; checked = ''; retryAt = 0; close(); }
    if (!pk || pc.standalone() || checking || checked === pk || Date.now() < retryAt) return;
    checking = true;
    try {
      const data = await request('status', pk);
      if (pc.viewer().pubkey !== pk) return;
      checked = pk;
      if (data.eligible && !data.pending) show(data, pk);
    } catch (_) { retryAt = Date.now() + 300000; /* Avoid repeated signer prompts while offline. */ }
    finally { checking = false; }
  }
  document.addEventListener('pc-app-ready', check);
  document.addEventListener('visibilitychange', check);
  setInterval(check, 10000);
  check();
}());
