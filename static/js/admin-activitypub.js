// Admin → Social → "Fediverse server (ActivityPub)": the status under the two settings.
// The settings themselves load and save generically (admin.js, by id/name); this only READS
// /api/admin/activitypub/status, when the tab is opened or Check status is pressed.
(function () {
    'use strict';
    const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c =>
        ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

    async function load() {
        const box = document.getElementById('ap_status');
        if (!box) return;
        box.textContent = 'Checking…';
        let j;
        try {
            const r = await fetch('/api/admin/activitypub/status');
            if (!r.ok) throw new Error('HTTP ' + r.status);
            j = await r.json();
        } catch (e) { box.textContent = 'Could not read the status: ' + e.message; return; }
        document.querySelectorAll('.ap-dom').forEach(el => { el.textContent = j.domain || 'your domain'; });
        if (!j.enabled) { box.innerHTML = 'Off. Tick the box above and Save to turn it on.'; return; }
        if (!j.domain) { box.innerHTML = '⚠ No domain: set one above, or set the NIP-05 domain on the Relay tab.'; return; }
        const on = (j.members || []).filter(m => m.member);
        const rows = on.map(m => `<tr><td><code>${esc(m.handle)}</code></td><td>${m.followers}</td></tr>`).join('');
        const d = j.delivery || {};
        box.innerHTML = `<div>On — <strong>${on.length}</strong> member(s) on the fediverse as <code>@name@${esc(j.domain)}</code>.</div>`
            + (rows ? `<table class="ap-table" style="margin-top:6px"><tr><th>Handle</th><th>Followers</th></tr>${rows}</table>` : '')
            + `<div style="margin-top:6px">Delivered ${d.delivered || 0}, failed ${d.failed || 0}, waiting to retry ${d.queued || 0}`
            + (d.last_error ? ` — last problem: ${esc(d.last_error)}` : '') + '</div>'
            + ((j.blocked_instances || []).length ? `<div>Blocked instances: ${j.blocked_instances.map(esc).join(', ')}</div>` : '');
    }

    document.addEventListener('click', e => {
        if (e.target.closest('[data-tab="social"]') || e.target.closest('#ap_status_refresh')) load();
    });
    if (location.hash === '#tab-social') document.addEventListener('DOMContentLoaded', load);
})();
