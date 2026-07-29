// Admin → Bots tab. CRUD + On/Off over /api/admin/bots; runtime status from /status.
// The bot manager (app/services/bot_manager_service.py) owns the actual processes.

// Config keys with dedicated text/textarea form fields (every per-bot setting has a real
// field — no JSON needed for normal config). Shown contextually per feature/platform.
const BOT_KNOWN_KEYS = [
    'server', 'username', 'access_token', 'pleroma_admin_token',
    'nostr_nsec',
    'nostr_profile_name', 'nostr_profile_nip05', 'nostr_profile_picture',
    'nostr_rate_per_user', 'nostr_rate_global', 'nostr_rate_window', 'nostr_rate_exempt',
    'nostr_random_reply_quiet', 'nostr_random_reply_per_hour',
    'prompt',
    'sql_database', 'db_user', 'db_pass', 'db_host',
    'nitter_poll_seconds', 'trusted_media_hosts',
    'tts_voice', 'tts_rate', 'tts_pitch',
    'welcome_message', 'welcome_image', 'welcome_lookback_minutes',
    'block_image', 'report_image', 'unfollow_image',
    'auto_post_interval_min', 'auto_post_interval_max', 'auto_post_max_per_day',
    'auto_post_quiet_hours', 'auto_post_seed', 'auto_post_topics',
    'text',  // image bot: caption posted with the image (IMAGE_POSTER_TEXT)
    'image_negative',  // image bot: negative prompt (IMAGE_POSTER_NEGATIVE)
];
// Config keys backed by a checkbox.
const BOT_KNOWN_CHECKS = ['auto_narrate', 'unfollow_silent_mode', 'auto_post_enabled', 'random_scenes', 'nostr_random_reply'];
// nitter_feeds is special (list of {rss} ↔ one URL per line) and handled separately.
// feature checkbox id -> main.py mode flag
const BOT_FEATURES = {
    bot_ft_nitter: '--nitter', bot_ft_welcome: '--welcome', bot_ft_block: '--blockbot',
    bot_ft_report: '--report', bot_ft_hashtag: '--hashtagbot', bot_ft_unfollow: '--unfollowbot',
    bot_ft_dvm: '--dvm', bot_ft_chess: '--chess', bot_ft_ttt: '--ttt', bot_ft_hangman: '--hangman', bot_ft_connect4: '--connect4', bot_ft_blackjack: '--blackjack', bot_ft_holdem: '--holdem',
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
        const scheduled = !!st.scheduled;          // image/auto-post bots: online = scheduled to post
        const offHost = st.on_this_host === false;
        // Online (green) = a live listener process OR a scheduled poster waiting for its next run.
        const dot = (running || scheduled) ? 'bot-dot-on' : (b.enabled ? 'bot-dot-pending' : 'bot-dot-off');
        const _nextTime = st.next_run ? new Date(st.next_run * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
        const statusText = offHost ? `other host${st.host ? ' (' + esc(st.host) + ')' : ''}`
            : running ? `running${st.pid ? ' · pid ' + st.pid : ''}`
            : scheduled ? `scheduled${_nextTime ? ' · next ' + _nextTime : ''}`
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

// Delete ALL of this bot's posts (NIP-09 kind-5 deletions, signed by the bot — propagates to relays/clients).
async function deleteBotPosts() {
    const id = _val('bot_f_id');
    if (!id) { alert('Save the bot first.'); return; }
    const b = _bots[id];
    if (!confirm(`Delete ALL posts for "${b ? b.name : id}"? This publishes NIP-09 deletions for every one of its notes. Its profile and game state are kept. This cannot be undone.`)) return;
    const btn = _g('bot_f_delposts');
    if (btn) { btn.disabled = true; btn.textContent = '🗑 Deleting…'; }
    try {
        const resp = await fetch(`/api/admin/bots/${id}/delete-posts`, { method: 'POST' });
        const d = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(d.detail || resp.statusText);
        if (btn) btn.textContent = `🗑 Deleted ${d.deleted}`;
        setTimeout(() => { if (btn) { btn.disabled = false; btn.textContent = '🗑 Delete all posts'; } }, 2500);
    } catch (err) {
        alert('Delete posts failed: ' + err.message);
        if (btn) { btn.disabled = false; btn.textContent = '🗑 Delete all posts'; }
    }
}

// Show/hide field groups based on type + platform.
function onBotFormChange() {
    const type = _val('bot_f_type');
    const platform = _val('bot_f_platform');
    const isImage = type === 'image';
    const ck = (cid) => { const e = _g(cid); return !!(e && e.checked); };
    const show = (gid, on) => { const e = _g(gid); if (e) e.style.display = on ? '' : 'none'; };

    const isNostr = platform === 'nostr';
    show('bot_grp_fedi', !isNostr);   // server/username/token: Pleroma only
    show('bot_grp_nostr', isNostr);                // Nostr: secret key + relays + media host
    show('bot_grp_features', !isImage);

    // Per-PLATFORM feature applicability — hide (and uncheck, so it's never saved) any feature the
    // selected platform can't run. Fediverse-only features need the Pleroma DB or admin
    // token (block / welcome / report / unfollow → don't apply to Nostr); Nostr-only are
    // the NIP-90 DVM + the Nostr game referees (don't apply to Fediverse). Cross-platform
    // ones (reply / Nitter / hashtag) always show.
    const isFedi = platform === 'pleroma';
    const showFeat = (f, on) => {
        const c = _g('bot_ft_' + f); if (!c) return;
        const lbl = c.closest('label'); if (lbl) lbl.style.display = on ? '' : 'none';
        if (!on && c.checked) c.checked = false;
    };
    ['block', 'welcome', 'report', 'unfollow'].forEach(f => showFeat(f, isFedi));
    ['dvm', 'chess', 'ttt', 'hangman', 'connect4', 'blackjack', 'holdem', 'stats'].forEach(f => showFeat(f, isNostr));
    // Nostr Stats: show the Preview/Post block only when its feature is ticked (Nostr-only).
    show('bot_grp_stats', isNostr && ck('bot_ft_stats'));

    // Per-feature sections appear only when their feature is enabled.
    show('bot_grp_nitter', !isImage && ck('bot_ft_nitter'));
    // block / welcome / report / unfollow all need the Pleroma DB.
    const needsDb = ck('bot_ft_block') || ck('bot_ft_welcome') || ck('bot_ft_report') || ck('bot_ft_unfollow');
    show('bot_grp_db', !isImage && needsDb);
    show('bot_grp_oauth', platform === 'pleroma');  // password connect (fedi)
    show('bot_grp_pleroma_admin', platform === 'pleroma' && !isImage && ck('bot_ft_report'));  // report only
    show('bot_grp_welcome', !isImage && ck('bot_ft_welcome'));
    show('bot_grp_block', !isImage && ck('bot_ft_block'));
    show('bot_grp_report', !isImage && ck('bot_ft_report'));
    show('bot_grp_unfollow', !isImage && ck('bot_ft_unfollow'));
    show('bot_grp_media', !isImage && (ck('bot_ft_reply') || ck('bot_ft_nitter')));
    show('bot_grp_voice', !isImage);

    // Scheduled auto-posting: offered for text bots; detail fields appear once enabled.
    // Fedi bots post to their own account.
    const autopostOn = !isImage && ck('bot_f_auto_post_enabled');
    show('bot_grp_autopost_toggle', !isImage);
    show('bot_grp_autopost', autopostOn);

    // Schedule cadence (interval/quiet/cap) is shared: text bots show it once auto-post is on,
    // image bots always (they're now interval-configurable; blank intervals = fixed hours).
    show('bot_grp_schedule', isImage || autopostOn);
    show('bot_grp_schedule_imghint', isImage);
    // "Post every" is an exact cadence; "Randomize up to" is optional jitter. For image bots,
    // leaving both blank means the fixed-hours schedule rather than a default interval.
    const hMin = _g('bot_hint_interval_min'), hMax = _g('bot_hint_interval_max');
    if (hMin) hMin.textContent = isImage ? '(blank = fixed hours)' : '(e.g. 30 = every 30 min)';
    if (hMax) hMax.textContent = '(optional — blank = exact interval)';

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
    _setVal('bot_f_platform', b ? b.platform : 'pleroma');
    _setVal('bot_f_host', b ? b.host : '');

    const cfg = (b && b.config) ? b.config : {};
    BOT_KNOWN_KEYS.forEach(k => _setVal('bot_f_' + k, cfg[k]));
    BOT_KNOWN_CHECKS.forEach(k => _setChk('bot_f_' + k, cfg[k]));
    // transient widgets (the profile fields themselves persist via BOT_KNOWN_KEYS above)
    { const af = _g('bot_f_nostr_avatar_file'); if (af) af.value = ''; }
    { const ps = _g('bot_provision_status'); if (ps) ps.textContent = ''; }
    { const pv = _g('bot_avatar_preview'); const u = cfg.nostr_profile_picture;
      if (pv) { if (u) { pv.src = u; pv.style.display = ''; } else { pv.removeAttribute('src'); pv.style.display = 'none'; } } }

    // nitter_feeds: [{rss, room?}] <-> one per line: "rss [room]" (room optional, preserved)
    const feeds = Array.isArray(cfg.nitter_feeds)
        ? cfg.nitter_feeds.filter(f => f && f.rss).map(f => f.room ? `${f.rss} ${f.room}` : f.rss)
        : [];
    _setVal('bot_f_nitter_feeds', feeds.join('\n'));

    // features from modes
    const plat = b ? b.platform : 'pleroma';
    const modes = (b && b.modes) ? b.modes.split(',').map(m => m.trim()) : [];
    _setChk('bot_ft_reply', modes.includes('--' + plat));               // reply on the bot's own platform
    Object.entries(BOT_FEATURES).forEach(([cid, flag]) => _setChk(cid, modes.includes(flag)));
    _setChk('bot_ft_stats', cfg.stats_enabled);   // stats is a CONFIG flag (not a main.py mode — argparse rejects unknown)

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
    Object.entries(BOT_FEATURES).forEach(([cid, flag]) => { if (_g(cid).checked) modes.add(flag); });
    return [...modes].join(',');
}

// Mint an access token from the bot account's password (Pleroma password grant
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
            body: JSON.stringify({ platform, server, username, password }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || resp.statusText);
        _setVal('bot_f_access_token', data.access_token);
        _g('bot_f_oauth_password').value = '';
        statusEl.textContent = '✅ Token minted and filled in. Click Save to keep it.';
    } catch (err) {
        statusEl.textContent = '❌ ' + err.message;
    }
}

