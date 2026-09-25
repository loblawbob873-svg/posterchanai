// Admin → Relay: the blocked-accounts LIST (data-tab "relay").
//
// The blocklist used to be only a textarea of bare npubs -- 461 on poster.place, four lines tall, no
// names, and the browser's Ctrl+F does not search inside a text box. An account blocked by a stray
// "🚫 Block author" could not be found again: "it's line 64" of a list nobody can read. This draws the
// same list with names and pictures, a search box, and an Unblock per row. Unblock goes to the server
// (one key off the list, applied live), then the textarea -- still there under "Edit as text", still
// what Save sends -- is rewritten AND taken as the new baseline, so the next Save cannot put the key back.
(function () {
    'use strict';
    const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c =>
        ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

    // Pure, so it can be tested: which rows match what was typed (name, NIP-05, npub or hex).
    function matches(row, q) {
        q = String(q || '').trim().toLowerCase();
        if (!q) return true;
        return [row.name, row.nip05, row.npub, row.pubkey].some(v => String(v || '').toLowerCase().includes(q));
    }

    let rows = [], loading = false;

    function draw() {
        const list = document.getElementById('blk_list'), sum = document.getElementById('blk_summary');
        if (!list || !sum) return;
        const q = (document.getElementById('blk_search') || {}).value || '';
        const shown = rows.filter(r => matches(r, q));
        sum.textContent = rows.length
            ? (q ? `${shown.length} of ${rows.length} blocked account(s) match` : `${rows.length} blocked account(s)`)
            : 'Nobody is blocked.';
        list.innerHTML = shown.slice(0, 300).map(r => `
            <div class="blk-row" data-pk="${esc(r.pubkey)}" data-npub="${esc(r.npub)}">
                ${r.picture ? `<img class="blk-pic" src="${esc(r.picture)}" alt="" loading="lazy" referrerpolicy="no-referrer">`
                            : '<span class="blk-pic blk-nopic"></span>'}
                <div class="blk-who">
                    <div class="blk-name">${esc(r.name || '(no profile on this relay)')}</div>
                    <div class="blk-id">${r.nip05 ? esc(r.nip05) + ' · ' : ''}<code>${esc(r.npub.slice(0, 20))}…</code></div>
                </div>
                <button type="button" class="btn-secondary btn-small blk-unblock">Unblock</button>
            </div>`).join('') + (shown.length > 300 ? `<div class="blk-more">${shown.length - 300} more — search to narrow it down</div>` : '');
    }

    async function load() {
        if (loading) return;
        loading = true;
        const sum = document.getElementById('blk_summary');
        if (sum) sum.textContent = 'Loading…';
        try {
            const r = await fetch('/api/admin/relay/blocked');
            if (!r.ok) throw new Error('HTTP ' + r.status);
            const j = await r.json();
            rows = j.accounts || [];
            draw();
            if (!j.names_complete && sum) sum.textContent += ' (some names could not be read from the relay)';
        } catch (e) {
            if (sum) sum.textContent = 'Could not load the blocked accounts: ' + e.message;
        } finally { loading = false; }
    }

    async function unblock(btn) {
        const row = btn.closest('.blk-row');
        const pk = row && row.dataset.pk;
        if (!pk) return;
        btn.disabled = true;
        btn.textContent = 'Unblocking…';
        try {
            const r = await (window.csrfFetch || fetch)('/api/admin/relay/unblock', {
                method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({target: pk})});
            if (!r.ok) throw new Error('HTTP ' + r.status);
            rows = rows.filter(x => x.pubkey !== pk);
            // Keep the text box -- what Save sends -- in step, and make it the baseline.
            const ta = document.getElementById('nostr_relay_blocked_pubkeys');
            if (ta) {
                const npub = (row && row.dataset.npub) || '';
                ta.value = ta.value.split('\n').filter(l => { const t = l.trim().toLowerCase(); return t !== pk && t !== npub; }).join('\n');
                if (typeof loadedValues !== 'undefined') loadedValues.set('nostr_relay_blocked_pubkeys', ta.value);
            }
            draw();
        } catch (e) {
            btn.disabled = false;
            btn.textContent = 'Unblock';
            const msg = 'Could not unblock: ' + e.message;
            if (window.pcAlert) window.pcAlert(msg);
            else { const sum = document.getElementById('blk_summary'); if (sum) sum.textContent = msg; }
        }
    }

    if (typeof document !== 'undefined') {
        document.addEventListener('click', e => {
            const b = e.target.closest('.blk-unblock');
            if (b) { unblock(b); return; }
            if (e.target.closest('[data-tab="relay"]')) load();
        });
        document.addEventListener('input', e => { if (e.target && e.target.id === 'blk_search') draw(); });
        if (location.hash === '#tab-relay') document.addEventListener('DOMContentLoaded', load);
    }
    if (typeof module !== 'undefined') module.exports = { matches };
})();
