// Admin → Relay, Blossom and Social: every LIST setting drawn like Identities / Blocked accounts.
//
// Origins, WoT seeds, GPU-sharing peers, blocked words, blocked bridge domains and the four relay URL
// lists were bare textareas. Each is now a searchable list with an Add box and a Remove per row; key
// lists show the owner's picture and name from this relay.
//
// ONE BOX PER LIST: "Search or add…". There used to be a Search box above the list AND an Add box below
// it, so adding meant typing the same text twice ("to add an item, I have to search and then enter it
// again below"). Typing now filters the rows; when the text is not already an entry a "+ Add" action
// appears under the box and Enter does the same. An exact match says "already in the list" and adds
// nothing. The box is always shown (it hid on short lists when it was only a search box) and, like the
// Add box before it, has NO id/name, so Save never reads it. Add/Remove go to the server
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
        nostr_relay_trusted_bridge_domains: ['domain', 'bridge domain', 'glowers.club'],
        nostr_relay_nip05_relays: ['relay', 'relay', 'wss://relay.example'],
        nostr_relay_upstream_relays: ['relay', 'relay', 'wss://relay.example'],
        nostr_relay_private_relays: ['relay', 'relay', 'wss://your-other-node.example'],
        // Admin → Blossom: the same list, Add box and Remove per row.
        blossom_whitelist: ['pubkey', 'person', 'npub1… or 64-char hex'],
        media_own_hosts: ['domain', 'host', 'media.example.com'],
        blossom_mirror_servers: ['server', 'server', 'https://backup.example.com/blossom'],
        // Admin → Social → Blocking: fediverse instances and single accounts.
        fedi_bridge_blocked_domains: ['fedi', 'instance or account', 'bad.example or someone@bad.example'],
    };

    // Pure, so they can be tested.
    /* What an entry MEANS, for "is this already in the list" -- a mirror of relay_lists.same(). The
     * server is still the authority (it refuses a duplicate with "already in the list"); this only
     * decides whether to OFFER the Add. */
    function sameKey(kind, s) {
        s = String(s || '');
        const e = (kind === 'word' || kind === 'topic') ? s.trim() : s.split(/\s+/).filter(Boolean).join(' ');
        if (kind === 'word' || kind === 'topic') return e.toLowerCase();
        if (kind === 'invite') return e;
        if (kind === 'host') return e.toLowerCase().replace(/\.+$/, '');
        if (kind === 'domain') return e.replace(/^[a-z]+:\/\//i, '').split('/')[0].replace(/^\.+|\.+$/g, '').toLowerCase();
        if (kind === 'fedi') {
            const f = e.toLowerCase().replace(/^[a-z]+:\/\//, '').replace(/\/+$/, '').replace(/^\*\./, '').replace(/^@/, '');
            const m = f.match(/^([^/]+)\/@([^/]+)$/);         // https://host/@user -> user@host
            return m ? m[2] + '@' + m[1] : f;
        }
        if (kind === 'peer') {
            const t = e.split(' ');
            return t[0].toLowerCase() + ' ' + (t[1] || '').toLowerCase().replace(/\/+$/, '');
        }
        return e.toLowerCase().replace(/\/+$/, '');            // relay, origin, server, pubkey
    }
    /* The row the typed text already IS, or null. A key matches by its npub or its hex as well. */
    function exactRow(kind, rows, typed) {
        const t = String(typed || '').trim();
        if (!t) return null;
        const k = sameKey(kind, t);
        return (rows || []).find(r => {
            if (sameKey(kind, r.value) === k) return true;
            if (kind === 'pubkey' || kind === 'peer') {
                const pk = kind === 'peer' ? k.split(' ')[0] : k;
                const relOk = kind !== 'peer' || sameKey('relay', r.relay || '') === (k.split(' ')[1] || '');
                return relOk && [r.npub, r.pubkey].some(v => v && String(v).toLowerCase() === pk);
            }
            return false;
        }) || null;
    }
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
        // A fediverse line is an INSTANCE (and its subdomains) or ONE ACCOUNT -- the same text box held both and
        // nothing on screen said which a line was.
        const what = kind === 'fedi' && r.type
            ? `<span class="ids-badge rl-type" title="${r.type === 'account' ? 'Blocks this one account' : 'Blocks this instance and its subdomains'}">${r.type}</span>` : '';
        return `
            <div class="blk-row rl-row" data-value="${esc(r.value)}">
                <div class="blk-who"><div class="blk-name"><code class="rl-val">${esc(r.value)}</code>${what}${bad}</div></div>
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
        // The search-or-add box has NO id/name: Save must never send it.
        panel.innerHTML = `
            <input type="search" class="rl-query list-query" placeholder="Search or add… e.g. ${esc(ph)}" autocomplete="off"
                   spellcheck="false" autocapitalize="off" enterkeyhint="done" aria-label="Search or add a ${esc(noun)}">
            <button type="button" class="btn-secondary btn-small rl-add-action list-add-action" hidden></button>
            <div class="rl-exists list-exists" hidden>Already in the list</div>
            <div class="blk-summary rl-summary">Open this tab to load the list.</div>
            <div class="blk-list rl-list"></div>
            <div class="rl-msg" role="status" aria-live="polite"></div>`;
        const det = document.createElement('details');
        det.className = 'blk-raw';
        det.innerHTML = `<summary>Edit as text (one ${esc(noun)} per line — applied on Save)</summary>`;
        ta.parentNode.insertBefore(panel, ta);
        ta.parentNode.insertBefore(det, ta);
        det.appendChild(ta);
        state[key] = { rows: [], loading: false, complete: true, kind };
    }

    function panelOf(key) { return document.querySelector(`.rl-panel[data-key="${key}"]`); }

    // The "+ Add" action and the "already in the list" note, for what is typed now.
    function offer(key) {
        const panel = panelOf(key), st = state[key];
        if (!panel || !st) return;
        const v = (panel.querySelector('.rl-query').value || '').trim();
        const act = panel.querySelector('.rl-add-action'), ex = panel.querySelector('.rl-exists');
        const hit = v ? exactRow(st.kind, st.rows, v) : null;
        act.hidden = !v || !!hit;
        act.textContent = v ? `+ Add "${v}"` : '';
        ex.hidden = !hit;
    }

    function draw(key) {
        const panel = panelOf(key), st = state[key];
        if (!panel || !st) return;
        const q = panel.querySelector('.rl-query').value || '';
        const shown = st.rows.filter(r => matches(r, q));
        const noun = LISTS[key][1];
        const sum = panel.querySelector('.rl-summary');
        const bad = st.rows.filter(r => r.valid === false).length;
        sum.textContent = st.rows.length
            ? (q ? `${shown.length} of ${st.rows.length} match` : `${st.rows.length} ${noun}${st.rows.length === 1 ? '' : 's'}`)
              + (bad ? ` · ${bad} invalid` : '') + (st.complete ? '' : ' (some profiles could not be read from the relay)')
            : 'The list is empty.';
        panel.querySelector('.rl-list').innerHTML = shown.slice(0, 300).map(r => rowHtml(st.kind, r)).join('')
            + (shown.length > 300 ? `<div class="blk-more">${shown.length - 300} more — search to narrow it down</div>` : '');
        panel.querySelector('.rl-list').hidden = !shown.length;
        if (st.flash) {
            const fl = st.flash;
            st.flash = null;
            flashRows(Array.from(panel.querySelectorAll('.rl-row')).filter(r => fl.includes(r.dataset.value)));
        }
        offer(key);
    }

    // Briefly mark rows (a new one, or the one an exact match points at) and bring them into view.
    function flashRows(rows) {
        rows.forEach(r => {
            r.classList.add('rl-new');
            setTimeout(() => r.classList.remove('rl-new'), 2200);
        });
        if (rows[0] && rows[0].scrollIntoView) rows[0].scrollIntoView({block: 'nearest'});
    }

    function msg(key, text, bad) {
        const el = panelOf(key) && panelOf(key).querySelector('.rl-msg');
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
            if (st.flashFrom) {          // the row(s) an Add just created (a read begun before it has none)
                const fresh = st.rows.filter(r => !st.flashFrom.has(r.value)).map(r => r.value);
                if (fresh.length) { st.flash = fresh; st.flashFrom = null; }
            }
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

    async function edit(key, body, btn, onDone) {
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
            if (onDone) onDone();
            await load(key);
            return true;
        } catch (e) {
            msg(key, (body.add ? 'Could not add: ' : 'Could not remove: ') + e.message, true);
            return false;
        } finally { if (btn) btn.disabled = false; }
    }

    /* Add what is typed in the one box. An exact match adds nothing (it points at the row instead). On
     * success the box is cleared, the WHOLE list is drawn again and the new row is highlighted; on a
     * refusal the text stays so it can be corrected. */
    async function add(panel) {
        const key = panel && panel.dataset.key, st = state[key];
        if (!st || panel.dataset.busy) return;
        const inp = panel.querySelector('.rl-query');
        const v = (inp.value || '').trim();
        if (!v) { inp.focus(); return; }
        const hit = exactRow(st.kind, st.rows, v);
        if (hit) {
            offer(key);
            flashRows(Array.from(panel.querySelectorAll('.rl-row')).filter(r => r.dataset.value === hit.value));
            return;
        }
        const before = new Set(st.rows.map(r => r.value));
        panel.dataset.busy = '1';
        try {
            await edit(key, {add: v}, panel.querySelector('.rl-add-action'), () => {
                inp.value = '';
                st.flashFrom = before;
            });
        } finally { delete panel.dataset.busy; }
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
            const ad = t.closest('.rl-add-action');
            if (ad) { add(ad.closest('.rl-panel')); return; }
            if (t.closest('[data-tab="relay"],[data-tab="blossom"],[data-tab="social"]')) loadAll();
        });
        // Enter in the box adds -- it must not submit the whole settings form.
        document.addEventListener('keydown', e => {
            if (e.key === 'Enter' && e.target && e.target.classList && e.target.classList.contains('rl-query')) {
                e.preventDefault();
                add(e.target.closest('.rl-panel'));
            }
        });
        // Typing filters the rows and offers the Add (`search` is a search box's clear-×).
        const typed = e => {
            const s = e.target && e.target.classList && e.target.classList.contains('rl-query') && e.target.closest('.rl-panel');
            if (s && state[s.dataset.key]) draw(s.dataset.key);
        };
        document.addEventListener('input', typed);
        document.addEventListener('search', typed, true);
        // A Save of the text boxes changes what the server holds: redraw from it.
        document.addEventListener('pc-admin-saved', ev => {
            const keys = (ev.detail && ev.detail.keys) || [];
            keys.filter(k => LISTS[k]).forEach(load);
        });
        const boot = () => {
            Object.keys(LISTS).forEach(mount);
            if (typeof location !== 'undefined' && ['#tab-relay', '#tab-blossom', '#tab-social'].includes(location.hash)) loadAll();
        };
        if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
    }
    if (typeof module !== 'undefined') module.exports = { LISTS, matches, rowHtml, sameKey, exactRow };
})();
