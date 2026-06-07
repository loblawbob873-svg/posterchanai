// Admin → Bots tab. CRUD + On/Off over /api/admin/bots; runtime status from /status.
// The bot manager (app/services/bot_manager_service.py) owns the actual processes.

// Config keys with dedicated text/textarea form fields (every per-bot setting has a real
// field — no JSON needed for normal config). Shown contextually per feature/platform.
const BOT_KNOWN_KEYS = [
    'server', 'username', 'access_token', 'pleroma_admin_token',
    'matrix_server', 'matrix_user_id', 'matrix_access_token', 'matrix_room_id', 'matrix_admins',
    'prompt',
    'sql_database', 'db_user', 'db_pass', 'db_host',
    'nitter_poll_seconds', 'shamebot_rooms', 'trusted_media_hosts',
    'tts_voice', 'tts_rate', 'tts_pitch',
    'welcome_message', 'welcome_image', 'welcome_lookback_minutes',
    'block_image', 'report_image', 'unfollow_image',
    'auto_post_interval_min', 'auto_post_interval_max', 'auto_post_max_per_day',
    'auto_post_quiet_hours', 'auto_post_seed', 'auto_post_topics', 'auto_post_rooms',
    'text',  // image bot: caption posted with the image (IMAGE_POSTER_TEXT)
];
// Config keys backed by a checkbox.
const BOT_KNOWN_CHECKS = ['auto_narrate', 'unfollow_silent_mode', 'stickers_enabled', 'auto_post_enabled'];
// nitter_feeds is special (list of {rss} ↔ one URL per line) and handled separately.
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
    const ck = (cid) => { const e = _g(cid); return !!(e && e.checked); };
    const show = (gid, on) => { const e = _g(gid); if (e) e.style.display = on ? '' : 'none'; };

    // Matrix can be a SECONDARY connection on a misskey/pleroma bot (e.g. a Matrix listener
    // that also posts to misskey). The "Also connect to Matrix" checkbox only makes sense then.
    const matrixSecondary = ck('bot_ft_matrix');
    show('bot_grp_fedi', !isMatrix);
    show('bot_grp_matrix', isMatrix || (matrixSecondary && !isImage));
    show('bot_grp_matrix_extra', (isMatrix || matrixSecondary) && !isImage);
    show('bot_ft_matrix_label', !isMatrix && !isImage);   // the checkbox itself
    show('bot_grp_features', !isImage);

    // Per-feature sections appear only when their feature is enabled.
    show('bot_grp_nitter', !isImage && ck('bot_ft_nitter'));
    // block / welcome / report / unfollow all need the Pleroma DB.
    const needsDb = ck('bot_ft_block') || ck('bot_ft_welcome') || ck('bot_ft_report') || ck('bot_ft_unfollow');
    show('bot_grp_db', !isImage && needsDb);
    show('bot_grp_oauth', platform === 'pleroma' || platform === 'misskey');  // password connect (fedi)
    show('bot_grp_oauth_totp', platform === 'misskey');   // Misskey signin can take a 2FA code
    show('bot_grp_pleroma_admin', platform === 'pleroma' && !isImage && ck('bot_ft_report'));  // report only
    show('bot_grp_welcome', !isImage && ck('bot_ft_welcome'));
    show('bot_grp_block', !isImage && ck('bot_ft_block'));
    show('bot_grp_report', !isImage && ck('bot_ft_report'));
    show('bot_grp_unfollow', !isImage && ck('bot_ft_unfollow'));
    show('bot_grp_media', !isImage && (ck('bot_ft_reply') || ck('bot_ft_nitter') || matrixSecondary));
    show('bot_grp_voice', !isImage);

    // Scheduled auto-posting: offered for text bots; detail fields appear once enabled.
    // The Rooms field is Matrix-only (fedi bots post to their own account, not rooms).
    const autopostOn = !isImage && ck('bot_f_auto_post_enabled');
    show('bot_grp_autopost_toggle', !isImage);
    show('bot_grp_autopost', autopostOn);
    show('bot_grp_autopost_rooms', autopostOn && isMatrix);

    // Image bots get an inline image-preview tester instead of the text auto-post section.
    show('bot_grp_imgtest', isImage);
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

    // nitter_feeds: [{rss, room?}] <-> one per line: "rss [room]" (room optional, preserved)
    const feeds = Array.isArray(cfg.nitter_feeds)
        ? cfg.nitter_feeds.filter(f => f && f.rss).map(f => f.room ? `${f.rss} ${f.room}` : f.rss)
        : [];
    _setVal('bot_f_nitter_feeds', feeds.join('\n'));

    // features from modes
    const plat = b ? b.platform : 'misskey';
    const modes = (b && b.modes) ? b.modes.split(',').map(m => m.trim()) : [];
    _setChk('bot_ft_reply', modes.includes('--' + plat));               // reply on the bot's own platform
    _setChk('bot_ft_matrix', plat !== 'matrix' && modes.includes('--matrix'));  // matrix as a secondary connection
    Object.entries(BOT_FEATURES).forEach(([cid, flag]) => _setChk(cid, modes.includes(flag)));

    // Anything no field covers (exotic keys) -> the rarely-shown escape hatch.
    const known = new Set([...BOT_KNOWN_KEYS, ...BOT_KNOWN_CHECKS, 'nitter_feeds']);
    const leftover = {};
    Object.keys(cfg).forEach(k => { if (!known.has(k)) leftover[k] = cfg[k]; });
    _setVal('bot_f_advanced', Object.keys(leftover).length ? JSON.stringify(leftover, null, 2) : '');
    _g('bot_grp_advanced').style.display = Object.keys(leftover).length ? '' : 'none';

    // reset the Test → Preview widgets so a prior bot's output doesn't linger
    const _ts = _g('bot_testpost_status'); if (_ts) _ts.textContent = '';
    const _tp = _g('bot_testpost_preview'); if (_tp) { _tp.textContent = ''; _tp.style.display = 'none'; }
    const _is = _g('bot_imgtest_status'); if (_is) _is.textContent = '';
    const _ii = _g('bot_imgtest_img'); if (_ii) { _ii.removeAttribute('src'); _ii.style.display = 'none'; }

    onBotFormChange();
    _g('botModal').style.display = 'flex';
}

