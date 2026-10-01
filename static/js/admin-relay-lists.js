// Admin → Relay: every LIST setting drawn like Identities / Blocked accounts (data-tab "relay").
//
// Origins, WoT seeds, GPU-sharing peers, blocked words, blocked bridge domains and the four relay URL
// lists were bare textareas. Each is now a searchable list with an Add box and a Remove per row; key
// lists show the owner's picture and name from this relay. Add/Remove go to the server
// (/api/admin/relay/list, app/services/relay_lists.py), which edits the value it holds NOW, applies it
// through the same path as Save and writes it through to the relay -- then the textarea (still there
// under "Edit as text", still what Save sends) is rewritten AND taken as the new baseline, so the next
// Save can neither put a removed entry back nor drop an added one.
(function () {
    'use strict';
    const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c =>
        ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

    // key -> [kind, noun, add-box placeholder]. Mirrors relay_lists.LISTS (a test pins the two).
    const LISTS = {
        nostr_relay_posterchan_origins: ['origin', 'origin', 'https://example.com'],
        nostr_relay_wot_seeds: ['pubkey', 'seed', 'npub1… or 64-char hex'],
        nostr_dvm_peers: ['peer', 'peer', 'npub1… wss://their-relay.example/relay'],
        nostr_relay_blocked_words: ['word', 'word or phrase', 'a word or phrase'],
        nostr_relay_blocked_relays: ['domain', 'domain', 'mostr.pub'],
        nostr_relay_nip05_relays: ['relay', 'relay', 'wss://relay.example'],
        nostr_relay_upstream_relays: ['relay', 'relay', 'wss://relay.example'],
        nostr_relay_private_relays: ['relay', 'relay', 'wss://your-other-node.example'],
    };

    // Pure, so they can be tested.
    function matches(row, q) {
        q = String(q || '').trim().toLowerCase();
        if (!q) return true;
        return [row.value, row.name, row.nip05, row.npub, row.pubkey, row.relay]
            .some(v => String(v || '').toLowerCase().includes(q));
    }
    function rowHtml(kind, r) {
        const bad = r.valid === false ? '<span class="ids-badge ids-no" title="The relay cannot use this entry">invalid</span>' : '';
        if (kind === 'pubkey' || kind === 'peer') {
            return `
            <div class="blk-row rl-row" data-value="${esc(r.value)}">
                ${r.picture ? `<img class="blk-pic" src="${esc(r.picture)}" alt="" loading="lazy" referrerpolicy="no-referrer">`
                            : '<span class="blk-pic blk-nopic"></span>'}
                <div class="blk-who">
                    <div class="blk-name">${esc(r.name || '(no profile on this relay)')}${bad}</div>
                    <div class="blk-id">${r.nip05 ? esc(r.nip05) + ' · ' : ''}<code>${esc(String(r.npub || r.value).slice(0, 20))}…</code>${
                        r.relay ? ' · ' + esc(r.relay) : ''}</div>
                </div>
                <button type="button" class="btn-secondary btn-small rl-remove" aria-label="Remove">Remove</button>
            </div>`;
        }
        return `
            <div class="blk-row rl-row" data-value="${esc(r.value)}">
                <div class="blk-who"><div class="blk-name"><code class="rl-val">${esc(r.value)}</code>${bad}</div></div>
                <button type="button" class="btn-secondary btn-small rl-remove" aria-label="Remove">Remove</button>
            </div>`;
    }

    const state = {};   // key -> {rows, loading, complete}

    // Wrap one textarea: list panel above, the textarea itself folded into "Edit as text".
    function mount(key) {
        const ta = document.getElementById(key);
        if (!ta || ta.dataset.rlMounted) return;
        ta.dataset.rlMounted = '1';
        const [kind, noun, ph] = LISTS[key];
        const panel = document.createElement('div');
        panel.className = 'blk-panel rl-panel';
        panel.dataset.key = key;
        // The add box has NO name: Save must never send it.
        panel.innerHTML = `
            <input type="search" class="rl-search" placeholder="Search…" autocomplete="off" aria-label="Search this list">
            <div class="blk-summary rl-summary">Open this tab to load the list.</div>
            <div class="blk-list rl-list"></div>
            <div class="rl-add">
                <input type="text" class="rl-add-input" placeholder="${esc(ph)}" autocomplete="off" spellcheck="false" aria-label="Add a ${esc(noun)}">
                <button type="button" class="btn-secondary btn-small rl-add-btn">Add</button>
            </div>
            <div class="rl-msg" role="status" aria-live="polite"></div>`;
        const det = document.createElement('details');
        det.className = 'blk-raw';
        det.innerHTML = `<summary>Edit as text (one ${esc(noun)} per line — applied on Save)</summary>`;
        ta.parentNode.insertBefore(panel, ta);
        ta.parentNode.insertBefore(det, ta);
        det.appendChild(ta);
        state[key] = { rows: [], loading: false, complete: true, kind };
    }

    function draw(key) {
        const panel = document.querySelector(`.rl-panel[data-key="${key}"]`), st = state[key];
        if (!panel || !st) return;
        const q = panel.querySelector('.rl-search').value || '';
        const shown = st.rows.filter(r => matches(r, q));
        const noun = LISTS[key][1];
        const sum = panel.querySelector('.rl-summary');
        const bad = st.rows.filter(r => r.valid === false).length;
        sum.textContent = st.rows.length
            ? (q ? `${shown.length} of ${st.rows.length} match` : `${st.rows.length} ${noun}${st.rows.length === 1 ? '' : 's'}`)
              + (bad ? ` · ${bad} invalid` : '') + (st.complete ? '' : ' (some profiles could not be read from the relay)')
            : 'The list is empty.';
        panel.querySelector('.rl-search').hidden = st.rows.length < 6;
        panel.querySelector('.rl-list').innerHTML = shown.slice(0, 300).map(r => rowHtml(st.kind, r)).join('')
            + (shown.length > 300 ? `<div class="blk-more">${shown.length - 300} more — search to narrow it down</div>` : '');
        panel.querySelector('.rl-list').hidden = !st.rows.length;
    }

    function msg(key, text, bad) {
        const el = document.querySelector(`.rl-panel[data-key="${key}"] .rl-msg`);
        if (el) { el.textContent = text || ''; el.classList.toggle('rl-err', !!bad); }
    }

    // The value the server now holds becomes the text box AND Save's baseline.
    function adopt(key, value) {
        const ta = document.getElementById(key);
        if (!ta || typeof value !== 'string') return;
        ta.value = value;
        if (typeof loadedValues !== 'undefined') loadedValues.set(key, ta.value);
    }

    /* A LOAD ASKED FOR WHILE ONE IS RUNNING RUNS AGAIN AFTER IT -- it used to be dropped. After a Remove the
     * list reloads; if a load was already in flight (the tab's own, or the previous click's), that reload
     * was skipped and the in-flight one answered with the list from BEFORE the removal. The row stayed on
     * screen although the server had removed it ("tried to remove ditto.pub": three 200s, row still there). */
    async function load(key) {
        const st = state[key];
        if (!st) return;
        if (st.loading) { st.again = true; return; }
        st.loading = true;
        const sum = document.querySelector(`.rl-panel[data-key="${key}"] .rl-summary`);
        if (sum && !st.rows.length) sum.textContent = 'Loading…';
        try {
            const r = await fetch('/api/admin/relay/list?key=' + encodeURIComponent(key));
            const j = await r.json().catch(() => ({}));
            if (r.status === 404 || r.status === 405) { legacy(key); return; }
            if (!r.ok) throw new Error(j.detail || ('HTTP ' + r.status));
            st.rows = j.items || [];
            st.complete = j.names_complete !== false;
            draw(key);
        } catch (e) {
            if (sum) sum.textContent = 'Could not load the list: ' + e.message;
        } finally {
            st.loading = false;
            if (st.again) { st.again = false; load(key); }
        }
    }
    /* A SERVER OLDER THAN THIS PAGE has no list endpoint (the page is served from a checkout that can
     * be ahead of the running backend -- server1 reloads templates on save, router.lan pulls /static
     * on its own). Then the list is not a list: fold it away and open the text box, which Save has
     * always sent. "Could not load the list: Not Found" over a closed box left nothing to edit. */
    function legacy(key) {
        const panel = document.querySelector(`.rl-panel[data-key="${key}"]`), ta = document.getElementById(key);
        if (panel) panel.hidden = true;
        const det = ta && ta.closest('details.blk-raw');
        if (det) { det.open = true; const sm = det.querySelector('summary'); if (sm) sm.textContent = 'One per line — applied on Save'; }
    }
    function loadAll() { Object.keys(LISTS).forEach(k => { mount(k); load(k); }); }

    async function edit(key, body, btn) {
        if (btn) btn.disabled = true;
        msg(key, 'Saving…');
        try {
            const r = await (window.csrfFetch || fetch)('/api/admin/relay/list', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(Object.assign({key}, body))});
            const j = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error(j.detail || ('HTTP ' + r.status));
            adopt(key, j.value);
            /* SAY IT SAVED. An Add or Remove is saved the moment it is clicked -- and it said nothing, so people
             * then pressed the page's Save button and were told "no changes to be saved", which reads as if
             * the entry had been lost ("i added ditto.pub ... clicked save, said no changes to be saved"). */
            const what = body.add ? `${body.add} added` : `${body.remove} removed`;
            msg(key, j.durable === false
                ? `✓ ${what} — applied; the relay has not confirmed it yet and it retries in the background.`
                : `✓ Saved — ${what}. No need to press Save.`);
            await load(key);
            return true;
        } catch (e) {
            msg(key, (body.add ? 'Could not add: ' : 'Could not remove: ') + e.message, true);
            return false;
        } finally { if (btn) btn.disabled = false; }
    }

    async function add(panel) {
        const key = panel.dataset.key, inp = panel.querySelector('.rl-add-input');
        const v = (inp.value || '').trim();
        if (!v) { inp.focus(); return; }
        if (await edit(key, {add: v}, panel.querySelector('.rl-add-btn'))) inp.value = '';
    }

    if (typeof document !== 'undefined') {
        document.addEventListener('click', e => {
            const t = e.target;
            if (!t.closest) return;
            const rm = t.closest('.rl-remove');
            if (rm) {
                const panel = rm.closest('.rl-panel'), row = rm.closest('.rl-row');
                if (panel && row) edit(panel.dataset.key, {remove: row.dataset.value}, rm);
                return;
            }
            const ad = t.closest('.rl-add-btn');
            if (ad) { add(ad.closest('.rl-panel')); return; }
            if (t.closest('[data-tab="relay"]')) loadAll();
        });
        // Enter in the Add box adds -- it must not submit the whole settings form.
        document.addEventListener('keydown', e => {
            if (e.key === 'Enter' && e.target && e.target.classList && e.target.classList.contains('rl-add-input')) {
                e.preventDefault();
                add(e.target.closest('.rl-panel'));
            }
        });
        document.addEventListener('input', e => {
            const s = e.target && e.target.classList && e.target.classList.contains('rl-search') && e.target.closest('.rl-panel');
            if (s) draw(s.dataset.key);
        });
        // A Save of the text boxes changes what the server holds: redraw from it.
        document.addEventListener('pc-admin-saved', ev => {
            const keys = (ev.detail && ev.detail.keys) || [];
            keys.filter(k => LISTS[k]).forEach(load);
        });
        const boot = () => {
            Object.keys(LISTS).forEach(mount);
            if (typeof location !== 'undefined' && location.hash === '#tab-relay') loadAll();
        };
        if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
    }
    if (typeof module !== 'undefined') module.exports = { LISTS, matches, rowHtml };
})();
