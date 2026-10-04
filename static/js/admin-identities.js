// Admin → Relay: the NIP-05 IDENTITIES list (data-tab "relay"), drawn like the blocked-accounts list.
//
// The registry was only a textarea of "name hex" lines: no way to see WHO a name belongs to, whether
// their profile really publishes the address (a mixed-case "JonnyFever" did not verify in lowercase
// clients and nothing showed it), or which line is a bot. This draws each identity with the owner's
// picture and profile name from this relay, the address, and a ✓ when their own profile publishes it
// exactly -- the same test that grants access to this node's features. Remove goes to the server
// (one name off the registry, applied live), then the textarea -- still there under "Edit as text",
// still what Save sends -- is rewritten AND taken as the new baseline, so the next Save cannot put it back.
(function () {
    'use strict';
    const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c =>
        ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

    // Pure, so it can be tested.
    function matches(row, q) {
        q = String(q || '').trim().toLowerCase();
        if (!q) return true;
        return [row.name, row.address, row.display, row.profile_nip05, row.npub, row.pubkey]
            .some(v => String(v || '').toLowerCase().includes(q));
    }
    // Unverified first: they are the ones that need looking at.
    function sortRows(rows) {
        return rows.slice().sort((a, b) => (a.verified === b.verified ? 0 : a.verified ? 1 : -1)
            || String(a.name).toLowerCase().localeCompare(String(b.name).toLowerCase()));
    }
    function rowHtml(r) {
        const why = r.verified ? '' : (r.profile_nip05 ? `their profile says ${r.profile_nip05}` : 'their profile does not publish it');
        return `
            <div class="blk-row ids-row" data-name="${esc(r.name)}">
                ${r.picture ? `<img class="blk-pic" src="${esc(r.picture)}" alt="" loading="lazy" referrerpolicy="no-referrer">`
                            : '<span class="blk-pic blk-nopic"></span>'}
                <div class="blk-who">
                    <div class="blk-name">${esc(r.address)}${r.verified
                        ? (r.via ? `<span class="ids-badge ids-ok" title="An extra name: their profile publishes ${esc(r.via)}, another address of theirs here">✓ verified via ${esc(r.via)}</span>`
                                 : '<span class="ids-badge ids-ok" title="Their profile publishes this address">✓ verified</span>')
                        : `<span class="ids-badge ids-no" title="${esc(why)}">not in profile</span>`}</div>
                    <div class="blk-id">${r.display ? esc(r.display) + ' · ' : '(no profile on this relay) · '}<code>${esc(String(r.npub).slice(0, 20))}…</code>${(r.others || []).length ? ` · also ${(r.others || []).map(esc).join(', ')}` : ''}</div>
                </div>
                <button type="button" class="btn-secondary btn-small ids-remove">Remove</button>
            </div>`;
    }

    let rows = [], loading = false, complete = true;

    function draw() {
        const list = document.getElementById('ids_list'), sum = document.getElementById('ids_summary');
        if (!list || !sum) return;
        const q = (document.getElementById('ids_search') || {}).value || '';
        const shown = sortRows(rows.filter(r => matches(r, q)));
        const unverified = rows.filter(r => !r.verified).length;
        sum.textContent = rows.length
            ? (q ? `${shown.length} of ${rows.length} identities match`
                 : `${rows.length} identities` + (unverified ? ` · ${unverified} not published in their profile` : ''))
            : 'No identities granted yet.';
        const prune = document.getElementById('ids_prune');
        if (prune) {
            // Only offered when every profile was read: an unread profile LOOKS unverified.
            prune.hidden = !(unverified && complete);
            prune.textContent = `Remove all ${unverified} not in profile`;
            prune.disabled = false;
        }
        list.innerHTML = shown.slice(0, 300).map(rowHtml).join('')
            + (shown.length > 300 ? `<div class="blk-more">${shown.length - 300} more — search to narrow it down</div>` : '');
    }

    async function load() {
        if (loading) return;
        loading = true;
        const sum = document.getElementById('ids_summary');
        if (sum) sum.textContent = 'Loading…';
        try {
            const r = await fetch('/api/admin/relay/identities');
            if (!r.ok) throw new Error('HTTP ' + r.status);
            const j = await r.json();
            rows = j.identities || [];
            complete = j.names_complete !== false;
            draw();
            if (!j.names_complete && sum) sum.textContent += ' (some profiles could not be read from the relay)';
        } catch (e) {
            if (sum) sum.textContent = 'Could not load the identities: ' + e.message;
        } finally { loading = false; }
    }

    async function remove(btn) {
        const row = btn.closest('.ids-row');
        const name = row && row.dataset.name;
        if (!name) return;
        const ok = (typeof pcConfirm === 'function')
            ? await pcConfirm(`Remove the identity "${name}"? Its owner loses the address and their permissions on this node.`)
            : true;
        if (!ok) return;
        btn.disabled = true;
        btn.textContent = 'Removing…';
        try {
            const r = await (window.csrfFetch || fetch)('/api/admin/relay/identity/remove', {
                method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name})});
            const j = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error(j.detail || ('HTTP ' + r.status));
            rows = rows.filter(x => x.name !== name);
            if (j.revoke_error && window.pcAlert) window.pcAlert('Removed, but their permissions could not be revoked: ' + j.revoke_error);
            // Keep the text box -- what Save sends -- in step, and make it the baseline.
            const ta = document.getElementById('nostr_relay_nip05_names');
            if (ta && typeof j.value === 'string') {
                ta.value = j.value;
                if (typeof loadedValues !== 'undefined') loadedValues.set('nostr_relay_nip05_names', ta.value);
            }
            draw();
        } catch (e) {
            btn.disabled = false;
            btn.textContent = 'Remove';
            const msg = 'Could not remove: ' + e.message;
            if (window.pcAlert) window.pcAlert(msg);
            else { const sum = document.getElementById('ids_summary'); if (sum) sum.textContent = msg; }
        }
    }

    // What the server revoked along with the names (relay_access_policy.revoke_identities).
    function revokedText(j) {
        const r = (j && j.revoked) || {};
        if (j && j.revoke_error) return 'Permissions NOT fully revoked.';
        return r.accounts || r.whitelist
            ? `Revoked permissions for ${r.accounts || 0} account(s)${r.whitelist ? `, ${r.whitelist} removed from the Blossom whitelist` : ''}.`
            : '';
    }
    // The names a bulk remove may take: exactly the "not in profile" rows on screen right now. The
    // server removes only those that STILL fail its own check (nip05_registry.remove_unverified).
    function unverifiedNames(list) { return (list || []).filter(r => !r.verified).map(r => r.name); }

    async function prune(btn) {
        const names = unverifiedNames(rows);
        if (!names.length) return;
        const sample = names.slice(0, 8).join(', ') + (names.length > 8 ? `, and ${names.length - 8} more` : '');
        const ok = (typeof pcConfirm === 'function')
            ? await pcConfirm(`Remove ${names.length} identit${names.length === 1 ? 'y' : 'ies'} whose profile does not publish the address? `
                + `Their owners lose the address AND their permissions on this node (AI, Blossom, image/music/video, torrents, Media Center, live streaming): ${sample}.`)
            : true;
        if (!ok) return;
        btn.disabled = true;
        btn.textContent = 'Removing…';
        try {
            const r = await (window.csrfFetch || fetch)('/api/admin/relay/identities/remove-unverified', {
                method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({names})});
            const j = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error(j.detail || ('HTTP ' + r.status));
            const ta = document.getElementById('nostr_relay_nip05_names');
            if (ta && typeof j.value === 'string') {
                ta.value = j.value;
                if (typeof loadedValues !== 'undefined') loadedValues.set('nostr_relay_nip05_names', ta.value);
            }
            await load();
            const sum = document.getElementById('ids_summary');
            if (sum) sum.textContent = `Removed ${j.removed || 0}. ${revokedText(j)} ` + sum.textContent;
            if (j.revoke_error && window.pcAlert) window.pcAlert('The names were removed, but their permissions could not all be revoked: ' + j.revoke_error + ' — run it again.');
        } catch (e) {
            btn.disabled = false;
            btn.textContent = 'Remove all not in profile';
            const msg = 'Could not remove: ' + e.message;
            if (window.pcAlert) window.pcAlert(msg);
            else { const sum = document.getElementById('ids_summary'); if (sum) sum.textContent = msg; }
        }
    }

    if (typeof document !== 'undefined') {
        document.addEventListener('click', e => {
            if (e.target && e.target.id === 'ids_prune') { prune(e.target); return; }
            const b = e.target.closest && e.target.closest('.ids-remove');
            if (b) { remove(b); return; }
            if (e.target.closest && e.target.closest('[data-tab="relay"]')) load();
        });
        document.addEventListener('input', e => { if (e.target && e.target.id === 'ids_search') draw(); });
        if (typeof location !== 'undefined' && location.hash === '#tab-relay') document.addEventListener('DOMContentLoaded', load);
    }
    if (typeof module !== 'undefined') module.exports = { matches, sortRows, rowHtml, unverifiedNames, revokedText };
})();