// ✨ Generate a brand-new Nostr identity for this bot: mints the nsec, grants Blossom upload access,
// publishes a profile (name/avatar/nip05) and makes the operator follow it (WoT). Fills the nsec
// field so the admin just picks features and clicks Save.
function _readFileDataURL(file) {
    return new Promise((resolve, reject) => {
        const fr = new FileReader();
        fr.onload = () => resolve(fr.result);
        fr.onerror = () => reject(new Error('could not read file'));
        fr.readAsDataURL(file);
    });
}

function _showAvatarPreview(url) {
    const pv = _g('bot_avatar_preview');
    if (pv) { pv.src = url; pv.style.display = ''; }
}

// Reveal/hide the bot's nsec (it's loaded into the masked field on Edit) so the operator can read it.
function toggleBotNsec() {
    const f = _g('bot_f_nostr_nsec'), btn = _g('bot_nsec_toggle');
    if (!f) return;
    const show = f.type === 'password';
    f.type = show ? 'text' : 'password';
    if (btn) btn.textContent = show ? '🙈 Hide' : '👁 Reveal';
}
async function copyBotNsec() {
    const f = _g('bot_f_nostr_nsec'); const st = _g('bot_provision_status');
    const v = f ? f.value.trim() : '';
    if (!v) { if (st) st.textContent = 'No secret key set for this bot yet.'; return; }
    try { await navigator.clipboard.writeText(v); if (st) st.textContent = '📋 nsec copied to clipboard.'; }
    catch (_) { if (st) st.textContent = 'Copy failed — reveal it and copy manually.'; }
}

