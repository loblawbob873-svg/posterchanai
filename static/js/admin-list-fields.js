// LIST FIELDS that are not global settings -- Admin → Bots → Edit (rate-limit exempt accounts, Concord
// rooms, topics to rotate, trusted media hosts) -- drawn exactly like Admin → Relay's lists: a searchable
// list, an Add box and a Remove per row, accounts shown with their picture and name.
//
// The difference from admin-relay-lists.js is WHERE the value lives. A relay list is a setting, so an
// Add/Remove there is saved the moment it is clicked. These belong to ONE BOT and are saved with the bot,
// so an edit here rewrites the textarea (still under "Edit as text", still what the bot's Save reads) and
// fires `input` on it, exactly as typing would. The server only answers questions: what are the rows of
// this text (/api/admin/list-field/rows), and what is this text with one entry added or removed
// (/api/admin/list-field/apply) -- by relay_lists' own rules, so the split matches what the bot reads.
//
// A Concord invite's `#` part is the room's key: a row shows only what precedes it unless the field has
// been revealed (`.revealed`, the same switch the textarea uses).
(function () {
    'use strict';
    const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c =>
        ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

    // kind -> [noun, add-box placeholder]
    const KINDS = {
        pubkey: ['account', 'npub1… or 64-char hex'],
        topic: ['topic', 'a topic'],
        invite: ['room', 'https://poster.place/c/naddr1…#…'],
        host: ['host', 'nas.lan or 192.168.0.85'],
        domain: ['domain', 'example.com'],
        relay: ['relay', 'wss://relay.example'],
        server: ['server', 'https://example.com'],
        word: ['word or phrase', 'a word or phrase'],
    };

    function matches(row, q) {
        q = String(q || '').trim().toLowerCase();
        if (!q) return true;
        return [row.shown || row.value, row.name, row.nip05, row.npub, row.pubkey]
            .some(v => String(v || '').toLowerCase().includes(q));
    }

    function rowHtml(kind, r, revealed) {
        const bad = r.valid === false ? '<span class="ids-badge ids-no" title="The bot cannot use this entry">invalid</span>' : '';
        if (kind === 'pubkey') {
            return `
            <div class="blk-row rl-row" data-value="${esc(r.value)}">
                ${r.picture ? `<img class="blk-pic" src="${esc(r.picture)}" alt="" loading="lazy" referrerpolicy="no-referrer">`
                            : '<span class="blk-pic blk-nopic"></span>'}
                <div class="blk-who">
                    <div class="blk-name">${esc(r.name || '(no profile on this relay)')}${bad}</div>
                    <div class="blk-id">${r.nip05 ? esc(r.nip05) + ' · ' : ''}<code>${esc(String(r.npub || r.value).slice(0, 20))}…</code></div>
                </div>
                <button type="button" class="btn-secondary btn-small lf-remove" aria-label="Remove">Remove</button>
            </div>`;
        }
        const text = kind === 'invite' && !revealed ? (r.shown || '••••••') : r.value;
        return `
            <div class="blk-row rl-row" data-value="${esc(r.value)}">
                <div class="blk-who"><div class="blk-name"><code class="rl-val">${esc(text)}</code>${bad}</div></div>
                <button type="button" class="btn-secondary btn-small lf-remove" aria-label="Remove">Remove</button>
            </div>`;
    }

    const state = new WeakMap();   // textarea -> {rows, seq, complete}

    function panelOf(ta) { return ta && ta.id ? document.querySelector(`.lf-panel[data-for="${ta.id}"]`) : null; }

    function mount(ta) {
        if (ta.dataset.lfMounted) return;
        ta.dataset.lfMounted = '1';
        const kind = ta.dataset.listKind, [noun, ph] = KINDS[kind] || ['entry', ''];
        const panel = document.createElement('div');
        panel.className = 'blk-panel rl-panel lf-panel';
        panel.dataset.for = ta.id;
        // The add box has NO id/name: the bot's Save must never read it.
        panel.innerHTML = `
            <input type="search" class="rl-search lf-search" placeholder="Search…" autocomplete="off" aria-label="Search this list">
            <div class="blk-summary rl-summary lf-summary"></div>
            <div class="blk-list rl-list lf-list"></div>
            <div class="rl-add">
                <input type="text" class="rl-add-input lf-add-input${kind === 'invite' ? ' bots-secret' : ''}" placeholder="${esc(ph)}"
                       autocomplete="off" spellcheck="false" aria-label="Add a ${esc(noun)}">
                <button type="button" class="btn-secondary btn-small lf-add-btn">Add</button>
            </div>
            <div class="rl-msg lf-msg" role="status" aria-live="polite"></div>`;
        const det = document.createElement('details');
        det.className = 'blk-raw lf-raw';
        det.innerHTML = `<summary>Edit as text (one ${esc(noun)} per line)</summary>`;
        ta.parentNode.insertBefore(panel, ta);
        ta.parentNode.insertBefore(det, ta);
        det.appendChild(ta);
        state.set(ta, {rows: [], seq: 0, complete: true});
        // Typing in the text box redraws the list from it.
        let t = 0;
        ta.addEventListener('input', () => { clearTimeout(t); t = setTimeout(() => load(ta), 250); });
    }

    function draw(ta) {
        const panel = panelOf(ta), st = state.get(ta);
        if (!panel || !st) return;
        const kind = ta.dataset.listKind, noun = (KINDS[kind] || ['entry'])[0];
        const revealed = ta.classList.contains('revealed');
        const addIn = panel.querySelector('.lf-add-input');
        if (addIn) addIn.classList.toggle('revealed', revealed);
        const q = panel.querySelector('.lf-search').value || '';
        const shown = st.rows.filter(r => matches(r, q));
        const bad = st.rows.filter(r => r.valid === false).length;
        panel.querySelector('.lf-summary').textContent = st.rows.length
            ? (q ? `${shown.length} of ${st.rows.length} match` : `${st.rows.length} ${noun}${st.rows.length === 1 ? '' : 's'}`)
              + (bad ? ` · ${bad} invalid` : '') + (st.complete ? '' : ' (some profiles could not be read from the relay)')
            : 'The list is empty.';
        panel.querySelector('.lf-search').hidden = st.rows.length < 6;
        const list = panel.querySelector('.lf-list');
        list.innerHTML = shown.slice(0, 300).map(r => rowHtml(kind, r, revealed)).join('')
            + (shown.length > 300 ? `<div class="blk-more">${shown.length - 300} more — search to narrow it down</div>` : '');
        list.hidden = !st.rows.length;
    }

    function msg(ta, text, bad) {
        const el = panelOf(ta) && panelOf(ta).querySelector('.lf-msg');
        if (el) { el.textContent = text || ''; el.classList.toggle('rl-err', !!bad); }
    }

    function post(path, body) {
        return (window.csrfFetch || fetch)('/api/admin/list-field/' + path, {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    }

    /* Rows for what the textarea holds NOW. Answers arrive out of order when a modal is reopened for
     * another bot mid-request, so only the newest one may draw. */
    async function load(ta) {
        const st = state.get(ta);
        if (!st) return;
        const seq = ++st.seq;
        const raw = ta.value || '';
        if (!raw.trim()) { st.rows = []; st.complete = true; draw(ta); return; }
        try {
            const r = await post('rows', {kind: ta.dataset.listKind, raw});
            const j = await r.json().catch(() => ({}));
            if (seq !== st.seq) return;
            if (r.status === 404 || r.status === 405) { legacy(ta); return; }
            if (!r.ok) throw new Error(j.detail || ('HTTP ' + r.status));
            st.rows = j.items || [];
            st.complete = j.names_complete !== false;
            draw(ta);
        } catch (e) {
            if (seq !== st.seq) return;
            const sum = panelOf(ta) && panelOf(ta).querySelector('.lf-summary');
            if (sum) sum.textContent = 'Could not read the list: ' + e.message + ' — the text below is still what Save uses.';
            const det = ta.closest('details.lf-raw');
            if (det) det.open = true;
        }
    }

    // A server older than this page: the list is not a list -- fold it away and open the text box.
    function legacy(ta) {
        const panel = panelOf(ta);
        if (panel) panel.hidden = true;
        const det = ta.closest('details.lf-raw');
        if (det) { det.open = true; const sm = det.querySelector('summary'); if (sm) sm.textContent = 'One per line'; }
    }

    async function edit(ta, body, btn) {
        if (btn) btn.disabled = true;
        try {
            const r = await post('apply', Object.assign({kind: ta.dataset.listKind, raw: ta.value || ''}, body));
            const j = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error(j.detail || ('HTTP ' + r.status));
            ta.value = j.value || '';
            ta.dispatchEvent(new Event('input', {bubbles: true}));   // the bot form sees it exactly as typing
            msg(ta, (body.add ? 'Added' : 'Removed') + ' — press Save to keep it.');
            await load(ta);
            return true;
        } catch (e) {
            msg(ta, (body.add ? 'Could not add: ' : 'Could not remove: ') + e.message, true);
            return false;
        } finally { if (btn) btn.disabled = false; }
    }

    async function add(panel) {
        const ta = document.getElementById(panel.dataset.for), inp = panel.querySelector('.lf-add-input');
        const v = (inp.value || '').trim();
        if (!ta) return;
        if (!v) { inp.focus(); return; }
        if (await edit(ta, {add: v}, panel.querySelector('.lf-add-btn'))) inp.value = '';
    }

    function all() { return Array.from(document.querySelectorAll('textarea[data-list-kind]')); }

    // Called after a form is filled programmatically (the bot modal opening): `.value =` fires no event.
    function refresh(ta) {
        (ta ? [ta] : all()).forEach(t => { mount(t); msg(t, ''); load(t); });
    }
    // Redraw without asking the server (a reveal/hide of a secret list).
    function redraw(ta) { (ta ? [ta] : all()).forEach(t => { if (state.get(t)) draw(t); }); }

    if (typeof document !== 'undefined') {
        document.addEventListener('click', e => {
            const t = e.target;
            if (!t.closest) return;
            const rm = t.closest('.lf-remove');
            if (rm) {
                const panel = rm.closest('.lf-panel'), row = rm.closest('.rl-row');
                const ta = panel && document.getElementById(panel.dataset.for);
                if (ta && row) edit(ta, {remove: row.dataset.value}, rm);
                return;
            }
            const ad = t.closest('.lf-add-btn');
            if (ad) add(ad.closest('.lf-panel'));
        });
        document.addEventListener('keydown', e => {
            if (e.key === 'Enter' && e.target && e.target.classList && e.target.classList.contains('lf-add-input')) {
                e.preventDefault();
                add(e.target.closest('.lf-panel'));
            }
        });
        document.addEventListener('input', e => {
            const p = e.target && e.target.classList && e.target.classList.contains('lf-search') && e.target.closest('.lf-panel');
            if (p) { const ta = document.getElementById(p.dataset.for); if (ta) draw(ta); }
        });
        const boot = () => all().forEach(mount);
        if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
        window.PCListFields = { refresh, redraw };
    }
    if (typeof module !== 'undefined') module.exports = { KINDS, matches, rowHtml };
})();
