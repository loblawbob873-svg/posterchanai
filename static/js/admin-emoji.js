// Admin → Emoji. Manages the instance's Pleroma/Akkoma-style packs over
// /api/admin/emoji/* (app/routers/admin_emoji.py); the on-disk format lives in emoji_service.py.
//
// The list is PAGED and the grid shows 72px thumbnails: a real pack is thousands of files (the one
// this was built against is 3336 emoji / 308 MB), so neither the JSON nor the images can be pulled
// in full. Search + "Load more" is the whole navigation model.

const EMOJI_PAGE = 200;
let _emState = { pack: '', q: '', offset: 0, total: 0, loading: false };

function _em(id) { return document.getElementById(id); }

async function emojiLoad(append) {
    const grid = _em('emojiGrid'), status = _em('emojiStatus');
    if (!grid || _emState.loading) return;
    _emState.loading = true;
    if (!append) { _emState.offset = 0; grid.innerHTML = ''; }
    const qs = new URLSearchParams({ q: _emState.q, pack: _emState.pack,
                                     offset: _emState.offset, limit: EMOJI_PAGE });
    try {
        const r = await fetch('/api/admin/emoji?' + qs.toString());
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const j = await r.json();
        _emState.total = j.total || 0;
        emojiRenderPacks(j);
        emojiRenderGrid(j.emojis || [], append);
        const shown = Math.min(_emState.offset + (j.emojis || []).length, _emState.total);
        const filtered = (_emState.q || _emState.pack);
        status.textContent = j.exists
            ? `${j.count.toLocaleString()} emoji · ${emojiSize(j.bytes)} · ${j.packs.length} pack${j.packs.length === 1 ? '' : 's'}`
              + (filtered ? ` — showing ${shown.toLocaleString()} of ${_emState.total.toLocaleString()} matching` : '')
            : `Directory not found: ${j.dir || '(unset)'} — create it, or point the field above somewhere else.`;
        _emState.offset += (j.emojis || []).length;
        const more = _em('emojiMore');
        if (more) more.hidden = _emState.offset >= _emState.total;
    } catch (err) {
        status.textContent = 'Could not load emoji: ' + err.message;
    } finally {
        _emState.loading = false;
    }
}

function emojiSize(b) {
    b = b || 0;
    if (b > 1073741824) return (b / 1073741824).toFixed(1) + ' GB';
    if (b > 1048576) return Math.round(b / 1048576) + ' MB';
    return Math.max(1, Math.round(b / 1024)) + ' KB';
}

function emojiRenderPacks(j) {
    const sel = _em('emojiPack');
    if (!sel) return;
    const want = _emState.pack;
    const opts = ['<option value="">All packs</option>'].concat((j.packs || []).map(p =>
        `<option value="${escapeHtml(p.name)}">${escapeHtml(p.name === '_' ? '(loose files)' : p.name)}`
        + ` — ${p.count.toLocaleString()}</option>`));
    const html = opts.join('');
    if (sel.dataset.html !== html) { sel.innerHTML = html; sel.dataset.html = html; }
    sel.value = want;
}

function emojiRenderGrid(list, append) {
    const grid = _em('emojiGrid');
    if (!grid) return;
    if (!append && !list.length) {
        grid.innerHTML = '<p class="bots-hint">No emoji here yet — drop some images above.</p>';
        return;
    }
    const html = list.map(e => `
        <div class="emoji-cell" data-pack="${escapeHtml(e.p)}" data-sc="${escapeHtml(e.s)}">
            <img src="${escapeHtml(e.t)}" alt=":${escapeHtml(e.s)}:" loading="lazy" decoding="async">
            <span class="emoji-cell-sc" title=":${escapeHtml(e.s)}:">${escapeHtml(e.s)}</span>
            <span class="emoji-cell-acts">
                <button type="button" class="emoji-act" data-act="rename" title="Rename">✏️</button>
                <button type="button" class="emoji-act" data-act="delete" title="Delete">🗑️</button>
            </span>
        </div>`).join('');
    if (append) grid.insertAdjacentHTML('beforeend', html); else grid.innerHTML = html;
}

async function emojiPost(url, body) {
    const r = await csrfFetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                     body: JSON.stringify(body) });
    let j = {};
    try { j = await r.json(); } catch (_) { /* empty body */ }
    if (!r.ok) throw new Error(j.detail || ('HTTP ' + r.status));
    return j;
}

