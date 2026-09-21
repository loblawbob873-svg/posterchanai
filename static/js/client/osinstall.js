/* INSTALL POSTERCHANOS — the graphical front of `gentoo.sh install-live`.
 *
 * WHAT THIS IS NOT: a second installer. Every decision that touches a disk is made by gentoo.sh,
 * the same script the release gate drives and the LiveCD's terminal entry runs; this page only
 * collects the answers it would otherwise prompt for, hands them over (desktop/installer.js), and
 * reads back what it reports. So the options offered here are exactly the ones gentoo.sh has:
 *
 *   - which WHOLE disk (the live medium is never offered — the bridge and the script agree on it);
 *   - erase it, or resume onto an earlier PosterChanOS layout already on it;
 *   - the disk-encryption password (LUKS, always — the profile has no unencrypted layout);
 *   - the btrfs system volume name (advanced; the default is the terminal installer's default).
 *
 * There is deliberately no user-account, hostname, time-zone or language step: the script has none
 * for a live install. Accounts are made when somebody first signs in with their Nostr key (the first
 * one becomes the administrator), and the time zone is System Settings → Date & Time. Inventing
 * fields the script would ignore is how an installer lies.
 *
 * Loaded on demand by os.js (only a live boot ever opens it), and DOM-free in its logic half so
 * tests/test_os_installer.py can run `PCInstaller._pure` under node.
 */
