/* System Settings → Secure Boot, loaded only when that page is opened (os.js _loadPage): it exists on PosterChanOS
 * alone, so every other boot would pay for it for nothing (tests/test_client_boot_payload_budget.py). */
(function(root){
  'use strict';
  /* SECURE BOOT ("anti-cheat") -- System Settings' view of pc-anticheat (desktop/anticheat.js). Asked for with the
   * installer's checkbox, 2026-10-27. The helper runs the set-up as a service and keeps the state; this page says
   * what that state asks of the person next. The one-time code is shown ONLY while MokManager still needs it (the
   * helper drops it once Secure Boot is on), and every cost is stated before the button, not after it. */
  /* THE DISK UNLOCKS WITH THE TPM under Secure Boot, set up by itself at the first Secure Boot start (pc-anticheat tpm,
   * "tpm unlock should be automatic and part of the Secure Boot System Settings", 2026-10-29). Says which it is. */
  const TPM = {
    on: ['ok', 'The disk unlocks with the TPM — no password at startup.'],
    waiting: ['', 'The TPM unlock sets itself up at the first start with Secure Boot on.'],
    building: ['', 'Adding TPM support to this system — a one-time build of about 15 minutes, in the background. The TPM unlock follows by itself.'],
    absent: ['warn', 'No TPM was found: switch the TPM (security chip) on in the firmware settings and the disk unlocks by itself. Until then the disk password is asked at startup.'],
    nokey: ['warn', 'This disk has no key file to seal into the TPM, so the disk password is asked at startup.'],
    failed: ['warn', 'Setting up the TPM unlock did not work this time; it is tried again at the next start.'],
    travel: ['', 'Travel mode is on: the disk password is asked at every start.'],
  };
  /* TRAVEL MODE ("If you travel, you will want a password to unlock", 2026-10-29): pc-anticheat travel on|off. Not just
   * the TPM: the key file leaves the boot partition and the startup images too, or a thief would not need the TPM. */
  function travelBlock(on){
    return `<div class="os-sb-travel" data-sb-travel="${on?'on':'off'}"><b>Travel mode</b>
      <p class="muted">${on
        ? 'Every start asks for the disk password. Turn it off to unlock by itself again — nothing to type, you are already signed in.'
        : 'Ask for the disk password at every start, for when the computer leaves the house: the TPM unlock and the key file on the boot partition are both put away until you turn it off.'}</p>
      <button class="btn" data-sb-travel-toggle>${on?'Turn travel mode off':'Turn travel mode on'}</button></div>`;
  }
  function tpmLine(t){ const v = TPM[t] || TPM.waiting; return `<p class="os-sb-tpm ${v[0]}" data-sb-tpm="${t || 'waiting'}">${v[1]}</p>`; }
  function page(ac, err, iconSvg, enc){
    const head=`<header class="os-set-pagehead"><div>${iconSvg('i-shield')}</div><span><h2>Secure Boot</h2><p>Anti-cheat mode: a Gentoo-signed kernel, started by Secure Boot, for games whose anti-cheat requires it.</p></span></header>`;
    if(err) return head+`<div class="empty" data-sb-error>${enc(err)}</div>`;
    const st=ac.state||'off', costs=`<ul class="os-sb-costs"><li>About 600 MB is downloaded, then the computer is set up to start through Secure Boot.</li>
      <li>The disk still unlocks by itself: with a TPM, which releases the key only while Secure Boot is on. Without a TPM
        (or if it is switched off in the firmware) the disk password is asked at startup. Hibernation is turned off (sleep still works).</li>
      <li>On the next restart a blue <b>MokManager</b> screen asks you to approve Gentoo's key once, with a one-time code shown here.</li>
      <li>Then switch Secure Boot on in the computer's firmware settings.</li></ul>`;
    const card=(title, body, actions)=>`<section class="os-set-card os-sb" data-sb-state="${enc(st)}"><div class="os-set-cardhead"><b>${title}</b></div>${body}${actions?`<div class="os-set-actions">${actions}</div>`:''}</section>`;
    const busy=['requested','running','undoing'].includes(st);
    let html;
    if(ac.nvidia && st==='off' && !ac.installed){
      html=card('Not available on this computer', `<p class="muted">This computer has an NVIDIA graphics card. NVIDIA's driver is built on this machine and is not signed, so Secure Boot refuses to load it.</p>`, '');
    }else if(st==='off'){
      html=card('Secure Boot is off', costs, `<button class="btn primary" data-sb-setup>Set up Secure Boot</button>`);
    }else if(busy){
      html=card(st==='undoing'?'Turning anti-cheat mode off…':'Setting up Secure Boot…',
        `<p class="muted">${st==='undoing'?'The normal boot entry is being put back.':'Downloading and installing the signed kernel. This can take a while; you can keep using the computer.'} This page updates by itself.</p>`, '');
    }else if(st==='enrol'){
      const pw=String(ac.password||'');
      html=card('Restart to finish',
        `<ol class="os-sb-steps"><li>Restart the computer. A blue <b>MokManager</b> screen appears — <b>press any key within
          10 seconds</b>, or it starts the computer without asking and the request is lost.</li>
          <li>Choose <b>Enroll MOK</b> → <b>Continue</b> → <b>Yes</b>, and type these ${pw.length} digits:</li></ol>
         <div class="os-sb-code" data-sb-code>${enc(pw)}</div>
         <ol class="os-sb-steps" start="3"><li>Choose <b>Reboot</b>, open the firmware settings and switch <b>Secure Boot</b> on.</li>
          <li>Start <b>PosterChanOS (anti-cheat)</b>. The first start asks for the disk password once; from then on the TPM unlocks it.</li></ol>`,
        `<button class="btn" data-sb-undo>Undo</button>`);
    }else if(st==='enabled'){
      html=card(ac.secureBoot==='enabled'?'Secure Boot is on':'Set up — switch Secure Boot on',
        ac.secureBoot==='enabled'
          ? `<p class="muted">This computer started through Secure Boot with the signed kernel. Run <code>pc-eac-check</code> in a terminal to see what an anti-cheat will check.</p>${tpmLine(ac.travel?'travel':ac.tpm)}${travelBlock(!!ac.travel)}`
          : `<p class="muted">The signed kernel is installed, but Secure Boot is still off in the firmware. Open the firmware settings, switch Secure Boot on, and start <b>PosterChanOS (anti-cheat)</b>.</p>`,
        `<button class="btn" data-sb-undo>Turn anti-cheat mode off</button>`);
    }else{
      html=card('Setting up Secure Boot did not finish',
        `<p class="muted">The set-up stopped before it finished. What it said last:</p><pre class="os-sb-log" data-sb-log>${enc(ac.log||'(no output)')}</pre>`,
        `<button class="btn primary" data-sb-setup>Try again</button>${ac.installed?'<button class="btn" data-sb-undo>Turn anti-cheat mode off</button>':''}`);
    }
    return head+html;
  }
  function bind(host, rerender, PC, pcAnticheat){
    const act=(sel, fn, label)=>{ const b=host.querySelector(sel); if(!b) return;
      b.onclick=async()=>{ b.disabled=true; b.textContent=label;
        try{ await fn(); }catch(e){ PC().toast(String((e&&e.message)||e).replace(/^Error invoking remote method '[^']+': (Error: )?/,'')); }
        rerender(); }; };
    act('[data-sb-setup]', ()=>pcAnticheat.setup(), 'Starting…');
    act('[data-sb-undo]', async()=>{
      const ok=await PC().uiConfirm('Turn anti-cheat mode off?\n\nThe computer goes back to its normal boot entry. Switch Secure Boot off in the firmware afterwards.',
        { ok:'Turn it off', cancel:'Keep it' });
      if(ok) await pcAnticheat.undo(); }, 'Working…');
    const tb=host.querySelector('[data-sb-travel]');
    if(tb && pcAnticheat.travel){
      const on=tb.getAttribute('data-sb-travel')==='on';
      act('[data-sb-travel-toggle]', async()=>{
        if(!on && !await PC().uiConfirm('Turn travel mode on?\n\nFrom the next start the disk password is asked every time. Make sure you know it: without it, nothing on this disk can be opened.',
          { ok:'Turn it on', cancel:'Cancel' })) return;
        await pcAnticheat.travel(!on);
        PC().toast(on?'Travel mode is off — the disk unlocks by itself again.':'Travel mode is on — the disk password is asked at every start.');
      }, 'Rebuilding the startup images…');
    }
  }
  const CSS = '.os-sb-tpm{margin:8px 0 0;font-size:13px}.os-sb-tpm.ok{color:var(--green,#00ff88)}.os-sb-tpm.warn{color:var(--warn,#ffb020)}.os-sb .os-set-cardhead{margin-bottom:10px}.os-sb-costs,.os-sb-steps{margin:0 0 6px;padding-left:20px;color:var(--text);font-size:13px;line-height:1.55}.os-sb-costs li,.os-sb-steps li{margin:3px 0}.os-sb-code{margin:10px 0 14px;font:700 30px/1.2 ui-monospace,monospace;letter-spacing:.14em;color:var(--accent);overflow-wrap:anywhere}.os-sb-log{max-height:240px;overflow:auto;margin:8px 0 0;padding:10px;border-radius:10px;background:#0006;font-size:11.5px;white-space:pre-wrap;overflow-wrap:anywhere}.os-sb .os-set-actions{margin-top:14px}.os-sb-travel{margin:14px 0 0;padding:12px 0 0;border-top:1px solid var(--border,#ffffff1a)}.os-sb-travel p{margin:4px 0 10px;font-size:13px}';
  /* Mounted into its section by renderSystemSettings: reads the helper, paints, and looks again every 5 s while the
   * set-up runs as a service, until the page is left (`alive` false). */
  async function mount(slot, d){
    if(!slot) return;
    if(!document.getElementById('pc-sb-css')){
      const st = document.createElement('style'); st.id = 'pc-sb-css'; st.textContent = CSS; document.head.appendChild(st);
    }
    const bridge = root.pcAnticheat && root.pcAnticheat.available() ? root.pcAnticheat : null;
    let ac = {}, err = '';
    if(!bridge) err = 'Secure Boot settings are unavailable on this device.';
    else{
      const r = await d.read(bridge.status(), 'Secure Boot');
      if(!d.alive()) return;
      if(r.ok) ac = r.value || {};
      else err = r.timedOut ? 'The Secure Boot helper did not answer.' : 'Could not read the Secure Boot state: ' + r.error;
    }
    slot.innerHTML = page(ac, err, d.iconSvg, d.enc);
    const again = () => { if(d.alive()) mount(slot, d); };
    if(bridge) bind(slot, again, d.PC, bridge);
    if(['requested','running','undoing'].includes(ac.state)) setTimeout(again, 5000);
  }
  root.PCSecureBoot = { page, bind, mount };
})(typeof window !== 'undefined' ? window : globalThis);
