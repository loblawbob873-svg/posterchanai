// Admin → Bots tab. CRUD + On/Off over /api/admin/bots; runtime status from /status.
// The bot manager (app/services/bot_manager_service.py) owns the actual processes.

// Config keys with dedicated form fields. Kept deliberately small — the per-feature
// prompts/messages/images all have defaults, so they live in the Advanced overrides box.
// sql_database is the one exception (no default; block/welcome/report need it).
const BOT_KNOWN_KEYS = [
    'server', 'username', 'access_token', 'pleroma_admin_token',
    'matrix_server', 'matrix_user_id', 'matrix_access_token', 'matrix_room_id', 'matrix_admins',
    'prompt', 'sql_database',
];
const BOT_KNOWN_CHECKS = [];
// feature checkbox id -> main.py mode flag
const BOT_FEATURES = {
    bot_ft_nitter: '--nitter', bot_ft_welcome: '--welcome', bot_ft_block: '--blockbot',
    bot_ft_report: '--report', bot_ft_hashtag: '--hashtagbot', bot_ft_unfollow: '--unfollowbot',
};

let _bots = {};            // id -> full bot row (incl config)
let _botStatusTimer = null;

function _g(id) { return document.getElementById(id); }
function _val(id) { const el = _g(id); return el ? el.value.trim() : ''; }
function _setVal(id, v) { const el = _g(id); if (el) el.value = (v === undefined || v === null) ? '' : v; }
function _setChk(id, v) { const el = _g(id); if (el) el.checked = !!v; }

async function loadBots() {
    try {
        const [rowsResp, statResp] = await Promise.all([
            fetch('/api/admin/bots'),
            fetch('/api/admin/bots/status'),
        ]);
        const rows = rowsResp.ok ? await rowsResp.json() : [];
        const stats = statResp.ok ? await statResp.json() : [];
        const statById = {};
        stats.forEach(s => { statById[s.id] = s; });
        _bots = {};
        rows.forEach(b => { b._status = statById[b.id] || {}; _bots[b.id] = b; });
        renderBots(rows);
    } catch (err) {
        console.error('Failed to load bots:', err);
    }
}

function renderBots(rows) {
    const list = _g('botList');
    if (!list) return;
    if (!rows.length) {
        list.innerHTML = '<p class="bots-hint">No bots yet. Click <b>+ Add bot</b> to create one.</p>';
        return;
    }
    list.innerHTML = rows.map(b => {
        const st = b._status || {};
        const running = !!st.running;
        const offHost = st.on_this_host === false;
        const dot = running ? 'bot-dot-on' : (b.enabled ? 'bot-dot-pending' : 'bot-dot-off');
        const statusText = offHost ? `other host${st.host ? ' (' + esc(st.host) + ')' : ''}`
            : running ? `running${st.pid ? ' · pid ' + st.pid : ''}`
            : b.enabled ? 'starting…' : 'off';
        return `
        <div class="bot-card">
            <span class="bot-dot ${dot}" title="${statusText}"></span>
            <div class="bot-card-main">
                <div class="bot-card-name">${esc(b.name)}</div>
                <div class="bot-card-meta">${esc(b.platform)} · ${esc(b.bot_type)} · ${statusText}</div>
            </div>
            <label class="bot-switch" title="${b.enabled ? 'On' : 'Off'}">
                <input type="checkbox" ${b.enabled ? 'checked' : ''} onchange="toggleBot(${b.id}, this.checked)">
                <span class="bot-slider"></span>
            </label>
            <button type="button" class="btn-small" onclick="openBotModal(${b.id})">Edit</button>
            <button type="button" class="btn-small btn-danger" onclick="deleteBot(${b.id})">Delete</button>
        </div>`;
    }).join('');
}