async function provisionBot() {
    const st = _g('bot_provision_status');
    const name = _val('bot_f_name') || 'ChessBot';
    if (st) st.textContent = 'Generating identity…';
    try {
        const r = await fetch('/api/admin/bots/provision', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, nip05: _val('bot_f_nostr_profile_nip05') }),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(d.detail || r.statusText);
        _setVal('bot_f_nostr_nsec', d.nsec);
        if (d.nip05) _setVal('bot_f_nostr_profile_nip05', d.nip05);   // persist the RESOLVED name@host so Save registers/publishes the same value
        if (!_val('bot_f_nostr_profile_name')) _setVal('bot_f_nostr_profile_name', name);
        // upload the chosen avatar now, signed by the freshly minted key
        const fEl = _g('bot_f_nostr_avatar_file');
        if (fEl && fEl.files && fEl.files[0]) {
            if (st) st.textContent = 'Uploading avatar…';
            try {
                const dataUrl = await _readFileDataURL(fEl.files[0]);
                const ur = await fetch('/api/admin/bots/upload-avatar', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ nsec: d.nsec, picture_data: dataUrl }),
                });
                const ud = await ur.json().catch(() => ({}));
                if (ur.ok && ud.url) { _setVal('bot_f_nostr_profile_picture', ud.url); _showAvatarPreview(ud.url); }
            } catch (_) { /* avatar optional */ }
        }
        if (st) st.innerHTML = `✅ Identity created — <code>${d.npub}</code><br>`
            + `Blossom access granted${d.followed ? ' · operator follows it (WoT)' : ''}`
            + `${d.nip05 ? ' · nip05 ' + d.nip05 : ''}. The bot publishes its profile on startup. `
            + `Pick features below and click <b>Save bot</b>.`;
    } catch (err) {
        if (st) st.textContent = '❌ ' + err.message;
    }
}

