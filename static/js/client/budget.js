/* Budget — bills, monthly summary and Plans, stored ONLY in an encrypted Nostr event.
 *
 * This replaces the external Budget Manager (a separate Flask app + per-user API key). The whole
 * feature now lives in the client and the whole dataset is ONE kind-30078 doc, `d=pcai:budget`,
 * NIP-44-encrypted to the user's OWN key. That's the deliberate difference from the rest of the
 * app's server-side docs (chats/drafts/uploads use a server-held storage key): nobody but this user
 * can read their finances — not the operator, not the relay, not a database dump. The trade is that
 * the server can't read them either, so the old Telegram/AI `budget`/`pay` commands are gone; the
 * `bill` photo-OCR flow now hands its parse to THIS module to write.
 *
 * The doc is a replaceable event, so every write is a read-modify-write of the whole document and
 * they MUST be serialized (see `chain`) — two concurrent saves would each publish from a stale copy
 * and the later one would silently drop the other's change.
 *
 * Domain rules are ported verbatim from the Flask app (~/finance/app.py) so the numbers don't move:
 *   settled(row)  = paid==='Y' OR hidden_month === this month
 *   income        = Σ cost where is_income
 *   bills due     = Σ |cost| over unsettled expenses  + Σ |total| over unsettled plan categories
 *   paid          = Σ |cost| over paid expenses       + Σ |total| over paid plan categories
 *   remaining     = income − paid − bills due
 *
 * A plan's line items are ALSO listed under Expenses (planItemRow), because a plan that only showed
 * up as a total on another tab read as "my plan is missing". Those rows are DERIVED and display-only:
 * the money is already in `due` via catTotal, so persisting them as bills would count every plan
 * twice. Paid state stays on the plan.
 * Monthly rollover (maybe_reset_month_for_user): on the first open of a new month, recurring bills
 * and every plan category go back to unpaid+visible, and PAID one-time bills are deleted. The manual
 * "Reset month" button does the same thing on demand (it just doesn't stamp lastReset).
 *
 * UI is built from the app's own classes (.btn/.input/.note/.muted) and never names a colour — it
 * reads var(--neon)/--green/--danger/--line etc., so all nine themes work with no per-theme code.
 */