function esc(s) {
    return String(s === undefined || s === null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

async function toggleBot(id, on) {
    try {
        const resp = await fetch(`/api/admin/bots/${id}/${on ? 'start' : 'stop'}`, { method: 'POST' });
        if (!resp.ok) throw new Error(await resp.text());
        setTimeout(loadBots, 800);
    } catch (err) {
        alert('Failed: ' + err.message);
        loadBots();
    }
}

async function deleteBot(id) {
    const b = _bots[id];
    if (!confirm(`Delete bot "${b ? b.name : id}"? This stops it and removes its config.`)) return;
    try {
        const resp = await fetch(`/api/admin/bots/${id}`, { method: 'DELETE' });
        if (!resp.ok) throw new Error(await resp.text());
        loadBots();
    } catch (err) {
        alert('Delete failed: ' + err.message);
    }
}

// Show/hide field groups based on type + platform.
function onBotFormChange() {
    const type = _val('bot_f_type');
    const platform = _val('bot_f_platform');
    const isMatrix = platform === 'matrix';
    const isImage = type === 'image';
    _g('bot_grp_fedi').style.display = isMatrix ? 'none' : '';
    _g('bot_grp_matrix').style.display = isMatrix ? '' : 'none';
    _g('bot_grp_pleroma_admin').style.display = (platform === 'pleroma' && !isImage) ? '' : 'none';
    _g('bot_grp_features').style.display = isImage ? 'none' : '';

    // Only the Pleroma DB field is contextual (shown for block/welcome/report).
    const ck = (cid) => { const e = _g(cid); return !!(e && e.checked); };
    const dbEl = _g('bot_grp_db');
    if (dbEl) dbEl.style.display = (!isImage && (ck('bot_ft_block') || ck('bot_ft_welcome') || ck('bot_ft_report'))) ? '' : 'none';
}

function openBotModal(id) {
    const b = id !== undefined ? _bots[id] : null;
    _g('botModalTitle').textContent = b ? `Edit ${b.name}` : 'Add bot';
    _g('botModalError').textContent = '';
    _setVal('bot_f_id', b ? b.id : '');
    _setVal('bot_f_name', b ? b.name : '');
    _setVal('bot_f_type', b ? b.bot_type : 'text');
    _setVal('bot_f_platform', b ? b.platform : 'misskey');
    _setVal('bot_f_host', b ? b.host : '');

    const cfg = (b && b.config) ? b.config : {};
    BOT_KNOWN_KEYS.forEach(k => _setVal('bot_f_' + k, cfg[k]));
    BOT_KNOWN_CHECKS.forEach(k => _setChk('bot_f_' + k, cfg[k]));

    // features from modes
    const modes = (b && b.modes) ? b.modes.split(',').map(m => m.trim()) : [];
    _setChk('bot_ft_reply', modes.some(m => ['--misskey', '--pleroma', '--matrix'].includes(m)));
    Object.entries(BOT_FEATURES).forEach(([cid, flag]) => _setChk(cid, modes.includes(flag)));

    // leftover config -> advanced JSON
    const known = new Set([...BOT_KNOWN_KEYS, ...BOT_KNOWN_CHECKS]);
    const leftover = {};
    Object.keys(cfg).forEach(k => { if (!known.has(k)) leftover[k] = cfg[k]; });
    _setVal('bot_f_advanced', Object.keys(leftover).length ? JSON.stringify(leftover, null, 2) : '');

    onBotFormChange();
    _g('botModal').style.display = 'flex';
}

function closeBotModal() { _g('botModal').style.display = 'none'; }

function _buildModes(type, platform) {
    if (type === 'image') return '';
    const modes = [];
    if (_g('bot_ft_reply').checked) modes.push('--' + platform);
    Object.entries(BOT_FEATURES).forEach(([cid, flag]) => { if (_g(cid).checked) modes.push(flag); });
    return modes.join(',');
}

async function saveBot() {
    const errEl = _g('botModalError');
    errEl.textContent = '';
    const id = _val('bot_f_id');
    const name = _val('bot_f_name');
    if (!name) { errEl.textContent = 'Name is required.'; return; }
    const type = _val('bot_f_type');
    const platform = _val('bot_f_platform');

    // assemble config from known fields
    const config = {};
    BOT_KNOWN_KEYS.forEach(k => { const v = _val('bot_f_' + k); if (v) config[k] = v; });
    BOT_KNOWN_CHECKS.forEach(k => { if (_g('bot_f_' + k) && _g('bot_f_' + k).checked) config[k] = true; });
    // merge advanced JSON
    const adv = _val('bot_f_advanced');
    if (adv) {
        try {
            const parsed = JSON.parse(adv);
            if (parsed && typeof parsed === 'object') Object.assign(config, parsed);
            else { errEl.textContent = 'Advanced config must be a JSON object.'; return; }
        } catch (e) { errEl.textContent = 'Advanced config is not valid JSON.'; return; }
    }

    const payload = {
        name, bot_type: type, platform,
        host: _val('bot_f_host'),
        modes: _buildModes(type, platform),
        config,
    };

    try {
        let resp;
        if (id) {
            resp = await fetch(`/api/admin/bots/${id}`, {
                method: 'PUT', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
        } else {
            payload.enabled = true;
            resp = await fetch('/api/admin/bots', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
        }
        if (!resp.ok) {
            const data = await resp.json().catch(() => ({}));
            throw new Error(data.detail || resp.statusText);
        }
        closeBotModal();
        loadBots();
    } catch (err) {
        errEl.textContent = 'Save failed: ' + err.message;
    }
}

// Poll status only while the Bots tab is visible.
function _botTabActive() {
    const t = _g('tab-bots');
    return t && t.classList.contains('active');
}
function _startBotPolling() {
    if (_botStatusTimer) return;
    _botStatusTimer = setInterval(() => { if (_botTabActive()) loadBots(); }, 5000);
}

document.querySelector('[data-tab="bots"]')?.addEventListener('click', () => {
    loadBots();
    _startBotPolling();
});
if (_botTabActive()) { loadBots(); _startBotPolling(); }