(function(root){
  'use strict';

  const H = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const PC = () => root.__PC || null;
  const BR = () => root.pcInstaller || null;
  const ic = (id) => `<svg class="ic pci-ic" aria-hidden="true"><use href="#${id}"></use></svg>`;

  /* ------------------------------------------------------------------ the logic (tested) */

  const STEPS = [
    { id: 'welcome',  label: 'Welcome' },
    { id: 'disk',     label: 'Disk' },
    { id: 'security', label: 'Encryption' },
    { id: 'summary',  label: 'Summary' },
    { id: 'install',  label: 'Install' },
  ];
  const ROOT_NAME = /^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$/;

  function fmtBytes(n){
    n = Number(n) || 0;
    const u = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
    let i = 0;
    while(n >= 1000 && i < u.length - 1){ n /= 1000; i++; }
    return (i && n < 10 ? n.toFixed(1) : Math.round(n)) + ' ' + u[i];
  }
  function busLabel(d){
    const t = String(d && d.tran || '').toLowerCase();
    if(/^nvme/.test(t) || /^nvme/.test(String(d && d.name || ''))) return 'NVMe';
    if(t === 'usb') return 'USB';
    if(t === 'sata' || t === 'ata') return 'SATA';
    if(/^mmc/.test(String(d && d.name || ''))) return 'eMMC';
    if(/^vd/.test(String(d && d.name || ''))) return 'Virtual';
    return t ? t.toUpperCase() : '';
  }
  function diskTitle(d){ return (d && d.model) ? d.model : 'Unnamed disk'; }

  /* The one question each step answers before Next is allowed. Returned as a sentence, because a
   * greyed-out button with no reason is a question the person has to answer by guessing. */
  function blocker(step, s){
    s = s || {};
    if(step === 'welcome') return s.info && !s.info.available ? 'This is not a PosterChanOS live session.' : '';
    if(step === 'disk'){
      const d = (s.disks || []).find(x => x.name === s.disk);
      if(!d) return 'Choose the disk to install PosterChanOS on.';
      if(!d.selectable) return d.why || 'That disk cannot be used.';
      if(s.mode === 'resume' && !d.posterchanLayout) return 'There is nothing on that disk to resume.';
      return '';
    }
    if(step === 'security'){
      if(!s.password) return 'Choose an encryption password.';
      if(/[\r\n\0]/.test(s.password)) return 'The password cannot contain a line break.';
      if(s.mode !== 'resume' && s.password !== s.password2) return 'The two passwords are not the same.';
      if(!ROOT_NAME.test(String(s.rootName || ''))) return 'The system volume name may use letters, digits, - and _ (up to 32).';
      return '';
    }
    if(step === 'summary'){
      const d = (s.disks || []).find(x => x.name === s.disk);
      if(s.mode !== 'resume' && (!d || String(s.typed || '').trim() !== d.name))
        return 'Type the disk’s name to confirm it may be erased.';
      return '';
    }
    return '';
  }

  /* What the bar and the checklist show, from the bridge's status. The bridge already turned the
   * log into { stage, percent }; this only groups the script's phases into the five a person reads. */
  const PHASES = [
    { id: 'prepare', label: 'Preparing the disk',        stages: ['medium', 'disk', 'format', 'mount'] },
    { id: 'copy',    label: 'Copying PosterChanOS',       stages: ['copy'] },
    { id: 'boot',    label: 'Installing the boot loader', stages: ['kernel', 'initramfs', 'accounts', 'bootloader'] },
    { id: 'desktop', label: 'Setting up the desktop',     stages: ['shell'] },
    { id: 'verify',  label: 'Checking the result',        stages: ['verify', 'done'] },
  ];
  function phaseStates(status){
    const st = status || {};
    const stage = st.progress && st.progress.stage || '';
    const at = PHASES.findIndex(p => p.stages.indexOf(stage) >= 0);
    const ok = !!(st.finished && st.ok);
    const failed = !!(st.finished && !st.ok);
    return PHASES.map((p, i) => ({
      id: p.id, label: p.label,
      state: ok ? 'done' : i < at ? 'done' : i === at ? (failed ? 'failed' : 'active')
           : (failed && at < 0 && i === 0) ? 'failed' : 'todo',
    }));
  }
  function view(status){
    const st = status || {};
    if(st.running || st.launching) return 'running';
    if(st.finished) return st.ok ? 'done' : 'failed';
    return 'idle';
  }
  function tailLines(log, n){
    const lines = String(log || '').split('\n').filter(l => l.trim() && !/^::pc-install::/.test(l));
    return lines.slice(-(n || 12)).join('\n');
  }

  /* ------------------------------------------------------------------ the page */

  /* Module state, so closing the window and opening it again keeps the choices — and, more to the
   * point, so a running install is found again rather than offered a second time. The password is
   * the exception: it is dropped the moment it has been handed to the installer. */
  const S = { step: 'welcome', info: null, disks: null, disksErr: '', disk: '', size: null, mode: 'fresh',
              password: '', password2: '', rootName: 'gentoo', typed: '', status: null, showLog: false,
              busy: false, error: '' };

  function paint(host){
    let dead = false, timer = 0;
    const bridge = BR();
    host.innerHTML = '';
    const el = document.createElement('div');
    el.className = 'pci';
    host.appendChild(el);

    if(!bridge){
      el.innerHTML = `<div class="pci-empty">${ic('i-drive')}<h2>Installer unavailable</h2>
        <p>The PosterChanOS installer runs in the live session of a PosterChanOS USB stick or DVD.</p></div>`;
      return () => {};
    }

    const toast = (m) => { try{ PC().toast(m); }catch(_){} };
    const idx = (id) => STEPS.findIndex(x => x.id === id);
    const cur = () => (S.disks || []).find(x => x.name === S.disk) || null;

    function rail(){
      /* A finished install ticks its own step too — every step is behind the person now. */
      const at = S.step === 'install' && view(S.status) === 'done' ? STEPS.length : idx(S.step);
      return `<aside class="pci-rail">
        <div class="pci-brand"><img src="/static/posterchan-relay.png" alt="" onerror="this.remove()">
          <span><b>PosterChanOS</b><small>Installer</small></span></div>
        <ol class="pci-steps">${STEPS.map((x, i) => `<li class="${i < at ? 'done' : i === at ? 'on' : ''}">
          <span class="pci-num">${i < at ? ic('i-check') : i + 1}</span><span>${H(x.label)}</span></li>`).join('')}</ol>
        <div class="pci-rail-foot">${ic('i-shield')}<span>gentoo.sh does the install.<br>This window only asks.</span></div>
      </aside>`;
    }
    function frame(title, sub, body, foot){
      el.innerHTML = rail() + `<main class="pci-main">
        <header class="pci-head"><h2>${H(title)}</h2>${sub ? `<p>${H(sub)}</p>` : ''}</header>
        <section class="pci-body">${body}</section>
        ${foot == null ? '' : `<footer class="pci-foot"><span class="pci-why" data-why></span>${foot}</footer>`}
      </main>`;
      el.querySelectorAll('[data-go]').forEach(b => b.onclick = () => go(b.dataset.go));
    }
    function nextFoot(label, danger){
      return `<button class="btn btn-ghost" data-back>Back</button>
        <button class="btn ${danger ? 'btn-danger' : 'btn-neon'}" data-next>${H(label || 'Next')}</button>`;
    }
    function bindNav(onNext){
      const back = el.querySelector('[data-back]'), next = el.querySelector('[data-next]');
      if(back) back.onclick = () => { const i = idx(S.step); if(i > 0) go(STEPS[i - 1].id); };
      if(next) next.onclick = () => { if(!refreshNext()) return; onNext ? onNext() : go(STEPS[idx(S.step) + 1].id); };
      refreshNext();
    }
    /* Next says WHY it is disabled, beside it, in words. */
    function refreshNext(){
      const why = blocker(S.step, S), next = el.querySelector('[data-next]'), note = el.querySelector('[data-why]');
      if(next) next.disabled = !!why || S.busy;
      if(note) note.textContent = why;
      return !why && !S.busy;
    }
    function go(step){ S.step = step; S.error = ''; draw(); }

    /* ---------------------------------------------------------------- welcome */
    function drawWelcome(){
      const info = S.info || {};
      const checks = [
        { ok: !!info.available, text: info.available ? 'Running from PosterChanOS live media' : 'Not a PosterChanOS live session' },
        { ok: !!info.efi, warn: !info.efi, text: info.efi ? 'Started in UEFI mode'
          : 'Started in legacy BIOS mode — PosterChanOS installs a UEFI boot loader, so restart and pick the UEFI entry for this USB in your firmware’s boot menu first' },
      ];
      frame('Install PosterChanOS', 'Put the system you are using right now onto this computer’s disk.',
        `<div class="pci-hero">
           <div class="pci-hero-mark">${ic('i-drive')}</div>
           <ul class="pci-points">
             <li>${ic('i-check')}<span>Everything you see in this live session is copied onto the disk you choose.</span></li>
             <li>${ic('i-lock')}<span>The disk is <b>encrypted</b> (LUKS) and formatted with <b>btrfs</b>, with snapshots.</span></li>
             <li>${ic('i-key')}<span>You sign in with your Nostr key when it first starts. The first person to sign in becomes its administrator.</span></li>
             <li>${ic('i-warn')}<span>The chosen disk is <b>erased</b>. Anything else on it — another operating system, your files — is gone.</span></li>
           </ul>
         </div>
         <div class="pci-checks">${checks.map(c => `<div class="pci-check ${c.ok ? 'ok' : c.warn ? 'warn' : 'bad'}">
           ${ic(c.ok ? 'i-check' : 'i-warn')}<span>${H(c.text)}</span></div>`).join('')}</div>`,
        `<button class="btn btn-neon" data-next>Get started</button>`);
      bindNav();
    }

    /* ---------------------------------------------------------------- disk */
    async function loadDisks(){
      S.disksErr = '';
      try{ S.disks = await bridge.disks(); }
      catch(e){ S.disks = []; S.disksErr = String((e && e.message) || e); }
      const d = cur();
      if(!d || !d.selectable){
        const pick = (S.disks || []).filter(x => x.selectable && !x.small);
        S.disk = pick.length === 1 ? pick[0].name : (d && d.selectable ? d.name : '');
      }
      if(!(cur() && cur().posterchanLayout)) S.mode = 'fresh';
      S.size = cur() ? cur().size : null;
    }
    function drawDisk(){
      const list = S.disks;
      const rows = !list ? `<div class="pci-loading"><div class="spinner"></div><span>Looking for disks…</span></div>`
        : !list.length ? `<div class="pci-note bad">${ic('i-warn')}<span>${S.disksErr ? H('Could not list the disks: ' + S.disksErr)
            : 'No disk was found. If this computer has an NVMe drive, its controller may be in RAID/RST mode and need a firmware setting changed.'}</span></div>`
        : list.map(d => `<label class="pci-disk${d.selectable ? '' : ' off'}${S.disk === d.name ? ' on' : ''}">
            <input type="radio" name="pci-disk" value="${H(d.name)}" ${S.disk === d.name ? 'checked' : ''} ${d.selectable ? '' : 'disabled'}>
            <span class="pci-disk-ic">${ic('i-drive')}</span>
            <span class="pci-disk-main">
              <b>${H(diskTitle(d))}</b>
              <small>/dev/${H(d.name)}${busLabel(d) ? ' · ' + H(busLabel(d)) : ''}${d.removable ? ' · removable' : ''}</small>
              <span class="pci-parts">${d.parts && d.parts.length
                ? d.parts.map(p => `<i>${H(p.label || p.fstype || p.name)} · ${H(fmtBytes(p.size))}</i>`).join('')
                : '<i class="empty">empty — no partitions</i>'}</span>
              ${d.why ? `<span class="pci-disk-why">${H(d.why)}</span>` : ''}
              ${d.selectable && d.small ? `<span class="pci-disk-why">Smaller than 8 GB — PosterChanOS may not fit.</span>` : ''}
              ${d.selectable && d.posterchanLayout ? `<span class="pci-disk-tag">An earlier PosterChanOS install</span>` : ''}
            </span>
            <span class="pci-size">${H(fmtBytes(d.size))}</span>
          </label>`).join('');
      const d = cur();
      const resume = d && d.selectable && d.posterchanLayout ? `<div class="pci-modes">
          <label class="${S.mode === 'fresh' ? 'on' : ''}"><input type="radio" name="pci-mode" value="fresh" ${S.mode === 'fresh' ? 'checked' : ''}>
            <b>Erase and install</b><small>A clean start. Everything on this disk is erased.</small></label>
          <label class="${S.mode === 'resume' ? 'on' : ''}"><input type="radio" name="pci-mode" value="resume" ${S.mode === 'resume' ? 'checked' : ''}>
            <b>Reinstall onto the existing encryption</b><small>For finishing an install that stopped part-way. Needs the password it was made with.</small></label>
        </div>` : '';
      frame('Where should PosterChanOS go?', 'Choose a whole disk. The drive this live system is running from is not offered.',
        `<div class="pci-toolbar"><button class="btn btn-ghost small" data-refresh>${ic('i-refresh')} Rescan</button></div>
         <div class="pci-disks">${rows}</div>${resume}`, nextFoot());
      el.querySelectorAll('input[name="pci-disk"]').forEach(r => r.onchange = () => {
        S.disk = r.value; S.typed = ''; S.size = cur() ? cur().size : null;
        if(!(cur() && cur().posterchanLayout)) S.mode = 'fresh';
        drawDisk();
      });
      el.querySelectorAll('input[name="pci-mode"]').forEach(r => r.onchange = () => { S.mode = r.value; S.password2 = ''; drawDisk(); });
      const rf = el.querySelector('[data-refresh]');
      if(rf) rf.onclick = async () => { S.disks = null; drawDisk(); await loadDisks(); if(!dead && S.step === 'disk') drawDisk(); };
      bindNav();
    }

    /* ---------------------------------------------------------------- security */
    function strength(p){
      const n = String(p || '').length;
      if(!n) return { w: 0, t: '' };
      if(n < 8) return { w: 25, t: 'Short — easy to guess', c: 'bad' };
      if(n < 12) return { w: 55, t: 'Fair', c: 'warn' };
      if(n < 16) return { w: 80, t: 'Good', c: 'ok' };
      return { w: 100, t: 'Strong', c: 'ok' };
    }
    function drawSecurity(){
      const resume = S.mode === 'resume';
      const sg = strength(S.password);
      frame(resume ? 'Unlock the existing install' : 'Protect the disk',
        resume ? 'Enter the password this disk was encrypted with.'
               : 'The whole disk is encrypted. This password is its recovery key.',
        `<div class="pci-form">
           <label class="pci-field"><span>${resume ? 'Disk password' : 'Encryption password'}</span>
             <span class="pci-pw"><input class="input" type="password" data-pw autocomplete="new-password" value="${H(S.password)}">
             <button class="btn btn-ghost small" data-show type="button">${ic('i-eye')}</button></span></label>
           ${resume ? '' : `<div class="pci-meter ${sg.c || ''}"><i style="width:${sg.w}%"></i></div><small class="pci-meter-t" data-meter>${H(sg.t)}</small>
           <label class="pci-field"><span>Type it again</span><input class="input" type="password" data-pw2 autocomplete="new-password" value="${H(S.password2)}"></label>`}
           <div class="pci-note">${ic('i-lock')}<span>This computer unlocks the disk by itself when it starts, so you will not be asked for this
             at every boot. Keep it somewhere safe anyway: it is what opens the disk from a live USB if the installed system ever
             cannot start, and nothing can recover it.</span></div>
           <div class="pci-note">${ic('i-user')}<span>There is no account to make here. When PosterChanOS first starts, sign in with your
             Nostr key; the first person to sign in becomes the administrator. Set the time zone afterwards in
             System Settings → Date &amp; Time.</span></div>
           <details class="pci-adv"${S.rootName !== 'gentoo' ? ' open' : ''}><summary>Advanced</summary>
             <label class="pci-field"><span>System volume name <small>(the btrfs subvolume the system lives in)</small></span>
               <input class="input" data-root value="${H(S.rootName)}" spellcheck="false" maxlength="32"></label>
           </details>
         </div>`, nextFoot());
      const pw = el.querySelector('[data-pw]'), pw2 = el.querySelector('[data-pw2]'), rn = el.querySelector('[data-root]');
      const meter = () => {
        const m = el.querySelector('.pci-meter'), t = el.querySelector('[data-meter]'), g = strength(S.password);
        if(m){ m.className = 'pci-meter ' + (g.c || ''); m.firstElementChild.style.width = g.w + '%'; }
        if(t) t.textContent = g.t;
      };
      if(pw){ pw.oninput = () => { S.password = pw.value; meter(); refreshNext(); }; setTimeout(() => { try{ pw.focus(); }catch(_){} }, 30); }
      if(pw2) pw2.oninput = () => { S.password2 = pw2.value; refreshNext(); };
      if(rn) rn.oninput = () => { S.rootName = rn.value.trim(); refreshNext(); };
      const show = el.querySelector('[data-show]');
      if(show) show.onclick = () => { const t = pw.type === 'password' ? 'text' : 'password'; pw.type = t; if(pw2) pw2.type = t; };
      el.querySelectorAll('.pci-form input').forEach(i => i.addEventListener('keydown', e => {
        if(e.key === 'Enter'){ const n = el.querySelector('[data-next]'); if(n && !n.disabled) n.click(); }
      }));
      bindNav();
    }

    /* ---------------------------------------------------------------- summary */
    function drawSummary(){
      const d = cur() || {};
      const resume = S.mode === 'resume';
      const rows = [
        ['Disk', `${diskTitle(d)} — /dev/${d.name} (${fmtBytes(d.size)})`],
        ['What happens', resume ? 'The existing encrypted layout is opened and PosterChanOS is written into it'
          : d.parts && d.parts.length ? `All ${d.parts.length} partition${d.parts.length === 1 ? '' : 's'} on it are erased` : 'The empty disk is partitioned'],
        ['Layout', resume ? 'Kept as it is' : '2 GB EFI system partition + the rest encrypted'],
        ['Encryption', 'LUKS, unlocked automatically at startup; your password is the recovery key'],
        ['Filesystem', `btrfs — system in @${S.rootName}, with @home and snapshots`],
        ['Boot loader', 'systemd-boot (UEFI), registered with the firmware'],
      ];
      frame('Ready to install', resume ? 'Check the details, then install.' : 'Check the details. Nothing has been changed yet.',
        `<dl class="pci-sum">${rows.map(r => `<dt>${H(r[0])}</dt><dd>${H(r[1])}</dd>`).join('')}</dl>
         ${resume ? '' : `<div class="pci-danger">
           <div class="pci-danger-h">${ic('i-warn')}<b>This erases /dev/${H(d.name)}</b></div>
           <p>Everything on ${H(diskTitle(d))} will be permanently deleted. To confirm, type the disk's name —
             <code>${H(d.name)}</code> — below.</p>
           <input class="input" data-typed spellcheck="false" autocomplete="off" placeholder="${H(d.name)}" value="${H(S.typed)}">
         </div>`}
         ${S.error ? `<div class="pci-note bad">${ic('i-warn')}<span>${H(S.error)}</span></div>` : ''}`,
        nextFoot(resume ? 'Install' : 'Erase and install', !resume));
      const t = el.querySelector('[data-typed]');
      if(t){ t.oninput = () => { S.typed = t.value; refreshNext(); }; setTimeout(() => { try{ t.focus(); }catch(_){} }, 30); }
      bindNav(start);
    }

    async function start(){
      const d = cur();
      if(!d) return go('disk');
      /* The typed name is the deliberate act; this is the last look, in the app's own dialog (a
       * native confirm() wedges the Electron renderer's focus — see feedback_no_native_dialogs). */
      if(S.mode !== 'resume'){
        let ok = false;
        try{
          ok = await PC().uiConfirm(`Erase ${diskTitle(d)} (/dev/${d.name}, ${fmtBytes(d.size)}) and install PosterChanOS?\n\nThis cannot be undone.`,
                                    { ok: 'Erase and install', cancel: 'Go back', danger: true, owner: el });
        }catch(_){ ok = false; }
        if(!ok || dead) return;
      }
      S.busy = true; refreshNext();
      try{
        const st = await bridge.start({ disk: d.name, size: d.size, rootName: S.rootName, password: S.password, mode: S.mode });
        S.password = ''; S.password2 = ''; S.typed = '';
        S.status = st; S.showLog = false;
        go('install');
      }catch(e){
        S.error = String((e && e.message) || e).replace(/^Error invoking remote method '[^']+': (Error: )?/, '');
        if(!dead && S.step === 'summary') drawSummary();
      }finally{ S.busy = false; if(!dead) refreshNext(); }
    }

    /* ---------------------------------------------------------------- install */
    function drawInstall(){
      const st = S.status || {};
      const v = view(st);
      const pct = Math.max(0, Math.min(100, Math.round((st.progress && st.progress.percent) || (v === 'done' ? 100 : 0))));
      const phases = phaseStates(st);
      const label = v === 'done' ? 'PosterChanOS is installed'
        : v === 'failed' ? 'The install did not finish'
        : (st.progress && st.progress.label) || st.message || 'Starting…';
      const copied = st.progress && st.progress.stage === 'copy' && st.progress.copied != null
        ? ` — ${st.progress.copied}% of the files` : '';
      const elapsed = st.started ? Math.max(0, Math.round(((st.finished || Date.now()) - st.started) / 1000)) : 0;
      const clock = `${Math.floor(elapsed / 60)}:${String(elapsed % 60).padStart(2, '0')}`;
      const disk = st.disk ? `${st.model ? st.model + ' — ' : ''}${st.disk}` : '';
      let panel = '';
      if(v === 'done'){
        panel = `<div class="pci-result ok">${ic('i-check')}<div><b>All done.</b>
            <p>PosterChanOS is on ${H(disk || 'the disk')}. Restart, and take the USB stick or disc out when the screen goes dark.</p></div></div>`;
      }else if(v === 'failed'){
        panel = `<div class="pci-result bad">${ic('i-warn')}<div><b>${H(st.message || 'The installer stopped.')}</b>
            <p>Nothing is lost from this live session. The last thing the installer said:</p>
            <pre class="pci-tail">${H(tailLines(st.log, 10) || '(it printed nothing)')}</pre></div></div>`;
      }
      const foot = v === 'done'
        ? `<button class="btn btn-ghost" data-stay>Keep trying PosterChanOS</button><button class="btn btn-neon" data-reboot>${ic('i-power')} Restart now</button>`
        : v === 'failed'
        ? `<button class="btn btn-ghost" data-retry-disk>Choose another disk</button><button class="btn btn-neon" data-retry>Try again</button>`
        : `<span class="pci-hint">You can close this window — the install keeps going.</span>`;
      frame(v === 'done' ? 'Installed' : v === 'failed' ? 'Install failed' : 'Installing PosterChanOS', disk,
        `<div class="pci-progress ${v}">
           <div class="pci-bar"><i style="width:${pct}%"></i></div>
           <div class="pci-bar-row"><span data-label>${H(label + copied)}</span><span><b>${pct}%</b> · ${clock}</span></div>
         </div>
         <ol class="pci-phases">${phases.map(p => `<li class="${p.state}">
           <span class="pci-dot">${p.state === 'done' ? ic('i-check') : p.state === 'failed' ? ic('i-close') : ''}</span>${H(p.label)}</li>`).join('')}</ol>
         ${panel}
         <div class="pci-logbox">
           <button class="btn btn-ghost small" data-log>${S.showLog ? 'Hide details' : 'Show details'}</button>
           ${S.showLog ? `<pre class="pci-log" data-logpre>${H(st.log || '')}</pre>` : ''}
         </div>`, foot);
      const lg = el.querySelector('[data-log]');
      if(lg) lg.onclick = () => { S.showLog = !S.showLog; drawInstall(); };
      const pre = el.querySelector('[data-logpre]');
      if(pre) pre.scrollTop = pre.scrollHeight;
      const rb = el.querySelector('[data-reboot]');
      if(rb) rb.onclick = async () => {
        try{ if(root.pcPower && root.pcPower.reboot){ await root.pcPower.reboot(); return; } }catch(e){ toast('could not restart: ' + ((e && e.message) || e)); return; }
        toast('restart from the power menu');
      };
      const stay = el.querySelector('[data-stay]');
      /* "Keep trying" closes the installer: the window's own close button where it is a frame on
       * the desktop, the document itself where it is its own toplevel. */
      if(stay) stay.onclick = () => {
        const x = el.closest('.osw') && el.closest('.osw').querySelector('.osw-x');
        if(x){ x.click(); return; }
        try{ root.close(); }catch(_){}
      };
      const rt = el.querySelector('[data-retry]');
      if(rt) rt.onclick = () => { S.status = null; go('security'); };
      const rd = el.querySelector('[data-retry-disk]');
      if(rd) rd.onclick = async () => { S.status = null; S.disks = null; go('disk'); await loadDisks(); if(!dead && S.step === 'disk') drawDisk(); };
    }

    async function poll(){
      if(dead) return;
      try{ S.status = await bridge.status(); }catch(_){}
      if(dead) return;
      if(S.step === 'install'){
        /* Repaint only the parts that move while the log is open, so a person reading it is not
         * scrolled back to the bottom every second by a full redraw. */
        const pre = el.querySelector('[data-logpre]');
        const reading = pre && pre.scrollTop + pre.clientHeight < pre.scrollHeight - 30;
        if(reading && view(S.status) === 'running'){
          const lab = el.querySelector('[data-label]'); if(lab && S.status.progress) lab.textContent = S.status.progress.label || '';
          const bar = el.querySelector('.pci-bar i'); if(bar) bar.style.width = ((S.status.progress && S.status.progress.percent) || 0) + '%';
        }else drawInstall();
      }
      if(view(S.status) === 'running') timer = setTimeout(poll, 1000);
    }

    function draw(){
      if(dead) return;
      if(S.step === 'welcome') return drawWelcome();
      if(S.step === 'disk'){
        drawDisk();
        if(!S.disks) loadDisks().then(() => { if(!dead && S.step === 'disk') drawDisk(); });
        return;
      }
      if(S.step === 'security') return drawSecurity();
      if(S.step === 'summary') return drawSummary();
      if(S.step === 'install'){
        drawInstall();
        clearTimeout(timer);
        if(view(S.status) === 'running') timer = setTimeout(poll, 1000);
        return;
      }
    }

    /* First paint: a job that is running (or has just ended) IS the screen — a reopened installer
     * must never offer to start a second install on top of the first. */
    (async () => {
      try{ S.info = await bridge.info(); }catch(_){ S.info = { available: false }; }
      try{ const st = await bridge.status(); if(st && (st.running || st.launching || (st.finished && S.step === 'install')))
             { S.status = st; S.step = 'install'; } }catch(_){}
      if(dead) return;
      draw();
    })();
    el.innerHTML = rail() + `<main class="pci-main"><div class="pci-loading"><div class="spinner"></div></div></main>`;
    return () => { dead = true; clearTimeout(timer); };
  }

  root.PCInstaller = { paint, _pure: { STEPS, PHASES, fmtBytes, busLabel, diskTitle, blocker, phaseStates, view, tailLines } };
})(typeof window !== 'undefined' ? window : globalThis);