// ⬆ Upload avatar for a new (just-generated) or existing bot → stored on Blossom, fills the URL.
async function uploadBotAvatar() {
    const st = _g('bot_provision_status');
    const fEl = _g('bot_f_nostr_avatar_file');
    if (!fEl || !fEl.files || !fEl.files[0]) { if (st) st.textContent = 'Pick an image file first.'; return; }
    const id = _val('bot_f_id');
    const nsec = _g('bot_f_nostr_nsec') ? _g('bot_f_nostr_nsec').value.trim() : '';
    if (!id && !nsec) { if (st) st.textContent = 'Generate an identity (or Save the bot) before uploading an avatar.'; return; }
    if (st) st.textContent = 'Uploading avatar…';
    try {
        const dataUrl = await _readFileDataURL(fEl.files[0]);
        const body = { picture_data: dataUrl };
        if (nsec) body.nsec = nsec; else body.bot_id = Number(id);
        const r = await fetch('/api/admin/bots/upload-avatar', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok || !d.url) throw new Error(d.detail || 'upload failed');
        _setVal('bot_f_nostr_profile_picture', d.url); _showAvatarPreview(d.url);
        if (st) st.textContent = '✅ Avatar uploaded — click Save to apply.';
    } catch (err) { if (st) st.textContent = '❌ ' + err.message; }
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
    // Nostr Stats feature → a CONFIG flag (not a main.py mode; the app posts it on the bot's behalf).
    if (_g('bot_ft_stats') && _g('bot_ft_stats').checked) config.stats_enabled = true;
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

// --- Nostr Stats feature: Preview (display only) / Post now (Nostr-only, from this bot's nsec) ---
async function statsPreview() {
    const st = _g('bot_stats_status'), img = _g('bot_stats_img');
    if (st) st.textContent = '⏳ building chart…';
    try {
        const r = await csrfFetch('/api/admin/stats-preview', { method: 'POST' });
        const d = await r.json().catch(() => ({}));
        if (r.ok && d.image) {
            if (img) { img.src = d.image; img.style.display = ''; }
            if (st) st.textContent = '✓ preview only — not posted';
        } else if (st) st.textContent = '❌ ' + (d.detail || 'failed');
    } catch (e) { if (st) st.textContent = '❌ ' + ((e && e.message) || e); }
}

async function statsRunNow() {
    const st = _g('bot_stats_status');
    const nsec = _g('bot_f_nostr_nsec') ? _g('bot_f_nostr_nsec').value.trim() : '';
    if (!nsec) { if (st) st.textContent = "❌ set this bot's Nostr secret key first"; return; }
    if (!confirm('Post the stats graph to Nostr now, from this bot?')) return;
    if (st) st.textContent = '⏳ posting…';
    try {
        const r = await csrfFetch('/api/admin/stats-run', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ nsec })
        });
        const d = await r.json().catch(() => ({}));
        if (st) st.textContent = r.ok ? ('✅ ' + (d.message || 'posted')) : ('❌ ' + (d.detail || 'failed'));
    } catch (e) { if (st) st.textContent = '❌ ' + ((e && e.message) || e); }
}