(function(){
  'use strict';

  // Same bridge contract every sub-module uses (stats.js/news.js/meme.js): app.js is an IIFE, so its
  // helpers are NOT globals — take them off window.__PC once it exists.
  let PC=null, $, enc, toast, uiConfirm, modal, closeModal, publish;
  const Relay = () => window.Relay;

  let _booted = false;
  function boot(){
    if(_booted) return;
    PC = window.__PC;
    if(!PC) return setTimeout(boot, 50);   // app.js publishes the bridge at DOMContentLoaded
    _booted = true;
    ({ $, enc, toast, uiConfirm, modal, closeModal, publish } = PC);
    window.PCBudget = {
      // renderView() re-fires on background relay traffic, not just on navigation, so repainting
      // unconditionally would wipe whatever is half-typed in the add-bill row (and flash a spinner).
      // Paint only when our DOM isn't on screen — i.e. on real ENTRY to the view; every actual data
      // change already calls the internal render() itself. The DOM check doubles as the "did we
      // leave?" signal, since switchView() never calls unmount().
      render(){
        if(document.querySelector('.bg-wrap')) return;
        _doc = null;    // re-read on entry — another device may have written since we last looked
        render();
      },
      // Nothing to flush: the save chain owns in-flight writes. Just drop the cache.
      unmount(){ _doc=null; },
      // Entry point for app.js's `bill` OCR flow (AI chat). Two things this MUST do that the
      // in-view callers don't: load the doc first (the user may never have opened Budget, so _doc
      // would be null and the push would throw), and never repaint — #feed belongs to the AI
      // conversation at that moment, so rendering the budget into it would erase the chat.
      async addParsed(vendor, amount){
        await load();
        return addBill(String(vendor||'').trim(), Number(amount)||0, false, true);
      },
    };
  }

  const D_TAG = 'pcai:budget';
  const KIND  = 30078;

  const ME = () => PC.ME;
  const money = n => '$' + Math.abs(Number(n)||0).toLocaleString(undefined,{minimumFractionDigits:2, maximumFractionDigits:2});
  const thisMonth = () => { const d=new Date(); return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0'); };
  const monthLabel = () => new Date().toLocaleString(undefined,{month:'long', year:'numeric'});
  const uid = () => 'b'+Date.now().toString(36)+Math.random().toString(36).slice(2,6);
  const num = v => { const n=parseFloat(String(v==null?'':v).replace(/[$,\s]/g,'')); return isFinite(n)?n:0; };

  // ---- document ---------------------------------------------------------------------------------
  const BLANK = () => ({ v:1, lastReset:'', bills:[], cats:[], items:[] });
  let _doc = null;          // in-memory copy; null = not loaded yet
  let _tab = 'bills';       // bills | plans
  let _showHidden = false;
  let chain = Promise.resolve();

  // A first REQ fired at a still-warming socket can EOSE empty, which would look like "all my bills
  // vanished" and — worse — the next save would publish that empty doc over the real one. So retry,
  // and treat a hard read failure as fatal-for-writes rather than as an empty budget.
  let _loading = null;
  function load(){
    if(_doc) return Promise.resolve(_doc);
    // renderView() can re-enter while the first read is still in flight; share the one request
    // rather than firing a second REQ (and a second decrypt prompt on an external signer).
    if(!_loading) _loading = _load().finally(()=>{ _loading = null; });
    return _loading;
  }
  async function _load(){
    let ev=null, sawRelay=false;
    for(let a=0; a<3 && !ev; a++){
      if(a) await new Promise(r=>setTimeout(r, 450*a));
      try{
        const evs = await Relay().query([{ authors:[ME().pubkey], kinds:[KIND], '#d':[D_TAG], limit:1 }]);
        sawRelay = true;
        ev = (evs||[]).sort((x,y)=>y.created_at-x.created_at)[0] || null;
      }catch(_){}
    }
    if(!ev){
      if(!sawRelay) throw new Error('relay unreachable');   // never let a dead socket masquerade as an empty budget
      _doc = BLANK();
      return _doc;
    }
    let raw='';
    try{ raw = await PC.nip44dec(ME().pubkey, ev.content||''); }
    catch(e){ throw new Error('could not decrypt your budget with this key'); }
    let d; try{ d = JSON.parse(raw)||{}; }catch(_){ d = {}; }
    _doc = Object.assign(BLANK(), d);
    for(const k of ['bills','cats','items']) if(!Array.isArray(_doc[k])) _doc[k]=[];
    return _doc;
  }

  function save(){
    // Serialize: each publish waits for the previous one, so a rapid pay-then-delete can't publish
    // two events built from the same base copy. `chain` is deliberately kept in a RESOLVED state
    // (the .catch below) so one failure doesn't poison every later write — but the promise handed
    // BACK to the caller keeps the rejection, so "Add to budget" can tell success from failure
    // instead of reporting a save that never landed.
    const done = chain.catch(()=>{}).then(async ()=>{
      const ct = await PC.nip44enc(ME().pubkey, JSON.stringify(_doc));
      const r = await publish(KIND, ct, [['d', D_TAG]], {quiet:true});
      if(!(r && r.ok)) throw new Error('relay rejected the write');
    });
    chain = done.catch(()=>{});
    done.catch(()=>toast('couldn’t save — that change is NOT stored'));
    return done;
  }

  // Start the month over: every recurring bill and plan back to unpaid+visible, and PAID one-time
  // bills dropped — they were last month's expenses, not a bill this month owes again. Shared whole
  // by the automatic rollover and the manual "Reset month" button, which is the point: "fresh month"
  // has to mean one thing, or the button leaves stale one-time rows sitting in Paid (the $11.62
  // that showed up under an otherwise-empty Paid tile). Only `lastReset` differs — see resetMonth.
  const _paidOneTime = () => _doc.bills.filter(b=>!b.is_recurring && b.paid==='Y');
  function _reopen(){
    for(const b of _doc.bills) if(b.is_recurring){ b.hidden_month=''; b.paid='N'; }
    for(const c of _doc.cats){ c.hidden_month=''; c.paid='N'; }
    const drop = new Set(_paidOneTime().map(b=>b.id));
    if(drop.size) _doc.bills = _doc.bills.filter(b=>!drop.has(b.id));
  }

  // Port of maybe_reset_month_for_user — the AUTOMATIC one, so a new month starts fresh on its own
  // the first time you open Budget that month (the Flask app did it on first page visit).
  function rollover(){
    const m = thisMonth();
    if(_doc.lastReset === m) return false;
    _reopen();
    _doc.lastReset = m;
    return true;
  }

  // The MANUAL button — the same fresh start, minus the `lastReset` stamp, so pressing it mid-month
  // is a do-over rather than a month advance that would swallow this month's automatic reset.
  // It DELETES paid one-time bills, so the confirm says so and counts them.
  async function resetMonth(){
    const once = _paidOneTime().length;
    if(!await uiConfirm(`Start ${monthLabel()} over? Every recurring bill and plan goes back to unpaid`
       + (once ? `, and ${once} paid one-time bill${once>1?'s':''} will be deleted.` : '.'))) return;
    _reopen();
    save(); repaint(); toast('month reset');
  }

  // ---- derived numbers (ported from the Flask index/api_summary) --------------------------------
  const catTotal = id => _doc.items.filter(i=>i.cat===id).reduce((s,i)=>s+(Number(i.amount)||0), 0);
  const settled = row => row.paid==='Y' || row.hidden_month===thisMonth();

  function summary(){
    const income = _doc.bills.filter(b=>b.is_income).reduce((s,b)=>s+(Number(b.cost)||0), 0);
    // Plans live on their own tab but count toward Bills Due, so the Plans tab carries a badge for
    // the unpaid ones — otherwise the total silently disagrees with the bills you can see.
    const duePlanCats = _doc.cats.filter(c=>!settled(c));
    let due = _doc.bills.filter(b=>!b.is_income && !settled(b)).reduce((s,b)=>s+Math.abs(Number(b.cost)||0), 0);
    due += duePlanCats.reduce((s,c)=>s+Math.abs(catTotal(c.id)), 0);
    let paid = _doc.bills.filter(b=>!b.is_income && b.paid==='Y').reduce((s,b)=>s+Math.abs(Number(b.cost)||0), 0);
    paid += _doc.cats.filter(c=>c.paid==='Y').reduce((s,c)=>s+Math.abs(catTotal(c.id)), 0);
    return { income, due, paid, remaining: income - paid - due,
             dueCount: _doc.bills.filter(b=>!b.is_income && !settled(b)).length,
             duePlanCount: duePlanCats.filter(c=>catTotal(c.id)).length };
  }

  const visibleBills = () => _showHidden ? _doc.bills.slice() : _doc.bills.filter(b=>b.hidden_month!==thisMonth());
  const visibleCats  = () => _showHidden ? _doc.cats.slice()  : _doc.cats.filter(c=>c.hidden_month!==thisMonth());
  const bySort = (a,b) => (a.sort_order||0)-(b.sort_order||0);

  // ---- mutations --------------------------------------------------------------------------------
  function addBill(name, cost, isIncome, isRecurring){
    if(!name) return Promise.resolve(false);
    _doc.bills.push({ id:uid(), name, cost:Number(cost)||0, paid:'N', payment_method:'',
                      is_income:!!isIncome, is_recurring:isRecurring!==false,
                      sort_order:_doc.bills.length, hidden_month:'' });
    const p = save();
    repaint();   // no-op when we're not the visible view (the AI-chat `bill` path)
    return p;
  }
  // #feed is ONE container shared by every view. Painting into it from a non-Budget view would
  // wipe whatever is there (the AI conversation, the timeline), so every post-mutation repaint
  // goes through this guard rather than calling render() directly.
  function repaint(){ if(PC.VIEW==='budget') render(); }
  const billById = id => _doc.bills.find(b=>b.id===id);
  const catById  = id => _doc.cats.find(c=>c.id===id);

  function togglePaid(id){ const b=billById(id); if(!b) return; b.paid = b.paid==='Y'?'N':'Y'; save(); repaint(); }
  function toggleCatPaid(id){ const c=catById(id); if(!c) return; c.paid = c.paid==='Y'?'N':'Y'; save(); repaint(); }
  // "Hide" = skip it for THIS month only (the Flask app's hidden_month). It stops counting toward
  // Bills Due and comes back on the next rollover — distinct from delete.
  function hideRow(row){ row.hidden_month = row.hidden_month===thisMonth() ? '' : thisMonth(); save(); repaint(); }

  async function delBill(id){
    const b=billById(id); if(!b) return;
    if(!await uiConfirm(`Delete “${b.name}” permanently?`)) return;
    _doc.bills = _doc.bills.filter(x=>x.id!==id); save(); repaint();
  }
  async function delCat(id){
    const c=catById(id); if(!c) return;
    if(!await uiConfirm(`Delete the plan “${c.name}” and its items?`)) return;
    _doc.cats  = _doc.cats.filter(x=>x.id!==id);
    _doc.items = _doc.items.filter(i=>i.cat!==id);
    save(); repaint();
  }

  // ---- render -----------------------------------------------------------------------------------
  function tile(label, value, tone){
    return `<div class="bg-tile${tone?' '+tone:''}"><span class="bg-tl">${enc(label)}</span><b class="bg-tv">${enc(value)}</b></div>`;
  }

  function billRow(b){
    const hidden = b.hidden_month===thisMonth();
    const paid = b.paid==='Y';
    return `<div class="bg-row${paid?' done':''}${hidden?' hid':''}" data-bill="${b.id}">
      <button class="bg-check" data-act="paid" title="${paid?'mark unpaid':'mark paid'}" aria-label="${paid?'mark unpaid':'mark paid'}">${paid?'✅':'⬜'}</button>
      <span class="bg-name">${enc(b.name)}${hidden?' <i class="bg-flag">skipped</i>':''}${!b.is_recurring?' <i class="bg-flag">one-time</i>':''}</span>
      <span class="bg-amt ${b.is_income?'in':'out'}">${b.is_income?'+':'−'}${enc(money(b.cost))}</span>
      <button class="bg-more" data-act="menu" aria-label="more">☰</button>
    </div>`;
  }

  // A plan's line items, shown in the EXPENSES list as well as on their own plan card.
  //
  // DERIVED AND DISPLAY-ONLY. The summary already counts a plan through catTotal() in `due`, so these
  // rows must not be added to _doc.bills and must not get their own paid checkbox — either would
  // double-count the same money, which in a budgeting tool is worse than not showing it at all. Paid
  // state belongs to the PLAN (that is what `settled(cat)` reads), so the checkbox lives on the plan
  // card and these rows carry the plan's state instead.
  //
  // A settled plan's items are shown struck-through like any paid bill rather than hidden, so the
  // Expenses list stays a complete picture of the month.
  function planItemRow(i, c){
    const hidden = c.hidden_month===thisMonth();
    const paid = c.paid==='Y';
    return `<div class="bg-row bg-planitem${paid?' done':''}${hidden?' hid':''}" data-cat="${c.id}" data-item="${i.id}">
      <span class="bg-check bg-checkless" title="paid on the plan “${enc(c.name)}”" aria-hidden="true">${paid?'✅':'⬜'}</span>
      <span class="bg-name"><span class="bg-itxt">${enc(i.name)}</span><i class="bg-flag bg-planflag" data-act="gotoplan" title="open the plan “${enc(c.name)}”">${enc(c.name)}</i>${hidden?' <i class="bg-flag">skipped</i>':''}</span>
      <span class="bg-amt out">−${enc(money(i.amount))}</span>
      <button class="bg-more" data-act="itemmenu" aria-label="more">☰</button>
    </div>`;
  }

  // Every plan item, paired with its plan, ordered with the plans themselves.
  function planItemRows(){
    return visibleCats().sort(bySort).flatMap(c =>
      _doc.items.filter(i=>i.cat===c.id).sort(bySort).map(i=>planItemRow(i, c))).join('');
  }

  function planCard(c){
    const items = _doc.items.filter(i=>i.cat===c.id).sort(bySort);
    const hidden = c.hidden_month===thisMonth();
    return `<div class="bg-plan${c.paid==='Y'?' done':''}${hidden?' hid':''}" data-cat="${c.id}">
      <div class="bg-row bg-plan-hd">
        <button class="bg-check" data-act="catpaid" aria-label="toggle paid">${c.paid==='Y'?'✅':'⬜'}</button>
        <span class="bg-name">${enc(c.name)}${hidden?' <i class="bg-flag">skipped</i>':''}</span>
        <span class="bg-amt out">${enc(money(catTotal(c.id)))}</span>
        <button class="bg-more" data-act="catmenu" aria-label="more">☰</button>
      </div>
      <div class="bg-items">
        ${items.map(i=>`<div class="bg-item" data-item="${i.id}">
            <span class="bg-iname">${enc(i.name)}</span>
            <span class="bg-iamt">${enc(money(i.amount))}</span>
            <button class="bg-idel" data-act="delitem" aria-label="remove">✕</button>
          </div>`).join('') || '<div class="muted small bg-empty">No items yet.</div>'}
      </div>
      <button class="btn btn-ghost small bg-additem" data-act="additem">+ Add item</button>
    </div>`;
  }

  function render(){
    const feed = $('#feed'); if(!feed) return;
    if(!ME()){ feed.innerHTML = '<div class="bg-wrap"><div class="muted" style="padding:24px">Sign in to use Budget.</div></div>'; return; }
    if(!_doc){
      feed.innerHTML = '<div class="spinner"></div>';
      load().then(()=>{ if(rollover()) save(); if(PC.VIEW==='budget') render(); })
            .catch(e=>{ if(PC.VIEW!=='budget') return;
              feed.innerHTML = `<div class="bg-wrap"><div class="bg-err">⚠ ${enc((e&&e.message)||'could not load your budget')}<div class="muted small">Nothing was changed. Try again once the relay reconnects.</div><button class="btn btn-ghost small" id="bg-retry">Retry</button></div></div>`;
              const r=$('#bg-retry'); if(r) r.onclick=()=>{ _doc=null; render(); }; });
      return;
    }

    const s = summary();
    const bills = visibleBills().sort((a,b)=> (b.is_income?1:0)-(a.is_income?1:0) || bySort(a,b));
    const income = bills.filter(b=>b.is_income), out = bills.filter(b=>!b.is_income);

    feed.innerHTML = `<div class="bg-wrap">
      <div class="bg-head">
        <div class="bg-monthrow">
          <div class="bg-month">${enc(monthLabel())}</div>
          <button class="btn btn-ghost small" id="bg-reset" title="mark every recurring bill and plan unpaid again">↺ Reset month</button>
        </div>
        <div class="bg-tiles">
          ${tile('Income', money(s.income), 'in')}
          ${tile('Bills due', money(s.due), 'due')}
          ${tile('Paid', money(s.paid), '')}
          ${tile('Remaining', (s.remaining<0?'−':'')+money(s.remaining), s.remaining<0?'neg':'ok')}
        </div>
      </div>
      <div class="bg-tabs">
        <button class="bg-tab${_tab==='bills'?' on':''}" data-tab="bills">Bills${s.dueCount?` <i class="bg-n">${s.dueCount}</i>`:''}</button>
        <button class="bg-tab${_tab==='plans'?' on':''}" data-tab="plans">Plans${s.duePlanCount?` <i class="bg-n">${s.duePlanCount}</i>`:''}</button>
        <span class="spacer"></span>
        <button class="bg-tab ghost${_showHidden?' on':''}" id="bg-hid" title="show rows skipped this month">👁</button>
      </div>
      ${_tab==='bills' ? `
        <div class="bg-list">
          ${income.length?`<div class="bg-sec">Income</div>${income.map(billRow).join('')}`:''}
          <div class="bg-sec">Expenses</div>
          ${(out.map(billRow).join('') + planItemRows()) || '<div class="muted small bg-empty">No bills yet — add one below.</div>'}
        </div>
        <div class="bg-add">
          <input class="input" id="bg-n" placeholder="Bill name" autocomplete="off">
          <input class="input bg-amtin" id="bg-a" placeholder="0.00" inputmode="decimal" autocomplete="off">
          <label class="bg-chk"><input type="checkbox" id="bg-inc"> Income</label>
          <label class="bg-chk"><input type="checkbox" id="bg-once"> One-time</label>
          <button class="btn btn-cyan" id="bg-save">Add</button>
          <button class="btn btn-ghost bg-ai" id="bg-ai">✨ Add Bill with AI</button>
        </div>` : `
        <div class="bg-list">
          ${visibleCats().sort(bySort).map(planCard).join('') || '<div class="muted small bg-empty">No plans yet. A plan is a group of line items (a credit card, a trip) that totals into Bills Due.</div>'}
        </div>
        <div class="bg-add">
          <input class="input" id="bg-cn" placeholder="New plan name" autocomplete="off">
          <button class="btn btn-cyan" id="bg-csave">Add plan</button>
        </div>`}
      <div class="bg-foot muted small">🔒 Encrypted to your key and stored as a Nostr event — only you can read it.
        <button class="bg-link" id="bg-import">Import</button></div>
    </div>`;

    wire();
  }

  // ---- events ----------------------------------------------------------------------------------
  function wire(){
    const wrap = $('.bg-wrap'); if(!wrap) return;

    wrap.querySelectorAll('.bg-tab[data-tab]').forEach(b=> b.onclick=()=>{ _tab=b.dataset.tab; render(); });
    const hid = $('#bg-hid'); if(hid) hid.onclick=()=>{ _showHidden=!_showHidden; render(); };
    const rst = $('#bg-reset'); if(rst) rst.onclick=resetMonth;

    // One delegated handler for the whole list — rows are re-rendered on every change, so per-row
    // listeners would be rebound constantly for no gain.
    const list = wrap.querySelector('.bg-list');
    if(list) list.onclick = (e)=>{
      const btn = e.target.closest('[data-act]'); if(!btn) return;
      const act = btn.dataset.act;
      const brow = e.target.closest('[data-bill]'), crow = e.target.closest('[data-cat]');
      const bid = brow && brow.dataset.bill, cid = crow && crow.dataset.cat;
      if(act==='paid')    return togglePaid(bid);
      if(act==='menu')    return billMenu(bid);
      if(act==='catpaid') return toggleCatPaid(cid);
      if(act==='catmenu') return catMenu(cid);
      // The plan name on a derived Expenses row: jump to the plan that owns the item, which is the
      // only place its amount can actually be edited (these rows are display-only by design).
      if(act==='gotoplan'){ _tab='plans'; render(); return; }
      if(act==='itemmenu') return itemMenu(cid, e.target.closest('[data-item]').dataset.item);
      if(act==='additem') return itemForm(cid);
      if(act==='delitem'){ const iid=e.target.closest('[data-item]').dataset.item;
        _doc.items=_doc.items.filter(i=>i.id!==iid); save(); repaint(); return; }
    };

    const nameEl=$('#bg-n'), amtEl=$('#bg-a'), saveEl=$('#bg-save');
    if(saveEl){
      const add = ()=>{
        const n=(nameEl.value||'').trim(), a=num(amtEl.value);
        if(!n) return toast('give the bill a name');
        addBill(n, a, $('#bg-inc').checked, !$('#bg-once').checked);
      };
      saveEl.onclick = add;
      // Enter in either field submits — on a phone the Add button is below the keyboard.
      [nameEl, amtEl].forEach(el=> el && (el.onkeydown = e=>{ if(e.key==='Enter'){ e.preventDefault(); add(); } }));
    }
    const cn=$('#bg-cn'), cs=$('#bg-csave');
    if(cs){
      const addCat = ()=>{ const n=(cn.value||'').trim(); if(!n) return toast('give the plan a name');
        _doc.cats.push({ id:uid(), name:n, paid:'N', hidden_month:'', sort_order:_doc.cats.length });
        save(); repaint(); };
      cs.onclick = addCat;
      if(cn) cn.onkeydown = e=>{ if(e.key==='Enter'){ e.preventDefault(); addCat(); } };
    }
    const imp=$('#bg-import'); if(imp) imp.onclick = importDialog;
    const ai=$('#bg-ai'); if(ai) ai.onclick = aiBillDialog;
  }

  function billMenu(id){
    const b=billById(id); if(!b) return;
    const hidden = b.hidden_month===thisMonth();
    modal(`<h3>${enc(b.name)}</h3>
      <div class="bg-menu">
        <button class="btn" data-m="edit">✏ Edit name / amount</button>
        <button class="btn" data-m="rec">${b.is_recurring?'Make one-time':'Make recurring'}</button>
        <button class="btn" data-m="hide">${hidden?'Un-skip this month':'Skip this month'}</button>
        <button class="btn btn-red" data-m="del">🗑 Delete</button>
      </div>`, root=>{
      root.querySelectorAll('[data-m]').forEach(x=> x.onclick=()=>{
        const m=x.dataset.m; closeModal();
        if(m==='edit') return editForm(b);
        if(m==='rec'){ b.is_recurring=!b.is_recurring; save(); repaint(); return; }
        if(m==='hide') return hideRow(b);
        if(m==='del')  return delBill(id);
      });
    });
  }

  function catMenu(id){
    const c=catById(id); if(!c) return;
    const hidden = c.hidden_month===thisMonth();
    modal(`<h3>${enc(c.name)}</h3>
      <div class="bg-menu">
        <button class="btn" data-m="ren">✏ Rename</button>
        <button class="btn" data-m="hide">${hidden?'Un-skip this month':'Skip this month'}</button>
        <button class="btn btn-red" data-m="del">🗑 Delete plan</button>
      </div>`, root=>{
      root.querySelectorAll('[data-m]').forEach(x=> x.onclick=()=>{
        const m=x.dataset.m; closeModal();
        if(m==='ren') return renameForm(c);
        if(m==='hide') return hideRow(c);
        if(m==='del')  return delCat(id);
      });
    });
  }

  function editForm(b){
    modal(`<h3>Edit bill</h3>
      <label class="fld">Name<input class="input" id="bg-en" value="${enc(b.name)}"></label>
      <label class="fld">Amount<input class="input" id="bg-ea" inputmode="decimal" value="${enc(String(b.cost))}"></label>
      <label class="fld">Payment method <span class="muted small">(optional)</span><input class="input" id="bg-ep" value="${enc(b.payment_method||'')}"></label>
      <button class="btn btn-cyan full" id="bg-eok">Save</button>`, root=>{
      root.querySelector('#bg-eok').onclick=()=>{
        const n=(root.querySelector('#bg-en').value||'').trim(); if(!n) return toast('name can’t be empty');
        b.name=n; b.cost=num(root.querySelector('#bg-ea').value);
        b.payment_method=(root.querySelector('#bg-ep').value||'').trim();
        closeModal(); save(); repaint();
      };
    });
  }

  function renameForm(c){
    modal(`<h3>Rename plan</h3><label class="fld">Name<input class="input" id="bg-rn" value="${enc(c.name)}"></label>
      <button class="btn btn-cyan full" id="bg-rok">Save</button>`, root=>{
      root.querySelector('#bg-rok').onclick=()=>{
        const n=(root.querySelector('#bg-rn').value||'').trim(); if(!n) return toast('name can’t be empty');
        c.name=n; closeModal(); save(); repaint();
      };
    });
  }

  // Add, or EDIT when `item` is passed. Editing an item had no home at all before: the plan card
  // offers only ✕, so a typo'd name or a mis-keyed amount could only be deleted and re-added — and a
  // plan's amount is what feeds Bills Due, so that is the number most worth being able to correct.
  function itemForm(cid, item){
    const ed = !!item;
    modal(`<h3>${ed?'Edit item':'Add item'}</h3>
      <label class="fld">Name<input class="input" id="bg-in" value="${ed?enc(item.name):''}"></label>
      <label class="fld">Amount<input class="input" id="bg-ia" inputmode="decimal" placeholder="0.00" value="${ed?enc(item.amount):''}"></label>
      <button class="btn btn-cyan full" id="bg-iok">${ed?'Save':'Add'}</button>`, root=>{
      root.querySelector('#bg-iok').onclick=()=>{
        const n=(root.querySelector('#bg-in').value||'').trim(); if(!n) return toast('give the item a name');
        const a=num(root.querySelector('#bg-ia').value);
        if(ed){ item.name=n; item.amount=a; }
        else _doc.items.push({ id:uid(), cat:cid, name:n, amount:a,
                              sort_order:_doc.items.filter(i=>i.cat===cid).length });
        closeModal(); save(); repaint();
      };
    });
  }

  // The ☰ on a derived Expenses row. It must act on the ITEM: wired to catMenu it offered "Delete
  // plan" from a row showing a single line item, which is a destructive action on something the row
  // does not represent.
  function itemMenu(cid, iid){
    const c = catById(cid), i = _doc.items.find(x=>x.id===iid);
    if(!c || !i) return;
    modal(`<h3>${enc(i.name)}</h3>
      <div class="bg-menu">
        <button class="btn" data-m="edit">✏ Edit item</button>
        <button class="btn" data-m="plan">Open plan “${enc(c.name)}”</button>
        <button class="btn btn-red" data-m="del">🗑 Delete item</button>
      </div>`, root=>{
      root.querySelectorAll('[data-m]').forEach(x=> x.onclick=()=>{
        const m=x.dataset.m; closeModal();
        if(m==='edit') return itemForm(cid, i);
        if(m==='plan'){ _tab='plans'; render(); return; }
        if(m==='del'){ _doc.items=_doc.items.filter(y=>y.id!==iid); save(); repaint(); return; }
      });
    });
  }

  // ---- "Add Bill with AI" ------------------------------------------------------------------------
  // Photograph a bill → the server OCRs it and pulls out vendor / total / due date → you confirm →
  // it lands in the encrypted doc. The split is forced and correct: OCR and the model live on the
  // server (it's the same /api/budget/scan → CommandService._bill_command path the chat `bill`
  // command uses), while the WRITE happens here because only this client can encrypt to your key.
  //
  // The parse is always shown in EDITABLE fields rather than as a yes/no confirm. OCR on a phone
  // photo mis-reads decimal points more often than it mis-reads names, and a wrong amount is the one
  // error that silently corrupts your totals — so correcting it has to be possible without starting
  // over.
  function aiBillDialog(){
    modal(`<h3>✨ Add Bill with AI</h3>
      <div class="muted small">Photograph a bill (or pick an image/PDF) and I'll read the vendor, total and due date. You confirm before anything is saved.</div>
      <div class="bg-aipick">
        <button class="btn btn-ghost bg-aibtn" id="bg-cam">📷 Take a photo</button>
        <button class="btn btn-ghost bg-aibtn" id="bg-pick">🖼 Choose a file</button>
      </div>
      <!-- two inputs, not one: capture= opens the camera straight away on a phone, which is the wrong
           thing when you actually wanted the file you already saved. Desktop ignores capture and both
           behave as a normal picker. -->
      <input type="file" id="bg-cami" accept="image/*" capture="environment" hidden>
      <input type="file" id="bg-picki" accept="image/*,application/pdf" hidden>
      <div id="bg-aistat" class="bg-aistat"></div>`, root=>{
      const stat=root.querySelector('#bg-aistat');
      const cam=root.querySelector('#bg-cami'), pick=root.querySelector('#bg-picki');
      root.querySelector('#bg-cam').onclick=()=>cam.click();
      root.querySelector('#bg-pick').onclick=()=>pick.click();

      const go=async(f)=>{
        if(!f) return;
        stat.innerHTML='<span class="spinner"></span> reading the bill…';
        root.querySelectorAll('.bg-aibtn').forEach(b=>b.disabled=true);
        try{
          await PC.ensureAiSession();     // the scan needs an app session (OCR + the model run server-side)
          const fd=new FormData(); fd.append('file', f, f.name||'bill.jpg');
          const r=await PC.authFetch('/api/budget/scan',{method:'POST', body:fd});
          if(!r.ok) throw new Error(r.status===401||r.status===403 ? 'your account can’t use AI features' : 'the scan failed');
          const d=await r.json();
          // _bill_command hands back type:'text' with the REASON when it couldn't pin down the fields.
          // Show that rather than a generic failure — it usually says "try a sharper, straight-on photo".
          if(!d || d.type!=='bill'){ stat.innerHTML=`<span class="muted small">${enc((d&&d.content)||'Couldn’t read that bill.')}</span>`;
            root.querySelectorAll('.bg-aibtn').forEach(b=>b.disabled=false); return; }
          closeModal();
          confirmParsed(d);
        }catch(err){
          stat.innerHTML=`<span class="muted small">${enc((err&&err.message)||'that didn’t work')}</span>`;
          root.querySelectorAll('.bg-aibtn').forEach(b=>b.disabled=false);
        }
      };
      cam.onchange=()=>go(cam.files&&cam.files[0]);
      pick.onchange=()=>go(pick.files&&pick.files[0]);
    });
  }

  function confirmParsed(d){
    const amt = Math.abs(Number(d.amount)||0);
    modal(`<h3>📄 Bill read</h3>
      <div class="muted small">Check these before saving — OCR gets decimal points wrong more often than names.</div>
      <label class="fld">Name<input class="input" id="bg-an" value="${enc(String(d.vendor||''))}"></label>
      <label class="fld">Amount<input class="input" id="bg-aa" inputmode="decimal" value="${enc(amt.toFixed(2))}"></label>
      <label class="bg-chk" style="margin:4px 0 10px"><input type="checkbox" id="bg-arec" checked> Recurring (comes back every month)</label>
      ${d.due?`<div class="muted small">Due ${enc(String(d.due))} — a reminder will be set.</div>`:''}
      <button class="btn btn-cyan full" id="bg-aok">Add to budget</button>`, root=>{
      root.querySelector('#bg-aok').onclick=async()=>{
        const n=(root.querySelector('#bg-an').value||'').trim();
        if(!n) return toast('give the bill a name');
        const a=num(root.querySelector('#bg-aa').value);
        const rec=root.querySelector('#bg-arec').checked;
        closeModal();
        await load();
        // Expenses are stored NEGATIVE, matching every row that came over from the old app.
        await addBill(n, -Math.abs(a), false, rec);
        toast('✅ added to your budget');
      };
    });
  }

  // One-time migration from the old Budget Manager. It has to run HERE rather than as a server
  // script: only this browser holds the key the doc is encrypted to. `scripts/export_budget_db.py`
  // produces the JSON; this merges it in (append, never replace) so a double-import is visible as
  // duplicates rather than silently destroying what's already there.
  function importDialog(){
    modal(`<h3>Import from Budget Manager</h3>
      <div class="muted small">Pick the file from <code>scripts/export_budget_db.py</code> (or paste it below).
        Rows are ADDED to your current budget — they don't replace it.</div>
      <label class="fld">Backup file<input class="input" id="bg-file" type="file" accept=".json,application/json"></label>
      <textarea id="bg-json" rows="8" placeholder='{"bills":[…],"cats":[…],"items":[…]}'></textarea>
      <button class="btn btn-cyan full" id="bg-iok2">Import</button>`, root=>{
      // Reading the file into the SAME textarea keeps one import path — the button never has to care
      // where the JSON came from, and you can still eyeball it before committing.
      const fi=root.querySelector('#bg-file');
      if(fi) fi.onchange=()=>{ const f=fi.files && fi.files[0]; if(!f) return;
        const fr=new FileReader();
        fr.onload=()=>{ root.querySelector('#bg-json').value=String(fr.result||''); toast(`loaded ${f.name}`); };
        fr.onerror=()=>toast('couldn’t read that file');
        fr.readAsText(f); };
      root.querySelector('#bg-iok2').onclick=async()=>{
        let d; try{ d=JSON.parse(root.querySelector('#bg-json').value||''); }
        catch(_){ return toast('pick a file first, or paste valid JSON'); }
        const nb=Array.isArray(d.bills)?d.bills:[], nc=Array.isArray(d.cats)?d.cats:[], ni=Array.isArray(d.items)?d.items:[];
        if(!nb.length && !nc.length) return toast('nothing to import');
        closeModal();
        if(!await uiConfirm(`Import ${nb.length} bill(s) and ${nc.length} plan(s)?`)) return;
        // Re-key everything on the way in: the export's ids come from SQLite AUTOINCREMENT and would
        // collide with ids already in the doc (and with each other across two imports).
        const cmap = {};
        for(const c of nc){ const id=uid(); cmap[c.id]=id;
          _doc.cats.push({ id, name:String(c.name||'?'), paid:c.paid==='Y'?'Y':'N',
                           hidden_month:String(c.hidden_month||''), sort_order:_doc.cats.length }); }
        for(const i of ni){ const cat=cmap[i.cat]; if(!cat) continue;
          _doc.items.push({ id:uid(), cat, name:String(i.name||'?'), amount:num(i.amount),
                            sort_order:_doc.items.filter(x=>x.cat===cat).length }); }
        for(const b of nb){
          _doc.bills.push({ id:uid(), name:String(b.name||'?'), cost:num(b.cost), paid:b.paid==='Y'?'Y':'N',
                            payment_method:String(b.payment_method||''), is_income:!!b.is_income,
                            is_recurring:b.is_recurring!==false, sort_order:_doc.bills.length,
                            hidden_month:String(b.hidden_month||'') }); }
        await save(); repaint(); toast(`imported ${nb.length} bill(s)`);
      };
    });
  }

  document.addEventListener('DOMContentLoaded', boot);
  boot();
})();