function closeBotModal() { _g('botModal').style.display = 'none'; }

function _buildModes(type, platform) {
    if (type === 'image') return '';
    const modes = new Set();
    if (_g('bot_ft_reply').checked) modes.add('--' + platform);          // reply on own platform
    if (platform !== 'matrix' && _g('bot_ft_matrix').checked) modes.add('--matrix');  // secondary matrix
    Object.entries(BOT_FEATURES).forEach(([cid, flag]) => { if (_g(cid).checked) modes.add(flag); });
    return [...modes].join(',');
}

// Mint an access token from the bot account's password (Pleroma password grant / Misskey
// signin) and drop it into the Access token field. Saves the admin the manual OAuth flow.
async function botOauthConnect() {
    const statusEl = _g('bot_oauth_status');
    const platform = _val('bot_f_platform');
    const server = _val('bot_f_server');
    const username = _val('bot_f_username');
    const password = _g('bot_f_oauth_password') ? _g('bot_f_oauth_password').value : '';
    if (!server || !username || !password) {
        statusEl.textContent = 'Enter Server URL, Bot username and the password first.';
        return;
    }
    statusEl.textContent = 'Connecting…';
    try {
        const resp = await fetch('/api/admin/bots/oauth/token', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ platform, server, username, password, totp: _val('bot_f_oauth_totp') }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || resp.statusText);
        _setVal('bot_f_access_token', data.access_token);
        _g('bot_f_oauth_password').value = '';
        if (_g('bot_f_oauth_totp')) _g('bot_f_oauth_totp').value = '';
        statusEl.textContent = '✅ Token minted and filled in. Click Save to keep it.';
    } catch (err) {
        statusEl.textContent = '❌ ' + err.message;
    }
}

