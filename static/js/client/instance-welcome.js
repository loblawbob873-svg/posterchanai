/* The instance welcome is independent of app.js so login and account switches share one flow. */
(function () {
  'use strict';
  let identity = '', checked = '', checking = false, dialog = null, retryAt = 0;
  const desktopOrSetup = () => !!(window.PCOS?.isOn?.() || window.PCOSWin?.isWindow?.() || document.querySelector('#osfr'));
  const profileKey=()=>{const v=window.__PC?.viewer();return (v?.pubkey||'')+':'+String(v?.profile?.nip05||'').trim();};
  const endpoint = path => (window.__PC_API_BASE__ || '') + '/api/instance-welcome/' + path;
  async function request(action, pk, refresh=false) {
    const pc = window.__PC;
    const event = await pc.signTemplate({kind: 27235, pubkey: pk,
      created_at: Math.floor(Date.now() / 1000), tags: [], content: 'instance-welcome-' + action});
    if (pc.viewer().pubkey !== pk) throw new Error('Account changed. Please try again.');
    const response = await fetch(endpoint(action), {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({pubkey: pk, auth: btoa(JSON.stringify(event)),refresh})});
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
    const address=data.address||'',domain=data.domain||'';
    const features=['File Storage','Live Streaming','AI Access',...Object.values(window.PCInstanceAccess?.apps||{})];
    node.innerHTML = '<form method="dialog"><button class="iw-close" aria-label="Close welcome">×</button></form>' +
      '<img class="iw-logo" alt="Instance logo"><p class="iw-eyebrow">YOUR COMMUNITY, ONE ADDRESS</p>' +
      '<h2 id="instance-welcome-title"></h2><p class="iw-intro"></p>' +
      '<div class="iw-steps"><h3>Complete your profile to use these apps</h3><ol>' +
      '<li>Get an approved NIP-05 name on <strong class="iw-domain"></strong>.</li>' +
      '<li>Open <strong>Edit profile</strong>. Replace your current <strong>NIP-05 / verified address</strong> with your approved address.</li>' +
      '<li><strong>Save your profile.</strong> Applying or receiving approval alone does not activate app access.</li></ol>' +
      '<p class="iw-address"></p></div>' +
      '<button type="button" class="btn btn-neon iw-apply"></button>' +
      '<p class="iw-status" role="status" aria-live="polite"></p>' +
      '<h3 class="iw-feature-title">More with your instance identity</h3><div class="iw-benefits"></div>' +
      '<p class="iw-note">An address on another domain does not qualify. Each app also follows the permissions your instance administrator grants.</p>' +
      '<form method="dialog"><button class="iw-later">Maybe later</button></form>';
    node.querySelector('h2').textContent = 'Welcome to ' + data.site_name;
    node.querySelector('.iw-intro').textContent=address?'Your instance name is ready. Add it to your profile to finish setup.':'One verified instance identity connects your community apps.';
    node.querySelector('.iw-domain').textContent=domain||'this instance';
    node.querySelector('.iw-address').textContent=address||('Use your approved name'+(domain?'@'+domain:'@your-instance-domain')+' — not just the domain.');
    const grid=node.querySelector('.iw-benefits');
    for(const name of features){const item=document.createElement('article');const title=document.createElement('h3');title.textContent=name;item.append(title);grid.append(item);}
    node.querySelector('img').src = document.querySelector('.brand-logo,.logo-img')?.src || pc.LOGO;
    const button = node.querySelector('.iw-apply'), status = node.querySelector('.iw-status');
    button.textContent=address?'Edit my profile':'Apply for an instance name';
    button.onclick = async () => {
      if(address){close();pc.editOwnProfile();return;}
      button.disabled = true; status.textContent = 'Sending your application…';
      try {
        const result = await request('apply', pk);
        if (!node.isConnected || pc.viewer().pubkey !== pk) return;
        if(result.already){show({...data,address:result.address},pk);return;}
        button.textContent = 'Application submitted';
        status.textContent = 'Your application is saved. We’ll DM your approved address. Then open Edit profile, set that NIP-05 address, and Save to activate access.';
      } catch (error) { button.disabled = false; status.textContent = error.message; }
    };
    node.addEventListener('close', () => { node.remove(); if (dialog === node) dialog = null; });
    document.body.append(node); dialog = node; node.showModal(); button.focus();
  }
  async function check() {
    const pc = window.__PC;
    if (!pc || !window.__PC_BOOTED || document.hidden) return;
    if (desktopOrSetup()) { close(); return; }
    const pk = pc.viewer().pubkey || '', marker=profileKey();
    if (identity !== pk) { identity = pk; checked = ''; retryAt = 0; close(); }
    if (!pk || pc.standalone() || checking || checked === marker || Date.now() < retryAt) return;
    checking = true;
    try {
      const data = await request('status', pk,!!checked && checked!==marker);
      if (pc.viewer().pubkey !== pk || profileKey()!==marker || desktopOrSetup()) return;
      window.PCInstanceAccess?.accept(data,pk);
      checked = marker;
      if (data.eligible && !data.pending) show(data, pk);
      else close();
    } catch (_) { retryAt = Date.now() + 300000; /* Avoid repeated signer prompts while offline. */ }
    finally { checking = false; }
  }
  window.PCInstanceWelcome={apply:()=>request('apply',window.__PC.viewer().pubkey)};
  // Desktop entry can happen while eligibility is loading, or after the dialog was opened.
  new MutationObserver(() => { if (desktopOrSetup()) close(); }).observe(document.body, {childList:true});
  document.addEventListener('pc-app-ready', check);
  document.addEventListener('visibilitychange', check);
  setInterval(check, 10000);
  check();
}());