async function emojiUpload(files) {
    if (!files || !files.length) return;
    const status = _em('emojiStatus');
    const fd = new FormData();
    fd.append('pack', _emState.pack || _em('emojiPack').value || '_');
    if (files.length === 1) fd.append('shortcode', _em('emojiShortcode').value.trim());
    fd.append('overwrite', _em('emojiOverwrite').checked ? 'true' : 'false');
    for (const f of files) fd.append('files', f);
    status.textContent = `Uploading ${files.length} file${files.length === 1 ? '' : 's'}…`;
    try {
        // No Content-Type header: the browser must set the multipart boundary itself.
        const r = await csrfFetch('/api/admin/emoji/upload', { method: 'POST', body: fd });
        const j = await r.json();
        _em('emojiShortcode').value = '';
        _em('emojiFiles').value = '';
        await emojiLoad(false);
        if (j.errors && j.errors.length) {
            // Report per-file failures instead of a bare "some failed" — with a 200-file drop the
            // only useful answer is WHICH ones and why.
            const lines = j.errors.slice(0, 12).map(e => `• ${e.file}: ${e.error}`).join('\n');
            alert(`Added ${j.added.length}, failed ${j.errors.length}:\n\n${lines}`
                  + (j.errors.length > 12 ? `\n…and ${j.errors.length - 12} more` : ''));
        }
    } catch (err) {
        status.textContent = 'Upload failed: ' + err.message;
    }
}

function emojiInit() {
    const grid = _em('emojiGrid');
    if (!grid || grid.dataset.wired) return;
    grid.dataset.wired = '1';

    _em('emojiReload').onclick = () => emojiLoad(false);
    _em('emojiMore').onclick = () => emojiLoad(true);
    _em('emojiPack').onchange = e => { _emState.pack = e.target.value; emojiLoad(false); };

    let t = null;
    _em('emojiSearch').oninput = e => {
        clearTimeout(t);
        t = setTimeout(() => { _emState.q = e.target.value.trim(); emojiLoad(false); }, 250);
    };

    _em('emojiPick').onclick = () => _em('emojiFiles').click();
    _em('emojiFiles').onchange = e => emojiUpload(e.target.files);

    const drop = _em('emojiDrop');
    ['dragenter', 'dragover'].forEach(ev => drop.addEventListener(ev, e => {
        e.preventDefault(); drop.classList.add('drag');
    }));
    ['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => {
        e.preventDefault(); if (ev === 'drop' || e.target === drop) drop.classList.remove('drag');
    }));
    drop.addEventListener('drop', e => emojiUpload(e.dataTransfer && e.dataTransfer.files));

    grid.addEventListener('click', async e => {
        const btn = e.target.closest('.emoji-act');
        if (!btn) return;
        const cell = btn.closest('.emoji-cell');
        const pack = cell.dataset.pack, sc = cell.dataset.sc;
        try {
            if (btn.dataset.act === 'rename') {
                const next = prompt(`Rename :${sc}: to`, sc);
                if (!next || next === sc) return;
                await emojiPost('/api/admin/emoji/rename',
                                { pack, shortcode: sc, new_shortcode: next.trim() });
            } else {
                if (!confirm(`Delete :${sc}: permanently?`)) return;
                await emojiPost('/api/admin/emoji/delete', { pack, shortcode: sc });
            }
            emojiLoad(false);
        } catch (err) {
            alert('Failed: ' + err.message);
        }
    });

    _em('emojiNewPack').onclick = async () => {
        const name = prompt('New pack name (letters, digits, _ and -)');
        if (!name) return;
        try {
            const j = await emojiPost('/api/admin/emoji/pack', { name: name.trim() });
            _emState.pack = j.name;
            emojiLoad(false);
        } catch (err) { alert('Failed: ' + err.message); }
    };

    _em('emojiDelPack').onclick = async () => {
        const pack = _em('emojiPack').value;
        if (!pack) { alert('Pick a pack first.'); return; }
        if (!confirm(`Delete the whole "${pack}" pack and every emoji in it? This cannot be undone.`)) return;
        try {
            const j = await emojiPost('/api/admin/emoji/pack/delete', { name: pack });
            _emState.pack = '';
            emojiLoad(false);
            alert(`Deleted ${j.removed} emoji.`);
        } catch (err) { alert('Failed: ' + err.message); }
    };

    emojiLoad(false);
}

// Every tab is in the DOM from the start (they're shown/hidden), so this could wire on load — but
// painting 200 thumbnails is wasted work for an admin who never opens Emoji, so it waits for the
// tab. (Emoji got its own tab in the settings reorg; it used to live under Site.)
document.addEventListener('DOMContentLoaded', () => {
    const tab = document.getElementById('tab-emoji');
    if (!tab) return;
    if (tab.classList.contains('active')) { emojiInit(); return; }
    document.querySelector('.tab-btn[data-tab="emoji"]')?.addEventListener('click', emojiInit);
});
