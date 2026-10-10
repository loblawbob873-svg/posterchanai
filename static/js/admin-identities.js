// Admin → Relay: the NIP-05 IDENTITIES list (data-tab "relay"), drawn like the blocked-accounts list.
//
// The registry was only a textarea of "name hex" lines: no way to see WHO a name belongs to, whether
// their profile really publishes the address (a mixed-case "JonnyFever" did not verify in lowercase
// clients and nothing showed it), or which line is a bot. This draws each identity with the owner's
// picture and profile name from this relay, the address, and -- as plain information -- what their own
// profile's NIP-05 field says. EVERY ROW IS A MEMBER: the NAME is what grants access (2026-10-05), and a
// profile showing an address of its own (dreadpirate's says DreadPirateRoberts@getalby.com) is allowed and
// expected. This list used to grade that as a red "not in profile" sorted to the top, which read as "about
// to be removed" for people who were fine (2026-10-08). Remove goes to the server
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
    // By name. Nothing here is a problem to surface first: every row is a member.
    function sortRows(rows) {
        return rows.slice().sort((a, b) => String(a.name).toLowerCase().localeCompare(String(b.name).toLowerCase()));
    }
    /* "Users that signed up on the instance and never posted or did anything" (2026-10-08). `activity` is
     * null when the relay could not be asked -- that row is UNKNOWN and is never shown under either filter,
     * because "could not read it" is not "did nothing". */
    function passesFilter(r, f) {
        if (!f) return true;
        const a = r.activity;
        if (!a) return false;
        if (f === 'never_posted') return !a.posts;
        if (f === 'inactive') return !a.posts && !a.events;
        return true;
    }
    const _day = ts => { try { return new Date(ts * 1000).toLocaleDateString(undefined, {year: 'numeric', month: 'short', day: 'numeric'}); } catch (_) { return ''; } };
    function activityNote(r) {
        const a = r.activity;
        if (!a) return 'activity unknown — the relay could not be read';
        const seen = a.first_seen ? ` · first seen ${_day(a.first_seen)}` : '';
        if (a.posts) return `${a.posts} post${a.posts === 1 ? '' : 's'} · last ${_day(a.last_post)}`;
        if (a.events) return `never posted · ${a.events} other event${a.events === 1 ? '' : 's'}, last ${_day(a.last_event)}${seen}`;
        return `never posted, no activity at all${seen}`;
    }
    // What their own profile says, as information -- never as a verdict.
    function profileNote(r) {
        if (r.verified) return r.via ? `profile shows ${r.via} (another of their names here)` : 'profile shows this address';
        return r.profile_nip05 ? `profile shows ${r.profile_nip05}` : 'profile shows no NIP-05';
    }
    function rowHtml(r) {
        return `
            <div class="blk-row ids-row" data-name="${esc(r.name)}">
                <input type="checkbox" class="ids-pick" data-pk="${esc(r.pubkey)}" aria-label="Select ${esc(r.address)}"${picked.has(r.pubkey) ? ' checked' : ''}>
                ${r.picture ? `<img class="blk-pic" src="${esc(r.picture)}" alt="" loading="lazy" referrerpolicy="no-referrer">`
                            : '<span class="blk-pic blk-nopic"></span>'}
                <div class="blk-who">
                    <div class="blk-name">${esc(r.address)}<span class="ids-badge ids-ok" title="This name is granted here, so its owner is a member">✓ member</span></div>
                    <div class="blk-id">${r.display ? esc(r.display) + ' · ' : (r.has_profile ? '(profile has no name) · ' : '(no profile on this relay) · ')}<code>${esc(String(r.npub).slice(0, 20))}…</code>${(r.others || []).length ? ` · also ${(r.others || []).map(esc).join(', ')}` : ''}</div>
                    <div class="blk-id ids-note">${esc(profileNote(r))}</div>
                    <div class="blk-id ids-act${r.activity && !r.activity.posts ? ' ids-idle' : ''}">${esc(activityNote(r))}</div>
                </div>
                <button type="button" class="btn-secondary btn-small ids-remove">Remove</button>
            </div>`;
    }

    let rows = [], loading = false, complete = true, actComplete = true, shownNow = [];
    const picked = new Set();

    function draw() {
        const list = document.getElementById('ids_list'), sum = document.getElementById('ids_summary');
        if (!list || !sum) return;
        const q = (document.getElementById('ids_search') || {}).value || '';
        const f = (document.getElementById('ids_filter') || {}).value || '';
        const shown = sortRows(rows.filter(r => matches(r, q) && passesFilter(r, f)));
        const what = f === 'never_posted' ? ' never posted' : (f === 'inactive' ? ' have no activity at all' : '');
        sum.textContent = rows.length
            ? (q || f ? `${shown.length} of ${rows.length} identities${what ? what : ' match'}` : `${rows.length} identities — every one a member`)
            : 'No identities granted yet.';
        if (f && !actComplete) sum.textContent += ' (activity could not be read from the relay — nobody is listed as inactive on a guess)';
        shownNow = shown.slice(0, 300);
        syncRetire();
        list.innerHTML = shownNow.map(rowHtml).join('')
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
            actComplete = j.activity_complete !== false;
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

    /* REMOVE, UNFOLLOW & BLOCK ("for the users that signed up and never posted ... we need a way to unfollow
     * them and make sure they can't come back later and DDOS the place"). One request for the selection: the
     * server blocks the keys first (signup and login refuse a blocked key), then takes their names and the
     * access those carried, then the operator unfollows them. */
    function syncRetire() {
        const b = document.getElementById('ids_retire');
        if (!b) return;
        const n = rows.filter(r => picked.has(r.pubkey)).length;
        b.disabled = !n;
        b.textContent = n ? `Remove, unfollow & block ${n}` : 'Remove, unfollow & block';
    }
    function retireSummary(j) {
        const parts = [`Blocked ${j.blocked || 0}`, `removed ${(j.names_removed || []).length} name(s)`, `unfollowed ${j.unfollowed || 0}`];
        if ((j.skipped_admins || []).length) parts.push(`${j.skipped_admins.length} admin(s) skipped`);
        if ((j.refused_operator_keys || []).length) parts.push(`${j.refused_operator_keys.length} operator key(s) skipped`);
        let msg = parts.join(', ') + '.';
        if (j.unfollow_error) msg += ' Unfollow NOT done: ' + j.unfollow_error;
        if (j.names_error) msg += ' Names NOT removed: ' + j.names_error;
        if (j.revoke_error) msg += ' Permissions NOT fully revoked.';
        return msg;
    }
    async function retire() {
        const pks = rows.filter(r => picked.has(r.pubkey)).map(r => r.pubkey);
        if (!pks.length) return;
        const ok = (typeof pcConfirm === 'function')
            ? await pcConfirm(`Remove, unfollow & block ${pks.length} account(s)? Their names and permissions go, the node stops following them, and the relay rejects them from now on — signing up or logging in again with the same key is refused.`)
            : true;
        if (!ok) return;
        const b = document.getElementById('ids_retire'), sum = document.getElementById('ids_summary');
        if (b) { b.disabled = true; b.textContent = 'Working…'; }
        try {
            const r = await (window.csrfFetch || fetch)('/api/admin/relay/identities/retire', {
                method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({pubkeys: pks})});
            const j = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error(j.detail || ('HTTP ' + r.status));
            const gone = new Set(pks.filter(p => !(j.skipped_admins || []).includes(p) && !(j.refused_operator_keys || []).includes(p)));
            rows = rows.filter(x => !gone.has(x.pubkey));
            gone.forEach(p => picked.delete(p));
            const ta = document.getElementById('nostr_relay_nip05_names');
            if (ta && typeof j.value === 'string') {
                ta.value = j.value;
                if (typeof loadedValues !== 'undefined') loadedValues.set('nostr_relay_nip05_names', ta.value);
            }
            draw();
            const msg = retireSummary(j);
            if (sum) sum.textContent = msg;
            if (window.pcAlert && (j.unfollow_error || j.names_error || j.revoke_error)) window.pcAlert(msg);
        } catch (e) {
            const msg = 'Nothing changed: ' + e.message;
            if (window.pcAlert) window.pcAlert(msg); else if (sum) sum.textContent = msg;
            syncRetire();
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
    if (typeof document !== 'undefined') {
        document.addEventListener('click', e => {
            const b = e.target.closest && e.target.closest('.ids-remove');
            if (b) { remove(b); return; }
            if (e.target.id === 'ids_retire') { retire(); return; }
            if (e.target.id === 'ids_sel_all') { shownNow.forEach(r => picked.add(r.pubkey)); draw(); return; }
            if (e.target.id === 'ids_sel_none') { picked.clear(); draw(); return; }
            if (e.target.closest && e.target.closest('[data-tab="relay"]')) load();
        });
        document.addEventListener('input', e => { if (e.target && e.target.id === 'ids_search') draw(); });
        document.addEventListener('change', e => {
            if (e.target && e.target.id === 'ids_filter') draw();
            if (e.target && e.target.classList && e.target.classList.contains('ids-pick')) {
                if (e.target.checked) picked.add(e.target.dataset.pk); else picked.delete(e.target.dataset.pk);
                syncRetire();
            }
        });
        if (typeof location !== 'undefined' && location.hash === '#tab-relay') document.addEventListener('DOMContentLoaded', load);
    }
    if (typeof module !== 'undefined') module.exports = { matches, sortRows, rowHtml, revokedText, passesFilter, activityNote, retireSummary };
})();