// Test → Preview: generate one post from the bot's SAVED config and show it (no posting).
async function previewPost() {
    const id = _val('bot_f_id');
    const st = _g('bot_testpost_status');
    const pv = _g('bot_testpost_preview');
    if (!id) { st.textContent = 'Save the bot first.'; return; }
    st.textContent = 'Generating…';
    pv.style.display = 'none';
    try {
        const r = await fetch(`/api/admin/bots/${id}/test-post/preview`, { method: 'POST' });
        const d = await r.json().catch(() => ({}));
        if (!r.ok || !d.ok) throw new Error(d.error || d.detail || r.statusText);
        st.textContent = '';
        pv.textContent = d.text;
        pv.style.display = '';
    } catch (err) {
        st.textContent = '❌ ' + err.message;
    }
}

// Image bots: generate one image from the SAVED prompt and show it inline (no posting).
async function previewImage() {
    const id = _val('bot_f_id');
    const st = _g('bot_imgtest_status');
    const im = _g('bot_imgtest_img');
    if (!id) { st.textContent = 'Save the bot first.'; return; }
    st.textContent = 'Generating… (can take ~10–30s)';
    im.style.display = 'none';
    try {
        const r = await fetch(`/api/admin/bots/${id}/test-post/preview`, { method: 'POST' });
        const d = await r.json().catch(() => ({}));
        if (!r.ok || !d.ok || !d.image) throw new Error(d.error || d.detail || r.statusText);
        st.textContent = '';
        im.src = 'data:image/png;base64,' + d.image;
        im.style.display = '';
    } catch (err) {
        st.textContent = '❌ ' + err.message;
    }
}

// Test → Publish now: fire one real post immediately (bypasses the schedule).
async function publishPost() {
    const id = _val('bot_f_id');
    const st = _g('bot_testpost_status');
    if (!id) { st.textContent = 'Save the bot first.'; return; }
    if (!confirm('Publish a test post now? It will be posted live to the timeline.')) return;
    st.textContent = 'Posting…';
    try {
        const r = await fetch(`/api/admin/bots/${id}/test-post/publish`, { method: 'POST' });
        const d = await r.json().catch(() => ({}));
        if (!r.ok || !d.ok) throw new Error(d.error || d.detail || r.statusText);
        st.textContent = '✅ Triggered — it should appear on the timeline shortly.';
    } catch (err) {
        st.textContent = '❌ ' + err.message;
    }
}

async function saveBot() {
    const errEl = _g('botModalError');
    errEl.textContent = '';
    const id = _val('bot_f_id');
    const name = _val('bot_f_name');
    if (!name) { errEl.textContent = 'Name is required.'; return; }
    const type = _val('bot_f_type');
    const platform = _val('bot_f_platform');

    // assemble config from the real fields
    const config = {};
    BOT_KNOWN_KEYS.forEach(k => { const v = _val('bot_f_' + k); if (v) config[k] = v; });
    BOT_KNOWN_CHECKS.forEach(k => { if (_g('bot_f_' + k) && _g('bot_f_' + k).checked) config[k] = true; });
    // nitter_feeds: textarea (one per line: "rss [room]") -> [{rss, room?}, ...] (room preserved)
    const feedLines = _val('bot_f_nitter_feeds').split('\n').map(s => s.trim()).filter(Boolean);
    if (feedLines.length) config.nitter_feeds = feedLines.map(line => {
        const parts = line.split(/\s+/);
        const room = parts.slice(1).join(' ').trim();
        return room ? { rss: parts[0], room } : { rss: parts[0] };
    });
    // escape hatch: only present if the bot had exotic keys (shown group)
    if (_g('bot_grp_advanced').style.display !== 'none') {
        const adv = _val('bot_f_advanced');
        if (adv) {
            try {
                const parsed = JSON.parse(adv);
                if (parsed && typeof parsed === 'object') Object.assign(config, parsed);
                else { errEl.textContent = 'Other settings must be a JSON object.'; return; }
            } catch (e) { errEl.textContent = 'Other settings is not valid JSON.'; return; }
        }
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
