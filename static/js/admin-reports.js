// Admin → Relay: REPORTS (data-tab "relay"). NIP-56 reports (kind 1984) filed by, or about, a member of
// this node -- what people file from a post's or a profile's ⋯ → Report in the client, and what other
// fediverse servers send as a Flag about one of ours. Before this the only reader was the opt-in report
// BOT, so an admin without it running never saw a report at all. Read-only: acting on one is the
// existing Block / Mute / Identities controls.
(function () {
    'use strict';
    const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c =>
        ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
    const when = ts => { try { return new Date(ts * 1000).toLocaleString(); } catch (_) { return ''; } };
    // A note1… reference opens in this node's own client at /<note1…>.
    const postHref = ref => (ref && ref.startsWith('nostr:note1')) ? '/' + ref.slice(6) : '';
    const who = (h, ref) => {
        const hh = String(h || '');
        if (hh.startsWith('nostr:npub1')) return `<code>${esc(hh.slice(6, 26))}…</code>`;
        return esc(hh || ref || '');
    };

    // Pure, so it can be tested.
    function rowHtml(r) {
        const href = postHref(r.note_ref);
        return `
            <div class="blk-row rep-row">
                <div class="blk-who">
                    <div class="blk-name">🚩 <b>${esc(r.type || 'other')}</b> · ${who(r.reported_handle, r.reported)}</div>
                    <div class="blk-id">reported by ${who(r.reporter_handle, r.reporter)} · ${esc(when(r.at))}</div>
                    ${r.reason ? `<div class="blk-id rep-reason">${esc(r.reason)}</div>` : '<div class="blk-id">(no comment)</div>'}
                    ${href ? `<div class="blk-id"><a href="${esc(href)}" target="_blank" rel="noopener">Open the reported post</a></div>` : ''}
                </div>
            </div>`;
    }

    let loading = false;
    async function load() {
        const list = document.getElementById('rep_list'), sum = document.getElementById('rep_summary');
        if (!list || !sum || loading) return;
        loading = true;
        sum.textContent = 'Loading…';
        try {
            const r = await fetch('/api/admin/relay/reports');
            if (r.status === 503) throw new Error('the relay could not be read — this is NOT "no reports"; try again');
            if (!r.ok) throw new Error('HTTP ' + r.status);
            const rows = (await r.json()).reports || [];
            sum.textContent = rows.length ? `${rows.length} report${rows.length === 1 ? '' : 's'} filed by or about members, newest first`
                                          : 'No reports filed by or about members.';
            list.innerHTML = rows.slice(0, 300).map(rowHtml).join('');
        } catch (e) {
            sum.textContent = 'Could not load the reports: ' + e.message;
        } finally { loading = false; }
    }

    if (typeof document !== 'undefined') {
        document.addEventListener('click', e => {
            if (e.target.id === 'rep_reload') { load(); return; }
            if (e.target.closest && e.target.closest('[data-tab="relay"]')) load();
        });
        if (typeof location !== 'undefined' && location.hash === '#tab-relay') document.addEventListener('DOMContentLoaded', load);
    }
    if (typeof module !== 'undefined') module.exports = { rowHtml };
})();
