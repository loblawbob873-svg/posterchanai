/* System Settings → Secure Boot, loaded only when that page is opened (os.js _loadPage): it exists on PosterChanOS
 * alone, so every other boot would pay for it for nothing (tests/test_client_boot_payload_budget.py). */
(function(root){
  'use strict';
  /* SECURE BOOT ("anti-cheat") -- System Settings' view of pc-anticheat (desktop/anticheat.js). Asked for with the
   * installer's checkbox, 2026-10-27. The helper runs the set-up as a service and keeps the state; this page says
   * what that state asks of the person next. The one-time code is shown ONLY while MokManager still needs it (the
   * helper drops it once Secure Boot is on), and every cost is stated before the button, not after it. */
  function page(ac, err, iconSvg, enc){
    const head=`<header class="os-set-pagehead"><div>${iconSvg('i-shield')}</div><span><h2>Secure Boot</h2><p>Anti-cheat mode: a Gentoo-signed kernel, started by Secure Boot, for games whose anti-cheat requires it.</p></span></header>`;
    if(err) return head+`<div class="empty" data-sb-error>${enc(err)}</div>`;
    const st=ac.state||'off', costs=`<ul class="os-sb-costs"><li>About 600 MB is downloaded, then the computer is set up to start through Secure Boot.</li>
      <li>You type the disk password at every startup, and hibernation is turned off (sleep still works).</li>
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
        `<ol class="os-sb-steps"><li>Restart the computer. A blue <b>MokManager</b> screen appears.</li>
          <li>Choose <b>Enroll MOK</b> → <b>Continue</b> → <b>Yes</b>, and type these ${pw.length} digits:</li></ol>
         <div class="os-sb-code" data-sb-code>${enc(pw)}</div>
         <ol class="os-sb-steps" start="3"><li>Choose <b>Reboot</b>, open the firmware settings and switch <b>Secure Boot</b> on.</li>
          <li>Start <b>PosterChanOS (anti-cheat)</b> and type the disk password.</li></ol>`,
        `<button class="btn" data-sb-undo>Undo</button>`);
    }else if(st==='enabled'){
      html=card(ac.secureBoot==='enabled'?'Secure Boot is on':'Set up — switch Secure Boot on',
        ac.secureBoot==='enabled'
          ? `<p class="muted">This computer started through Secure Boot with the signed kernel. Run <code>pc-eac-check</code> in a terminal to see what an anti-cheat will check.</p>`
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
  }
  const CSS = '.os-sb .os-set-cardhead{margin-bottom:10px}.os-sb-costs,.os-sb-steps{margin:0 0 6px;padding-left:20px;color:var(--text);font-size:13px;line-height:1.55}.os-sb-costs li,.os-sb-steps li{margin:3px 0}.os-sb-code{margin:10px 0 14px;font:700 30px/1.2 ui-monospace,monospace;letter-spacing:.14em;color:var(--accent);overflow-wrap:anywhere}.os-sb-log{max-height:240px;overflow:auto;margin:8px 0 0;padding:10px;border-radius:10px;background:#0006;font-size:11.5px;white-space:pre-wrap;overflow-wrap:anywhere}.os-sb .os-set-actions{margin-top:14px}';
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
