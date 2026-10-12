// LIST FIELDS that are not global settings -- Admin → Bots → Edit (rate-limit exempt accounts, Concord
// rooms, topics to rotate, trusted media hosts) -- drawn exactly like Admin → Relay's lists: ONE
// "Search or add…" box that filters the rows and, when the text is not already an entry, offers "+ Add"
// (Enter does the same), and a Remove per row; accounts shown with their picture and name.
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

    /* What an entry MEANS, for "is this already in the list" -- a mirror of relay_lists.same() (and of
     * admin-relay-lists.js's copy). The server stays the authority: it refuses a duplicate itself. */
    function sameKey(kind, s) {
        s = String(s || '');
        const e = (kind === 'word' || kind === 'topic') ? s.trim() : s.split(/\s+/).filter(Boolean).join(' ');
        if (kind === 'word' || kind === 'topic') return e.toLowerCase();
        if (kind === 'invite') return e;
        if (kind === 'host') return e.toLowerCase().replace(/\.+$/, '');
        if (kind === 'domain') return e.replace(/^[a-z]+:\/\//i, '').split('/')[0].replace(/^\.+|\.+$/g, '').toLowerCase();
        return e.toLowerCase().replace(/\/+$/, '');            // relay, origin, server, pubkey
    }
    // The row the typed text already IS, or null. An account matches by its npub or its hex as well.
    function exactRow(kind, rows, typed) {
        const t = String(typed || '').trim();
        if (!t) return null;
        const k = sameKey(kind, t);
        return (rows || []).find(r => sameKey(kind, r.value) === k
            || (kind === 'pubkey' && [r.npub, r.pubkey].some(v => v && String(v).toLowerCase() === k))) || null;
    }
    // What the "+ Add" action may SAY about the typed text: never a Concord room's key while it is hidden.
    function shownTyped(kind, v, revealed) {
        return kind === 'invite' && !revealed && v.includes('#') ? v.split('#')[0] + '#••••••' : v;
    }

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
        // The search-or-add box has NO id/name: the bot's Save must never read it. A room invite's box is
        // masked like the invite field itself (its `#` part is the room's key).
        panel.innerHTML = `
            <input type="${kind === 'invite' ? 'text' : 'search'}" class="lf-query list-query${kind === 'invite' ? ' bots-secret' : ''}"
                   placeholder="Search or add… e.g. ${esc(ph)}" autocomplete="off" spellcheck="false" autocapitalize="off"
                   enterkeyhint="done" aria-label="Search or add a ${esc(noun)}">
            <button type="button" class="btn-secondary btn-small lf-add-action list-add-action" hidden></button>
            <div class="lf-exists list-exists" hidden>Already in the list</div>
            <div class="blk-summary rl-summary lf-summary"></div>
            <div class="blk-list rl-list lf-list"></div>
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
        const box = panel.querySelector('.lf-query');
        box.classList.toggle('revealed', revealed);
        const q = box.value || '';
        const shown = st.rows.filter(r => matches(r, q));
        const bad = st.rows.filter(r => r.valid === false).length;
        panel.querySelector('.lf-summary').textContent = st.rows.length
            ? (q ? `${shown.length} of ${st.rows.length} match` : `${st.rows.length} ${noun}${st.rows.length === 1 ? '' : 's'}`)
              + (bad ? ` · ${bad} invalid` : '') + (st.complete ? '' : ' (some profiles could not be read from the relay)')
            : 'The list is empty.';
        const list = panel.querySelector('.lf-list');
        list.innerHTML = shown.slice(0, 300).map(r => rowHtml(kind, r, revealed)).join('')
            + (shown.length > 300 ? `<div class="blk-more">${shown.length - 300} more — search to narrow it down</div>` : '');
        list.hidden = !shown.length;
        if (st.flash) {
            const fl = st.flash;
            st.flash = null;
            flashRows(Array.from(list.querySelectorAll('.rl-row')).filter(r => fl.includes(r.dataset.value)));
        }
        offer(ta);
    }

    // The "+ Add" action and the "already in the list" note, for what is typed now.
    function offer(ta) {
        const panel = panelOf(ta), st = state.get(ta);
        if (!panel || !st) return;
        const kind = ta.dataset.listKind;
        const v = (panel.querySelector('.lf-query').value || '').trim();
        const act = panel.querySelector('.lf-add-action'), ex = panel.querySelector('.lf-exists');
        const hit = v ? exactRow(kind, st.rows, v) : null;
        act.hidden = !v || !!hit;
        act.textContent = v ? `+ Add "${shownTyped(kind, v, ta.classList.contains('revealed'))}"` : '';
        ex.hidden = !hit;
    }

    // Briefly mark rows (a new one, or the one an exact match points at) and bring them into view.
    function flashRows(rows) {
        rows.forEach(r => {
            r.classList.add('rl-new');
            setTimeout(() => r.classList.remove('rl-new'), 2200);
        });
        if (rows[0] && rows[0].scrollIntoView) rows[0].scrollIntoView({block: 'nearest'});
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
            if (st.flashFrom) {          // the row(s) an Add just created (an older answer has none)
                const fresh = st.rows.filter(r => !st.flashFrom.has(r.value)).map(r => r.value);
                if (fresh.length) { st.flash = fresh; st.flashFrom = null; }
            }
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

    async function edit(ta, body, btn, onDone) {
        if (btn) btn.disabled = true;
        try {
            const r = await post('apply', Object.assign({kind: ta.dataset.listKind, raw: ta.value || ''}, body));
            const j = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error(j.detail || ('HTTP ' + r.status));
            ta.value = j.value || '';
            ta.dispatchEvent(new Event('input', {bubbles: true}));   // the bot form sees it exactly as typing
            msg(ta, (body.add ? 'Added' : 'Removed') + ' — press Save to keep it.');
            if (onDone) onDone();
            await load(ta);
            return true;
        } catch (e) {
            msg(ta, (body.add ? 'Could not add: ' : 'Could not remove: ') + e.message, true);
            return false;
        } finally { if (btn) btn.disabled = false; }
    }

    /* Add what is typed in the one box. An exact match adds nothing (it points at the row instead). On
     * success the box is cleared, the WHOLE list is drawn again and the new row is highlighted; on a
     * refusal the text stays so it can be corrected. */
    async function add(panel) {
        const ta = panel && document.getElementById(panel.dataset.for), st = ta && state.get(ta);
        if (!st || panel.dataset.busy) return;
        const inp = panel.querySelector('.lf-query');
        const v = (inp.value || '').trim();
        if (!v) { inp.focus(); return; }
        const hit = exactRow(ta.dataset.listKind, st.rows, v);
        if (hit) {
            offer(ta);
            flashRows(Array.from(panel.querySelectorAll('.rl-row')).filter(r => r.dataset.value === hit.value));
            return;
        }
        const before = new Set(st.rows.map(r => r.value));
        panel.dataset.busy = '1';
        try {
            await edit(ta, {add: v}, panel.querySelector('.lf-add-action'), () => {
                inp.value = '';
                st.flashFrom = before;
            });
        } finally { delete panel.dataset.busy; }
    }

    function all() { return Array.from(document.querySelectorAll('textarea[data-list-kind]')); }

    // Called after a form is filled programmatically (the bot modal opening): `.value =` fires no event.
    function refresh(ta) {
        (ta ? [ta] : all()).forEach(t => {
            mount(t);
            msg(t, '');
            const q = panelOf(t) && panelOf(t).querySelector('.lf-query');
            if (q) q.value = '';            // another bot's form: nothing typed for the last one carries over
            load(t);
        });
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
            const ad = t.closest('.lf-add-action');
            if (ad) add(ad.closest('.lf-panel'));
        });
        document.addEventListener('keydown', e => {
            if (e.key === 'Enter' && e.target && e.target.classList && e.target.classList.contains('lf-query')) {
                e.preventDefault();
                add(e.target.closest('.lf-panel'));
            }
        });
        // Typing filters the rows and offers the Add (`search` is a search box's clear-×).
        const typed = e => {
            const p = e.target && e.target.classList && e.target.classList.contains('lf-query') && e.target.closest('.lf-panel');
            if (p) { const ta = document.getElementById(p.dataset.for); if (ta && state.get(ta)) draw(ta); }
        };
        document.addEventListener('input', typed);
        document.addEventListener('search', typed, true);
        const boot = () => all().forEach(mount);
        if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
        window.PCListFields = { refresh, redraw };
    }
    if (typeof module !== 'undefined') module.exports = { KINDS, matches, rowHtml, sameKey, exactRow, shownTyped };
})();
