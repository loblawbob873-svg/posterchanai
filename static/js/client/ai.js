/* AI chat — the conversation view, the node-agent panel, the image/voice/music studios, voice
 * input and read-aloud, Live Translate, and the rendering of AI replies and their file actions.
 * Split out of app.js.
 *
 * Loaded on first use: app.js keeps a small block of entry points (search for `_aiDeps`) and builds
 * this factory the first time one is called — opening AI chat or Live Translate, a studio opened from
 * another screen, or a reminder popup. The code below is BYTE-IDENTICAL to what it replaced in
 * app.js apart from its reads of app.js's live `let` bindings, which the parser rewrote to
 * `S.<name>` (getters on `dep.state`) at exact identifier offsets.
 *
 * Stayed in app.js on purpose: `_ai` (the conversation state the effect studio and renderView read),
 * `_ltNorm` (compose's translate uses it), the OS-notification plumbing (`osNotify`), and the
 * save-to-drive helpers `_keepBytes`/`_keptToast`, which Files uses too.
 */
window.PCAiFactory = function(dep){
  const S = dep.state;   // live app.js bindings: S.LOGO, S.ME, S.VIEW, S._aiToken, S._narrateAudio, S.signer
  const {
    $, $$, FilesIdx, _absUrl, _ai, _artifactFile, _cameraPhoto, _effectReturnValid, _fileUnder,
    _forgetEffectReturn, _fxApplyMod, _fxSetEffect, _keepBytes, _keptToast, _ltNorm,
    _rememberReminder, _reminderOwner, _remindersChanged, _returnFromEffect, _serverOrigin,
    _signerLabel, _syncEffectReturn, _walkEntries, blossomPicker, closeModal, compose,
    compressImage, compressVideo, copyValue, eTags, enc, ensureAiSession, mdToHtml, modal,
    notificationAllowed, openCommandSheet, openLightbox, openMenuPopover, openOsNotificationRoute,
    osNotify, publish, saveBlobAs, selfProof, sendEffectReply, showEffectGuide, sign, startAiShare,
    startEffectStudio, switchView, toast, uiConfirm, uiPrompt, uploadBlob,
  } = dep;

  async function renderAI(opts){
    const feed=$('#feed'); feed.innerHTML='<div class="spinner"></div>';
    /* A BARE SPINNER IS NOT A STATE, IT IS THE ABSENCE OF ONE. This wait ends at a signer, which
     * can legitimately take tens of seconds — a remote signer re-sending a request destroyed while
     * its socket redialled, a phone the user has not picked up yet. All of that is fine; what was
     * not fine is that the screen said nothing about it and offered no way out, so "slow" and
     * "broken" looked identical and were reported as the same bug.
     *
     * So: leave the spinner alone for the first few seconds (most sessions start well inside that
     * and a flash of explanatory text would be noise), then say what is being waited for and offer
     * a start-over that ACTUALLY starts over — `force`, because plain Retry adopted the very
     * promise it was meant to escape. Nothing here cancels the attempt in flight: if the signer
     * answers late, that session is still good. */
    const slow=setTimeout(()=>{
      if(S.VIEW!=='ai' || !feed.isConnected || !feed.querySelector('.spinner')) return;
      feed.innerHTML=`<div class="ai-view ai-gate"><div class="spinner"></div>
        <p class="muted">${enc('Starting your app session — waiting for '+_signerLabel()+'…')}</p>
        <button class="btn btn-ghost" id="ai-session-restart">Start over</button></div>`;
      const b=$('#ai-session-restart'); if(b) b.onclick=()=>renderAI({force:true});
    }, 6000);
    let a;
    try{ a=await ensureAiSession(opts); }
    catch(e){
      clearTimeout(slow);
      if(S.VIEW!=='ai') return;
      const guest=!S.ME||!S.ME.pubkey;
      feed.innerHTML=`<div class="ai-view ai-gate"><h2><svg class="ic h-ic" aria-hidden="true"><use href="#i-ai"></use></svg>PosterChan AI</h2>
        <p class="muted">${enc(guest?'Sign in with a Nostr account to use AI.':((e&&e.message)||'Could not start an AI session.'))}</p>
        ${guest?'<button class="btn btn-neon" id="ai-signin">Sign in</button>':'<button class="btn btn-ghost" id="ai-session-retry">Retry</button>'}</div>`;
      const b=$(guest?'#ai-signin':'#ai-session-retry');
      if(b) b.onclick=()=>guest?showAuthGate():renderAI({force:true});
      return;
    }
    clearTimeout(slow);
    if(S.VIEW!=='ai') return;
    if(a.error){ feed.innerHTML='<div class="empty">Could not start an AI session — try again.</div>'; return; }
    if(a.can_ai){ return aiMount(feed); }
    feed.innerHTML=`<div class="ai-view ai-gate">
      <h2><svg class="ic h-ic" aria-hidden="true"><use href="#i-ai"></use></svg>PosterChan AI</h2>
      <p class="muted">AI access isn't enabled for your account yet. Request access and an admin will approve it.</p>
      <button class="btn btn-neon" id="ai-request">Request AI access</button>
      <div class="muted small" id="ai-request-status"></div></div>`;
    $('#ai-request').onclick=requestAiAccess;
  }
  async function requestAiAccess(){
    const s=$('#ai-request-status'); const b=$('#ai-request'); if(b) b.disabled=true;
    if(s) s.textContent='sending request…';
    try{
      const auth = await sign(27235, 'ai-request', [['p', S.ME.pubkey]]);
      const r = await fetch('/api/auth/ai-request', { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ pubkey: S.ME.pubkey, auth: btoa(JSON.stringify(auth)) }) }).then(r=>r.json());
      if(s) s.textContent = (r && r.ok) ? '✓ Request sent — an admin will approve it.' : ('Could not send request: '+((r&&r.error)||''));
    }catch(_){ if(s) s.textContent='Could not send request.'; }
    if(b) b.disabled=false;
  }

  // ----- the chat itself (ported from the old web UI; talks to /api/ws/chat over the session) -----
  // `hist` is what you have SENT, oldest first — ↑/↓ in the compose box walk it. `histIdx` is -1 whenever
  // you are editing your own live draft (which `histDraft` holds while you browse away from it).
  // The AI conversation id is REMEMBERED. It used to live only in memory, so leaving the view and
  // coming back created a brand-new empty chat while the render landed in the previous one — the
  // "started a generation, left, lost it" report. The result was never gone (the server persists
  // media into the message), it was just in a conversation nothing reopened.
  const _AI_CONV_KEY='aiLastConv';
  function _aiRememberConv(id){ try{ ClientSettings.set(_AI_CONV_KEY, id||0); }catch(_){ } }
  function _aiLastConv(){ try{ return +(ClientSettings.get(_AI_CONV_KEY,0)||0)||0; }catch(_){ return 0; } }
  let _aiWindowDraft='';
  function askWindowContext(ctx,instruction,opts){
    ctx=ctx||{}; instruction=String(instruction||'').trim(); if(!instruction)return false;
    const windows=Array.isArray(ctx.windows)&&ctx.windows.length?ctx.windows:[ctx];
    const blocks=windows.map((x,i)=>{const excerpt=String(x.selection||x.text||'').trim();return 'Window '+(i+1)+':\n- Title: '+String(x.title||'Window')+'\n- App: '+String(x.view||x.kind||'unknown')+
      (excerpt?'\n- '+(x.selection?'Selected content':'Visible content')+':\n'+excerpt:'\n- No page contents were shared; use only the app name and title.');});
    _aiWindowDraft=((opts&&opts.agent)?'node agent local ':'')+instruction+'\n\nWindow context (user explicitly shared):\n'+blocks.join('\n\n');
    switchView('ai');
    let tries=0; const place=()=>{const ta=$('#ai-input');if(ta){ta.value=_aiWindowDraft;_aiWindowDraft='';ta.dispatchEvent(new Event('input'));ta.focus();return;}if(++tries<30)setTimeout(place,50);};
    setTimeout(place,0); return true;
  }
  function _cookie(name){ const m=document.cookie.match(new RegExp('(?:^|; )'+name+'=([^;]*)')); return m?decodeURIComponent(m[1]):''; }

  // ---- Node Control panel: a beginner-friendly launcher for the agentic `node` command --------------
  // Read-only state (node NAMES + your own jobs) comes from /api/node/state, gated server-side exactly
  // like the command. Every ACTION here just fills #ai-input with a `node …` command and sends it through
  // the normal chat pipeline — so the panel adds NO privileged surface; it can't do anything you couldn't
  // already type. Access is cached per session so the toolbar button only shows for allowlisted users.
  async function _nodeFetchState(){
    try{
      const r=await fetch('/api/node/state', {credentials:'include', headers:S._aiToken?{'Authorization':'Bearer '+S._aiToken}:{}});
      if(!r.ok){ _ai.nodeAccess=false; return null; }
      _ai.nodeAccess=true; return await r.json();
    }catch(_){ return null; }
  }
  // Send a `node …` command into the AI chat. `fresh` = start a NEW conversation first (used by Run, so
  // each agent task gets its own chat to hold its output). Without a VALID conversation the chat WS closes
  // with "Conversation not found" (chat.py:1143) and the command lands nowhere — the bug the user hit — so
  // always guarantee a real conversation before sending (create one if missing, or a fresh one on Run).
  async function _nodeRun(cmd, fresh){
    closeModal();
    if(S.VIEW!=='ai'){ switchView('ai'); }
    if(fresh || !_ai.convId){ try{ await aiNewConversation(); }catch(_){} }
    const ta=$('#ai-input'); if(ta){ ta.value=cmd; aiSend(); }
  }
  /* Saved agent tasks — a per-user list you can re-run with one click; each is
   * {name, mode:'agent'|'cmd', node, all, text}.
   *
   * THIS WAS THE ONE THING IN THE PANEL WITH NO COPY ANYWHERE, and it was reported gone. It lived in
   * `pc_nostr_settings` in localStorage and nowhere else: not synced, not backed up, not in the
   * relay-change carry. Every other pref in these settings either syncs to `pcai:client-prefs` or
   * restores from a relay list, so after any localStorage loss they all come back and this is the
   * single visible casualty — which also makes the loss look like a targeted bug rather than what it
   * is. And it is not one failure: a moved app origin, a corrupt blob (`Settings.all()` returns `{}`
   * on ANY parse error, and the next `set` writes a fresh object over all 46 keys), or a quota
   * failure inside a swallowing try/catch all end the same way.
   *
   * So the list now lives in an ENCRYPTED kind-30078 doc, `d=pcai:agent-tasks`, NIP-44 to the user's
   * own key — the Notes/Budget shape, not `pcai:client-prefs`, which is published as PLAINTEXT.
   * These are shell commands and agent goals; syncing them in the clear to relays would trade a lost
   * task for a leaked one. localStorage stays as the local cache so the panel still opens offline
   * and on a device that has never read the doc.
   *
   * The rules below are Budget's, and each is load-bearing here for the same reason:
   *   - a read that no relay answered is NOT "no saved tasks" (`sawRelay`) — the empty-read wipe;
   *   - nothing is published until a read has succeeded, so an unreachable pool cannot replace the
   *     real list with the local cache's idea of it;
   *   - writes are serialized, so a rapid save-then-delete cannot publish two events built from the
   *     same base copy;
   *   - and the doc is pinned in BOTH `_isPinned` (store.js) and `_CARRY_D` above, because every
   *     private doc in this app has missed one of those at least once. */
  const _AGT_D = 'pcai:agent-tasks';
  let _agtDoc = null;          // null = not read yet; an array once a read has SUCCEEDED
  let _agtChain = Promise.resolve();
  let _agtLoading = null;
  function _agentSavedGet(){
    if(Array.isArray(_agtDoc)) return _agtDoc;
    try{ return ClientSettings.get('agentSavedTasks', [])||[]; }catch(_){ return []; }   // cache, until the doc lands
  }
  // Read the encrypted doc once per panel open, sharing one REQ across re-entrant renders (a second
  // would also mean a second decrypt prompt on an external signer).
  function _agentSavedLoad(){
    if(Array.isArray(_agtDoc)) return Promise.resolve(_agtDoc);
    if(!S.ME || !S.ME.pubkey) return Promise.resolve(_agentSavedGet());
    if(!_agtLoading) _agtLoading = _agentSavedRead().finally(()=>{ _agtLoading = null; });
    return _agtLoading;
  }
  async function _agentSavedRead(){
    let ev=null, sawRelay=false;
    for(let a=0; a<3 && !ev; a++){
      if(a) await new Promise(r=>setTimeout(r, 450*a));
      try{
        const evs = await Relay.query([{ authors:[S.ME.pubkey], kinds:[30078], '#d':[_AGT_D], limit:1 }]);
        sawRelay = true;
        ev = (evs||[]).sort((x,y)=>y.created_at-x.created_at)[0] || null;
      }catch(_){}
    }
    if(!sawRelay) return _agentSavedGet();      // relays silent → keep showing the cache, and do NOT arm writes
    if(!ev){
      /* No doc yet. A device that already has tasks in localStorage is the MIGRATION case: adopt
       * them and publish, so the first open after this ships is what puts them somewhere durable. */
      _agtDoc = _agentSavedGet().slice();
      if(_agtDoc.length) _agentSavedPublish();
      return _agtDoc;
    }
    // `signer`, not `PC` — inside app.js the bridge is window.__PC and `PC` is simply undefined.
    // Budget calls PC.nip44dec because budget.js is a separate module with its own handle; lifting
    // that line in here would have thrown on the first read and been caught by the `catch(_)` below,
    // i.e. it would have looked exactly like an undecryptable doc and silently never synced.
    if(typeof ev.content!=='string' || !ev.content) return _agentSavedGet();
    let raw=''; try{ raw = await S.signer.nip44dec(S.ME.pubkey, ev.content); }
    catch(_){ return _agentSavedGet(); }        // undecryptable → never overwrite it with the cache
    let d=null; try{ d = JSON.parse(raw); }catch(_){ d = null; }
    const list = Array.isArray(d) ? d : (d && Array.isArray(d.tasks) ? d.tasks : null);
    if(!list) return _agentSavedGet();
    _agtDoc = list;
    try{ ClientSettings.set('agentSavedTasks', list); }catch(_){}   // refresh the offline cache
    return _agtDoc;
  }
  function _agentSavedPublish(){
    const done = _agtChain.catch(()=>{}).then(async ()=>{
      const ct = await S.signer.nip44enc(S.ME.pubkey, JSON.stringify(_agtDoc||[]));
      const r = await publish(30078, ct, [['d', _AGT_D]], {quiet:true});
      if(!(r && r.ok)) throw new Error('relay rejected the write');
    });
    _agtChain = done.catch(()=>{});
    done.catch(()=>toast('couldn’t sync that task — it is saved on this device only'));
    return done;
  }
  function _agentSavedSet(list){
    const next = list||[];
    try{ ClientSettings.set('agentSavedTasks', next); }catch(_){}   // local cache first: it must work offline
    /* Publish only once a read has SUCCEEDED. Before that `_agtDoc` is null, and writing would push
     * this device's cache over a doc we have not seen — the replaceable-doc wipe, with somebody
     * else's device holding the real list. */
    if(!Array.isArray(_agtDoc) || !S.ME || !S.ME.pubkey) return;
    _agtDoc = next;
    _agentSavedPublish();
  }
  function _agentTaskCmd(t){
    const tgt = t.all ? 'all' : (t.node||'local');
    return t.mode==='agent' ? `node agent ${tgt} ${t.text}` : (t.all?`node all ${t.text}`:`node ${tgt} ${t.text}`);
  }
  async function openNodePanel(){
    const state=await _nodeFetchState();
    if(_ai.nodeAccess===false){ toast('Node access isn’t enabled for your account'); return; }
    if(!state){ toast('Couldn’t load node state — try again'); return; }
    const nodes=state.nodes||[]; const jobs=state.jobs||[];
    if(!nodes.length){ toast('No nodes are configured'); return; }
    // Default the picker to the sandbox (the Debian container) when it's offered — agentic tasks
    // belong there, and defaulting to the first node ('local') repeatedly sent runs to a bare host
    // by accident ("forgot the node"). Falls back to the first node when there's no sandbox.
    const _defNode=nodes.includes('sandbox')?'sandbox':nodes[0];
    const nodeOpts=nodes.map(n=>`<option value="${enc(n)}"${n===_defNode?' selected':''}>${enc(n)}</option>`).join('');
    // With a single target (a sandbox-only user has just their own container) the Node picker is
    // meaningless — hide it. Nostr changed the transport, not that a multi-node user still picks a box.
    const soloSandbox = nodes.length===1 && nodes[0]==='sandbox';
    const oneNode = nodes.length<=1;
    const jobRow=j=>{
      const ic=j.status==='running'?'⚙️':(j.status==='done'?(j.exit_code===0?'✅':'⚠️'):(j.status==='killed'?'⏹️':'❌'));
      const meta=j.status==='running'?'running':(j.exit_code!=null?('exit '+j.exit_code):j.status);
      return `<div class="node-job">
        <span class="nj-ic">${ic}</span><span class="nj-id">#${enc(String(j.id))}</span>
        <span class="nj-node">${enc(j.node||'')}</span>
        <code class="nj-cmd" title="${enc(j.command||'')}">${enc(j.command||'')}</code>
        <span class="nj-status ${j.status==='running'?'run':''}">${enc(meta)}</span>
        <span class="nj-acts">${j.status==='running'?`<button class="btn btn-ghost small nj-kill" data-id="${enc(String(j.id))}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-stop"></use></svg>Kill</button>`:''}<button class="btn btn-ghost small nj-log" data-id="${enc(String(j.id))}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-text"></use></svg>Log</button></span>
      </div>`;
    };
    modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-ai"></use></svg>Agents</h3>
      <div class="node-panel">
        <p class="muted small np-intro">${soloSandbox?'Run agentic tasks in your private 🐳 Debian sandbox — ask in plain English and the agent figures out the commands, or run a raw shell command. It runs isolated from the host; output shows up in the chat below.':'Run things on your servers — ask in plain English and the agent figures out the commands, or run a raw shell command. Output shows up in the chat below.'}</p>
        <div class="np-row${oneNode?' hidden':''}">
          <label class="np-lbl">Node</label>
          <select class="input" id="np-node">${nodeOpts}</select>
          <label class="np-all"><input type="checkbox" id="np-all"> All nodes</label>
        </div>
        <div class="np-modes">
          <label class="np-mode active"><input type="radio" name="np-mode" value="agent" checked> 🤖 Ask the agent</label>
          <label class="np-mode"><input type="radio" name="np-mode" value="cmd"> ⌨️ Run a command</label>
        </div>
        <textarea class="input" id="np-input" rows="2"></textarea>
        <div class="np-egs" id="np-egs"></div>
        <div class="np-run"><span class="muted small np-hint">Ctrl+Enter to run</span><button class="btn btn-ghost small" id="np-save-cur" title="Save this task to run again later"><svg class="ic b-ic" aria-hidden="true"><use href="#i-star"></use></svg>Save task</button><button class="btn btn-neon" id="np-go"><svg class="ic b-ic" aria-hidden="true"><use href="#i-play"></use></svg>Run</button></div>
        <div class="np-tabs">
          <button class="np-tab active" data-tab="recent"><svg class="ic b-ic" aria-hidden="true"><use href="#i-clock"></use></svg>Recent jobs</button>
          <button class="np-tab" data-tab="saved"><svg class="ic b-ic" aria-hidden="true"><use href="#i-star"></use></svg>Saved</button>
        </div>
        <div class="np-tabpanel" data-panel="recent">
          ${jobs.length?`<div class="node-jobs">${jobs.map(jobRow).join('')}</div>`:'<p class="muted small">No jobs yet — run something above.</p>'}
        </div>
        <div class="np-tabpanel hidden" data-panel="saved"><div class="np-saved" id="np-saved"></div></div>
      </div>`, root=>{
      const inp=$('#np-input',root), nodeSel=$('#np-node',root), allCb=$('#np-all',root), egBox=$('#np-egs',root);
      const AGENT_EGS=['check disk space and memory use','why is the app slow right now?','show the last 40 lines of the service log','is anything eating CPU?'];
      const CMD_EGS=['df -h','uptime','systemctl status posterchanai','free -m'];
      const mode=()=> ((root.querySelector('input[name="np-mode"]:checked')||{}).value)||'agent';
      const paint=()=>{ const m=mode();
        inp.placeholder = m==='agent' ? 'Describe what you want — e.g. why is disk usage high on /var?' : 'Shell command — e.g. df -h';
        egBox.innerHTML = (m==='agent'?AGENT_EGS:CMD_EGS).map(e=>`<button class="np-eg">${enc(e)}</button>`).join('');
      };
      paint();
      $$('.np-mode',root).forEach(l=> l.addEventListener('change',()=>{ $$('.np-mode',root).forEach(x=>x.classList.toggle('active',x.querySelector('input').checked)); paint(); }));
      egBox.addEventListener('click',e=>{ const b=e.target.closest('.np-eg'); if(b){ inp.value=b.textContent; inp.focus(); } });
      const run=()=>{ const text=inp.value.trim(); if(!text){ inp.focus(); return; }
        const all=allCb.checked, tgt=all?'all':nodeSel.value;
        const cmd = mode()==='agent' ? `node agent ${tgt} ${text}` : (all?`node all ${text}`:`node ${tgt} ${text}`);
        _nodeRun(cmd, true);   // fresh conversation for this run
      };
      $('#np-go',root).onclick=run;
      inp.addEventListener('keydown',e=>{ if((e.ctrlKey||e.metaKey)&&e.key==='Enter'){ e.preventDefault(); run(); } });
      root.querySelectorAll('.nj-kill').forEach(b=> b.onclick=()=>_nodeRun('node kill '+b.dataset.id));
      root.querySelectorAll('.nj-log').forEach(b=> b.onclick=()=>_nodeRun('node log '+b.dataset.id));
      // --- tabs: Recent jobs / Saved tasks ---
      $$('.np-tab',root).forEach(t=> t.onclick=()=>{
        $$('.np-tab',root).forEach(x=>x.classList.toggle('active',x===t));
        $$('.np-tabpanel',root).forEach(p=>p.classList.toggle('hidden', p.dataset.panel!==t.dataset.tab));
      });
      // --- saved tasks: render, run, EDIT, delete ---
      // A saved task was write-once: you could run or delete it, but a typo in the command (or wanting
      // it pointed at a different node) meant deleting it and building it again from scratch. `editIdx`
      // swaps that one row for a real editor — every field a task has, in the same modal, no dialogs.
      const savedBox=$('#np-saved',root);
      const renderSaved=(editIdx)=>{ const list=_agentSavedGet();
        if(!list.length){ savedBox.innerHTML='<p class="muted small">No saved tasks yet. Set up a task above and hit ⭐ Save task.</p>'; return; }
        const rowHtml=(t,i)=>`<div class="np-saved-row">
          <span class="nj-ic">${t.mode==='agent'?'🤖':'⌨️'}</span>
          <span class="np-saved-name" title="${enc(_agentTaskCmd(t))}">${enc(t.name||t.text)}</span>
          <span class="np-saved-tgt muted small">${enc(t.all?'all':(t.node||'local'))}</span>
          <span class="nj-acts"><button class="btn btn-neon small nps-run" data-i="${i}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-play"></use></svg>Run</button><button class="btn btn-ghost small nps-edit" data-i="${i}" title="Edit this task"><svg class="ic b-ic" aria-hidden="true"><use href="#i-pen"></use></svg></button><button class="btn btn-red small nps-del" data-i="${i}" title="Delete this task"><svg class="ic b-ic" aria-hidden="true"><use href="#i-trash"></use></svg></button></span>
        </div>`;
        const editHtml=(t,i)=>`<div class="np-saved-edit" data-i="${i}">
          <label class="fld">Name<input class="input" id="nse-name" value="${enc(t.name||'')}" placeholder="e.g. Nightly disk check"></label>
          <div class="np-modes">
            <label class="np-mode${t.mode==='agent'?' active':''}"><input type="radio" name="nse-mode" value="agent"${t.mode==='agent'?' checked':''}> 🤖 Ask the agent</label>
            <label class="np-mode${t.mode==='agent'?'':' active'}"><input type="radio" name="nse-mode" value="cmd"${t.mode==='agent'?'':' checked'}> ⌨️ Run a command</label>
          </div>
          <div class="np-row${oneNode?' hidden':''}">
            <label class="np-lbl">Node</label>
            <select class="input" id="nse-node">${nodes.map(n=>`<option value="${enc(n)}"${n===(t.node||_defNode)?' selected':''}>${enc(n)}</option>`).join('')}</select>
            <label class="np-all"><input type="checkbox" id="nse-all"${t.all?' checked':''}> All nodes</label>
          </div>
          <textarea class="input" id="nse-text" rows="3" placeholder="What should this task do?">${enc(t.text||'')}</textarea>
          <div class="np-run"><button class="btn btn-ghost small" id="nse-cancel">Cancel</button><button class="btn btn-neon small" id="nse-save"><svg class="ic b-ic" aria-hidden="true"><use href="#i-check"></use></svg>Save</button></div>
        </div>`;
        savedBox.innerHTML=list.map((t,i)=> i===editIdx ? editHtml(t,i) : rowHtml(t,i)).join('');
        savedBox.querySelectorAll('.nps-run').forEach(b=> b.onclick=()=>{ const t=_agentSavedGet()[+b.dataset.i]; if(t) _nodeRun(_agentTaskCmd(t), true); });
        savedBox.querySelectorAll('.nps-edit').forEach(b=> b.onclick=()=>renderSaved(+b.dataset.i));
        savedBox.querySelectorAll('.nps-del').forEach(b=> b.onclick=async()=>{ const list=_agentSavedGet(); const t=list[+b.dataset.i]; if(t&&await uiConfirm(`Delete saved task “${t.name||t.text}”?`)){ list.splice(+b.dataset.i,1); _agentSavedSet(list); renderSaved(); } });
        const ed=$('.np-saved-edit',savedBox);
        if(ed){
          $$('.np-mode',ed).forEach(l=> l.addEventListener('change',()=>$$('.np-mode',ed).forEach(x=>x.classList.toggle('active',x.querySelector('input').checked))));
          $('#nse-cancel',ed).onclick=()=>renderSaved();
          $('#nse-save',ed).onclick=()=>{
            const txt=($('#nse-text',ed).value||'').trim();
            if(!txt){ toast('The task can’t be empty'); $('#nse-text',ed).focus(); return; }
            // Re-read the list at SAVE time: it may have changed under an open editor (another row
            // deleted), and writing back a stale copy would resurrect what was deleted.
            const cur=_agentSavedGet(); const i=+ed.dataset.i;
            if(!cur[i]){ toast('That task is gone'); renderSaved(); return; }
            cur[i]={name:(($('#nse-name',ed).value||'').trim()||txt),
                    mode:(($('input[name="nse-mode"]:checked',ed)||{}).value)||'agent',
                    node:$('#nse-node',ed)?$('#nse-node',ed).value:(cur[i].node||_defNode),
                    all:!!($('#nse-all',ed)&&$('#nse-all',ed).checked), text:txt};
            _agentSavedSet(cur); renderSaved(); toast('Task updated');
          };
          setTimeout(()=>{ try{ $('#nse-name',ed).focus(); }catch(_){} },30);
        }
      };
      renderSaved();
      // …then again once the encrypted doc lands, so a device that has never opened this panel (or
      // has just lost its localStorage) fills in from the account rather than showing "no saved
      // tasks yet". Painting the cache FIRST is the cache-first rule: the list is already known.
      _agentSavedLoad().then(()=>{ try{ if(savedBox.isConnected) renderSaved(); }catch(_){} }).catch(()=>{});
      $('#np-save-cur',root).onclick=async()=>{ const text=inp.value.trim(); if(!text){ inp.focus(); toast('Fill in the task first, then save it'); return; }
        const name=await uiPrompt('Name this task', {value:text.slice(0,40), placeholder:'e.g. Nightly disk check'}); if(name===null) return;
        const list=_agentSavedGet(); list.unshift({name:(name||text).trim(), mode:mode(), node:nodeSel.value, all:allCb.checked, text});
        _agentSavedSet(list); renderSaved();
        $$('.np-tab',root).forEach(x=>x.classList.toggle('active',x.dataset.tab==='saved')); $$('.np-tabpanel',root).forEach(p=>p.classList.toggle('hidden',p.dataset.panel!=='saved'));
        toast('⭐ Task saved');
      };
      setTimeout(()=>{ try{ inp.focus(); }catch(_){} }, 30);
    });
  }

  function _aiBadge(on){
    const b=document.getElementById('ai-badge'); if(!b) return;
    if(on){ b.textContent='●'; b.classList.remove('hidden'); } else b.classList.add('hidden');
  }
  async function aiMount(feed){
    _aiBadge(false);   // entering the view IS the acknowledgement
    feed.innerHTML=`<div class="ai-chat">
      <div class="ai-bar"><button class="btn btn-ghost small" id="ai-back-social" hidden>← Back to Social</button><button class="btn btn-ghost small" id="ai-make" title="Make something — image, song, video, a cloned voice…"><svg class="ic b-ic" aria-hidden="true"><use href="#i-ai"></use></svg>Make</button><select id="ai-conv" class="input"></select><button class="btn btn-ghost small" id="ai-new"><svg class="ic b-ic" aria-hidden="true"><use href="#i-plus"></use></svg>New</button><button class="btn btn-ghost small" id="ai-nodes" title="Agents — run tasks on your servers" style="display:none"><svg class="ic b-ic" aria-hidden="true"><use href="#i-ai"></use></svg></button><button class="btn btn-ghost small" id="ai-tts" title="Voice narration"><svg class="ic b-ic" aria-hidden="true"><use href="#i-volume"></use></svg></button><button class="btn btn-ghost small" id="ai-del" title="delete this chat"><svg class="ic b-ic" aria-hidden="true"><use href="#i-trash"></use></svg></button></div>
      <div class="ai-msgs" id="ai-msgs"></div>
      <div class="ai-attachbar" id="ai-attachbar"></div>
      <div class="ai-compose">
        <button class="mini" id="ai-attach" title="attach"><svg class="ic b-ic" aria-hidden="true"><use href="#i-paperclip"></use></svg></button><input type="file" id="ai-file" multiple hidden>
        <button class="mini" id="ai-mic" title="Voice input (speech-to-text)"><svg class="ic b-ic" aria-hidden="true"><use href="#i-mic"></use></svg></button>
        <textarea id="ai-input" class="input" rows="1" placeholder="Message PosterChan AI…"></textarea>
        <button class="btn btn-neon" id="ai-send" aria-label="Send"><svg class="ic b-ic" aria-hidden="true"><use href="#i-play"></use></svg></button>
      </div>
    </div>`;
    _ai.attach=[];
    $('#ai-new').onclick=()=>{_forgetEffectReturn(_ai.fxReturn);aiNewConversation();};
    // Node Control button — revealed only for users on the node_exec allowlist (access checked once/session).
    { const nb=$('#ai-nodes'); if(nb){ nb.onclick=openNodePanel;
        if(_ai.nodeAccess===true) nb.style.display='';
        else if(_ai.nodeAccess===undefined) _nodeFetchState().then(()=>{ if(_ai.nodeAccess){ const b=$('#ai-nodes'); if(b) b.style.display=''; } }); } }
    // The starter cards paint themselves on a fresh chat; this is how you reach the same tools
    // mid-conversation, and it is why the composer no longer carries a second copy of the button.
    $('#ai-back-social').onclick=()=>_returnFromEffect(_ai.fxReturn);_syncEffectReturn();
    $('#ai-make').onclick=()=>openGenPicker();
    $('#ai-del').onclick=()=>aiDeleteConversation();
    $('#ai-conv').onchange=e=>{_forgetEffectReturn(_ai.fxReturn);aiOpenConversation(parseInt(e.target.value,10));};
    $('#ai-attach').onclick=()=>{
      // Attach from Camera (app), a local file, OR an existing Blossom file — so you don't have to
      // re-upload something already on your drive. Blossom picks are fetched into a real attachment
      // (bytes base64'd for the model), not pasted as a URL in the message.
      const opts = window.Capacitor
        ? [['camera','📷 Camera'],['local','🖼️ Photos / files'],['blossom','📁 Files']]
        : [['local','💻 Local file'],['blossom','📁 Files']];
      openMenuPopover($('#ai-attach'), opts, async a=>{
        if(a==='camera'){ const f=await _cameraPhoto(); if(f) aiAddFiles([f]); }
        else if(a==='blossom'){ blossomPicker(null, ({url,type,ext})=>aiAttachFromBlossom(url,type,ext)); }
        else $('#ai-file').click();
      });
    };
    $('#ai-file').onchange=e=>aiAddFiles([...e.target.files]).then(()=>{ e.target.value=''; });
    { const mic=$('#ai-mic'); if(mic) mic.onclick=aiToggleMic; }
    { const tb=$('#ai-tts'); if(tb) tb.onclick=aiToggleTTS; _aiTtsBtn(); }
    $('#ai-send').onclick=aiSend;
    const ta=$('#ai-input');
    ta.addEventListener('keydown',e=>{ if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); aiSend(); } });
    // ↑/↓ walk back through what you have already sent, as a shell or any other chat box does. Re-running a
    // long `geni …` prompt with one word changed is the single most common thing to want here, and it was
    // mouse-only. Gated on the caret being on the FIRST/LAST line, so the arrows still move normally inside
    // a multi-line draft and only step out of it at the edges.
    ta.addEventListener('keydown',e=>{
      if(e.key!=='ArrowUp' && e.key!=='ArrowDown') return;
      if(e.shiftKey||e.ctrlKey||e.metaKey||e.altKey) return;
      if(ta.selectionStart!==ta.selectionEnd) return;              // a selection means they are editing
      const H=_ai.hist;
      if(e.key==='ArrowUp'){
        if(ta.value.slice(0, ta.selectionStart).includes('\n')) return;   // not on the first line yet
        if(!H.length || _ai.histIdx===0) return;                  // nothing older to go to
        if(_ai.histIdx<0){ _ai.histDraft=ta.value; _ai.histIdx=H.length; }
        _ai.histIdx--;
      }else{
        if(ta.value.slice(ta.selectionEnd).includes('\n')) return;         // not on the last line yet
        if(_ai.histIdx<0) return;                                 // already on the live draft
        _ai.histIdx++;
      }
      e.preventDefault();
      let v;
      if(_ai.histIdx>=0 && _ai.histIdx<H.length) v=H[_ai.histIdx];
      else { _ai.histIdx=-1; v=_ai.histDraft||''; }               // walked back off the end → your draft
      _ai.histApplying=true;                                      // ...so the input handler below doesn't
      ta.value=v; ta.dispatchEvent(new Event('input'));           //    read this as "they typed something"
      _ai.histApplying=false;
      try{ ta.setSelectionRange(v.length, v.length); }catch(_){ }
    });
    // Page Up/Down scroll the TRANSCRIPT while the caret stays in the box. Every scroll key bails on a
    // focused text field, and this field holds the caret the whole session — so a long reply arriving
    // above you could not be scrolled without leaving the box first. The compose box grows to at most a
    // couple of hundred pixels, so these keys are not doing anything for the caret here.
    ta.addEventListener('keydown', e=>{
      if(e.key!=='PageUp' && e.key!=='PageDown') return;
      if(e.ctrlKey||e.metaKey||e.altKey) return;
      const box=$('#ai-msgs'); if(!box) return;
      e.preventDefault();
      box.scrollTop += (e.key==='PageDown' ? 1 : -1) * Math.max(120, box.clientHeight-64);
    });
    // Typing anything drops you out of history onto a fresh draft — otherwise a recalled line you had begun
    // editing would be silently thrown away by the next ↓.
    ta.addEventListener('input',()=>{ if(!_ai.histApplying) _ai.histIdx=-1;
      ta.style.height='auto'; ta.style.height=Math.min(ta.scrollHeight,200)+'px'; aiUpdateLinkActions(); });
    // Ctrl/⌘-V an image from the clipboard → attach it (so you can paste a screenshot then `post`, etc.)
    ta.addEventListener('paste', async e=>{
      const items=(e.clipboardData && e.clipboardData.items)||[]; const files=[];
      for(const it of items){ if(it.type && it.type.startsWith('image/')){ const f=it.getAsFile();
        if(f) files.push(f.name?f:new File([f],'pasted-'+Date.now()+'.png',{type:f.type||'image/png'})); } }
      if(files.length){ e.preventDefault(); await aiAddFiles(files); toast(files.length+' image'+(files.length>1?'s':'')+' attached'); }
    });
    // Drag-and-drop files onto the chat → attach them (same as the 📎 button). Recurses dropped folders.
    { const chat=feed.querySelector('.ai-chat');
      if(chat){
        chat.addEventListener('dragover',e=>{ if(e.dataTransfer&&[...(e.dataTransfer.types||[])].includes('Files')){ e.preventDefault(); chat.classList.add('ai-drop'); } });
        chat.addEventListener('dragleave',e=>{ if(e.target===chat) chat.classList.remove('ai-drop'); });
        chat.addEventListener('drop',async e=>{ if(!(e.dataTransfer&&[...(e.dataTransfer.types||[])].includes('Files'))) return;
          e.preventDefault(); chat.classList.remove('ai-drop');
          const dt=e.dataTransfer, items=dt&&dt.items, entries=[];
          if(items&&items.length&&items[0].webkitGetAsEntry){ for(let i=0;i<items.length;i++){ const en=items[i].webkitGetAsEntry(); if(en) entries.push(en); } }
          let files=[];
          if(entries.length){ files=await _walkEntries(entries); } else { files=[...((dt&&dt.files)||[])]; }
          if(files.length){ await aiAddFiles(files); toast(files.length+' file'+(files.length>1?'s':'')+' attached'); }
        });
      } }
    // Enter / Space on a focused control in the transcript acts on it. Images are the reason this exists
    // (they are the one thing here that is not a real button), but routing it through the SAME click path
    // means anything else that ends up focusable behaves identically rather than growing a second handler.
    $('#ai-msgs').addEventListener('keydown', e=>{
      if(e.key!=='Enter' && e.key!==' ' && e.key!=='Spacebar') return;
      if(e.ctrlKey||e.metaKey||e.altKey) return;
      const t=e.target;
      if(!t || t.tagName!=='IMG' || t.tabIndex<0) return;    // real buttons already do this themselves
      e.preventDefault();
      t.click();
    });
    $('#ai-msgs').addEventListener('click',async e=>{
      // Guided card → open the studio for that command (no syntax to remember).
      const gc=e.target.closest('.aw-card'); if(gc){ e.preventDefault(); if(gc.dataset.open==='nodes'){ openNodePanel(); }
        else if(gc.dataset.gen==='voice'){ openVoiceStudio(); }   // its own studio: a voice is a saved CLIP, not a prompt
        else { openGenStudio(gc.dataset.gen); } return; }
      const eg=e.target.closest('.ai-eg'); if(eg){ e.preventDefault(); const ta=$('#ai-input'); if(ta){ ta.value=eg.dataset.cmd; ta.focus(); ta.dispatchEvent(new Event('input')); } return; }   // welcome example → prefill, let the user type
      const bno=e.target.closest('.ai-billno'); if(bno){ e.preventDefault();
        const row=bno.closest('.ai-budget-btns'); if(row) row.innerHTML='<span class="muted small">Not added.</span>'; return; }
      const vo=e.target.closest('[data-view-open]'); if(vo){ e.preventDefault(); switchView(vo.dataset.viewOpen); return; }
      const cmd=e.target.closest('.ai-cmd'); if(cmd){ e.preventDefault(); const ta=$('#ai-input'); if(ta){ ta.value=cmd.dataset.cmd; aiSend(); } return; }
      // "Add to budget" on a read bill. The write is split because the two halves live in different
      // places: the budget row is encrypted to this user's key so ONLY the client can write it, while
      // the reminder is server data, so `bill add` still handles that. One tap does both.
      const ab=e.target.closest('.ai-billadd'); if(ab){ e.preventDefault();
        const row=ab.closest('.ai-budget-btns');
        try{
          if(!window.PCBudget) throw new Error('budget module not loaded');
          await window.PCBudget.addParsed(ab.dataset.vendor, ab.dataset.amount);
          if(row) row.innerHTML='<span class="muted small">✅ Added to your budget.</span>';
          const ta=$('#ai-input'); if(ta){ ta.value='bill add'; aiSend(); }   // …and set the reminder
        }catch(err){
          if(row) row.innerHTML='<span class="muted small">Couldn’t save that — open Discover → Budget and add it there.</span>';
        }
        return; }
      const fxc=e.target.closest('.fx-cmd'); if(fxc){ e.preventDefault();
        if(fxc.dataset.cmd==='__fxguide'){ showEffectGuide(); return; }   // 🎬 Effects → open the studio picker
        const ta=$('#ai-input'); if(ta){ if(_ai.fxImage && !_ai.attach.length) aiAddFiles([_ai.fxImage]); _fxSetEffect(ta, fxc.dataset.cmd); ta.focus(); ta.dispatchEvent(new Event('input')); } return; }   // effect chip → set base effect (keeps motion/caption)
      const fxm=e.target.closest('.fx-mot[data-add]'); if(fxm){ e.preventDefault(); const ta=$('#ai-input'); if(ta){ if(_ai.fxImage && !_ai.attach.length) aiAddFiles([_ai.fxImage]); _fxApplyMod(ta, fxm.dataset.add); ta.focus(); ta.dispatchEvent(new Event('input')); } return; }   // motion → single geometry / glow·alive·trippy compose
      const rfx=e.target.closest('.ai-reply-fx'); if(rfx){ e.preventDefault(); sendEffectReply(rfx.dataset.mid, rfx); return; }   // post the generated effect back as a reply
      const cfx=e.target.closest('.ai-copy-fx'); if(cfx){ e.preventDefault(); copyEffectUrl(cfx.dataset.mid, cfx); return; }   // upload + copy the public Blossom URL
      const sfx=e.target.closest('.ai-save-fx'); if(sfx){ e.preventDefault(); saveEffectToBlossom(sfx.dataset.mid, sfx); return; }   // keep generated media on the user's Blossom drive
      const dfx=e.target.closest('.ai-dl-fx'); if(dfx){ e.preventDefault(); downloadEffectMedia(dfx.dataset.mid, dfx); return; }     // save the bytes to the device
      const nfx=e.target.closest('.ai-note-fx'); if(nfx){ e.preventDefault(); notesFromEffectMedia(nfx.dataset.mid, nfx); return; }  // the bytes → the private notebook
      const mfx=e.target.closest('.ai-mp3-fx'); if(mfx){ e.preventDefault(); convertEffectToMp3(mfx.dataset.mid, mfx); return; }     // branded MP4 → MP3 via `extractaudio`
      const cpf=e.target.closest('.ai-copyfile'); if(cpf){ e.preventDefault(); copyFileUrl(cpf.dataset.url, cpf); return; }   // inline /api/files/ media → re-upload + copy public URL
      const rpf=e.target.closest('.ai-replyfile'); if(rpf){ e.preventDefault(); replyFileUrl(rpf.dataset.url, rpf); return; }
      const ppf=e.target.closest('.ai-postfile'); if(ppf){ e.preventDefault(); postFileUrl(ppf.dataset.url, ppf); return; }     // share generated media → new Nostr post
      const svf=e.target.closest('.ai-savefile'); if(svf){ e.preventDefault(); saveFileToBlossom(svf.dataset.url, svf, svf.dataset.kind, svf.dataset.name); return; }   // artifact → drive, or the music library
      const ntf=e.target.closest('.ai-notefile'); if(ntf){ e.preventDefault(); notesFromFileUrl(ntf.dataset.url, ntf, ntf.dataset.name); return; }     // artifact → the private notebook
      const dlf=e.target.closest('.ai-dlfile'); if(dlf){ e.preventDefault(); downloadFileUrl(dlf.dataset.url, dlf, dlf.dataset.name); return; }       // artifact → device
      const m3f=e.target.closest('.ai-mp3file'); if(m3f){ e.preventDefault(); mp3FromFileUrl(m3f.dataset.url, m3f); return; }       // branded MP4 → MP3
      const mbf=e.target.closest('.ai-memefile'); if(mbf){ e.preventDefault(); memeBuildFile(mbf.dataset.url, mbf, mbf.dataset.kind); return; }   // keep editing the result in the Meme Builder
      const pfx=e.target.closest('.ai-post-fx'); if(pfx){ e.preventDefault(); postEffectMedia(pfx.dataset.mid, pfx); return; }   // share effect media → new Nostr post
      const mbx=e.target.closest('.ai-meme-fx'); if(mbx){ e.preventDefault(); memeBuildEffect(mbx.dataset.mid, mbx); return; }   // keep editing a geni/videogeni result in the Meme Builder
      const mag=e.target.closest('.ai-magnet'); if(mag){ const ta=$('#ai-input'); if(ta){ ta.value='torrents add '+mag.dataset.magnet; aiSend(); } return; }
      const fco=e.target.closest('.fc-opt'); if(fco){ e.preventDefault(); const st=_ai.decks&&_ai.decks[fco.dataset.fc]; if(st && st.answered[st.idx]==null){ const i=+fco.dataset.opt; st.answered[st.idx]=i; if(i===(st.cards[st.idx]||{}).correct) st.score++; _fcRedraw(fco.dataset.fc); } return; }   // answer a card → ✓/✗ + explanation
      const fcn=e.target.closest('.fc-next'); if(fcn){ e.preventDefault(); const st=_ai.decks&&_ai.decks[fcn.dataset.fc]; if(st && st.idx<st.cards.length-1){ st.idx++; _fcRedraw(fcn.dataset.fc); } return; }
      const fcp=e.target.closest('.fc-prev'); if(fcp){ e.preventDefault(); const st=_ai.decks&&_ai.decks[fcp.dataset.fc]; if(st && st.idx>0){ st.idx--; _fcRedraw(fcp.dataset.fc); } return; }
      const fcr=e.target.closest('.fc-restart'); if(fcr){ e.preventDefault(); const st=_ai.decks&&_ai.decks[fcr.dataset.fc]; if(st){ st.idx=0; st.score=0; st.answered=new Array(st.cards.length).fill(null); _fcRedraw(fcr.dataset.fc); } return; }
      const im=e.target.closest('img'); if(im){ openLightbox(im.dataset.full||im.src); }
    });
    await aiLoadConversations();
    // 🎬 Effect handoff: if we entered the AI view to apply an effect to a post's image, set it up now
    // that the chat is fully mounted (fixes the race where the conv load wiped the attached image).
    if(_ai.pendingFx){ const fx=_ai.pendingFx; _ai.pendingFx=null; await startEffectStudio(fx.url); }
    // Same handoff for a share that came in through the "PosterChan AI" share-sheet entry.
    if(_ai.pendingShare){ const sh=_ai.pendingShare; _ai.pendingShare=null; await startAiShare(sh); }
  }
  async function aiLoadConversations(){
    let convs=[]; try{ convs=await fetch('/api/conversations').then(r=>r.json()); }catch(_){}
    const sel=$('#ai-conv'); if(!sel) return;
    sel.innerHTML=(convs||[]).map(c=>`<option value="${c.id}">${enc(c.title||'New Chat')}</option>`).join('');
    // Prefer the conversation we were LAST in (a render may have finished there while you were
    // elsewhere) over "the newest row", which is not the same thing once you have several chats.
    const _last=_aiLastConv();
    const _has=(convs||[]).some(c=>c.id===_last);
    // aiMount must finish this load before consuming an Effects handoff. In a fresh
    // account the automatic first chat otherwise races the Effects chat creation.
    if(_has) return aiOpenConversation(_last);
    else if(convs && convs.length) return aiOpenConversation(convs[0].id);
    else return aiNewConversation();
  }
  async function aiNewConversation(stillWanted){
    try{
      const c=await fetch('/api/conversations',{ method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({title:'New Chat'}) }).then(r=>r.json());
      if(stillWanted && !stillWanted())return null;
      const sel=$('#ai-conv'); if(sel){ const o=document.createElement('option'); o.value=c.id; o.textContent=c.title||'New Chat'; sel.prepend(o); sel.value=c.id; }
      await aiOpenConversation(c.id);   // await so callers (e.g. the Effects studio) attach AFTER the conv render settles
      return c.id;
    }catch(_){ toast('could not start a chat'); }
  }
  async function aiDeleteConversation(){
    const id=_ai.convId; if(!id) return;
    if(!await uiConfirm('Delete this chat and all its messages?')) return;
    try{
      let r=await fetch('/api/conversations/'+id, { method:'DELETE' });
      // 409 = a command is STILL RUNNING on this chat. A slow one (flashcards can take minutes)
      // outlives the websocket, so the chat looks dead and deleting is the natural reaction — but
      // that purges it before the answer lands and the reply is lost. Say so, and let them insist.
      if(r.status===409){
        let d={}; try{ d=(await r.json()).detail||{}; }catch(_){ }
        if(!await uiConfirm((d.message||'A command is still running on this chat.')+'\n\nDelete it anyway?')) return;
        r=await fetch('/api/conversations/'+id+'?force=1', { method:'DELETE' });
      }
      if(!r.ok) throw 0;
    }
    catch(_){ toast('delete failed'); return; }
    try{ if(_ai.ws){ _ai.ws.onclose=null; _ai.ws.close(); } _ai.ws=null; }catch(_){}
    _ai.convId=null;
    const sel=$('#ai-conv'); if(sel){ const o=sel.querySelector(`option[value="${id}"]`); if(o) o.remove(); }
    if(sel && sel.options.length){ aiOpenConversation(parseInt(sel.options[0].value,10)); }
    else aiNewConversation();
    toast('chat deleted');
  }
  async function aiOpenConversation(id){
    if(!id) return;
    // Switching conversations ABANDONS the reply `awaiting` was tracking: aiConnect force-closes the
    // previous socket (onclose nulled, so aiRecover never fires for it). Leaving the flag set then
    // made the leave-the-view guard keep the NEW, idle socket open forever, since nothing was coming
    // to clear it. Reset it with the switch — the abandoned reply is still persisted server-side and
    // shows on reopening that conversation.
    if(_ai.fxReturn && _ai.fxReturn.ready && id!==_ai.fxReturn.conversation){
      _ai.fxReturn=null;_ai.replyTo=null;_syncEffectReturn();
    }
    if(_ai.convId !== id){
      _ai.awaiting = false;
      try{ clearTimeout(_ai.recoverWatch); }catch(_){ }
      _ai.recoverWatch = null;
    }
    _ai.convId=id; _aiRememberConv(id); _ai.streamEl=null; _ai.streamBuf=""; _ai.decks={};   // decks re-hydrate from [[FC]] markers on render — drop the old set so it can't leak across opens
    const sel=$('#ai-conv'); if(sel && sel.value!=String(id)) sel.value=String(id);
    const box=$('#ai-msgs'); if(box) box.innerHTML='<div class="spinner"></div>';
    let conv=null; try{ conv=await fetch('/api/conversations/'+id).then(r=>r.json()); }catch(_){}
    if(S.VIEW!=='ai' || _ai.convId!==id) return;
    if(box){ box.innerHTML='';
      const msgs = (conv && conv.messages) || [];
      if(!msgs.length){ box.innerHTML = _aiWelcomeHtml(); _aiRevealNodeCard(); }   // fresh chat → friendly splash with starter commands
      for(const m of msgs){
        let html = m.role==='user'?enc(m.content):aiFormat(m.content||'');
        // A generated image is persisted to the message's own image_path column, NOT as `![](url)`
        // markdown like generated video/audio — so aiFormat never sees it and it rendered as a bare
        // <img> with no action row at all. Same artifact URL shape _aiFileActions already takes
        // (relative + authed), so a reloaded geni result gets the same buttons the live one has.
        // Assistant only: on a user turn the image is that user's own upload, echoed back.
        if(m.image_path) html += `<div class="ai-media"><img src="${enc(_absUrl(m.image_path))}" loading="lazy" onerror="window.__aiMediaRetry(this)"></div>`
                                 + (m.role==='user'?'':_aiFileActions(m.image_path,'image'));
        aiAddMessage(m.role, html);
      }
      aiScroll();
    }
    aiConnect(id);
  }
  // ---- Create studio: guided image / music / video generation ---------------------------------
  // Typing `musicgeni <style prompt> | <lyrics>` assumes you know the command AND its syntax. This is
  // the same idea as the Effects studio: pick from options, watch the command build itself in the
  // footer, hit Generate. The command is still what gets sent, so the chat/Telegram paths are
  // untouched and anyone who prefers typing keeps working exactly as before.
  const _GEN = {
    // Voice has no prompt sheet — a voice is a saved CLIP, not a description — so it carries only
    // what the picker renders and is routed to its own studio at both call sites.
    voice: { cmd:'voice', ic:'mic', title:'Clone a voice',
      blurb:'A few seconds of someone speaking, then it can say anything.' },
    image: { cmd:'geni', ic:'palette', title:'Generate an image',
      blurb:'Describe what you want to see. Add a style if you like.',
      ph:'a neon city street in the rain, a lone figure with an umbrella',
      groups:[
        ['Style', ['photorealistic','anime','oil painting','watercolour','3D render','pixel art','comic book','cyberpunk','vaporwave','low poly']],
        ['Mood',  ['moody lighting','golden hour','neon glow','dark and gritty','soft pastel','high contrast']],
        ['Shot',  ['close-up portrait','wide landscape','top-down','macro detail']],
      ] },
    music: { cmd:'musicgeni', ic:'music', title:'Generate a song',
      blurb:'Describe the style. Lyrics are optional — leave them blank and the AI writes them.',
      ph:'dreamy synthwave with a driving bassline, 90 BPM',
      groups:[
        ['Genre', ['synthwave','lo-fi hip hop','hard rock','acoustic folk','jazz','drum and bass','country','metal','reggae','orchestral']],
        ['Mood',  ['upbeat','melancholic','dreamy','aggressive','chill','epic']],
        ['Vocals',['male vocals','female vocals','choir','no vocals']],
      ] },
    video: { cmd:'videogeni', ic:'film', title:'Generate a short video',
      blurb:'Describe the shot. Keep it simple — short clips work best.',
      ph:'a paper plane gliding over a misty forest at sunrise',
      groups:[
        ['Look',   ['cinematic','anime','claymation','drone footage','black and white','vintage film']],
        ['Motion', ['slow pan','orbiting camera','zoom in','static shot','handheld']],
      ] },
    // The rest of the splash actions get the same treatment: one field, the right keyboard, and a
    // preview of the exact command. `single` groups are radio-style (you translate INTO one language);
    // `compose` overrides how the picks attach when appending them as ", a, b" would be wrong.
    audio: { go:'Download', cmd:'ytdl mp3', ic:'headphones', title:'Download audio as MP3',
      blurb:'Paste a link — YouTube, TikTok, X, SoundCloud and friends.',
      ph:'https://www.youtube.com/watch?v=…', rows:1, kind:'url' },
    videodl: { go:'Download', cmd:'ytdl video', ic:'download', title:'Download a video',
      blurb:'Paste a YouTube, X or TikTok link. Trim or shrink it on the way down.',
      ph:'https://www.youtube.com/watch?v=…', rows:1, kind:'url',
      // ytdl takes `clip <start> <end>` and `compress` as modifiers. They're worth surfacing for a
      // second reason: a PLAIN download is copied to server storage and only reported as a path,
      // while a clipped/compressed one is delivered straight into the chat (and therefore into
      // Files). Trimming a long video is also how you avoid waiting for the whole thing.
      opts:[['clipfrom','Clip from','0:10','text'],['clipto','Clip to','0:30','text']],
      toggles:[['compress','🗜 Compress it (smaller file)']],
      compose:(base, picks, o)=>{
        let out='ytdl video '+base;
        if((o.clipfrom||'').trim() && (o.clipto||'').trim()) out+=` clip ${o.clipfrom.trim()} ${o.clipto.trim()}`;
        if(o.compress) out+=' compress';
        return out; } },
    shot: { go:'Capture', cmd:'screenshot', ic:'camera', title:'Screenshot a web page',
      blurb:'Paste a page URL and I will capture it.',
      ph:'https://example.com', rows:1, kind:'url' },
    translate: { go:'Translate', cmd:'translate', ic:'translate', title:'Translate text',
      blurb:'Paste the text, then pick a language (English if you skip it).',
      ph:'paste the text to translate…', rows:3,
      groups:[['Into', ['English','Spanish','French','German','Portuguese','Italian','Japanese','Korean','Chinese','Russian','Arabic','Hindi']]],
      single:true,
      compose:(base, picks)=> 'translate ' + base + (picks[0] ? ' to ' + picks[0] : '') },
    search: { go:'Search', cmd:'search', ic:'search', title:'Search the web',
      blurb:'What do you want to look up?',
      ph:'best nostr clients 2026', rows:1 },
    // ---- things you do TO A FILE. Same sheet, but it asks for the file first (and any argument the
    // command needs) instead of a prompt. These were only reachable by attaching something and finding
    // the action bar, so nobody discovered them from an empty chat.
    compress: { go:'Compress', cmd:'compress', ic:'compress', title:'Compress a file', file:true,
      accept:'image/*,video/*,application/pdf',
      blurb:'Shrink an image, video or PDF. Attach the file and I will do the rest.' },
    clip: { go:'Clip', cmd:'clip', ic:'scissors', title:'Clip a video', file:true, accept:'video/*',
      blurb:'Trim a section out of a video. Times look like 0:10 and 0:30.',
      extra:[['start','Start','0:10'],['end','End','0:30']], needExtra:true },
    convert: { go:'Convert', cmd:'convert', ic:'refresh', title:'Convert a file', file:true,
      accept:'image/*,application/pdf',
      blurb:'Images become a PDF; a PDF becomes images. Attach it and I will pick the direction.' },
    extractaudio: { go:'Extract', cmd:'extractaudio', ic:'music', title:'Extract the audio', file:true,
      accept:'video/*', blurb:'Pull the soundtrack out of a video as an MP3.' },
    circlecrop: { go:'Crop', cmd:'circlecrop', ic:'circle-crop', title:'Circle-crop an image', file:true,
      accept:'image/*', blurb:'Round the image into a circle on a transparent background.' },
    removebackground: { go:'Remove', cmd:'removebackground', ic:'wand', title:'Remove the background', file:true,
      accept:'image/*', blurb:'Cut the subject out onto a transparent background.' },
    ocr: { go:'Read', cmd:'ocr', ic:'text', title:'Read the text in a file', file:true,
      accept:'image/*,application/pdf', blurb:'Pull the words out of a photo, screenshot or PDF.' },
    meme: { go:'Make it', cmd:'meme', ic:'smile', title:'Add meme text', file:true, accept:'image/*',
      blurb:'Outlined white caption across the image.',
      extra:[['text','Caption','when the code finally works']], needExtra:true },
    // TWO files: the face and the voice to clone. `multi` is what lets both be picked at once —
    // the command takes the first IMAGE for the face and the first clip WITH AUDIO for the voice,
    // so the order they arrive in doesn't matter.
    talk: { go:'Say it', cmd:'talk', ic:'speech', title:'Make a face talk', file:true, multi:true,
      accept:'image/*,audio/*,video/*',
      blurb:'Pick a photo of a face AND a few seconds of the voice to clone. The face lip-syncs your line in that voice.',
      extra:[['text','What should they say?','I am the president now']], needExtra:true },
    collage: { go:'Combine', cmd:'collage', ic:'grid', title:'Make a collage', file:true, multi:true,
      accept:'image/*', blurb:'Combine several images into one. Pick two or more.' },
    flashcards: { go:'Study', cmd:'flashcards', ic:'cards', title:'Make study flashcards', file:true,
      accept:'image/*,application/pdf,.ppt,.pptx,.doc,.docx',
      blurb:'Turn a PDF, slide deck or photo of your notes into a quiz.' },
    images: { go:'Search', cmd:'images', ic:'image', title:'Search for images',
      blurb:'What are you looking for?',
      ph:'shiba inu puppy', rows:1,
      groups:[['Refine', ['wallpaper','transparent png','black and white','high resolution','vector']]] },
  };

  // The ✨ button: which guided action? (the splash shows these as cards; this is the mid-chat route)
  function openGenPicker(){
    const items=[['image','Make an image'],['music','Make a song'],['video','Make a video'],
                 ['voice','Clone a voice'],
                 ['audio','Get the audio from a link'],['videodl','Download a video'],
                 ['shot','Screenshot a page'],['translate','Translate text'],
                 ['search','Search the web'],['images','Find images'],
                 ['compress','Compress a file'],['clip','Clip a video'],['extractaudio','Extract the audio'],
                 ['convert','Convert images / PDF'],['removebackground','Remove a background'],
                 ['circlecrop','Circle-crop an image'],['meme','Add meme text'],['talk','Make a face talk'],
                 ['collage','Make a collage'],['ocr','Read the text in a file'],
                 ['flashcards','Make study flashcards']];
    modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-ai"></use></svg>Make something</h3>
      <div class="gen-picker">${items.map(([k,label])=>{ const G=_GEN[k];
        return `<button class="gen-pick" data-gen="${k}">${ICO(G.ic,'awc-ic')}
          <span><b>${enc(label)}</b><span class="muted small">${enc(G.blurb)}</span></span></button>`; }).join('')}</div>`,
      root=>{ $$('.gen-pick',root).forEach(b=> b.onclick=()=>{ closeModal();
        if(b.dataset.gen==='voice') openVoiceStudio(); else openGenStudio(b.dataset.gen); }); });
  }

  // `opts` lets ANOTHER part of the app borrow this sheet without borrowing the chat: `over` patches the
  // wording (title/blurb/button) and `onSubmit` takes the result instead of sending the command. That is
  // how the Meme Builder generates an image layer — same chips, same live command preview, same mobile
  // layout, one implementation. Without it the builder would need a second prompt UI that drifts.
  // ---- Voice studio ------------------------------------------------------------------------
  // A "voice" is a NAME plus a short reference clip on your own Blossom drive — there is no training
  // and no per-voice model, so the whole library is a few hundred bytes of JSON. It lives in a
  // kind-30078 doc so it follows you across devices, exactly like the client prefs above.
  const VOICES_D = 'pcai:voices';
  let _voices = null;              // array, or null = NOT LOADED (which is not the same as empty)
  // Returns the saved list, or null meaning "could not reach a relay".
  //
  // "No document yet" and "relay unreachable" look IDENTICAL at the query layer — both are an empty
  // result — and conflating them is a bug in either direction. Treat unreachable as empty and the
  // first save replaces a real library with one entry (this doc is replaceable). Treat empty as
  // unreachable and the FIRST voice can never be saved at all, because a user who has never saved one
  // has no document to find: that is what shipped, and it is why adding a clip complained about
  // relays. So ask the connection directly — Relay.ready() resolves true only for a socket that is
  // actually OPEN and not a zombie — and only then is an empty result allowed to mean empty.
  async function voicesRead(){
    let live = false;
    try{ live = await Relay.ready(3000); }catch(_){ live = false; }
    if(!live) return null;
    // Retry on EMPTY as well as on error, and only call it empty once the retries are spent. A socket
    // can be live and still read empty for a moment — freshly connected, EOSE racing the event — and
    // returning [] on that first look would let the next save replace a real library with one entry.
    // The cost is ~1.4s on a genuinely first save; the alternative is silently losing someone's voices.
    for(let a=0; a<3; a++){
      if(a) await new Promise(r=>setTimeout(r, 450*a));
      try{
        const evs = await Relay.query([{ authors:[S.ME.pubkey], kinds:[30078], '#d':[VOICES_D], limit:1 }]);
        const ev = (evs||[]).sort((x,y)=>y.created_at-x.created_at)[0] || null;
        if(ev){ try{ return (JSON.parse(ev.content||'{}').voices)||[]; }catch(_){ return []; } }
      }catch(_){}                 // the QUERY failed (not "found nothing") → retry
    }
    // Three live reads, nothing there. Now it is genuinely an empty library, so a first save can start.
    return [];
  }
  let _voicesChain = Promise.resolve();
  // Serialised read-modify-write, and it REFUSES to write when the read failed. This doc is
  // replaceable: publishing a list built on top of "I couldn't read anything" replaces the user's
  // whole library with whatever this one tab happened to hold — the same wipe that took out mutes,
  // follows and a drive's file index. A retry costs nothing; a wipe is unrecoverable.
  function voicesSave(mutate){
    _voicesChain = _voicesChain.catch(()=>{}).then(async()=>{
      const cur = await voicesRead();
      if(cur === null) throw new Error('couldn’t reach your relay — not saving, so nothing is lost');
      const next = mutate(cur.slice());
      await publish(30078, JSON.stringify({ voices: next }), [['d', VOICES_D]]);
      _voices = next;
      return next;
    });
    return _voicesChain;
  }

  async function voiceStatus(){
    try{ const r = await fetch('/client/voice/status'); return r.ok ? await r.json() : null; }
    catch(_){ return null; }
  }

  // Record a reference clip with the mic. Same MediaRecorder shape as the Meme Builder's voice-over.
  async function voiceRecord(onDone){
    if(!navigator.mediaDevices || typeof MediaRecorder==='undefined'){
      toast('recording isn’t available in this browser — upload a clip instead'); return; }
    let stream;
    try{ stream = await navigator.mediaDevices.getUserMedia({ audio:true }); }
    catch(_){ toast('microphone permission denied'); return; }
    const mimes=['audio/webm;codecs=opus','audio/webm','audio/mp4','audio/ogg;codecs=opus'];
    const mime=mimes.find(m=>{ try{ return MediaRecorder.isTypeSupported(m); }catch(_){ return false; } })||'';
    const rec = mime ? new MediaRecorder(stream,{mimeType:mime}) : new MediaRecorder(stream);
    const chunks=[]; rec.ondataavailable=e=>{ if(e.data&&e.data.size) chunks.push(e.data); };
    let secs=0, tick=null;
    modal(`<h3>Record a voice</h3>
      <p class="muted small">Read a couple of sentences in a normal speaking voice, somewhere quiet.
        Five to fifteen seconds is plenty — more doesn’t make the copy better.</p>
      <div class="mb-rec"><span class="mb-recdot on"></span><b id="vr-clock">0:00</b></div>
      <button class="btn btn-neon full" id="vr-stop">Stop and use this</button>`, root=>{
      tick=setInterval(()=>{ secs++; const el=root.querySelector('#vr-clock');
        if(el) el.textContent=`${Math.floor(secs/60)}:${String(secs%60).padStart(2,'0')}`; }, 1000);
      root.querySelector('#vr-stop').onclick=()=>{ try{ rec.stop(); }catch(_){} };
    });
    rec.onstop=()=>{
      clearInterval(tick);
      try{ stream.getTracks().forEach(t=>t.stop()); }catch(_){}
      closeModal();
      const blob=new Blob(chunks,{type:mime||'audio/webm'});
      if(!blob.size){ toast('nothing was recorded'); return; }
      onDone(new File([blob], 'voice-sample.webm', {type: blob.type}));
    };
    rec.start();
  }

  // Take a clip (recorded or picked), put it on Blossom, and add it to the library.
  let _voiceOpts = null;   // remembered across the add-a-voice detour so a borrowed studio comes back borrowed
  // Pick a clip you ALREADY have on your drive. Deliberately not routed through voiceAdd: that
  // uploads, and re-uploading a blob that is already on Blossom would store the same bytes under a
  // second name and leave the drive with two copies of one clip. A voice is a name plus a URL, and
  // we already have the URL.
  function voiceAddFromBlossom(){
    blossomPicker(null, async ({ url }) => {
      if(!url) return;
      const name = await uiPrompt('Name this voice', { value: '', placeholder: 'e.g. me, narrator, gran' });
      if(name === null) return;
      const nm = (name||'').trim().slice(0,40) || 'untitled';
      try{
        await voicesSave(list => {
          list.push({ id:'v'+Date.now().toString(36), name:nm, url, created:Math.floor(Date.now()/1000) });
          return list;
        });
        toast('voice saved');
        openVoiceStudio(_voiceOpts);
      }catch(e){ toast('couldn’t save that voice: '+((e&&e.message)||e)); }
    }, {
      title: '🌸 Pick a voice clip',
      // Video counts: a phone recording is mp4/webm, and the server pulls the audio out of it.
      filter: b => /^(audio|video)\//.test(b.type||''),
      empty: 'No audio or video on your drive yet — record one, or upload a clip in Files.',
    });
  }

  async function voiceAdd(file){
    const name = await uiPrompt('Name this voice', { value: '', placeholder: 'e.g. me, narrator, gran' });
    if(name === null) return;
    const nm = (name||'').trim().slice(0,40) || 'untitled';
    try{
      toast('uploading the sample…');
      const url = await uploadBlob(file);
      // Register the clip in the FILE INDEX, not just in the voice list. Without this the sample is a
      // blob nothing on screen accounts for: invisible in Files, so the user can neither see what a
      // voice is costing them nor tidy one up, and anything that ever sweeps unreferenced blobs would
      // take it and leave a voice pointing at nothing. It also gives them a way to re-download the
      // original sample, which the voice list alone does not.
      try{
        const sha=(String(url).split('/').pop()||'').split('.')[0].split('?')[0];
        if(/^[0-9a-f]{64}$/i.test(sha)){
          if(!FilesIdx.folders().includes('Voices')) FilesIdx.addFolder('Voices');
          FilesIdx.setFile(sha, { name:nm+' (voice sample)', folder:'Voices',
            mime:file.type||'audio/webm', size:file.size, ts:Math.floor(Date.now()/1000) });
        }
      }catch(_){}   // indexing is a convenience — never lose the voice over it
      await voicesSave(list => {
        list.push({ id: 'v'+Date.now().toString(36), name: nm, url, created: Math.floor(Date.now()/1000) });
        return list;
      });
      toast('voice saved');
      openVoiceStudio(_voiceOpts);
    }catch(e){ toast('couldn’t save that voice: '+((e&&e.message)||e)); }
  }

  // `opts` is threaded in EXPLICITLY. It belongs to openVoiceStudio, and this is a sibling function —
  // reading it here without a parameter is a ReferenceError that only fires once a generation has
  // already finished, i.e. after ~2 minutes of GPU, reported as "voice failed: opts is not defined".
  async function voiceSpeak(voice, text, root, opts){
    const st = root && root.querySelector('#vs-status');
    const say = (m)=>{ if(st) st.textContent = m; };
    try{
      say('fetching the voice…');
      // Check the fetch. A dead/expired Blossom URL returns an HTML error page with a 200-ish shape
      // in some setups, and posting THAT as the reference gives a baffling "couldn't read audio"
      // from the far end instead of "your sample is gone".
      const rr = await fetch(voice.url);
      if(!rr.ok){ say(''); toast('that voice’s sample is missing from your drive — re-record it'); return; }
      const ref = await rr.blob();
      if(!ref || !ref.size){ say(''); toast('that voice’s sample is empty — re-record it'); return; }
      const auth = await selfProof();
      const fd = new FormData();
      fd.append('reference', ref, 'ref.wav');
      fd.append('text', text);
      fd.append('pubkey', S.ME.pubkey);
      fd.append('auth', auth);
      // A generation runs at roughly 10x realtime — 91 characters measured 138s on the Arc, about
      // half that on the CUDA node — and queues behind every other GPU task. A STATIC line reads as
      // hung at that length, which is how people end up firing more requests at a GPU already busy
      // with the first (and collecting 429s). So count UP, and give a length-based estimate so the
      // number has something to be measured against.
      const started = Date.now();
      const est = Math.max(20, Math.round(text.length * 1.2));   // ~1.2s/char, the slower node
      let tick = setInterval(()=>{
        const el = Math.round((Date.now()-started)/1000);
        say(el > est * 1.5
          ? `still going — ${el}s. Long lines really do take this long; it is not stuck.`
          : `speaking… ${el}s of about ${est}s. This holds the server’s GPU, so one at a time.`);
      }, 1000);
      let r;
      try{ r = await fetch('/client/voice/speak', { method:'POST', body: fd }); }
      finally{ clearInterval(tick); }
      if(!r.ok){
        let msg = 'HTTP '+r.status;
        try{ const j = await r.json(); msg = j.detail || j.error || msg; }catch(_){}
        // 429 is the one-at-a-time guard, not a failure — a voice generation holds the node's GPU
        // outright. Saying so in the status line (not just a toast that vanishes) is what stops
        // someone pressing Speak repeatedly and collecting more of them.
        say(r.status === 429 ? msg : '');
        toast(msg); return;
      }
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);

      // DELIVER TO THE CHAT FIRST, always. The take used to go only into the modal, and the modal is
      // dismissed by tapping the backdrop — over a 138s generation that is not an edge case, it is
      // what people do. The result was then appended into a detached node and silently thrown away,
      // after the GPU had already done all the work: "I can't see the generated result at all".
      // The transcript is a surface that cannot be dismissed out from under an in-flight request.
      if(!(opts && opts.onTake)){
        // Its OWN object URL. The take row's dismiss button revokes `url`, and the transcript bubble
        // outlives the modal — sharing one URL means dropping a take silently kills the audio in the
        // chat, which is the copy the user was told is the durable one.
        const chatUrl = URL.createObjectURL(blob);
        aiAddMessage('assistant',
          `<div class="muted small">🗣️ ${enc(voice.name)}</div>`+
          `<div style="margin:4px 0">${enc(text)}</div>`+
          `<audio controls src="${chatUrl}" style="width:100%"></audio>`);
      }

      // …and ALSO into the studio, when it is still open — that is what you compare takes in. isConnected
      // is the test that matters: a stale `root` from a closed modal looks perfectly normal otherwise.
      const out = (root && root.isConnected) ? root.querySelector('#vs-out') : null;
      if(out){
        // APPEND, don't replace. Getting one line out of a voice is the rare case — you try a
        // reading, change a word, try the other voice, and want to hear them against each other.
        // Replacing meant every previous take vanished the moment you asked for the next one, so
        // comparing two required generating the first one again (~45s of GPU each time).
        const row = document.createElement('div');
        row.className = 'vs-take';
        row.innerHTML = `<div class="vs-take-hd"><b>${enc(voice.name)}</b>
            <span class="vs-take-acts">
              <button class="vs-keep" title="Save this take to my drive">
                <svg class="ic b-ic" aria-hidden="true"><use href="#i-upload"></use></svg></button>
              <a class="vs-dl" download="${enc(voice.name)}.wav" href="${url}" title="Download this take">
                <svg class="ic b-ic" aria-hidden="true"><use href="#i-download"></use></svg></a>
              <button class="vs-drop" title="Remove this take">
                <svg class="ic b-ic" aria-hidden="true"><use href="#i-trash"></use></svg></button>
            </span></div>
          <div class="vs-said">${enc(text)}</div>
          <audio controls autoplay src="${url}"></audio>`;
        // A take is a blob: URL — it dies with the modal. That is fine for the ones you are comparing
        // and wrong for the one you wanted: getting it back costs another ~45s of GPU. "Keep" puts it
        // on the drive like any other file, so it outlives the session.
        if(opts && opts.onTake){
          const use=document.createElement('button');
          use.className='btn btn-neon small full'; use.style.marginTop='6px';
          use.textContent = opts.useLabel || 'Use this take';
          use.onclick=()=>{ closeModal(); opts.onTake(blob, voice.name, text); };
          row.appendChild(use);
        }
        // Takes pile up over a session with no way to clear one. Revoke the blob URL as it goes, or the
        // audio stays in memory for the life of the page.
        const drop = row.querySelector('.vs-drop');
        if(drop) drop.onclick=()=>{ try{ URL.revokeObjectURL(url); }catch(_){} row.remove(); };
        const keep = row.querySelector('.vs-keep');
        if(keep) keep.onclick = async ()=>{
          keep.disabled = true;
          try{
            const nm = `${voice.name} - ${text.slice(0,40).replace(/[\r\n]+/g,' ')}`;
            const f = new File([blob], nm.replace(/[^\w .-]/g,'_')+'.wav', {type:'audio/wav'});
            const u = await uploadBlob(f);
            const sha=(String(u).split('/').pop()||'').split('.')[0].split('?')[0];
            if(/^[0-9a-f]{64}$/i.test(sha)){
              if(!FilesIdx.folders().includes('Voices')) FilesIdx.addFolder('Voices');
              FilesIdx.setFile(sha, { name:nm, folder:'Voices', mime:'audio/wav',
                size:blob.size, ts:Math.floor(Date.now()/1000) });
            }
            keep.classList.add('on'); keep.title='Saved to your drive';
            toast('saved to your drive');
          }catch(e){ keep.disabled=false; toast('couldn’t save: '+((e&&e.message)||e)); }
        };
        out.appendChild(row);
        row.scrollIntoView({ block:'nearest' });
      }
      // Borrowed studio (the Meme Builder) with the modal already gone: the "use this" button lives in
      // the row we could not attach, so nobody could ever reach it. You asked for this line in the
      // meme — just add it rather than making the GPU work a second time.
      if(!out && opts && opts.onTake){ opts.onTake(blob, voice.name, text); return; }
      if(!out && !(opts && opts.onTake)) toast('done — it is in the chat');
      // Clear the box and hand focus back, so the next line is just typing. ONLY if it still holds the
      // line we just spoke: a generation runs for minutes, and wiping whatever the user typed while
      // waiting is destroying work, not tidying up.
      const ta = (root && root.isConnected) ? root.querySelector('#vs-text') : null;
      if(ta && ta.value.trim() === text.trim()){ ta.value=''; ta.focus(); }
      say('');
    }catch(e){ say(''); toast('voice failed: '+((e&&e.message)||e)); }
  }

  // `opts.onTake(blob, voiceName, text)` lets ANOTHER part of the app borrow this studio without
  // borrowing the chat — the same trick meme.js uses on openGenStudio for image layers. The voice list,
  // the recorder, the queue notice and the mobile layout are all shared, so the two can't drift; only
  // the ending differs (the Meme Builder turns the take into an audio layer).
  async function openVoiceStudio(opts){
    _voiceOpts = opts || null;
    const status = await voiceStatus();
    if(status && !status.installed){
      modal(`<h3>Voice cloning</h3><p class="muted">This server hasn’t got the voice model
        installed. An admin can add it with <code>./install.sh --voice</code>, then switch it on in
        Admin → Voice.</p>`); return;
    }
    if(status && !status.enabled){
      modal(`<h3>Voice cloning</h3><p class="muted">Voice cloning is switched off on this server
        (Admin → Voice).</p>`); return;
    }
    if(_voices === null) _voices = await voicesRead();
    const list = _voices || [];
    const unreachable = _voices === null;
    const rows = list.length ? list.map(v=>`
      <div class="vs-row" data-id="${enc(v.id)}">
        <button class="vs-pick" data-id="${enc(v.id)}"><b>${enc(v.name)}</b></button>
        <audio class="vs-prev" controls preload="none" src="${enc(v.url)}"></audio>
        <button class="vs-del btn btn-ghost small" data-id="${enc(v.id)}" aria-label="Delete ${enc(v.name)}">
          <svg class="ic b-ic" aria-hidden="true"><use href="#i-trash"></use></svg></button>
      </div>`).join('')
      : `<p class="muted small">${unreachable
          ? 'Couldn’t reach your relay, so your saved voices aren’t showing. Try again in a moment.'
          : 'No voices yet. Record a few seconds of someone speaking and give it a name.'}</p>`;
    const q = status && status.queue ? `<p class="muted small">${status.queue} job(s) already queued on this server’s GPU.</p>` : '';
    modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-music"></use></svg>Voices</h3>
      <p class="muted small">Give it a few seconds of someone speaking and it can say anything in that
        voice. Nothing is trained — the clip itself is the voice, so you can add one in a minute.</p>
      <p class="muted small">Speaking runs on the server's GPU at roughly ten times realtime: a short
        phrase takes half a minute, a couple of sentences a few minutes. One at a time.</p>
      ${q}
      <div class="vs-list">${rows}</div>
      <div class="vs-src">
        <button class="btn btn-cyan small" id="vs-rec"><svg class="ic b-ic" aria-hidden="true"><use href="#i-mic"></use></svg>Record</button>
        <button class="btn btn-cyan small" id="vs-up"><svg class="ic b-ic" aria-hidden="true"><use href="#i-upload"></use></svg>Upload</button>
        <button class="btn btn-cyan small" id="vs-blossom"><svg class="ic b-ic" aria-hidden="true"><use href="#i-folder"></use></svg>My Files</button>
      </div>
      <div class="vs-active" id="vs-active"></div>
      <label class="mb-f"><span>What should it say?</span>
        <textarea class="input" id="vs-text" rows="3" maxlength="${(status&&status.max_chars)||800}"
                  placeholder="Type what you want spoken…"></textarea></label>
      <button class="btn btn-neon full" id="vs-go"><svg class="ic b-ic" aria-hidden="true"><use href="#i-volume"></use></svg>Speak it</button>
      <div class="muted small" id="vs-status" style="margin-top:8px"></div>
      <div id="vs-out" class="vs-takes"></div>`, root=>{
      let picked = list[0] ? list[0].id : null;
      // Name the active voice in words, right above the box you type into. With a SINGLE saved voice
      // it is already selected on open, so tapping it changes nothing visible — which reads as "I
      // can't choose it". A border highlight is not an answer to "which one is it going to use?".
      const paint=()=>{
        $$('.vs-pick',root).forEach(b=>b.classList.toggle('on', b.dataset.id===picked));
        const a=root.querySelector('#vs-active'), v=list.find(x=>x.id===picked);
        if(a) a.innerHTML = v ? `Speaking as <b>${enc(v.name)}</b>`
                              : `<span class="muted">Pick a voice above first.</span>`;
      };
      paint();
      $$('.vs-pick',root).forEach(b=> b.onclick=()=>{ picked=b.dataset.id; paint(); });
      $$('.vs-del',root).forEach(b=> b.onclick=async()=>{
        const v=list.find(x=>x.id===b.dataset.id); if(!v) return;
        if(!await uiConfirm(`Delete the voice “${v.name}”?`)) return;
        try{ await voicesSave(cur=>cur.filter(x=>x.id!==v.id)); closeModal(); openVoiceStudio(opts); }
        catch(e){ toast((e&&e.message)||'couldn’t delete that'); }
      });
      root.querySelector('#vs-rec').onclick=()=>{ closeModal(); voiceRecord(f=>voiceAdd(f)); };
      root.querySelector('#vs-blossom').onclick=()=>{ closeModal(); voiceAddFromBlossom(); };
      root.querySelector('#vs-up').onclick=()=>{
        const inp=document.createElement('input'); inp.type='file'; inp.accept='audio/*,video/*';
        inp.onchange=()=>{ const f=(inp.files||[])[0]; if(f){ closeModal(); voiceAdd(f); } };
        inp.click();
      };
      const goBtn = root.querySelector('#vs-go');
      const go=async()=>{
        const v=list.find(x=>x.id===picked);
        if(!v){ toast('add a voice first'); return; }
        const t=(root.querySelector('#vs-text').value||'').trim();
        if(!t){ toast('type what it should say'); return; }
        // One at a time, enforced HERE and not only by the server's 429. Pressing Speak again during a
        // run was rejected, and then the FIRST take arrived seconds later carrying the earlier line —
        // which reads as "it spoke the old thing instead of what I just typed". The button being dead
        // says "still working" far better than a toast that arrives and leaves.
        if(goBtn.disabled) return;
        goBtn.disabled = true;
        const label = goBtn.textContent;
        goBtn.textContent = '🔊 Speaking…';
        try{ await voiceSpeak(v, t, root, opts); }
        finally{ if(goBtn.isConnected){ goBtn.disabled = false; goBtn.textContent = label; } }
      };
      root.querySelector('#vs-go').onclick=go;
      // Ctrl/Cmd+Enter sends, like every other composer here. Plain Enter must NOT — this box holds
      // the words to be spoken, and line breaks in them are meaningful.
      root.querySelector('#vs-text').addEventListener('keydown', e=>{
        if(e.key==='Enter' && (e.ctrlKey||e.metaKey)){ e.preventDefault(); go(); }
      });
    });
  }

  function openGenStudio(kind, opts){
    opts = opts || {};
    const base0 = _GEN[kind]; if(!base0) return;
    const G = opts.over ? Object.assign({}, base0, opts.over) : base0;
    const picked=new Set();
    let lyrics='', instrumental=false;
    const chip=(v)=>`<button type="button" class="fxs-chip gen-chip" data-pick="${enc(v)}">${enc(v)}</button>`;
    const music = kind==='music';
    const rows = G.rows || 3;
    const groups = G.groups || [];
    modal(`<div class="fxs">
      <div class="fxs-hd">
        <div class="fxs-title"><div><h3>${ICO(G.ic,'b-ic')}${enc(G.title)}</h3>
          <div class="muted small">${enc(G.blurb)}</div></div>
          <button type="button" class="fxs-x" id="gen-x" aria-label="Close"><svg class="ic x-ic" aria-hidden="true"><use href="#i-close"></use></svg></button></div>
      </div>
      <div class="fxs-body">
        ${G.file ? `
        <div class="fxs-sec"><svg class="ic b-ic" aria-hidden="true"><use href="#i-paperclip"></use></svg>${G.multi?'Your files':'Your file'}</div>
        <button type="button" class="btn btn-ghost full gen-file-btn" id="gen-file-btn"><svg class="ic b-ic" aria-hidden="true"><use href="#i-paperclip"></use></svg>Choose ${G.multi?'files':'a file'}</button>
        <input type="file" id="gen-file" accept="${enc(G.accept||'')}" ${G.multi?'multiple':''} hidden>
        <div class="gen-file-list muted small" id="gen-file-list"></div>
        ${(G.extra||[]).map(([id,label,ph])=>`<div class="fxs-sec">${enc(label)}</div>
          <input class="input gen-x" id="gen-x-${enc(id)}" placeholder="${enc(ph)}" autocomplete="off">`).join('')}
        ` : `
        <div class="fxs-sec">${G.kind==='url' ? '🔗 Link' : '✍️ What do you want?'}</div>
        ${rows>1
          ? `<textarea class="input gen-prompt" id="gen-prompt" rows="${rows}" placeholder="${enc(G.ph)}"></textarea>`
          : `<input class="input gen-prompt" id="gen-prompt" type="${G.kind==='url'?'url':'text'}"
               inputmode="${G.kind==='url'?'url':'text'}" autocapitalize="off" autocorrect="off"
               spellcheck="false" placeholder="${enc(G.ph)}">`}`}
        ${groups.map(([label,opts])=>`<div class="fxs-sec">${enc(label)} <span class="fxs-hint">optional · tap to add</span></div>
          <div class="fxs-grid">${opts.map(chip).join('')}</div>`).join('')}
        ${(G.opts||[]).length?`<div class="fxs-sec"><svg class="ic b-ic" aria-hidden="true"><use href="#i-scissors"></use></svg>Trim <span class="fxs-hint">optional · leave blank for the whole thing</span></div>
          <div class="gen-optrow">${(G.opts||[]).map(([id,label,ph])=>
            `<label class="gen-opt"><span class="muted small">${enc(label)}</span>
              <input class="input gen-x" id="gen-x-${enc(id)}" placeholder="${enc(ph)}" autocomplete="off"></label>`).join('')}</div>`:''}
        ${(G.toggles||[]).map(([id,label])=>`<label class="gen-check">
          <input type="checkbox" class="gen-t" id="gen-t-${enc(id)}"> ${enc(label)}</label>`).join('')}
        ${music?`<div class="fxs-sec"><svg class="ic b-ic" aria-hidden="true"><use href="#i-mic"></use></svg>Lyrics <span class="fxs-hint">leave blank and the AI writes them</span></div>
          <label class="gen-check"><input type="checkbox" id="gen-inst"> Instrumental — no vocals at all</label>
          <textarea class="input gen-lyrics" id="gen-lyrics" rows="3" placeholder="[verse]&#10;your words here…"></textarea>`:''}
      </div>
      <div class="fxs-ft"><code class="fxs-cmd muted" id="gen-cmd">${G.kind==='url'?'paste a link first':'describe something first'}</code>
        <div class="fxs-acts"><button class="btn btn-neon" id="gen-go" disabled><svg class="ic b-ic" aria-hidden="true"><use href="#i-play"></use></svg>${enc(G.go||'Generate')}</button></div></div>
    </div>`, root=>{
      root.classList.add('fxs-modal');
      if(root.parentElement) root.parentElement.classList.add('fxs-bg');
      const ta=$('#gen-prompt',root), cmdEl=$('#gen-cmd',root), go=$('#gen-go',root);
      let files=[];
      if(G.file){
        const fb=$('#gen-file-btn',root), fi=$('#gen-file',root), fl=$('#gen-file-list',root);
        fb.onclick=()=>fi.click();
        fi.onchange=e=>{ files=[...(e.target.files||[])]; if(!G.multi) files=files.slice(0,1);
          fl.textContent = files.length ? files.map(f=>f.name).join(', ') : '';
          fb.textContent = files.length ? `📎 ${files.length>1?files.length+' files':'Change file'}` : `📎 Choose ${G.multi?'files':'a file'}`;
          sync(); };
      }
      const lyEl=$('#gen-lyrics',root), instEl=$('#gen-inst',root);
      const build=()=>{
        if(G.file){
          if(!files.length) return '';
          const args=(G.extra||[]).map(([id])=>(($('#gen-x-'+id,root)||{}).value||'').trim());
          if(G.needExtra && args.some(a=>!a)) return '';     // clip needs BOTH times; meme needs the caption
          if(G.multi && files.length<2) return '';           // a collage of one image is just the image
          return (G.cmd+' '+args.join(' ')).trim();
        }
        const base=(ta.value||'').trim();
        if(!base) return '';
        if(G.compose){
          const o={};
          (G.opts||[]).forEach(([id])=>{ o[id]=(($('#gen-x-'+id,root)||{}).value||''); });
          (G.toggles||[]).forEach(([id])=>{ o[id]=!!(($('#gen-t-'+id,root)||{}).checked); });
          return G.compose(base, [...picked], o);
        }
        // .trim() so a borrower can blank the verb (`over:{cmd:''}`): the Meme Builder isn't running a
        // chat command, and its footer should preview the PROMPT, not "geni …" — which reads like an
        // instruction to go and type that somewhere. No effect on the real commands (cmd is non-empty).
        let out=(G.cmd+' '+[base, ...picked].join(', ')).trim();
        if(music){
          if(instrumental) out+=' instrumental';
          else if(lyrics.trim()) out+=' | '+lyrics.trim().replace(/\s*\n\s*/g,' / ');
        }
        return out;
      };
      const sync=()=>{
        $$('.gen-chip',root).forEach(b=> b.classList.toggle('on', picked.has(b.dataset.pick)));
        if(lyEl) lyEl.disabled=instrumental;
        const c=build();
        cmdEl.textContent = c || (G.file
            ? (!files.length ? (G.multi?'choose at least two images':'choose a file first')
               : (G.multi && files.length<2 ? 'choose at least two images' : 'fill in the field above'))
            : (G.kind==='url' ? 'paste a link first' : 'describe something first'));
        cmdEl.classList.toggle('muted', !c);
        go.disabled = !c;
      };
      root.addEventListener('click', e=>{
        const b=e.target.closest('.gen-chip'); if(!b) return;
        e.preventDefault();
        const v=b.dataset.pick;
        if(picked.has(v)) picked.delete(v);
        else { if(G.single) picked.clear(); picked.add(v); }   // single-select: you translate INTO one language
        sync();
      });
      if(ta) ta.addEventListener('input', sync);
      $$('.gen-x',root).forEach(i=> i.addEventListener('input', sync));
      $$('.gen-t',root).forEach(i=> i.addEventListener('change', sync));
      if(lyEl) lyEl.addEventListener('input', ()=>{ lyrics=lyEl.value; sync(); });
      if(instEl) instEl.addEventListener('change', ()=>{ instrumental=instEl.checked; sync(); });
      $('#gen-x',root).onclick=()=>closeModal();
      go.onclick=async()=>{
        const c=build(); if(!c) return;
        closeModal();
        // Borrowed sheet (Meme Builder): hand the caller the finished prompt and let it do the work.
        // `prompt` is the command WITHOUT its verb — the description plus the chips it picked — which
        // is what a caller that isn't the chat actually wants.
        if(opts.onSubmit){
          const desc=(ta ? (ta.value||'').trim() : '');
          opts.onSubmit({ cmd:c, prompt:[desc, ...picked].filter(Boolean).join(', '), picks:[...picked], files });
          return;
        }
        // File commands run on ATTACHMENTS, so the file has to be attached before the command is sent.
        if(G.file && files.length){ try{ await aiAddFiles(files); }catch(_){ } }
        const inp=$('#ai-input');
        if(inp){ inp.value=c; inp.dispatchEvent(new Event('input')); }
        aiSend();                                  // one tap = it runs; no command to remember
      };
      sync();
      if(ta && matchMedia('(min-width:821px)').matches) ta.focus();
    });
  }

  // 🏠 Home — put the starter splash back mid-conversation, WITHOUT starting a new chat or touching
  // what's there (that's what ＋ New is for). It lands at the bottom, where you're already looking,
  // and the next message drops it again (aiAddMessage removes .ai-welcome).
  // Reveal the "Manage a server" splash card only for node-allowlisted users (access cached per session).
  // Shared by the empty-conversation splash and anything else that paints those cards, or it stays hidden.
  function _aiRevealNodeCard(){
    const box=$('#ai-msgs'); if(!box) return;
    const rev=()=>{ const c=box.querySelector('#aw-nodes'); if(c && _ai.nodeAccess) c.style.display=''; };
    if(_ai.nodeAccess===undefined) _nodeFetchState().then(rev); else rev();
  }
  function _aiWelcomeHtml(){
    return `<div class="ai-welcome">
      <img class="aw-logo" src="${S.LOGO}" alt="PosterChan" onerror="this.style.display='none'">
      <h3>Welcome to PosterChan AI</h3>
      <p class="muted">Ask me anything — or make something. Tap a card and I'll walk you through it.</p>
      <div class="aw-make">
        <button class="aw-card" data-gen="image"><svg class="ic awc-ic" aria-hidden="true"><use href="#i-palette"></use></svg><b>Make an image</b><span>describe it, pick a style</span></button>
        <button class="aw-card" data-gen="music"><svg class="ic awc-ic" aria-hidden="true"><use href="#i-music"></use></svg><b>Make a song</b><span>genre, mood, lyrics optional</span></button>
        <button class="aw-card" id="aw-nodes" data-open="nodes" style="display:none"><svg class="ic awc-ic" aria-hidden="true"><use href="#i-robot"></use></svg><b>Agents</b><span>run tasks on your servers</span></button>
        <button class="aw-card" data-gen="video"><svg class="ic awc-ic" aria-hidden="true"><use href="#i-film"></use></svg><b>Make a video</b><span>a short clip from a prompt</span></button>
        <button class="aw-card" data-gen="voice"><svg class="ic awc-ic" aria-hidden="true"><use href="#i-mic"></use></svg><b>Clone a voice</b><span>a short clip → say anything</span></button>
      </div>
      <p class="muted small aw-or">…or grab something from the web:</p>
      <div class="aw-make">
        <button class="aw-card" data-gen="audio"><svg class="ic awc-ic" aria-hidden="true"><use href="#i-headphones"></use></svg><b>Get the audio</b><span>a link → MP3</span></button>
        <button class="aw-card" data-gen="videodl"><svg class="ic awc-ic" aria-hidden="true"><use href="#i-download"></use></svg><b>Download a video</b><span>YouTube, X, TikTok → file</span></button>
        <button class="aw-card" data-gen="shot"><svg class="ic awc-ic" aria-hidden="true"><use href="#i-camera"></use></svg><b>Screenshot a page</b><span>capture any URL</span></button>
        <button class="aw-card" data-gen="translate"><svg class="ic awc-ic" aria-hidden="true"><use href="#i-translate"></use></svg><b>Translate</b><span>text → any language</span></button>
        <button class="aw-card" data-gen="search"><svg class="ic awc-ic" aria-hidden="true"><use href="#i-search"></use></svg><b>Search the web</b><span>look something up</span></button>
        <button class="aw-card" data-gen="images"><svg class="ic awc-ic" aria-hidden="true"><use href="#i-image"></use></svg><b>Find images</b><span>image search</span></button>
      </div>
      <p class="muted small aw-or">…or work with a file you already have:</p>
      <div class="aw-make">
        <button class="aw-card" data-gen="compress"><svg class="ic awc-ic" aria-hidden="true"><use href="#i-compress"></use></svg><b>Compress</b><span>image, video or PDF</span></button>
        <button class="aw-card" data-gen="clip"><svg class="ic awc-ic" aria-hidden="true"><use href="#i-scissors"></use></svg><b>Clip a video</b><span>trim start → end</span></button>
        <button class="aw-card" data-gen="extractaudio"><svg class="ic awc-ic" aria-hidden="true"><use href="#i-music"></use></svg><b>Extract audio</b><span>video → MP3</span></button>
        <button class="aw-card" data-gen="convert"><svg class="ic awc-ic" aria-hidden="true"><use href="#i-refresh"></use></svg><b>Convert</b><span>images ↔ PDF</span></button>
        <button class="aw-card" data-gen="removebackground"><svg class="ic awc-ic" aria-hidden="true"><use href="#i-wand"></use></svg><b>Remove background</b><span>transparent PNG</span></button>
        <button class="aw-card" data-gen="circlecrop"><svg class="ic awc-ic" aria-hidden="true"><use href="#i-circle-crop"></use></svg><b>Circle crop</b><span>round avatar cut-out</span></button>
        <button class="aw-card" data-gen="meme"><svg class="ic awc-ic" aria-hidden="true"><use href="#i-smile"></use></svg><b>Meme text</b><span>caption an image</span></button>
        <button class="aw-card" data-gen="talk"><svg class="ic awc-ic" aria-hidden="true"><use href="#i-speech"></use></svg><b>Make it talk</b><span>a face lip-syncs, in a cloned voice</span></button>
        <button class="aw-card" data-gen="collage"><svg class="ic awc-ic" aria-hidden="true"><use href="#i-grid"></use></svg><b>Collage</b><span>several images → one</span></button>
        <button class="aw-card" data-gen="ocr"><svg class="ic awc-ic" aria-hidden="true"><use href="#i-text"></use></svg><b>Read the text</b><span>OCR a photo or PDF</span></button>
        <button class="aw-card" data-gen="flashcards"><svg class="ic awc-ic" aria-hidden="true"><use href="#i-cards"></use></svg><b>Flashcards</b><span>PDF or notes → quiz</span></button>
      </div>
      <p class="muted small">Tap <button class="ai-cmd" data-cmd="help">help</button> to see everything I can do.</p>
    </div>`;
  }
  function aiConnect(id){
    try{ if(_ai.ws){ _ai.ws.onclose=null; _ai.ws.close(); } }catch(_){}
    clearTimeout(_ai.wsWatch);
    // Use the INSTANCE's ws origin (not location.host — that's https://localhost in the bundled app), and
    // prefer the captured bearer token over the cookie (the cross-origin session cookie isn't readable from
    // document.cookie in the app). Both resolve to the same server in the PWA.
    const wsBase = _serverOrigin().replace(/^http/, 'ws');
    const tok = S._aiToken || _cookie('access_token');
    let opened=false;
    const owner=_reminderOwner();
    const ws=new WebSocket(`${wsBase}/api/ws/chat/${id}`+(tok?`?token=${encodeURIComponent(tok)}`:''));
    _ai.ws=ws;
    const current=()=>owner===_reminderOwner() && _ai.ws===ws;
    ws.onopen=()=>{ if(!current())return; opened=true; _ai.wsBroken=false; clearTimeout(_ai.wsWatch); const q=_ai.pending||[]; _ai.pending=[]; for(const p of q){ try{ ws.send(JSON.stringify(p)); }catch(_){} } };
    ws.onmessage=e=>{ if(!current())return; let d; try{ d=JSON.parse(e.data); }catch(_){ return; } aiHandle(d); };
    // No keepalive on this WS: a slow effect/image/video generation can outlast an idle/proxy timeout
    // and the socket closes mid-flight. The server still finishes + PERSISTS the reply, so its live push
    // was lost and the answer only appeared after a manual refresh ("sometimes I never get an update").
    // If a reply was pending, pull it in (below). Idle drops need nothing — aiWsSend reconnects on send.
    ws.onclose=()=>{ if(current() && _ai.awaiting && S.VIEW==='ai' && _ai.convId===id) aiRecover(id); };
    // If the socket can't even OPEN — e.g. a CDN/proxy that drops the WS upgrade (Cloudflare over
    // HTTP/3 does this) — a queued message would sit forever and never send. After a grace period,
    // fall back to plain HTTP (POST /api/chat/send) so the command still runs + persists. Every later
    // send then goes straight over HTTP too, until a socket actually opens again (self-heals).
    _ai.wsWatch = setTimeout(()=>{ if(current() && !opened){ _ai.wsBroken=true; aiHttpFlush(id); } }, 6000);
  }
  // WS upgrade failed → run any queued payloads over plain HTTP (the endpoint persists exactly like the
  // WS), then re-render the conversation so the reply shows. Used transparently when the socket won't open.
  async function aiHttpFlush(id){
    const q=_ai.pending||[]; _ai.pending=[];
    for(const p of q){ await aiHttpSend(p, id); }
  }
  async function aiHttpSend(payload, id){
    id = id || _ai.convId; if(!id) return;
    try{
      const r=await fetch('/api/chat/send',{ method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({ conversation_id:id, content:payload.content||'', images:payload.images||[],
          pdfs:payload.pdfs||[], documents:payload.documents||[], videos:payload.videos||[], files:payload.files||[] }) });
      if(!r.ok) throw new Error('http '+r.status);
    }catch(e){ if(S.VIEW==='ai' && _ai.convId===id) aiAddMessage('assistant', enc('⚠️ Could not reach the server — try again.')); }
    _ai.awaiting=false;
    if(S.VIEW==='ai' && _ai.convId===id) aiOpenConversation(id);   // re-render the persisted reply (also retries the WS)
  }
  // The chat WS dropped while a reply was pending. Poll the persisted conversation (HTTP, no socket
  // needed) until the new assistant message lands — covers slow effects that finish after the drop —
  // then re-render it (which also reopens the WS). Bounded so it can't poll forever.
  function aiRecover(id){
    if(_ai.recovering) return;   // one poller at a time — onclose AND the stall-watchdog can both call this
    _ai.recovering=true;
    const had = $('#ai-msgs') ? $('#ai-msgs').querySelectorAll('.ai-msg.assistant').length : 0;
    let tries=0;
    (async function poll(){
      // ~5 min ceiling: a slow effect/video/music render can finish minutes after the socket drops. The
      // old 60s bound expired before the file landed, leaving the reply unseen until a manual refresh
      // (the recurring "clay never showed" report). Effects are the slowest path, so size for them.
      if(!_ai.awaiting || S.VIEW!=='ai' || _ai.convId!==id || ++tries>100){ _ai.recovering=false; return; }
      let conv=null; try{ conv=await fetch('/api/conversations/'+id).then(r=>r.json()); }catch(_){}
      const got=((conv&&conv.messages)||[]).filter(m=>m.role==='assistant').length;
      if(got>had){ _ai.awaiting=false; _ai.recovering=false; if(S.VIEW==='ai' && _ai.convId===id) aiOpenConversation(id); return; }
      setTimeout(poll, 3000);
    })();
  }
  // Send (or queue) a payload on the chat WS — never fail just because it's mid-connect; queue it
  // and the onopen handler flushes. Reconnects if the socket is closed.
  function aiWsSend(payload){
    if(_ai.wsBroken){ aiHttpSend(payload); return; }   // socket can't open here → straight to HTTP
    if(_ai.ws && _ai.ws.readyState===1){ try{ _ai.ws.send(JSON.stringify(payload)); return; }catch(_){} }
    (_ai.pending=_ai.pending||[]).push(payload);
    if(!_ai.ws || _ai.ws.readyState>1) aiConnect(_ai.convId);   // CLOSING/CLOSED → reconnect; CONNECTING → just wait
  }
  function aiAddMessage(role, html){
    const box=$('#ai-msgs'); if(!box) return null;
    const w=box.querySelector('.ai-welcome'); if(w) w.remove();   // first real message → drop the splash
    const el=document.createElement('div'); el.className='ai-msg '+(role==='user'?'user':'assistant');
    el.innerHTML=`<div class="ai-bubble">${html}</div>`;
    if(role!=='user' && !/ai-err/.test(html)){   // 🔊 manual read-aloud (built-in TTS); not on error bubbles
      const spk=document.createElement('button'); spk.textContent='🔊'; spk.title='Read aloud';
      spk.style.cssText='background:none;border:none;cursor:pointer;opacity:.55;font-size:13px;padding:2px 4px;align-self:flex-start';
      spk.onclick=()=>{ const b=el.querySelector('.ai-bubble'); aiSpeak(b?b.textContent:''); };   // manual click always speaks (even when auto-narration is muted)
      el.appendChild(spk);
    }
    box.appendChild(el); aiScroll(); return el;
  }
  // Voice narration of AI replies — on by default, mutable via the 🔊/🔇 toggle (matches the old UI).
  let _ttsEnabled = localStorage.getItem('ttsEnabled') !== 'false';
  function _aiTtsBtn(){ const b=$('#ai-tts'); if(b){ b.textContent=_ttsEnabled?'🔊':'🔇'; b.title=_ttsEnabled?'Voice narration on — tap to mute':'Voice narration muted — tap to enable'; } }
  function aiToggleTTS(){
    _ttsEnabled=!_ttsEnabled;
    try{ localStorage.setItem('ttsEnabled', _ttsEnabled); }catch(_){}
    if(!_ttsEnabled){ try{ if(S._narrateAudio){ S._narrateAudio.pause(); S._narrateAudio=null; } }catch(_){} }   // mute = stop current
    _aiTtsBtn();
  }
  // Speak text via the node's built-in TTS. isAuto=true is the auto-narration path (skipped when muted);
  // a manual 🔊 click passes no flag and always speaks.
  async function aiSpeak(text, isAuto){
    if(isAuto && !_ttsEnabled) return;
    text=(text||'').replace(/https?:\/\/\S+/gi,' ').replace(/\s+/g,' ').trim().slice(0,2000);
    if(!text){ return; }
    try{ if(S._narrateAudio){ S._narrateAudio.pause(); S._narrateAudio=null; } }catch(_){}
    toast('🔊 reading…');
    try{
      const r=await fetch('/client/narrate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})});
      const j=await r.json().catch(()=>({}));
      if(!r.ok || !j.audio){ toast(j.error||'narration unavailable'); return; }
      S._narrateAudio=new Audio('data:audio/mp3;base64,'+j.audio); S._narrateAudio.play().catch(()=>toast('tap 🔊 to play'));
    }catch(_){ toast('narration failed'); }
  }
  // Why the mic can't run, phrased so the answer is actionable. The old one-line check reported all
  // three causes as "voice input not supported on this browser", which is what a desktop-app mic
  // failure looked like — a dead end. Returns '' when nothing is in the way.
  function _micBlocker(){
    // No mediaDevices at all is almost always this: an insecure origin (http://<lan-host>), where
    // Chromium removes the API outright rather than failing the call.
    if(!window.isSecureContext) return 'the mic needs an https connection to this instance';
    if(!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia)) return 'no microphone access here (mediaDevices missing)';
    if(!window.MediaRecorder) return 'this browser can’t record audio (MediaRecorder missing)';
    return '';
  }
  // 🎤 Voice input: record a clip, transcribe via the node's Whisper STT, append to the AI input.
  let _aiRec=null, _aiChunks=[], _aiMicStarting=false;
  async function aiToggleMic(){
    const mic=$('#ai-mic');
    if(_aiRec && _aiRec.state==='recording'){ _aiRec.stop(); return; }
    if(_aiMicStarting) return;   // a start is already in flight (async getUserMedia) — ignore the double-tap
    { const why=_micBlocker(); if(why){ toast(why); return; } }
    _aiMicStarting=true;
    let stream=null;
    try{
      stream=await navigator.mediaDevices.getUserMedia({audio:true});
      _aiChunks=[];
      const mime=['audio/webm;codecs=opus','audio/webm','audio/mp4',''].find(t=>!t || MediaRecorder.isTypeSupported(t));
      _aiRec=new MediaRecorder(stream, mime?{mimeType:mime}:undefined);
      _aiRec.ondataavailable=e=>{ if(e.data && e.data.size) _aiChunks.push(e.data); };
      _aiRec.onstop=async()=>{
        try{ stream.getTracks().forEach(t=>t.stop()); }catch(_){}   // always release the mic first
        if(mic){ mic.style.color=''; mic.textContent='🎤'; }
        const blob=new Blob(_aiChunks,{type:(_aiRec&&_aiRec.mimeType)||'audio/webm'});
        if(blob.size<200){ return; }
        toast('transcribing…');
        const fd=new FormData(); fd.append('audio', blob, 'voice.webm'); fd.append('language','en');   // AI-chat voice input stays English (Live Translate uses auto-detect)
        try{
          const r=await fetch('/client/stt',{method:'POST',body:fd});
          const j=await r.json().catch(()=>({}));
          if(!r.ok || !(j.text||'').trim()){ toast(j.error||'voice input unavailable'); return; }
          const ta=$('#ai-input'); if(ta){ ta.value=(ta.value?ta.value.trim()+' ':'')+j.text.trim(); ta.focus(); ta.dispatchEvent(new Event('input')); }
        }catch(_){ toast('voice input failed'); }
      };
      _aiRec.start();
      if(mic){ mic.style.color='#ff4d6d'; mic.textContent='⏹'; }
      toast('🎤 recording — tap to stop');
    }catch(e){
      try{ if(stream) stream.getTracks().forEach(t=>t.stop()); }catch(_){}
      toast(e && e.name==='NotAllowedError' ? 'microphone permission denied' : 'could not start recording'+(e&&e.name?' ('+e.name+')':''));
    }finally{
      _aiMicStarting=false;
    }
  }
  // ---------- Live Translate (in-person two-way voice translator) ----------
  // Push-to-talk: one big mic button. Speak in either of the room's two chosen languages; Whisper
  // (/client/stt, language=auto) transcribes + detects which was spoken, we translate to the OTHER
  // (/client/translate), show both in a big transcript, and optionally speak the translation aloud
  // (ltSpeak → /client/narrate, voice matched to the target language). Reuses the AI chat's mic pattern.
  const LT_LANGS=[
    // code, name, flag, script bucket. (The TTS voice per language lives SERVER-side in TTSService —
    // /client/narrate is passed `lang` and picks the voice, so the voice table isn't duplicated here.)
    ['en','English','🇬🇧','lat'],['th','Thai','🇹🇭','thai'],['my','Burmese','🇲🇲','mymr'],['es','Spanish','🇪🇸','lat'],['fr','French','🇫🇷','lat'],
    ['de','German','🇩🇪','lat'],['it','Italian','🇮🇹','lat'],['pt','Portuguese','🇵🇹','lat'],['ru','Russian','🇷🇺','cyril'],
    ['zh','Chinese','🇨🇳','han'],['ja','Japanese','🇯🇵','kana'],['ko','Korean','🇰🇷','hang'],['ar','Arabic','🇸🇦','arab'],
    ['hi','Hindi','🇮🇳','deva'],['vi','Vietnamese','🇻🇳','lat'],['id','Indonesian','🇮🇩','lat'],['tl','Filipino','🇵🇭','lat'],
    ['uk','Ukrainian','🇺🇦','cyril'],['tr','Turkish','🇹🇷','lat'],['pl','Polish','🇵🇱','lat'],['nl','Dutch','🇳🇱','lat'],
  ];
  const _ltRow=c=>LT_LANGS.find(l=>l[0]===c);
  const _ltName=c=>{ const f=_ltRow(c); return f?f[1]:(c||'').toUpperCase(); };
  const _ltFlag=c=>{ const f=_ltRow(c); return f?f[2]:'🏳️'; };
  function _ltPair(){ try{ const p=JSON.parse(localStorage.getItem('pcTranslatePair')||'null'); if(p&&p.a&&p.b&&_ltRow(p.a)&&_ltRow(p.b)) return p; }catch(_){} return { a:'en', b:'th' }; }
  function _ltSavePair(p){ try{ localStorage.setItem('pcTranslatePair', JSON.stringify(p)); }catch(_){} }
  let _ltSpeak = (localStorage.getItem('pcTranslateSpeak')||'1')!=='0';
  let _ltRec=null, _ltChunks=[], _ltMicStarting=false, _ltBusy=false, _ltCancel=false;

  // Dominant Unicode script of the transcript — used to disambiguate the two languages when Whisper's
  // detected code is empty/near-miss (reliable for cross-script pairs like English+Thai/CJK/Arabic).
  function _ltScriptOf(text){
    const c={};
    for(const ch of (text||'')){ const o=ch.codePointAt(0); let k=null;
      if(o>=0x0E00&&o<=0x0E7F)k='thai'; else if(o>=0x1000&&o<=0x109F)k='mymr'; else if(o>=0x0400&&o<=0x04FF)k='cyril';
      else if(o>=0x0600&&o<=0x06FF)k='arab'; else if(o>=0x0900&&o<=0x097F)k='deva';
      else if(o>=0x3040&&o<=0x30FF)k='kana'; else if(o>=0xAC00&&o<=0xD7AF)k='hang';
      else if(o>=0x4E00&&o<=0x9FFF)k='han';
      else if((o>=0x41&&o<=0x5A)||(o>=0x61&&o<=0x7A)||(o>=0xC0&&o<=0x24F))k='lat';
      else continue;
      c[k]=(c[k]||0)+1;
    }
    let top=null,n=0; for(const k in c){ if(c[k]>n){ n=c[k]; top=k; } }
    return top;
  }
  // Decide [source, target] within the chosen pair. Prefer Whisper's detected ISO code; if it's neither
  // (empty/near-miss), fall back to the transcript's script — so a mis-detected B turn is NOT silently
  // routed as A→B and echoed back untranslated. Last resort: default the source to A.
  function _ltRoute(lang, text, a, b){
    if(lang===a) return [a, b];
    if(lang===b) return [b, a];
    const sa=(_ltRow(a)||[])[3], sb=(_ltRow(b)||[])[3], ts=_ltScriptOf(text);
    if(ts && sa!==sb){
      if(ts===sb && ts!==sa) return [b, a];
      if(ts===sa && ts!==sb) return [a, b];
    }
    return [a, b];
  }
  function renderTranslate(){
    const feed=$('#feed'); if(!feed) return;
    // NOTE: _ltCancel is intentionally NOT reset here — a stale cancel from navigating away suppresses
    // that turn's in-flight pipeline/narration; ltToggleMic clears it when the next recording starts.
    const pair=_ltPair();
    const opts=sel=>LT_LANGS.map(([c,n,fl])=>`<option value="${c}"${c===sel?' selected':''}>${fl} ${enc(n)}</option>`).join('');
    feed.innerHTML=`<div class="lt-wrap">
      <div class="lt-bar">
        <select id="lt-a" class="lt-lang">${opts(pair.a)}</select>
        <button id="lt-swap" class="lt-swap" title="swap languages"><svg class="ic x-ic" aria-hidden="true"><use href="#i-swap"></use></svg></button>
        <select id="lt-b" class="lt-lang">${opts(pair.b)}</select>
      </div>
      <div class="lt-log" id="lt-log"><div class="lt-hint muted">Pick the two languages, then tap the mic and speak. Either person can talk — it auto-detects and translates to the other side.</div></div>
      <div class="lt-foot">
        <button id="lt-speak" class="lt-speak" title="speak the translation aloud">${_ltSpeak?'🔊':'🔇'}</button>
        <button id="lt-mic" class="lt-mic" aria-label="tap to speak"><svg class="ic b-ic" aria-hidden="true"><use href="#i-mic"></use></svg></button>
        <span id="lt-status" class="lt-status muted"></span>
      </div></div>`;
    const save=()=>{ _ltSavePair({ a:$('#lt-a').value, b:$('#lt-b').value }); };
    // Guard: the two sides must differ, else every turn would translate a language into itself.
    const guard=(changed, other)=>{ if(changed.value===other.value){ const alt=LT_LANGS.find(l=>l[0]!==changed.value); if(alt) other.value=alt[0]; } };
    $('#lt-a').onchange=()=>{ guard($('#lt-a'),$('#lt-b')); save(); };
    $('#lt-b').onchange=()=>{ guard($('#lt-b'),$('#lt-a')); save(); };
    $('#lt-swap').onclick=()=>{ const a=$('#lt-a'), b=$('#lt-b'); const t=a.value; a.value=b.value; b.value=t; save(); };
    $('#lt-speak').onclick=()=>{ _ltSpeak=!_ltSpeak; try{ localStorage.setItem('pcTranslateSpeak', _ltSpeak?'1':'0'); }catch(_){} $('#lt-speak').textContent=_ltSpeak?'🔊':'🔇'; if(!_ltSpeak){ try{ if(S._narrateAudio){ S._narrateAudio.pause(); S._narrateAudio=null; } }catch(_){} } };
    $('#lt-mic').onclick=ltToggleMic;
  }
  // Called from renderView when leaving Live Translate. Sets the cancel flag (so onstop skips the clip
  // AND an in-flight pipeline skips its final render/speak — no audio playing on another screen),
  // stops any active recording (releases the mic/getUserMedia stream), and halts current narration.
  function ltTeardown(){
    _ltCancel=true;
    try{ if(_ltRec && _ltRec.state==='recording') _ltRec.stop(); }catch(_){}
    try{ if(S._narrateAudio){ S._narrateAudio.pause(); S._narrateAudio=null; } }catch(_){}
  }
  function _ltStatus(s){ const el=$('#lt-status'); if(el) el.textContent=s||''; }
  function _ltAddTurn(srcLang, original, tgtLang, translated){
    const log=$('#lt-log'); if(!log) return;
    const h=log.querySelector('.lt-hint'); if(h) h.remove();
    const el=document.createElement('div'); el.className='lt-turn';
    el.innerHTML=`<div class="lt-src"><span class="lt-tag">${_ltFlag(srcLang)} ${enc(_ltName(srcLang))}</span>${enc(original)}</div>`
      +`<div class="lt-tr"><button class="lt-replay" title="play the translation again" aria-label="replay"><svg class="ic b-ic" aria-hidden="true"><use href="#i-volume"></use></svg></button>`
      +`<span class="lt-tag">${_ltFlag(tgtLang)} ${enc(_ltName(tgtLang))}</span>${enc(translated)}</div>`;
    // Replay always plays (even when auto-speak is off). Clear the cancel flag first: it's an explicit
    // on-view action, and the flag can linger true after a navigate-away (would otherwise mute ltSpeak).
    const rb=el.querySelector('.lt-replay'); if(rb) rb.onclick=()=>{ _ltCancel=false; ltSpeak(translated, tgtLang); };
    log.appendChild(el); log.scrollTop=log.scrollHeight;
  }
  // Speak the translation. Passes the target language code so /client/narrate (TTSService) picks a
  // language-matched voice — including Latin-script targets (Spanish/French/…) the English default
  // voice would mispronounce. The voice table lives on the server (single source of truth).
  async function ltSpeak(text, lang){
    try{ if(S._narrateAudio){ S._narrateAudio.pause(); S._narrateAudio=null; } }catch(_){}
    try{
      const r=await fetch('/client/narrate',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({ text:(text||'').slice(0,2000), lang })});
      const j=await r.json().catch(()=>({}));
      if(_ltCancel) return;   // navigated away while the audio was generating → don't play it elsewhere
      if(!r.ok || !j.audio) return;
      S._narrateAudio=new Audio('data:audio/mp3;base64,'+j.audio); S._narrateAudio.play().catch(()=>{});
    }catch(_){}
  }
  async function ltToggleMic(){
    const mic=$('#lt-mic');
    if(_ltRec && _ltRec.state==='recording'){ _ltRec.stop(); return; }
    if(_ltMicStarting) return;
    if(_ltBusy){ toast('one moment — still translating the last turn'); return; }   // serialize turns
    { const why=_micBlocker(); if(why){ toast(why); return; } }
    _ltMicStarting=true; let stream=null;
    try{
      stream=await navigator.mediaDevices.getUserMedia({audio:true});
      _ltChunks=[]; _ltCancel=false;
      const mime=['audio/webm;codecs=opus','audio/webm','audio/mp4',''].find(t=>!t || MediaRecorder.isTypeSupported(t));
      _ltRec=new MediaRecorder(stream, mime?{mimeType:mime}:undefined);
      _ltRec.ondataavailable=e=>{ if(e.data && e.data.size) _ltChunks.push(e.data); };
      _ltRec.onstop=async()=>{
        try{ stream.getTracks().forEach(t=>t.stop()); }catch(_){}   // release the mic FIRST, always
        const mic2=$('#lt-mic'); if(mic2){ mic2.classList.remove('rec'); mic2.textContent='🎤'; }
        if(_ltCancel){ _ltCancel=false; _ltStatus(''); return; }   // navigated away mid-record → don't process
        const blob=new Blob(_ltChunks,{type:(_ltRec&&_ltRec.mimeType)||'audio/webm'});
        if(blob.size<200){ _ltStatus(''); return; }
        await ltPipeline(blob);
      };
      _ltRec.start();
      if(mic){ mic.classList.add('rec'); mic.textContent='⏹'; }
      _ltStatus('listening… tap to stop');
    }catch(e){
      try{ if(stream) stream.getTracks().forEach(t=>t.stop()); }catch(_){}
      toast(e && e.name==='NotAllowedError' ? 'microphone permission denied' : 'could not start recording'+(e&&e.name?' ('+e.name+')':''));
    }finally{ _ltMicStarting=false; }
  }
  async function ltPipeline(blob){
    _ltBusy=true;
    try{
      const a=($('#lt-a')||{}).value||'en', b=($('#lt-b')||{}).value||'th';
      _ltStatus('transcribing…');
      let text='', lang='';
      try{
        const fd=new FormData(); fd.append('audio', blob, 'turn.webm'); fd.append('language','auto');
        const r=await fetch('/client/stt',{method:'POST',body:fd});
        const j=await r.json().catch(()=>({}));
        if(!r.ok || !(j.text||'').trim()){ _ltStatus(''); toast(j.error||'could not hear that — try again'); return; }
        text=j.text.trim(); lang=(j.lang||'').toLowerCase();
      }catch(_){ _ltStatus(''); toast('transcription failed'); return; }
      let [src, tgt]=_ltRoute(lang, text, a, b);
      _ltStatus('translating…');
      // translate() posts the target ISO CODE (like the timeline translatePost caller), so
      // /client/translate's "already in the target language" check works and returns the text
      // UNCHANGED when we mis-routed — that's our signal to swap and retry with the other language.
      const translate=async(toCode)=>{
        try{
          const r=await fetch('/client/translate',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({ text, to:toCode, fast:true })});   // skip detection round-trip + examples → faster
          const j=await r.json().catch(()=>({}));
          return (r.ok && (j.text||'').trim()) ? j.text.trim() : (j.error ? {err:j.error} : {err:'translation unavailable'});
        }catch(_){ return {err:'translate failed'}; }
      };
      let translated=await translate(tgt);
      if(translated && translated.err){ _ltStatus(''); toast(translated.err); return; }
      // Echo = endpoint returned the text unchanged (it's already in tgt → we routed backwards). Compare
      // normalized, since the endpoint collapses whitespace on that unchanged return.
      if(_ltNorm(translated)===_ltNorm(text)){
        const swapped=await translate(src);   // translate to the OTHER side instead
        if(swapped && swapped.err){ _ltStatus(''); toast(swapped.err); return; }   // don't render the untranslated original
        if(_ltNorm(swapped)!==_ltNorm(text)){ [src, tgt]=[tgt, src]; translated=swapped; }
        // else: both sides return it unchanged → the phrase is the same in both languages; keep it as-is
      }
      if(_ltCancel){ _ltStatus(''); return; }   // left the view mid-pipeline → don't render/speak
      _ltStatus('');
      _ltAddTurn(src, text, tgt, translated);
      if(_ltSpeak) ltSpeak(translated, tgt);   // TTS in the target language's own voice
    }finally{ _ltBusy=false; }
  }
  function aiScroll(){ const box=$('#ai-msgs'); if(!box) return; box.scrollTop=box.scrollHeight; _aiImgKeys(box); }
  // An image in the transcript opens the lightbox when CLICKED, but an <img> takes no focus, so with the
  // keyboard there was no way to open one at all. Stamping tabindex as messages land (rather than at build
  // time) catches every path — a streamed reply, a generated image, a reloaded conversation.
  function _aiImgKeys(box){
    box.querySelectorAll('img:not([tabindex])').forEach(im=>{
      im.tabIndex=0; im.setAttribute('role','button');
      if(!im.getAttribute('aria-label')) im.setAttribute('aria-label', im.alt || 'Open image');
    });
  }
  function aiHandle(d){
    // Stale re-delivery: the server replays a queued `response` whose live push was missed (socket
    // dropped / user on another conversation). We reload the whole conversation from the DB on
    // open/recover, so replaying a reply that was PERSISTED would render it TWICE (the reported
    // double-geni-image). Skip only those (`persisted`). Non-persisted replays — e.g. interim agent
    // progress chunks the server never saves — still render, since the DB reload won't have them.
    if(d.pending && d.persisted){ if(d.type==='response') _ai.awaiting=false; return; }
    // Any live frame means the socket is healthy → push the stall-watchdog out so an actively-streaming
    // (or progress-reporting) reply never trips the recovery poll.
    if(_ai.awaiting && _ai.recoverWatch){ clearTimeout(_ai.recoverWatch); const _cid=_ai.convId;
      _ai.recoverWatch=setTimeout(()=>{ if(_ai.awaiting && S.VIEW==='ai' && _ai.convId===_cid) aiRecover(_cid); }, 30000); }
    if(d.type==='stream'){
      const c=(d.data&&d.data.content)??d.content??''; if(typeof c!=='string') return;
      if(!_ai.streamEl){ _ai.streamBuf=''; _ai.streamEl=aiAddMessage('assistant',''); }
      _ai.streamBuf+=c; const b=_ai.streamEl.querySelector('.ai-bubble'); if(b){ b.textContent=_ai.streamBuf; } aiScroll();
    } else if(d.type==='stream_clear'){
      _ai.streamBuf=''; if(_ai.streamEl){ const b=_ai.streamEl.querySelector('.ai-bubble'); if(b) b.textContent=''; }
    } else if(d.type==='stream_end'){
      const said=_ai.streamBuf;
      if(_ai.streamEl){ const b=_ai.streamEl.querySelector('.ai-bubble'); if(b) b.innerHTML=aiFormat(_ai.streamBuf); }
      _ai.streamEl=null; _ai.streamBuf=''; _ai.awaiting=false; aiScroll();
      aiSpeak(said, true);   // auto-narrate the reply (no-op when muted)
    } else if(d.type==='text'){
      aiAddMessage('assistant', aiFormat(d.content||'')); _ai.awaiting=false;
      aiSpeak(d.content||'', true);
    } else if(d.type==='response'){
      // Landed while you were on another view — the socket is deliberately kept open for an
      // in-flight render, so say so instead of leaving it to be discovered.
      if(S.VIEW!=='ai'){ _aiBadge(true); try{ toast('🤖 your AI result is ready'); }catch(_){} }
      aiAddMessage('assistant', aiRenderResponse(d.data||{})); _ai.awaiting=false;
    } else if(d.type==='error'){
      aiAddMessage('assistant', `<span class="ai-err">⚠ ${enc(d.message||'error')}</span>`);
      _ai.streamEl=null; _ai.streamBuf=''; _ai.awaiting=false;
    } else if(d.type==='reminder'){
      reminderAlert((d.content!=null?d.content:(d.data&&d.data.content))||'Reminder',d.data?{...d.data,...d}:d);   // fired reminder → popup + sound
    } else if(d.type==='agent_progress'){
      _agentProgress(d.step, d.max, d.node);   // live "working… step N/M" pill for a long run
    } else if(d.type==='agent_done'){
      { const e=$('#ai-agent-prog'); if(e) e.remove(); clearTimeout(_agentProgTO); }   // run over → drop the pill
      _agentDoneNotify(!!d.ok, d.conv);   // background agent finished — tell the user wherever they are
    }
  }
  // Live progress pill for a running background agent: a single element that updates in place with the
  // step count, so a slow multi-step run (the model reloads onto the shared GPU each step) never looks
  // dead in the silent gaps. Ephemeral — self-clears on agent_done or after 3 min of no updates.
  let _agentProgTO=null;
  function _agentProgress(step, max, node){
    const msgs=$('#ai-msgs'); if(!msgs) return;
    let el=$('#ai-agent-prog');
    if(!el){ el=document.createElement('div'); el.id='ai-agent-prog'; el.className='agent-prog'; msgs.appendChild(el); }
    el.innerHTML=`<span class="ap-spin"></span> 🤖 working… step ${enc(String(step||'?'))}/${enc(String(max||'?'))}${(node&&node!=='local')?` on <code>${enc(node)}</code>`:''}`;
    try{ aiScroll(); }catch(_){}
    clearTimeout(_agentProgTO); _agentProgTO=setTimeout(()=>{ const e=$('#ai-agent-prog'); if(e) e.remove(); }, 180000);
  }
  // A background agent run finished (success or failure). The result was persisted to its launch
  // conversation, but the user has usually navigated away — so surface a longer-lived, CLICKABLE toast
  // that jumps to that chat, and if they're already on it, reload so the persisted result renders.
  function _agentDoneNotify(ok, conv){
    /* Repaint if the chat is ON SCREEN, which is not the same as being the current VIEW.
     *
     * This used to ask `VIEW==='ai'`, and on the desktop that is false whenever another WINDOW has
     * focus — the AI chat is sitting right there in its own window, fully visible, while VIEW names
     * whatever you clicked last. So a run you watched finish left its own conversation unchanged and
     * you had to leave and come back to see the result, which is exactly how it was reported.
     *
     * The DOM is the honest question: `#ai-msgs` exists only while the chat is mounted. Same reason
     * the desktop had to stop trusting `w.view` for what a window is showing. */
    if(document.getElementById('ai-msgs') && _ai.convId===conv){
      try{ aiOpenConversation(conv); }catch(_){}
    }
    const t=document.createElement('div'); t.className='toast'; t.style.cursor='pointer';
    t.textContent=(ok?'✅ Agent run finished':'⚠️ Agent run finished with problems')+' — tap to open';
    t.onclick=()=>{ try{ t.remove(); }catch(_){} switchView('ai'); if(conv) aiOpenConversation(conv); };
    const root=$('#toast-root'); if(root){ root.appendChild(t); setTimeout(()=>{ try{ t.remove(); }catch(_){} }, 12000); }
  }

  // Store every delivery before applying interruption preferences. Silencing an alert must never
  // erase its Notification centre history, and repeated delivery of one occurrence must not ring again.
  function reminderAlert(text,data={}){
    const fresh=_rememberReminder({...data,content:text},_reminderOwner(),true);
    _remindersChanged();
    if(data.reminder_id&&data.due_at&&!fresh)return;
    if(!notificationAllowed('reminders'))return;
    const route=data.route==='calendar'?'calendar':'notifications';
    osNotify('⏰ Reminder', text, { tag:'pc-reminder-'+String(data.reminder_id||''),type:'reminders',route });
    try{ if(window.PCOS && PCOS.isOn() && PCOS.osToast)
           PCOS.osToast('<b>Reminder</b> — '+enc(String(text||'').slice(0,120)),S.LOGO,()=>openOsNotificationRoute(route)); }catch(_){}
    const ex=document.getElementById('reminderOverlay'); if(ex) ex.remove();
    const ov=document.createElement('div'); ov.id='reminderOverlay';
    ov.style.cssText='position:fixed;inset:0;z-index:600;display:grid;place-items:center;padding:24px;background:rgba(4,2,12,.8);backdrop-filter:blur(4px)';
    ov.innerHTML=`<div class="reminder-card"><div style="font-size:42px">⏰</div><h2 style="margin:10px 0">Reminder</h2>
      <div style="font-size:18px;margin-bottom:20px">${aiFormat(String(text||''))}</div>
      <button class="btn btn-neon" id="reminderOpen">${route==='calendar'?'Open Calendar':'Open Notifications'}</button> <button class="btn btn-ghost" id="reminderDismiss">Dismiss</button></div>`;
    const close=()=>ov.remove();
    ov.addEventListener('click',e=>{ if(e.target===ov) close(); });
    document.body.appendChild(ov);
    const b=ov.querySelector('#reminderDismiss'); if(b) b.onclick=close;
    const open=ov.querySelector('#reminderOpen');if(open)open.onclick=()=>{close();openOsNotificationRoute(route);};
  }
  // Markdown + the backend's custom inline markup the old web UI rendered: !video[](url), !audio[](url),
  // ![](url) images, links, and magnet/.torrent → an "add torrent" action. So command outputs
  // (musicgeni/videogeni/compress/clip = !video/!audio; torrents = magnet) display right, not as text.
  function aiFormat(src){
    src=String(src||''); const slots=[]; const stash=h=>{ slots.push(h); return ` S${slots.length-1} `; };
    // Persisted flashcard deck: [[FC]]<base64 JSON>[[/FC]] (saved server-side) → re-hydrate the
    // interactive quiz on reload, identical to the live render. Decode is unicode-safe (atob+escape).
    src=src.replace(/\[\[FC\]\]([A-Za-z0-9+/=]+)\[\[\/FC\]\]/g,(m,b64)=>{
      try{ const data=JSON.parse(decodeURIComponent(escape(atob(b64)))); if(data && Array.isArray(data.cards) && data.cards.length){
        _ai.decks=_ai.decks||{}; const id='fc'+Date.now().toString(36)+Math.floor(Math.random()*1e4).toString(36);
        _ai.decks[id]={ cards:data.cards, idx:0, answered:new Array(data.cards.length).fill(null), score:0, title:data.title };
        return stash(`<div class="flashcard-deck" id="${id}">${_fcRender(id)}</div>`);
      } }catch(_){}
      return '';
    });
    // src attributes are absolutized to the instance origin (_absUrl) so a /api/files/… artifact loads from
    // the server, not https://localhost, in the bundled app. The URL passed to _aiFileActions stays relative
    // so its fetch (re-upload) goes through the shim, which adds credentials for the authed /api/files/ call.
    src=src.replace(/!video\[([^\]]*)\]\(\s*((?:https?:\/\/|\/)[^)\s]+)\s*\)/g,(m,a,u)=>stash(`<div class="ai-media"><video controls src="${enc(_absUrl(u))}" onerror="window.__aiMediaRetry(this)"></video></div>`+_aiFileActions(u,'video',a)));
    src=src.replace(/!audio\[([^\]]*)\]\(\s*((?:https?:\/\/|\/)[^)\s]+)\s*\)/g,(m,a,u)=>stash(`<div class="ai-media"><audio controls src="${enc(_absUrl(u))}"></audio></div>`+_aiFileActions(u,'audio',a)));
    // inline images from a command output (effects/stamps, compress/convert) → show with the same
    // copy-link / reply buttons; stash BEFORE mdToHtml so it doesn't render a plain <img>.
    src=src.replace(/!\[([^\]]*)\]\(\s*((?:https?:\/\/|\/)[^)\s]+)\s*\)/g,(m,a,u)=>stash(`<div class="ai-media"><img src="${enc(_absUrl(u))}" data-full="${enc(_absUrl(u))}" onerror="window.__aiMediaRetry(this)"></div>`+_aiFileActions(u,'image',a)));
    /* A NON-MEDIA artifact arrives as a plain markdown LINK, and that is the one form of it that
     * never worked outside a browser. The agent's `/workspace` backup is what reaches a user —
     * `[⬇️ sandbox-workspace.tar.gz](/api/files/…)` — and mdInline renders it as `<a href="/api/…">`,
     * a ROOT-RELATIVE href that resolves against the PAGE origin. In the bundled app that origin is
     * https://localhost (Android) or app://posterchan (desktop), where nothing serves /api at all:
     * the shell answers with its own not-found body and NO request ever leaves the device. Measured
     * on the run that was reported — the instance log has no GET for the file, only the chat being
     * opened. (mdInline cannot absolutize it either: the same renderer draws untrusted nostr
     * markdown, where a relative href must stay inert.)
     *
     * So it becomes the SAME `.ai-dlfile` button the media rows already offer, whose fetch goes
     * through the shim with credentials — one download path for every artifact, in both shells,
     * rather than a second one that only holds up on the web. The ⬇️ the server puts in the label
     * is stripped before the label becomes a FILENAME on somebody's disk. */
    src=src.replace(/\[([^\]]+)\]\(\s*(\/api\/files\/[^)\s]+)\s*\)/g,(m,label,u)=>{
      const nm=String(label||'').replace(/^[\s⬇↓️]+/,'').trim();
      return stash(`<button class="btn btn-cyan small ai-dlfile" data-url="${enc(u)}" data-name="${enc(_artName(nm,u))}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-download"></use></svg>${enc(nm||'Download')}</button>`);
    });
    // Torrent browse buttons: [Download](cmd:torrents download tv 1) → a command button; and
    // [Add](magnet:<url-encoded magnet>) → an add-torrent button. (cmd: hrefs contain spaces that
    // would break markdown link parsing, so stash them BEFORE mdToHtml runs.)
    src=src.replace(/\[([^\]]+)\]\(cmd:([^)]+)\)/g,(m,label,cmd)=>stash(`<button class="ai-cmd" data-cmd="${enc(cmd.trim())}">${enc(label)}</button>`));
    src=src.replace(/\[([^\]]+)\]\(magnet:([^)\s]+)\)/g,(m,label,mag)=>{ let u=mag; try{ u=decodeURIComponent(mag); }catch(_){} return stash(`<button class="ai-magnet" data-magnet="${enc(u)}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-magnet"></use></svg>${enc(label)}</button>`); });
    src=src.replace(/magnet:\?[^\s)<]+/gi,u=>stash(`<button class="ai-magnet" data-magnet="${enc(u)}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-magnet"></use></svg>Add torrent</button>`));
    let html=mdToHtml(src);
    return html.replace(/ S(\d+) /g,(m,i)=>slots[+i]||'');
  }
  // Render the rich command payloads the backend streams as a `response`.
  // When the effects studio is active (_ai.replyTo set), offer a button to post the generated media
  // back as a reply to the source post. Stashes the base64 so the reply can upload it to Blossom.
  // Inline command-output media (effects/compress/convert) lives at an authed /api/files/ artifact
  // URL (encrypted at rest) — NOT shareable. These fetch those bytes and RE-UPLOAD to PUBLIC Blossom
  // so the link works in a Nostr reply. Only for local (/) URLs; external media is already public.
  /* THE NAME THE FILE CAME WITH, which the URL does not have.
   *
   * An /api/files/ artifact is stored content-addressed — `enc_<sha256>.mp3` — so every action that
   * took its filename from the URL filed the bytes under that: a song downloaded with `ytdl` landed
   * in the Music library titled `enc_c62e8fb4…`, and Download / Save to Notes wrote
   * `posterchan-<timestamp>.mp3`. The real name is in the markdown label the server wrote
   * (`!audio[Rick Astley - Never Gonna Give You Up.mp3](…)`), which is also the ONLY thing that
   * survives a reload — the payload fields are long gone by then.
   *
   * Two labels are MARKERS, not names: `song` and `video`, which is how this row knows whether an
   * MP4 has an audio track. A generated song has no filename to preserve, so they resolve to none
   * rather than to a file called "song.mp4". */
  const _AI_LABEL_MARKER = /^(song|video|image|audio|file|media)$/i;
  function _artName(label, u){
    let n = String(label || '').trim();
    if(!n || _AI_LABEL_MARKER.test(n)) return '';
    /* A label is prose from a server response: strip anything that could make it a path or an
     * illegal Windows filename, and bound it, before it becomes a file on somebody's disk or a
     * row in their drive. Hyphens and spaces STAY — "Artist - Title" is the shape of nearly every
     * song yt-dlp hands back, and stripping them would rename all of them. */
    n = n.replace(/[\\/:*?"<>|\u0000-\u001f]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 120);
    // …and never lead with a dot: a leading-dot name is a HIDDEN file on unix, and a label of
    // "../../etc/passwd" arrives here as ".. .. etc passwd" once the slashes are gone.
    n = n.replace(/^[.\s]+/, '').trim();
    if(!n) return '';
    // Keep the extension the BYTES actually have. The drive picks its icon from it and a download
    // picks the app that opens it, and a title like "Song (Official Video)" only looks like it has
    // one. `srcExt` in the music library comes from here too.
    const ext = ((String(u).split(/[?#]/)[0].split('.').pop()) || '').toLowerCase();
    if(/^[a-z0-9]{1,5}$/.test(ext) && !new RegExp('\\.' + ext + '$', 'i').test(n)) n += '.' + ext;
    return n;
  }
  function _aiFileActions(u, kind, label){
    if(!/^\//.test(u)) return '';
    const nm=enc(_artName(label, u));
    const copy=`<button class="btn btn-cyan small ai-copyfile" data-url="${enc(u)}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-link"></use></svg>Copy link</button>`;
    const post=`<button class="btn btn-neon small ai-postfile" data-url="${enc(u)}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-send"></use></svg>Post</button>`;
    // data-kind: the save handler routes AUDIO to the music library, and this row is rendered from
    // persisted markdown that carries nothing else to tell a song from a screenshot.
    // data-name: …and nothing else to tell it what the song is CALLED (see _artName).
    const save=`<button class="btn btn-cyan small ai-savefile" data-url="${enc(u)}" data-kind="${enc(kind||'')}" data-name="${nm}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-cloud"></use></svg>Save to Files</button>`;
    const dl=`<button class="btn btn-cyan small ai-dlfile" data-url="${enc(u)}" data-name="${nm}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-download"></use></svg>Download</button>`;
    const nt=`<button class="btn btn-cyan small ai-notefile" data-url="${enc(u)}" data-name="${nm}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-note"></use></svg>Save to Notes</button>`;
    // 🎵 only where there IS an audio track. This row renders from the PERSISTED markdown, which
    // carries no payload fields — so the distinction rides in the label the server writes:
    // `!video[song]` for musicgeni/narrate, `!video[video]` for a silent videogeni clip.
    const mp3=(kind==='video' && /song|music|narrat/i.test(label||''))
      ? `<button class="btn btn-cyan small ai-mp3file" data-url="${enc(u)}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-music"></use></svg>Convert to MP3</button>` : '';
    const reply=_ai.replyTo?`<button class="btn btn-cyan small ai-replyfile" data-url="${enc(u)}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-reply"></use></svg>Send the Reply</button>`:'';
    // Keep working on a result instead of it being a dead end: an effect output was final here, so
    // refining one meant downloading the file and adding it back by hand. The builder takes VIDEO
    // layers as well as images, which is what makes this worth having for effects at all.
    // Not offered for audio — addMedia only seeds image/video layers.
    const mb=(kind==='audio')?'':`<button class="btn btn-cyan small ai-memefile" data-url="${enc(u)}" data-kind="${enc(kind||'image')}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-film"></use></svg>Meme Builder</button>`;
    return `<div class="fx-reply-row">${reply}${mp3}${post}${save}${nt}${mb}${dl}${copy}</div>`;
  }
  // Set an action button's LABEL without eating its sprite icon. These buttons show progress
  // ('saving…', '✓ copied') and then restore themselves, and every one of them did it with
  // `btn.textContent=…` — which replaces ALL children, so the first time a button reported progress it
  // dropped the <svg> and never got it back. Only the text node moves; the icon stays put.
  function _btnText(btn, text){
    if(!btn) return;
    const t=[...btn.childNodes].find(n=>n.nodeType===3 && n.nodeValue.trim()!=='');
    if(t) t.nodeValue=text; else btn.appendChild(document.createTextNode(text));
  }
  // …and what it said BEFORE, so a failed handler can put back the label it actually found. The
  // artifact button's label is the FILE NAME (`sandbox-workspace.tar.gz`), so restoring a hardcoded
  // 'Download' renames the button for the rest of the conversation.
  function _btnLabel(btn){
    if(!btn) return '';
    const t=[...btn.childNodes].find(n=>n.nodeType===3 && n.nodeValue.trim()!=='');
    return t ? t.nodeValue : '';
  }
  // Save an /api/files/ artifact to Blossom (the same re-upload Copy link does, minus the clipboard).
  async function saveFileToBlossom(u, btn, kind, name){
    if(btn){ btn.disabled=true; _btnText(btn,'saving…'); }
    try{
      /* An `ytdl` MP3 arrives here, not through the effect row — which is exactly why this goes
       * through the same _keepBytes as a generated song. Otherwise where a track ends up depends on
       * which command produced it, and half your music is in a folder you cannot play from.
       *
       * …and it arrives with a NAME, which is what the library titles the track with: without it
       * every downloaded song is called `enc_<sha256>` in a list you are meant to browse. */
      const r = await _keepBytes(await _artifactFile(u, name), kind);
      if(r.url) _ai.pubUrl = Object.assign(_ai.pubUrl || {}, { [u]: r.url });   // a later share reuses this upload
      _keptToast(r);
      if(btn){ _btnText(btn, r.library ? '✓ in Music' : '✓ saved'); btn.disabled=false; }
    }
    catch(e){ toast('save failed: '+((e&&e.message)||e)); if(btn){ btn.disabled=false; _btnText(btn,'Save to Files'); } }
  }
  // Download an artifact to the device. The URL is AUTHED, so an <a download> pointing at it would
  // 401 — fetch with credentials, then click an object URL of the bytes.
  /* Any generated file → a private note.
   *
   * ONE helper for both action rows. There are two rows because a result arrives either as an
   * /api/files/ artifact or as a base64 payload, and every button has had to be added to both — the
   * "no Meme Builder after geni" bug was exactly that, one row getting a button the other did not.
   *
   * The bytes are ENCRYPTED into the notebook, not linked: an /api/files/ artifact is temporary and
   * needs this session's cookie, so a note pointing at one is a broken image by tomorrow and on
   * every other device. A Notes attachment survives both, because it IS the note.
   */
  async function saveFileToNotes(file, title, btn){
    if(!window.PCNotes || !window.PCNotes.save){ toast('Notes is not loaded'); return; }
    if(btn){ btn.disabled=true; _btnText(btn,'saving…'); }
    try{
      const r=await window.PCNotes.save({ title: title || 'Saved from PosterChan AI',
                                          body: '', tags:['saved-ai'], files:[file] });
      toast(r.queued ? '📓 saved to Notes — will sync when you are back online' : '📓 saved to Notes');
      if(btn){ _btnText(btn,'✓ in Notes'); btn.disabled=false; }
    }catch(e){
      toast('could not save to Notes: '+((e&&e.message)||'error'));
      if(btn){ btn.disabled=false; _btnText(btn,'Save to Notes'); }
    }
  }
  // An artifact lives behind the session cookie, so it has to be fetched before it can be a note.
  async function notesFromFileUrl(u, btn, want){
    if(btn){ btn.disabled=true; _btnText(btn,'fetching…'); }
    try{
      const blob=await fetch(u, { credentials:'include' }).then(r=>{ if(!r.ok) throw new Error('fetch '+r.status); return r.blob(); });
      const ext=((u.split(/[?#]/)[0].split('.').pop())||'bin').toLowerCase();
      const name=want||('posterchan-'+Date.now()+'.'+ext);
      await saveFileToNotes(new File([blob], name, { type: blob.type || 'application/octet-stream' }),
                            'PosterChan AI — '+name, btn);
    }catch(e){
      toast('could not save to Notes: '+((e&&e.message)||e));
      if(btn){ btn.disabled=false; _btnText(btn,'Save to Notes'); }
    }
  }
  // A base64 payload is already here; no fetch, just bytes.
  async function notesFromEffectMedia(mid, btn){
    const m=_ai.fxMedia && _ai.fxMedia[mid];
    if(!m){ toast('that result is no longer in this conversation'); return; }
    try{
      const bin = m.b64 ? Uint8Array.from(atob(m.b64), c=>c.charCodeAt(0))
                        : new Uint8Array(await fetch(m.url, { credentials:'include' }).then(r=>r.arrayBuffer()));
      const name='posterchan-'+Date.now()+'.'+(m.ext||'bin');
      await saveFileToNotes(new File([bin], name, { type:m.mime||'application/octet-stream' }),
                            'PosterChan AI — '+name, btn);
    }catch(e){
      toast('could not save to Notes: '+((e&&e.message)||e));
      if(btn){ btn.disabled=false; _btnText(btn,'Save to Notes'); }
    }
  }
  async function downloadFileUrl(u, btn, name){
    const was=_btnLabel(btn);
    if(btn){ btn.disabled=true; _btnText(btn,'downloading…'); }
    try{
      const blob=await fetch(u, { credentials:'include' }).then(r=>{ if(!r.ok) throw new Error('fetch '+r.status); return r.blob(); });
      const ext=((u.split(/[?#]/)[0].split('.').pop())||'bin').toLowerCase();
      /* saveBlobAs, NEVER a bare <a download>. The APK's WebView IGNORES a programmatic download
       * (MainActivity registers no DownloadListener) and reports nothing back, so this claimed
       * "✓ downloaded" over a file that never left the page — measured on the agent's
       * `sandbox-workspace.tar.gz`, the one artifact a user has no other way to get at. On-device
       * the bytes go out through the OS share sheet instead, which is its own confirmation, so the
       * button says SHARED there rather than claiming a save the user has not made yet.
       *
       * The song's own name when the label carried one (see _artName) — a downloads folder full of
       * `posterchan-1786…mp3` is a folder you have to play to identify. */
      const how=await saveBlobAs(blob, name||('posterchan-'+Date.now()+'.'+ext));
      if(btn){ _btnText(btn, how==='shared'?'✓ shared':'✓ downloaded'); btn.disabled=false; }
    }catch(e){ toast('download failed: '+((e&&e.message)||e)); if(btn){ btn.disabled=false; _btnText(btn, was||'Download'); } }
  }
  // Branded MP4 artifact → MP3, via the same `extractaudio` command a user could type.
  async function mp3FromFileUrl(u, btn){
    if(btn){ btn.disabled=true; _btnText(btn,'converting…'); }
    try{
      const blob=await fetch(u, { credentials:'include' }).then(r=>{ if(!r.ok) throw new Error('fetch '+r.status); return r.blob(); });
      await aiAddFiles([new File([blob], 'song.mp4', { type:blob.type||'video/mp4' })]);
      const ta=$('#ai-input'); if(ta) ta.value='extractaudio';
      aiSend();
      if(btn){ _btnText(btn,'Convert to MP3'); btn.disabled=false; }
    }catch(e){ toast('convert failed: '+((e&&e.message)||e)); if(btn){ btn.disabled=false; _btnText(btn,'Convert to MP3'); } }
  }
  // Effect result → Meme Builder. The media has to be uploaded to Blossom FIRST: a layer `src` is
  // fetched SERVER-side at render time, and /api/files/… is behind get_current_user, so handing the
  // builder that path renders a layer the server is refused access to. _fileToPublicUrl is the same
  // upload the Copy link / Post buttons already use, and it caches, so using several of them on one
  // result uploads once.
  async function memeBuildFile(u, btn, kind){
    const label=btn?btn.textContent:'';
    if(btn){ btn.disabled=true; _btnText(btn,'uploading…'); }
    try{
      const pub=await _fileToPublicUrl(u);
      const isVid=(kind==='video')||/\.(mp4|webm|mov|m4v)(\?|#|$)/i.test(pub);
      const from=_ai.replyTo||null;
      switchView('meme');
      // Seed AFTER the view renders, or the builder's own first render wipes the new layer — the
      // same ordering memeBuildPost documents.
      setTimeout(()=>{
        const ok=window.PCMeme && window.PCMeme.addMedia && window.PCMeme.addMedia(pub, isVid?'video/mp4':'image/jpeg', from);
        toast(ok?'🎞️ added to the Meme Builder':'could not add that media');
      }, 60);
      if(btn){ btn.disabled=false; _btnText(btn,label||'Meme Builder'); }
    }catch(e){
      toast('failed: '+((e&&e.message)||e));
      if(btn){ btn.disabled=false; _btnText(btn,label||'Meme Builder'); }
    }
  }

  /* `folder` is only passed by "Save to Blossom" — the one action whose PURPOSE is keeping the file.
   * Copy link and Post upload as a side effect of sharing, and filing those would put a copy of every
   * link you ever pasted into the drive. The cache means a share after a save costs nothing either
   * way, and the file keeps the folder the save gave it. */
  async function _fileToPublicUrl(u, folder){
    _ai.pubUrl=_ai.pubUrl||{};
    if(_ai.pubUrl[u]) return _ai.pubUrl[u];
    const pub=await uploadBlob(await _artifactFile(u), folder ? {folder} : undefined);
    _ai.pubUrl[u]=pub; return pub;
  }
  async function copyFileUrl(u, btn){
    if(btn){ btn.disabled=true; _btnText(btn,'uploading…'); }
    // The label follows copyValue's ANSWER: a shell with no clipboard falls back to showing the
    // link, and a button that says "✓ copied" over that is a lie the user acts on.
    try{ const pub=await _fileToPublicUrl(u); const ok=await copyValue(pub, 'link copied', 'Link:');
         if(btn){ _btnText(btn, ok?'✓ copied':'Copy link'); btn.disabled=false; } }
    catch(e){ toast('failed: '+((e&&e.message)||e)); if(btn){ btn.disabled=false; _btnText(btn,'Copy link'); } }
  }
  async function replyFileUrl(u, btn){
    const target=_ai.fxReturn, owner=S.ME&&S.ME.pubkey, conversation=_ai.convId;
    const to=_ai.replyTo; if(!to){ toast('no post to reply to'); return; }
    if(target&&!_effectReturnValid(target)){toast('The account or conversation changed.');return;}
    if(btn){ btn.disabled=true; _btnText(btn,'posting…'); }
    try{ const pub=await _fileToPublicUrl(u);
      if(!S.ME||S.ME.pubkey!==owner||_ai.convId!==conversation||_ai.replyTo!==to||(target&&!_effectReturnValid(target)))throw new Error('The account or conversation changed.');
      const r=await publish(1, pub, eTags(to.id, to.pk));   // failure toast by publish()
      if(r && r.ok){ toast('✓ reply posted'); if(btn){ _btnText(btn,'✓ replied'); } if(target && S.VIEW==='ai')_returnFromEffect(target); }
      else if(btn){ btn.disabled=false; _btnText(btn,'Send the Reply'); } }
    catch(e){ toast('reply failed: '+((e&&e.message)||e)); if(btn){ btn.disabled=false; _btnText(btn,'Send the Reply'); } }
  }
  // Share generated media as a NEW Nostr post: re-upload the (authed/local) artifact to public Blossom,
  // then open the composer pre-filled with the public link (add a caption, then post).
  async function postFileUrl(u, btn){
    if(btn){ btn.disabled=true; _btnText(btn,'uploading…'); }
    try{ const pub=await _fileToPublicUrl(u); compose({text: pub}); }
    catch(e){ toast('failed: '+((e&&e.message)||e)); }
    finally{ if(btn){ btn.disabled=false; _btnText(btn,'Post'); } }
  }
  // Live base64 media → Meme Builder. memeBuildFile's counterpart for a PAYLOAD instead of an artifact
  // URL: same reason it can't hand over what it has, though — the builder fetches a layer `src`
  // SERVER-side at render time, so neither an authed /api/files/ path NOR a data: URI works. Upload to
  // public Blossom first, reusing the `m.url` the Post button caches so two buttons on one result
  // upload once.
  async function memeBuildEffect(mid, btn){
    const m=_ai.fxMedia[mid]; if(!m){ toast('nothing to add'); return; }
    const label=btn?btn.textContent:'';
    if(btn){ btn.disabled=true; _btnText(btn,'uploading…'); }
    try{
      if(!m.url){ const bin=Uint8Array.from(atob(m.b64), c=>c.charCodeAt(0)); m.url=await uploadBlob(new File([bin], 'media.'+m.ext, { type:m.mime })); }
      const isVid=/^video\//i.test(m.mime||'');
      const from=_ai.replyTo||null;
      const url=m.url;
      switchView('meme');
      // Seed AFTER the view renders, or the builder's own first render wipes the new layer — the same
      // ordering memeBuildFile/memeBuildPost document.
      setTimeout(()=>{
        const ok=window.PCMeme && window.PCMeme.addMedia && window.PCMeme.addMedia(url, isVid?'video/mp4':'image/jpeg', from);
        toast(ok?'🎞️ added to the Meme Builder':'could not add that media');
      }, 60);
    }catch(e){ toast('failed: '+((e&&e.message)||e)); }
    finally{ if(btn){ btn.disabled=false; _btnText(btn,label||'Meme Builder'); } }
  }
  async function postEffectMedia(mid, btn){
    const m=_ai.fxMedia[mid]; if(!m){ toast('nothing to post'); return; }
    if(btn){ btn.disabled=true; _btnText(btn,'uploading…'); }
    try{
      if(!m.url){ const bin=Uint8Array.from(atob(m.b64), c=>c.charCodeAt(0)); m.url=await uploadBlob(new File([bin], 'media.'+m.ext, { type:m.mime })); }
      compose({text: m.url});
    }catch(e){ toast('failed: '+((e&&e.message)||e)); }
    finally{ if(btn){ btn.disabled=false; _btnText(btn,'Post'); } }
  }
  function _fxReplyBtn(b64, mime, ext, opts){
    if(!b64) return '';
    const o=opts||{};
    const mid='fx'+Date.now().toString(36)+Math.floor(Math.random()*1e4).toString(36);
    _ai.fxMedia[mid]={ b64, mime, ext, hasAudio: !!o.hasAudio };
    // 🎵 Only where there IS an audio track to pull: a song / narration wrapped in the branded MP4.
    // videogeni output is silent, so offering it there would just fail — the server marks the ones
    // that carry audio (has_audio) rather than the client guessing from the mp4 container.
    const mp3=o.hasAudio?`<button class="btn btn-cyan small ai-mp3-fx" data-mid="${mid}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-music"></use></svg>Convert to MP3</button>`:'';
    const dl=`<button class="btn btn-cyan small ai-dl-fx" data-mid="${mid}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-download"></use></svg>Download</button>`;
    const copy=`<button class="btn btn-cyan small ai-copy-fx" data-mid="${mid}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-link"></use></svg>Copy link</button>`;
    const post=`<button class="btn btn-neon small ai-post-fx" data-mid="${mid}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-send"></use></svg>Post</button>`;
    const save=`<button class="btn btn-cyan small ai-save-fx" data-mid="${mid}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-cloud"></use></svg>Save to Files</button>`;
    const nt=`<button class="btn btn-cyan small ai-note-fx" data-mid="${mid}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-note"></use></svg>Save to Notes</button>`;
    // Same "keep working on the result" button the ARTIFACT row (_aiFileActions) has carried all along.
    // It was missing here, which is the whole of "no Meme Builder after geni": a generated image arrives
    // as a base64 PAYLOAD and never becomes an /api/files/ artifact, so it only ever renders this row.
    // Not for audio — addMedia only seeds image/video layers.
    const mb=/^audio\//i.test(mime||'')?'':`<button class="btn btn-cyan small ai-meme-fx" data-mid="${mid}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-film"></use></svg>Meme Builder</button>`;
    const reply=_ai.replyTo?`<button class="btn btn-cyan small ai-reply-fx" data-mid="${mid}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-reply"></use></svg>Send the Reply</button>`:'';
    return `<div class="fx-reply-row">${reply}${mp3}${post}${save}${nt}${mb}${dl}${copy}</div>`;
  }
  // ⬇ Save the bytes to the device. Chat media is base64 in the message, so build a Blob and hand it
  // to saveBlobAs — NOT an <a href="data:…">, which Chrome blocks past a few MB (a 3-minute song or a
  // 720p clip is exactly that size), and not a bare <a download> either: in the APK that is ignored
  // outright, which is the same silent "✓ downloaded" over nothing that downloadFileUrl had.
  async function downloadEffectMedia(mid, btn){
    const m=_ai.fxMedia[mid]; if(!m){ toast('nothing to download'); return; }
    const was=_btnLabel(btn);
    if(btn){ btn.disabled=true; _btnText(btn,'downloading…'); }
    try{
      const bin=Uint8Array.from(atob(m.b64), c=>c.charCodeAt(0));
      const how=await saveBlobAs(new Blob([bin], { type:m.mime }), 'posterchan-'+Date.now()+'.'+m.ext);
      if(btn){ _btnText(btn, how==='shared'?'✓ shared':'✓ downloaded'); btn.disabled=false; }
    }catch(e){ toast('download failed: '+((e&&e.message)||e)); if(btn){ btn.disabled=false; _btnText(btn, was||'Download'); } }
  }
  // 🎵 Pull the song out of the branded MP4 as an MP3. Runs the SAME `extractaudio` command a user
  // could type with the video attached (chat.py handles that case explicitly), so there is ONE
  // extraction implementation; the result comes back as a normal generated_audio message, which now
  // carries its own ⬇ Download button.
  async function convertEffectToMp3(mid, btn){
    const m=_ai.fxMedia[mid]; if(!m){ toast('nothing to convert'); return; }
    if(btn){ btn.disabled=true; _btnText(btn,'converting…'); }
    try{
      const bin=Uint8Array.from(atob(m.b64), c=>c.charCodeAt(0));
      await aiAddFiles([new File([bin], 'song.'+m.ext, { type:m.mime })]);
      const ta=$('#ai-input'); if(ta) ta.value='extractaudio';
      aiSend();
      if(btn){ _btnText(btn,'Convert to MP3'); btn.disabled=false; }
    }catch(e){
      toast('convert failed: '+((e&&e.message)||e));
      if(btn){ btn.disabled=false; _btnText(btn,'Convert to MP3'); }
    }
  }
  // 💾 Keep a generated image/video. Chat media is a base64 blob in the message — it lives only in
  // that conversation and is gone if you clear it, so this puts the bytes on your Blossom drive
  // where Files can see them. Same uploadBlob every other upload uses, so it honours the user's own
  // media server, the built-in-vs-nostr.build routing and the BUD-01 batch auth.
  /* Does this result belong in the MUSIC LIBRARY rather than the drive?
   *
   * A song from musicgeni normally arrives as the branded MP4, not as audio/* — `has_audio` is the
   * flag the server already sets to distinguish it from a silent videogeni clip, and it is the only
   * honest signal available here. Narration lands in the library too; that is a fair place for it.
   */
  function _isMusicMedia(m){
    return !!m && (/^audio\//i.test(m.mime||'') || (m.hasAudio && /^video\//i.test(m.mime||'')));
  }
  async function saveEffectToBlossom(mid, btn){
    const m=_ai.fxMedia[mid]; if(!m){ toast('nothing to save'); return; }
    // Already uploaded (Copy link / Post got there first) → don't spend a second upload or signature.
    if(m.url || m.inLibrary){
      // Uploaded already by Copy link or Post — those share, they do not file, so this still has to
      // put it in a folder. Otherwise "Post, then Save" leaves the file in the drive unfiled while
      // "Save, then Post" files it, for no reason the user could ever work out.
      if(m.url) _fileUnder(m.url, { name:'generated.'+m.ext, type:m.mime }, 'Posts');
      toast(m.inLibrary?'already in your Music library':'already saved to Blossom');
      if(btn) _btnText(btn,'✓ saved'); return; }
    if(btn){ btn.disabled=true; _btnText(btn,'saving…'); }
    try{
      const bin=Uint8Array.from(atob(m.b64), c=>c.charCodeAt(0));
      // Where it goes is _keepBytes's decision, shared with the artifact row. All this path knows is
      // whether the result is music, which for a branded MP4 only the server's has_audio can say.
      const music=_isMusicMedia(m);
      const stamp=new Date().toISOString().slice(0,16).replace('T',' ');
      const name=music ? ('Generated '+stamp+'.'+m.ext) : ('generated.'+m.ext);
      const r=await _keepBytes(new File([bin], name, { type:m.mime }), music ? 'audio' : '');
      if(r.library) m.inLibrary=true; else m.url=r.url;
      _keptToast(r);
      if(btn){ _btnText(btn, r.library ? '✓ in Music' : '✓ saved'); btn.disabled=false; }
    }catch(e){
      toast('save failed: '+((e&&e.message)||e));
      if(btn){ btn.disabled=false; _btnText(btn,'Save to Files'); }
    }
  }
  // Upload generated media to Blossom and copy its URL — paste the link into any reply yourself.
  async function copyEffectUrl(mid, btn){
    const m=_ai.fxMedia[mid]; if(!m){ toast('nothing to copy'); return; }
    if(btn){ btn.disabled=true; _btnText(btn,'uploading…'); }
    try{
      if(!m.url){ const bin=Uint8Array.from(atob(m.b64), c=>c.charCodeAt(0)); m.url=await uploadBlob(new File([bin], 'effect.'+m.ext, { type:m.mime })); }
      const ok=await copyValue(m.url, 'link copied', 'Link:');
      if(btn){ _btnText(btn, ok?'✓ copied':'Copy link'); btn.disabled=false; }
    }catch(e){ toast('upload failed: '+((e&&e.message)||e)); if(btn){ btn.disabled=false; _btnText(btn,'Copy link'); } }
  }
  // --- Interactive multiple-choice flashcards (study quiz) ---------------------------------------
  // Self-contained port of the old web UI deck: state lives in _ai.decks[id]; taps re-render via the
  // #ai-msgs click delegation. No KaTeX here (math cards show raw $…$ — rare from a web page).
  function _fcRender(id){
    const st=_ai.decks&&_ai.decks[id]; if(!st) return '';
    const total=st.cards.length, card=st.cards[st.idx]||{}, picked=st.answered[st.idx];
    const letters=['A','B','C','D','E','F'];
    const opts=(card.options||[]).map((o,i)=>{
      let cls='fc-opt';
      if(picked!=null){ if(i===card.correct) cls+=' fc-correct'; else if(i===picked) cls+=' fc-wrong'; }
      return `<button type="button" class="${cls}" data-fc="${id}" data-opt="${i}"${picked!=null?' disabled':''}>${enc((letters[i]||'•')+'. '+o)}</button>`;
    }).join('');
    const explain=(picked!=null && card.explanation)?`<div class="fc-explain"><strong>Why:</strong> ${enc(card.explanation)}</div>`:'';
    const done=st.answered.filter(a=>a!=null).length;
    return `<div class="fc-head"><span class="fc-title">🎴 ${enc(st.title||'Flashcards')}</span><span class="fc-progress">${st.idx+1}/${total}</span></div>`
      +`<div class="fc-card"><div class="fc-q">${enc(card.question||'')}</div><div class="fc-options">${opts}</div>${explain}</div>`
      +`<div class="fc-controls"><button type="button" class="fc-prev" data-fc="${id}"${st.idx===0?' disabled':''}><svg class="ic b-ic" aria-hidden="true"><use href="#i-arrow-left"></use></svg>Prev</button>`
      +`<span class="fc-score">Score ${st.score}/${done}</span>`
      +`<button type="button" class="fc-restart" data-fc="${id}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-refresh"></use></svg>Restart</button>`
      +`<button type="button" class="fc-next" data-fc="${id}"${st.idx>=total-1?' disabled':''}>Next ▶</button></div>`;
  }
  function _fcRedraw(id){ const el=document.getElementById(id); if(el) el.innerHTML=_fcRender(id); aiScroll(); }
  function aiRenderResponse(d){
    const head = d.content ? aiFormat(d.content) : '';
    if(d.type==='flashcards' && Array.isArray(d.cards) && d.cards.length){
      _ai.decks=_ai.decks||{};
      const id='fc'+Date.now().toString(36)+Math.floor(Math.random()*1e4).toString(36);
      _ai.decks[id]={ cards:d.cards, idx:0, answered:new Array(d.cards.length).fill(null), score:0, title:d.title };
      return `<div class="flashcard-deck" id="${id}">${_fcRender(id)}</div>`;
    }
    if(d.type==='bill'){
      // Confirm-before-write, as a tap — but the write is now SPLIT. The budget doc is encrypted to
      // this user's key, so only we can file the bill (PCBudget.addParsed); the reminder is server
      // data, so `bill add` still does that half. One tap, both halves — see the click handler.
      return head
        + `<div class="ai-budget-btns"><button class="ai-billadd" data-vendor="${enc(d.vendor||'')}" data-amount="${enc(String(d.amount||0))}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-check"></use></svg>Add to budget</button>`
        + `<button class="ai-billno"><svg class="ic b-ic" aria-hidden="true"><use href="#i-close"></use></svg>Cancel</button></div>`;
    }
    // `mime` is sent since the server now compresses generated images to JPEG; older servers (and the
    // APK meeting one) send no mime, so fall back to the PNG this always used to be.
    if(d.type==='generated_image' && d.image){ const im=d.mime||'image/png'; const iext=im==='image/jpeg'?'jpg':'png';
      return head+`<div class="ai-media"><img src="data:${im};base64,${d.image}" alt="generated"></div>`+_fxReplyBtn(d.image,im,iext); }
    if(d.type==='generated_video' && d.video) return head+`<div class="ai-media"><video controls src="data:video/mp4;base64,${d.video}"></video></div>`+_fxReplyBtn(d.video,'video/mp4','mp4',{hasAudio:!!d.has_audio});
    // Generated audio had NO buttons at all — a song you could play once and then lose. Same row as
    // every other generated medium, so it can be downloaded, kept on Blossom, or posted.
    if(d.type==='generated_audio' && d.audio){ const fmt=(d.format||'mp3').toLowerCase(); const mime=({mp3:'audio/mpeg',wav:'audio/wav',flac:'audio/flac',opus:'audio/ogg',aac:'audio/aac'})[fmt]||'audio/mpeg';
      return head+`<div class="ai-media"><audio controls src="data:${mime};base64,${d.audio}"></audio></div>`+_fxReplyBtn(d.audio,mime,fmt); }
    if((d.type==='meme') && d.image) return head+`<div class="ai-media"><img src="data:image/png;base64,${d.image}" alt="meme"></div>`+_fxReplyBtn(d.image,'image/png','png');
    if(d.type==='mail_attachment' && d.data){ const mime=d.mime_type||'application/octet-stream';
      if(mime.startsWith('image/')) return head+`<div class="ai-media"><img src="data:${mime};base64,${d.data}"></div>`;
      return head+`<a class="ai-file" href="data:${mime};base64,${d.data}" download="${enc(d.filename||'attachment')}">📎 ${enc(d.filename||'attachment')}</a>`; }
    if(d.type==='images' && Array.isArray(d.images)){
      // _absUrl: the proxy route is RELATIVE (/api/proxy-image/…) and AUTHED — on the APK a relative /api URL
      // resolves to the localhost WebView origin (404 → empty box), so hit the real API host; and since a
      // cross-origin <img> can't carry the Bearer, onerror hands off to __blobFallback (authed fetch → blob).
      const items=d.images.slice(0,12).map(im=>{ const src=im.thumb_id?_absUrl('/api/proxy-image/'+im.thumb_id):(im.img_src||im.thumbnail_src||im.thumbnail||''); const full=im.img_src||src; return src?`<img loading="lazy" src="${enc(src)}" data-full="${enc(full)}" onerror="window.__blobFallback(this)">`:''; }).join('');
      return head+`<div class="ai-imggrid">${items}</div>`;
    }
    if(d.type==='search' && Array.isArray(d.results)){
      return head+'<div class="ai-search">'+d.results.map(r=>`<div class="ai-sr"><a href="${enc(r.url||'')}" target="_blank" rel="noopener">${enc(r.title||r.url||'')}</a><div class="muted small">${enc((r.content||'').slice(0,200))}</div></div>`).join('')+'</div>';
    }
    if(d.type==='saved_searches' && Array.isArray(d.saved_searches)){
      // Telegram-style: each pin = its description, then a Run + Delete button.
      return head+'<div class="ai-pins">'+d.saved_searches.map(p=>{
        const run=p.run||('search '+(p.query||''));
        return `<div class="ai-pin"><div class="ai-pin-q">📌 ${enc(p.query||run)}</div>`
          +`<div class="ai-pin-btns"><button class="ai-cmd" data-cmd="${enc(run)}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-play"></use></svg>Run</button>`
          +`<button class="ai-cmd ai-cmd-danger" data-cmd="pin delete ${enc(String(p.id))}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-trash"></use></svg>Delete</button></div></div>`;
      }).join('')+'</div>';
    }
    if(d.type==='reminders' && Array.isArray(d.reminders)){
      // Reminders used to share the `files` branch below, which renders any item WITHOUT a url as a
      // plain <span> — a reminder has no url, so every one came out as flat text. That's why the
      // "buttons" weren't buttons. Each reminder now shows its due time and a real Cancel button,
      // mirroring the pins layout (.ai-cmd carries the command to run on click).
      return `<p>⏰ <b>Your reminders</b> (${d.reminders.length})</p>`
        +'<div class="ai-pins">'+d.reminders.map(r=>
          `<div class="ai-pin"><div class="ai-pin-q">⏰ ${enc(r.text||'')}`
          +`<div class="muted small">${enc(r.human||'')}</div></div>`
          +`<div class="ai-pin-btns"><button class="ai-cmd ai-cmd-danger" `
          +`data-cmd="remind cancel ${enc(String(r.id))}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-trash"></use></svg>Cancel</button></div></div>`).join('')+'</div>';
    }
    if(d.type==='files' && Array.isArray(d.files)){
      const arr=d.files;
      return head+'<div class="ai-files">'+arr.map(f=>{ const u=f.url||f.path||''; const n=f.filename||f.name||f.query||f.text||u||'item'; return u?`<a class="ai-file" href="${enc(u)}" target="_blank" rel="noopener">📄 ${enc(n)}</a>`:`<span class="ai-file">${enc(n)}</span>`; }).join('')+'</div>';
    }
    return head || aiFormat(d.text||d.message||'');   // graceful fallback: never drop a payload
  }
  // Pull a file the user already has on their Blossom drive into the AI chat as a real attachment
  // (fetch the blob → File → aiAddFiles, which base64-encodes it) so the model receives the actual
  // bytes for OCR / read-text / effects — not just a link it can't open.
  async function aiAttachFromBlossom(url, type, ext){
    if(!url) return;
    try{
      toast('📥 fetching from Blossom…');
      const r=await fetch(url); if(!r.ok) throw new Error('http '+r.status);
      const blob=await r.blob();
      let name=(url.split('/').pop()||'file').split('?')[0];
      if(ext && !/\.[a-z0-9]+$/i.test(name)) name+='.'+ext;
      await aiAddFiles([new File([blob], name, {type: type||blob.type||'application/octet-stream'})]);
      toast('attached');
    }catch(e){ toast('couldn\'t attach: '+((e&&e.message)||e)); }
  }
  // Ceiling on ONE AI-chat attachment. The file travels base64 (4/3 the raw size) inside a JSON frame on
  // the chat WebSocket, and run.py caps a frame at 64 MB (ws_max_size) — so ~44 MB raw is the real limit.
  // Long before that, readAsDataURL itself throws: a 1.28 GB stream recording becomes a ~1.7 GB string,
  // past V8's ~512 MB maximum string length. That throw used to be swallowed by a bare `catch(_){}`, so
  // the file silently never attached and `compress` answered "attach an image, video or PDF" — which read
  // as "we don't compress video" when the bytes had simply never left the browser.
  const AI_MAX_ATTACH = 44*1024*1024;
  async function aiAddFiles(files){
    for(const f of files){
      const ext=(f.name.split('.').pop()||'').toLowerCase();
      const kind = /^image\//.test(f.type)?'image'
                 : (/^video\//.test(f.type) || /^(mp4|webm|mov|m4v|mkv|avi)$/.test(ext))?'video'   // was missing → mp4 fell through to 'doc' (got flashcards/read-text)
                 : (f.type==='application/pdf'||ext==='pdf')?'pdf'
                 : /^text\/|json|xml|csv|^$/.test(f.type)?'text' : 'doc';
      try{
        if(kind==='text'){ _ai.attach.push({kind, name:f.name, text:await f.text()}); }
        else {
          // AI chat sends the ORIGINAL image (NO mandatory compression) — translate/OCR/read-text need full
          // detail; JPEG artifacts from downscaling smear small text and break OCR. (Compression is for
          // SOCIAL uploads only.) Safety cap ONLY: a truly huge photo (>8MB) would make a multi-MB base64 WS
          // payload that stalls the send, so those get a GENTLE reduce (high quality 0.85, 6MB cap); anything
          // ≤8MB passes through untouched.
          let src = (kind==='image' && f.size > 8*1024*1024) ? await compressImage(f, {maxBytes: 6*1024*1024, minQ: 0.85}) : f;
          // An over-size VIDEO gets one chance to fit: shrink it on the node first. That's what `compress`
          // would have done to it anyway, so a clip that's merely chunky now works instead of being refused.
          if(kind==='video' && src.size > AI_MAX_ATTACH){
            toast('🗜 compressing '+f.name+'…');
            src = await compressVideo(src);
          }
          if(src.size > AI_MAX_ATTACH){
            toast(`${f.name} is too big to attach (${Math.round(src.size/1048576)} MB — ${Math.round(AI_MAX_ATTACH/1048576)} MB max)`);
            continue;
          }
          const b64=await new Promise((res,rej)=>{ const r=new FileReader(); r.onload=()=>res(String(r.result).split(',')[1]||''); r.onerror=rej; r.readAsDataURL(src); });
          _ai.attach.push({kind, name:f.name, ext, b64});
        }
      }catch(e){ toast(`couldn't attach ${f.name}: ${(e&&e.message)||e}`); }   // never swallow — a silent drop looks like a broken command
    }
    aiRenderAttach();
  }
  // What you can DO with an attached file (old web UI / Telegram media-action keyboard). Each is
  // [label, mode, command]: mode 'fx' opens the Effects picker; 'run' sends the command immediately
  // (one-shot, e.g. compress/ocr); 'fill' prefills so you complete an argument (clip/convert/meme).
  // Commands match the upload allowlist in chat.py (note: it's `ocr`, not "readtext").
  function _aiAttachActions(){
    const k=new Set(_ai.attach.map(a=>a.kind));
    if(k.has('image')) return [['🎬 Effects','fx','__fxguide'],['🪄 Remove BG','run','removebackground'],['⭕ Circle crop','run','circlecrop'],['🔤 Read text','run','ocr'],['🌐 Translate','fill','translate '],['🗜 Compress','run','compress'],['🔄 Convert','fill','convert '],['😂 Meme','fill','meme '],['🗣️ Talk','fill','talk '],['🧾 Bill','run','bill'],['⏰ Remind','run','remind']];   // Translate: OCRs + translates the text (prefills — add a language or Enter for English). Bill: reads vendor/total/due, then confirms before writing to the budget. Talk prefills: it needs the line to say.
    if(k.has('pdf')||k.has('doc')) return [['🎴 Flashcards','run','flashcards'],['🔤 Read text','run','ocr'],['🌐 Translate','fill','translate '],['🧾 Bill','run','bill'],['⏰ Remind','run','remind']];
    if(k.has('video')) return [['🗜 Compress','run','compress'],['✂️ Clip','fill','clip '],['🎵 Extract audio','run','extractaudio']];   // matches Telegram's video keyboard (Convert is image↔PDF only — useless for video)
    return [['🗜 Compress','run','compress'],['🔄 Convert','fill','convert ']];
  }
  function _aiMediaAction(mode, cmd){
    if(mode==='fx'){ showEffectGuide(); return; }
    const ta=$('#ai-input'); if(!ta) return;
    ta.value=cmd; ta.focus(); ta.dispatchEvent(new Event('input'));
    if(mode==='run') aiSend();   // one-shot — runs on the attached file now; 'fill' waits for the arg
  }
  // Old web-UI link actions: when the AI input holds a video URL (YouTube/TikTok/X/IG/Vimeo/Twitch/…)
  // and no file is attached, offer a Download (→ ytdl). Updates live as you type/paste.
  // Link-action bar — faithful port of the old web UI's updateLinkActionBar (Telegram parity): when
  // the input is a SINGLE bare URL/magnet, offer the right actions for that link type. Most run
  // immediately; ✂️ Clip prefills so you can edit the timecodes before sending.
  function aiUpdateLinkActions(){
    const bar=$('#ai-attachbar'); if(!bar || _ai.attach.length) return;
    const text=((($('#ai-input')||{}).value)||'').trim();
    const isMagnet=/^magnet:\?/i.test(text);
    const um=text.match(/^https?:\/\/\S+$/i);
    if(!isMagnet && !um){ if(bar.dataset.link){ bar.innerHTML=''; delete bar.dataset.link; } return; }
    const url=isMagnet?text:um[0];
    const isTorrent=isMagnet || /\.torrent(\?|$)/i.test(url);
    const isYT=/(?:youtube\.com\/|youtu\.be\/)/i.test(url);
    /* An X post, or a mirror of one. `ytdl` rewrites a mirror link to x.com server-side
     * (youtube_service._looks_like_nitter_host), so the buttons must appear for exactly the links
     * that command can actually take — the alias list is mirrored here because it did NOT used to
     * be, and a pasted xcancel.com link therefore showed the generic Summary/Screenshot row while
     * `ytdl` would have downloaded it perfectly well. */
    const isX=/\/\/[^/]*(?:x\.com|twitter\.com|nitter|xcancel\.com|twiiit\.com|lightbrd\.com)/i.test(url);
    let acts;   // [label, command, prefillOnly]
    if(isTorrent) acts=[['🧲 Add Torrent','torrents add '+url,0]];
    else if(isYT) acts=[['📋 Summary','yt '+url,0],['🎵 MP3','ytdl '+url,0],['🎬 Movie','ytdl video '+url,0],['✂️ Clip','ytdl video '+url+' clip 0:00 0:30',1],['📣 Post','post '+url,0]];
    else if(isX) acts=[['🎵 MP3','ytdl '+url,0],['🎬 Video','ytdl video '+url,0],['✂️ Clip','ytdl video '+url+' clip 0:00 0:30',1],['📣 Post','post '+url,0]];
    else acts=[['📋 Summary','Summarize this page: '+url,0],['📸 Screenshot','screenshot '+url,0],['🌐 Translate','Translate this page to English: '+url,1],['🎴 Flashcards','flashcards '+url,0],['📣 Post','post '+url,0]];   // Translate prefills (edit the target language, then Enter)
    bar.dataset.link='1';
    bar.innerHTML='<div class="fx-row" style="display:flex;flex-wrap:wrap;gap:6px">'+acts.map((a,i)=>`<button class="fx-mot fx-linkact" data-i="${i}">${enc(a[0])}</button>`).join('')+'</div>';
    $$('.fx-linkact',bar).forEach(b=>{ const a=acts[+b.dataset.i]; b.onclick=()=>{ const t=$('#ai-input'); if(!t) return;
      t.value=a[1]; t.focus(); t.dispatchEvent(new Event('input'));
      if(!a[2]){ aiSend(); if(bar){ bar.innerHTML=''; delete bar.dataset.link; } }   // prefillOnly (✂️ Clip) → let the user edit timecodes, then Enter
    }; });
  }
  function aiRenderAttach(){
    const bar=$('#ai-attachbar'); if(!bar) return;
    // clear stale chips first — else removing the LAST attachment leaves its chip in the DOM
    // (aiUpdateLinkActions only wipes the bar when it had link-actions), so the ✕ looks dead.
    if(!_ai.attach.length){ bar.innerHTML=''; delete bar.dataset.link; aiUpdateLinkActions(); return; }
    const chips=_ai.attach.map((a,i)=>`<span class="ai-chip">${enc(a.name)} <button data-i="${i}" class="ai-chip-x" aria-label="Remove attachment"><svg class="ic x-ic" aria-hidden="true"><use href="#i-close"></use></svg></button></span>`).join('');
    const acts=_aiAttachActions();
    const actions='<div class="fx-row" style="display:flex;flex-wrap:wrap;gap:6px;margin-top:8px">'+
      acts.map((a,i)=>`<button class="fx-mot fx-act" data-i="${i}">${enc(a[0])}</button>`).join('')+'</div>';
    bar.innerHTML='<div class="ai-chips" style="display:flex;flex-wrap:wrap;gap:6px">'+chips+'</div>'+actions;
    $$('.ai-chip-x',bar).forEach(b=> b.onclick=()=>{ _ai.attach.splice(+b.dataset.i,1); aiRenderAttach(); });
    $$('.fx-act',bar).forEach(b=>{ const a=acts[+b.dataset.i]; b.onclick=()=>_aiMediaAction(a[1], a[2]); });   // wire directly (attach bar is outside the #ai-msgs delegation)
  }
  function aiSend(){
    const ta=$('#ai-input'); if(!ta) return; const text=ta.value.trim();
    if(!text && !_ai.attach.length) return;
    // `help` used to answer with 109 commands as one 8,900-character wall of markdown, which reads as
    // intimidating rather than helpful. Open the searchable sheet instead — same shape as the Effects
    // studio. Only the bare word: "help me write X" is still a real question for the model.
    if(!_ai.attach.length && /^help$/i.test(text)){
      // The APK ships its own copy of this client and updates independently of the server, so a new
      // app can meet an older backend with no /client/commands. Only swallow `help` once we KNOW we
      // have a catalogue to show — otherwise let it through and the server answers as it always did.
      ta.value=''; ta.dispatchEvent(new Event('input'));
      openCommandSheet().then(ok=>{ if(!ok){ ta.value='help'; aiSend(); } });
      return;
    }
    const att=_ai.attach.slice(); _ai.attach=[]; aiRenderAttach();
    const labels=att.map(a=>`📎 ${enc(a.name)}`).join(' ');
    aiAddMessage('user', (text?enc(text):'') + (labels?`<div class="ai-userfiles">${labels}</div>`:''));
    const payload={ type:'message', content:text };
    const imgs=att.filter(a=>a.kind==='image').map(a=>({base64:a.b64, filename:a.name}));   if(imgs.length) payload.images=imgs;
    const pdfs=att.filter(a=>a.kind==='pdf').map(a=>({base64:a.b64, filename:a.name}));       if(pdfs.length) payload.pdfs=pdfs;
    const docs=att.filter(a=>a.kind==='doc').map(a=>({base64:a.b64, filename:a.name, type:a.ext})); if(docs.length) payload.documents=docs;
    const vids=att.filter(a=>a.kind==='video').map(a=>({base64:a.b64, filename:a.name}));      if(vids.length) payload.videos=vids;   // compress/clip/extractaudio operate on the video bytes
    const txts=att.filter(a=>a.kind==='text').map(a=>({content:a.text, filename:a.name}));     if(txts.length) payload.files=txts;
    const cid=_ai.convId;
    _ai.awaiting=true;   // a reply is now pending — if the WS drops before it lands, aiRecover() polls it back
    // Safety net for a socket that STALLS without ever firing onclose (a proxy silently stops forwarding,
    // or the result frame is dropped) — onclose-only recovery never fires there. If awaiting isn't
    // resolved within 30s, start the recovery poll anyway. Live stream chunks reset it (see aiHandle), so
    // a long but healthy streamed answer won't trip it; an effect (no streaming) just polls until it lands.
    clearTimeout(_ai.recoverWatch);
    _ai.recoverWatch=setTimeout(()=>{ if(_ai.awaiting && S.VIEW==='ai' && _ai.convId===cid) aiRecover(cid); }, 30000);
    aiWsSend(payload);   // sends now if open, else queues + (re)connects and flushes on open
    // Remember it for ↑ — skipping an immediate repeat, which is only ever noise to walk back through.
    if(text && _ai.hist[_ai.hist.length-1]!==text){ _ai.hist.push(text); if(_ai.hist.length>100) _ai.hist.shift(); }
    _ai.histIdx=-1; _ai.histDraft='';
    ta.value=''; ta.style.height='auto';
  }
  return {
    aiAddFiles, aiNewConversation, aiRecover, askWindowContext, ltTeardown, openGenStudio,
    openVoiceStudio, reminderAlert, renderAI, renderTranslate,
  };
};
