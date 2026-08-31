// Admin Panel JavaScript
// Extracted from admin.html for modularity

// Tab switching. The settings live in ~18 tabs now (grouped in the nav), so the panel also
// REMEMBERS the open tab: a save-and-come-back lands where you left off instead of on LLM, and
// #tab-name in the URL deep-links to one (that's what the moved-section pointers link to).
function showAdminTab(name, opts) {
    const pane = document.getElementById('tab-' + name);
    const btn = document.querySelector('.tab-btn[data-tab="' + name + '"]');
    if (!pane || !btn) return false;

    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    pane.classList.add('active');

    // The global "Save Settings" button belongs to the settings tabs only — a tab that manages its
    // own records instead of settings hides it. (The Users tab, the one such tab, is retired.)
    const saveBtn = document.querySelector('#settingsForm .save-btn');
    if (saveBtn) saveBtn.style.display = (name === 'users') ? 'none' : '';

    try { localStorage.setItem('pcaiAdminTab', name); } catch (_) { /* private mode */ }

    // Mobile: the nav is collapsed behind a "Group › Tab" button — keep it labelled with the tab
    // you're on, and close it again once you've picked one (desktop has no toggle, so this no-ops).
    const label = document.getElementById('tabsToggleLabel');
    if (label) {
        const group = btn.closest('.tab-group')?.querySelector('.tab-group-label')?.textContent.trim();
        label.textContent = (group ? group + ' › ' : '') + btn.textContent.trim();
    }
    if (!(opts && opts.keepNavOpen)) setTabsNavOpen(false);
    return true;
}

// Collapsed-nav toggle (mobile). `.admin-tabs.open` is what the media query keys on.
function setTabsNavOpen(open) {
    const nav = document.getElementById('adminTabs');
    const toggle = document.getElementById('tabsToggle');
    if (!nav || !toggle) return;
    nav.classList.toggle('open', !!open);
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
}
document.getElementById('tabsToggle')?.addEventListener('click', () => {
    const nav = document.getElementById('adminTabs');
    setTabsNavOpen(!(nav && nav.classList.contains('open')));
});

document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        showAdminTab(btn.dataset.tab);
        if (btn.dataset.tab === 'relay') loadRelayIdentity();
    });
});

// Restore the last tab (or a #hash deep-link) once the DOM is up. Every per-tab lazy loader is
// wired to its button's click event, so replay a click rather than calling showAdminTab directly —
// otherwise a restored Emoji/Bots/Storage tab would render empty. The setTimeout matters: those
// loaders ATTACH their click listener inside their own DOMContentLoaded handler, which runs after
// this one (admin.js is the first script), so a click dispatched inline here would hit nothing.
document.addEventListener('DOMContentLoaded', () => {
    setTimeout(() => {
        let want = (location.hash || '').replace(/^#tab-/, '');
        if (!want) { try { want = localStorage.getItem('pcaiAdminTab') || ''; } catch (_) { } }
        if (!want || want === 'ai') return;                   // 'ai' is already the default
        const btn = document.querySelector('.tab-btn[data-tab="' + want + '"]');
        if (btn) btn.click();
    }, 0);
});

// Relay operator identity — fetched lazily when the Relay tab opens; copy buttons read the cache.
let _relayKeys = null;
async function loadRelayIdentity() {
    const el = document.getElementById('relay-npub');
    try {
        const r = await fetch('/api/admin/nostr-relay/identity').then(r => r.json());
        if (r && r.ok) { _relayKeys = { npub: r.npub, nsec: r.nsec }; if (el) el.textContent = r.npub; }
        else if (el) el.textContent = (r && r.error) || '(no operator key)';
    } catch (_) { if (el) el.textContent = '(failed to load)'; }
}
/* THE one copy path for the admin panel. Every Copy button here goes through it.
 *
 * The panel is usually EMBEDDED — the client frames <instance>/admin in an iframe (desktop app, PWA,
 * APK), and that frame is cross-origin. `clipboard-write` defaults to a `self` allowlist, so unless
 * the framer delegates it the async Clipboard API is DENIED in the frame and writeText() rejects.
 * A rejection is asynchronous: `try { navigator.clipboard.writeText(v) } catch (_) {}` does not catch
 * it. That is exactly how the .onion Copy button reported "copied" in the Windows app while the
 * clipboard stayed empty — the sync catch never fired, so the execCommand fallback never ran either.
 * The other half of the same fix delegates the permission (`allow="clipboard-write"` on the admin
 * iframe in static/js/client/app.js), but that only ships with the next desktop build, and a plain
 * HTTP instance (an .onion, a LAN box) has no navigator.clipboard at all — so this must stand alone.
 *
 * So: AWAIT the write, fall back to execCommand over a real focused selection, and if even that is
 * refused leave the value SELECTED and say "press Ctrl+C" — never (await pcPrompt()), which wedges keyboard
 * focus in an Electron window. `srcEl` is the visible input holding the value, when there is one;
 * without it there is nothing on screen to select, so a value the admin would otherwise lose
 * (the relay nsec) still falls back to (await pcPrompt()). Returns whether the clipboard actually got it.
 */
async function copyToClipboard(text, btn, srcEl) {
    const v = String(text == null ? '' : text);
    if (!v) return false;
    let ok = false;
    try { await navigator.clipboard.writeText(v); ok = true; }
    catch (_) { /* insecure context, no API, or the frame was denied clipboard-write */ }
    if (!ok) {
        const el = srcEl || document.createElement('textarea');
        if (!srcEl) { el.value = v; el.style.position = 'fixed'; el.style.top = '0'; el.style.opacity = '0'; document.body.appendChild(el); }
        try { el.focus(); el.select(); ok = document.execCommand('copy'); } catch (_) { }
        if (!srcEl) el.remove();
    }
    if (btn) {
        const o = btn.dataset.copyLabel || btn.textContent;
        btn.dataset.copyLabel = o;
        btn.textContent = ok ? '✓ copied' : '⚠ press Ctrl+C';
        setTimeout(() => { btn.textContent = o; }, 2000);
    }
    if (!ok && srcEl) { try { srcEl.focus(); srcEl.select(); } catch (_) { } }   // leave it ready for Ctrl+C
    return ok;
}
async function copyRelayKey(which) {
    const btn = (typeof event !== 'undefined') && event.target;
    if (!_relayKeys) await loadRelayIdentity();   // load on demand if the tab fetch hasn't run
    const v = _relayKeys && _relayKeys[which];
    if (!v) { pcAlert('No ' + which + ' available (no operator key).'); return; }
    const ok = await copyToClipboard(v, btn);
    if (!ok) window.prompt('Copy the ' + which + ' manually:', v);   // nothing on screen to select
}

// On-demand model download (kind = chat | image | music). Models aren't auto-downloaded; this
// fires the background download and polls status so the admin SEES completion (✓) or errors (✗).
async function downloadModel(kind, btnId, statusId){
    const btn = document.getElementById(btnId), st = document.getElementById(statusId);
    if (btn) btn.disabled = true;
    if (st){ st.textContent = 'Starting…'; st.style.color = '#9fa1c6'; }
    try { await fetch('/api/admin/models/' + kind + '/download', { method: 'POST' }); }
    catch (e){ if (st){ st.textContent = '✗ ' + e; st.style.color = '#ff6b6b'; } if (btn) btn.disabled = false; return; }
    const poll = async () => {
        let s;
        try { s = await fetch('/api/admin/models/' + kind + '/status').then(r => r.json()); }
        catch (_) { s = { state: 'error', message: 'status check failed' }; }
        if (s.state === 'running'){
            if (st){ st.textContent = '⏳ ' + (s.pct != null ? s.pct + '% — ' : '') + (s.message || 'downloading…'); st.style.color = '#00ffff'; }
            setTimeout(poll, 2000);
        } else if (s.state === 'done'){
            if (st){ st.textContent = '✓ ' + (s.message || 'done'); st.style.color = '#3ddc84'; }
            if (btn) btn.disabled = false;
        } else if (s.state === 'error'){
            if (st){ st.textContent = '✗ ' + (s.message || 'error'); st.style.color = '#ff6b6b'; }
            if (btn) btn.disabled = false;
        } else { if (st) st.textContent = ''; if (btn) btn.disabled = false; }
    };
    setTimeout(poll, 800);
}

// Default news sources
const DEFAULT_NEWS_SOURCES = `drudgereport.com|Drudge Report
npr.org/sections/news|NPR
nypost.com|NY Post
foxnews.com|Fox News
newsweek.com|Newsweek`;

// Store loaded values to detect changes
const loadedValues = new Map();

// Load settings
// Gate the "Use local Bot API server" checkbox: it can only be turned ON after
// a successful "Test Local Server" (so you can't enable it before the daemon is
// running). It can always be turned OFF if currently on.
function applyLocalApiGate(unlocked) {
    const chk = document.getElementById('telegram_local_api');
    const label = document.getElementById('telegram_local_api_label');
    const hint = document.getElementById('telegram_local_api_hint');
    if (!chk) return;
    const allow = unlocked || chk.checked;
    chk.disabled = !allow;
    if (label) label.style.opacity = allow ? '1' : '0.5';
    if (hint) {
        if (chk.checked) {
            hint.innerHTML = 'Local Bot API server is in use. Uncheck to return to the cloud API (20&nbsp;MB cap).';
        } else if (allow) {
            hint.innerHTML = '✅ Test passed — tick this, then <b>Save Settings</b> and <b>Setup Webhook</b>.';
        } else {
            hint.innerHTML = '🔒 Locked until <b>Test Local Server</b> succeeds (step&nbsp;3) — this prevents enabling it before the server is running.';
        }
    }
}

async function loadSettings() {
    try {
        const response = await fetch('/api/admin/settings');
        if (response.ok) {
            const settings = await response.json();
            for (const [key, value] of Object.entries(settings)) {
                const el = document.getElementById(key);
                if (el) {
                    // Store the loaded value (from database) for comparison later
                    const dbValue = value !== null && value !== undefined ? String(value) : '';
                    loadedValues.set(key, dbValue);
                    
                    if (el.type === 'checkbox') {
                        el.checked = value === 'true' || value === true;
                        loadedValues.set(key, el.checked ? 'true' : 'false');
                    } else if (el.tagName === 'SELECT') {
                        el.value = value || el.options[0].value;
                        loadedValues.set(key, el.value);
                    } else if (el.type === 'number') {
                        // For number inputs, handle empty/null values explicitly
                        // If database has a value, use it; otherwise clear the field (don't use HTML default)
                        if (dbValue !== '') {
                            el.value = dbValue;
                            loadedValues.set(key, dbValue);
                        } else {
                            // Database has no value - clear the field (remove HTML default)
                            // This prevents accidentally saving the HTML default value
                            el.value = '';
                            loadedValues.set(key, ''); // Empty string means "not in database"
                        }
                    } else {
                        // Always use database value, even if empty
                        // This ensures database values take precedence over HTML defaults
                        el.value = dbValue;
                    }
                }
            }
        }

        // Lock/unlock the local Bot API toggle based on the loaded state.
        applyLocalApiGate(false);
    } catch (err) {
        console.error('Failed to load settings:', err);
    }
}

// WebDAV sync client code removed

// Reset news sources to defaults (Tools tab)
document.getElementById('resetNewsSourcesBtn')?.addEventListener('click', () => {
    document.getElementById('news_sources').value = DEFAULT_NEWS_SOURCES;
});

// Save settings - send all named form values so DB stays in sync (fixes missed updates when only "changed" fields were sent)
document.getElementById('settingsForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const form = e.target;
    const settings = {};
    for (const el of form.querySelectorAll('input, textarea, select')) {
        if (el.name) {
            let currentValue;
            if (el.type === 'checkbox') {
                currentValue = el.checked ? 'true' : 'false';
            } else {
                currentValue = el.value || '';
            }
            const loadedValue = loadedValues.get(el.name);
            // Send field if: we have a value from DB (so form was populated and we persist current state), or it's a new/changed value
            if (loadedValue !== undefined) {
                settings[el.name] = currentValue;
            } else if (el.type === 'number' && currentValue !== '' && currentValue !== el.getAttribute('value')) {
                settings[el.name] = currentValue;
            } else if (loadedValue === undefined && currentValue !== '') {
                settings[el.name] = currentValue;
            }
        }
    }

    try {
        const response = await csrfFetch('/api/admin/settings', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ settings })
        });
        if (response.ok) {
            pcAlert('Settings saved!');
        } else {
            pcAlert('Failed to save settings');
        }
    } catch (err) {
        pcAlert('Error saving settings');
    }
});

// Load users
async function loadUsers() {
    try {
        const response = await fetch('/api/admin/users');
        if (response.ok) {
            const users = await response.json();
            const list = document.getElementById('userList');
            if (!list) return;   // Users tab retired — accounts managed from the Nostr client
            list.innerHTML = users.map(u => {
                const quota_mb = u.storage_quota > 0 ? (u.storage_quota / (1024 * 1024)).toFixed(1) : '∞';
                return `
                <div class="user-item">
                    <div class="user-item-header">
                        <span class="username">${escapeHtml(u.username)}</span>
                        <span class="badge ${u.is_admin ? 'admin' : ''}">${u.is_admin ? 'Admin' : 'User'}</span>
                        <div class="user-item-actions">
                            <button class="btn-secondary btn-small" onclick="resetPassword(${u.id}, '${escapeHtml(u.username)}')">Reset Password</button>
                            <button class="btn-danger btn-small" onclick="deleteUser(${u.id})">Delete</button>
                        </div>
                    </div>
                    <div class="user-item-controls">
                        <div class="user-control-row">
                            <span class="user-control-label">Quota</span>
                            <input type="number" id="quota_${u.id}" class="quota-input"
                                   value="${quota_mb === '∞' ? '0' : quota_mb}" step="0.1" min="0"
                                   placeholder="MB (0=unlimited)">
                            <button class="btn-secondary btn-small" onclick="updateStorageQuota(${u.id}, '${escapeHtml(u.username)}')">Set</button>
                            <button class="btn-secondary btn-small" onclick="generateThumbnailsForUser(${u.id}, '${escapeHtml(u.username)}')" title="Generate thumbnails for this user's images">🖼️ Thumbnails</button>
                        </div>
                        <div class="user-control-row">
                            <span class="user-control-label">Access</span>
                            <label class="cap-toggle"><input type="checkbox" id="cap_image_${u.id}" ${u.can_image ? 'checked' : ''}> 🖼️ Image</label>
                            <label class="cap-toggle"><input type="checkbox" id="cap_music_${u.id}" ${u.can_music ? 'checked' : ''}> 🎵 Music</label>
                            <label class="cap-toggle"><input type="checkbox" id="cap_video_${u.id}" ${u.can_video ? 'checked' : ''}> 🎬 Video</label>
                            <label class="cap-toggle"><input type="checkbox" id="cap_torrent_${u.id}" ${u.can_torrent ? 'checked' : ''}> 🧲 Torrent</label>
                            <label class="cap-toggle"><input type="checkbox" id="cap_blossom_${u.id}" ${u.can_blossom ? 'checked' : ''}> 🌸 Blossom</label>
                            <label class="cap-toggle"><input type="checkbox" id="cap_stream_${u.id}" ${u.can_stream ? 'checked' : ''}> 🔴 Go Live</label>
                            <label class="cap-toggle"><input type="checkbox" id="cap_ai_${u.id}" ${u.can_ai ? 'checked' : ''}> 🤖 AI</label>
                            <button class="btn-secondary btn-small" onclick="updateCapabilities(${u.id}, '${escapeHtml(u.username)}')">Save Access</button>
                            ${u.is_admin ? '<span class="cap-note">admin: always allowed</span>' : ''}
                        </div>
                    </div>
                </div>
            `;
            }).join('');
        }
    } catch (err) {
        console.error('Failed to load users:', err);
    }
}

// Reset password
async function resetPassword(userId, username) {
    const newPassword = (await pcPrompt(`Enter new password for ${username}:`));
    if (!newPassword) return;

    try {
        const response = await csrfFetch(`/api/admin/users/${userId}/password`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ password: newPassword })
        });
        if (response.ok) {
            pcAlert('Password updated!');
        } else {
            const data = await response.json();
            pcAlert(data.detail || 'Failed to update password');
        }
    } catch (err) {
        pcAlert('Error updating password');
    }
}

// Delete user
async function deleteUser(id) {
    if (!(await pcConfirm('Delete this user?'))) return;
    try {
        const response = await csrfFetch(`/api/admin/users/${id}`, { method: 'DELETE' });
        if (response.ok) {
            loadUsers();
        } else {
            const data = await response.json();
            pcAlert(data.detail || 'Failed to delete user');
        }
    } catch (err) {
        pcAlert('Error deleting user');
    }
}

// Storage scanning moved to user-level - users can scan their own storage from User Settings

async function updateStorageQuota(userId, username) {
    const quotaInput = document.getElementById(`quota_${userId}`);
    const quota_mb = parseFloat(quotaInput.value);
    
    if (isNaN(quota_mb) || quota_mb < 0) {
        pcAlert('Please enter a valid quota (MB, 0 for unlimited)');
        return;
    }
    
    try {
        const response = await csrfFetch(`/api/admin/users/${userId}/storage-quota?quota_mb=${quota_mb}`, {
            method: 'PUT'
        });
        if (response.ok) {
            pcAlert(`Storage quota updated for ${username}: ${quota_mb === 0 ? 'Unlimited' : quota_mb + 'MB'}`);
            loadUsers();
        } else {
            const data = await response.json();
            pcAlert(data.detail || 'Failed to update quota');
            loadUsers(); // Reload to reset
        }
    } catch (err) {
        pcAlert('Error updating quota');
        loadUsers();
    }
}

async function updateCapabilities(userId, username) {
    const cap = (k) => document.getElementById(`cap_${k}_${userId}`).checked;
    const params = new URLSearchParams({
        can_image: cap('image'),
        can_music: cap('music'),
        can_video: cap('video'),
        can_torrent: cap('torrent'),
        can_blossom: cap('blossom'),
        can_stream: cap('stream'),
        can_ai: cap('ai'),
    });
    try {
        const response = await csrfFetch(`/api/admin/users/${userId}/capabilities?${params.toString()}`, {
            method: 'PUT'
        });
        if (response.ok) {
            pcAlert(`Access updated for ${username}`);
            loadUsers();
        } else {
            const data = await response.json().catch(() => ({}));
            pcAlert(data.detail || 'Failed to update access');
            loadUsers();
        }
    } catch (err) {
        pcAlert('Error updating access');
        loadUsers();
    }
}

// Storage scanning moved to user-level - users can scan their own storage from User Settings
// Removed rescanUserStorage function - users should use the scan button in their own User Settings

async function generateThumbnailsForUser(userId, username) {
    if (!(await pcConfirm(`Generate thumbnails for all images for user "${username}"? This may take a moment if there are many images.`))) {
        return;
    }
    
    try {
        const response = await csrfFetch(`/api/admin/generate-thumbnails?user_id=${userId}`, {
            method: 'POST'
        });
        
        if (response.ok) {
            const data = await response.json();
            if (data.results && data.results.length > 0) {
                const result = data.results[0];
                if (result.status === 'success') {
                    pcAlert(`Thumbnails generated for ${username}:\n• ${result.successful.toLocaleString()} generated\n• ${result.failed.toLocaleString()} failed`);
                } else {
                    pcAlert(`Error rescanning storage for ${username}: ${result.error || 'Unknown error'}`);
                }
            } else {
                pcAlert(data.message || 'Storage rescanned');
            }
        } else {
            const error = await response.json();
            pcAlert(`Error: ${error.detail || 'Failed to rescan storage'}`);
        }
    } catch (err) {
        console.error('Storage rescan error:', err);
        pcAlert(`Error: ${err.message}`);
    }
}

// Generate thumbnails for a specific user
async function generateThumbnailsForUser(userId, username) {
    if (!(await pcConfirm(`Generate thumbnails for all images for user "${username}"? This may take a moment if there are many images.`))) {
        return;
    }
    
    try {
        const response = await csrfFetch(`/api/admin/generate-thumbnails?user_id=${userId}`, {
            method: 'POST'
        });
        
        if (response.ok) {
            const data = await response.json();
            if (data.results && data.results.length > 0) {
                const result = data.results[0];
                if (result.status === 'success') {
                    pcAlert(`Thumbnails generated for ${username}:\n• ${result.successful.toLocaleString()} generated\n• ${result.failed.toLocaleString()} failed`);
                } else {
                    pcAlert(`Error generating thumbnails for ${username}: ${result.error || 'Unknown error'}`);
                }
            } else {
                pcAlert(data.message || 'Thumbnail generation completed');
            }
        } else {
            const error = await response.json();
            pcAlert(`Error: ${error.detail || 'Failed to generate thumbnails'}`);
        }
    } catch (err) {
        console.error('Thumbnail generation error:', err);
        pcAlert(`Error: ${err.message}`);
    }
}

// Create user
// Users tab retired — guard so a missing #createUserForm doesn't abort the whole script.
document.getElementById('createUserForm')?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const username = document.getElementById('newUsername').value;
    const password = document.getElementById('newPassword').value;
    const is_admin = document.getElementById('newIsAdmin').checked;

    try {
        const response = await csrfFetch('/api/admin/users', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password, is_admin })
        });
        if (response.ok) {
            document.getElementById('newUsername').value = '';
            document.getElementById('newPassword').value = '';
            document.getElementById('newIsAdmin').checked = false;
            loadUsers();
        } else {
            const data = await response.json();
            pcAlert(data.detail || 'Failed to create user');
        }
    } catch (err) {
        pcAlert('Error creating user');
    }
});

// Test email
document.getElementById('sendTestEmailBtn').addEventListener('click', async () => {
    const emailInput = document.getElementById('test_email_address');
    const resultDiv = document.getElementById('testEmailResult');
    const email = emailInput.value.trim();

    if (!email) {
        resultDiv.className = 'test-result error';
        resultDiv.textContent = 'Please enter an email address';
        return;
    }

    resultDiv.className = 'test-result loading';
    resultDiv.textContent = 'Sending test email...';

    try {
        const response = await csrfFetch('/api/admin/test-email', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ to_email: email })
        });

        const data = await response.json();

        if (response.ok) {
            resultDiv.className = 'test-result success';
            resultDiv.textContent = data.message || 'Test email sent successfully!';
        } else {
            resultDiv.className = 'test-result error';
            resultDiv.textContent = data.detail || 'Failed to send test email';
        }
    } catch (err) {
        resultDiv.className = 'test-result error';
        resultDiv.textContent = 'Error: ' + err.message;
    }
});

// Test Telegram connection
document.getElementById('testTelegramBtn').addEventListener('click', async () => {
    const statusDiv = document.getElementById('telegramStatus');
    const tokenInput = document.getElementById('telegram_bot_token');
    const token = tokenInput.value.trim();

    if (!token) {
        statusDiv.className = 'test-result error';
        statusDiv.textContent = 'Please enter a bot token';
        return;
    }

    statusDiv.className = 'test-result loading';
    statusDiv.textContent = 'Testing connection...';

    try {
        const response = await csrfFetch('/api/telegram/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ bot_token: token, enabled: true })
        });

        const data = await response.json();

        if (response.ok) {
            statusDiv.className = 'test-result success';
            statusDiv.textContent = `Connected! Bot: @${data.bot.username}. Token saved.`;
            // Also save the token to settings by calling set-webhook with empty URL
            await csrfFetch('/api/telegram/set-webhook', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ bot_token: token, webhook_url: "", enabled: true })
            });
        } else {
            statusDiv.className = 'test-result error';
            statusDiv.textContent = data.detail || 'Failed to connect to Telegram';
        }
    } catch (err) {
        statusDiv.className = 'test-result error';
        statusDiv.textContent = 'Error: ' + err.message;
    }
});

// Test local Bot API server (reads saved URL + token from settings)
const _testLocalApiBtn = document.getElementById('testLocalApiBtn');
if (_testLocalApiBtn) _testLocalApiBtn.addEventListener('click', async () => {
    const statusDiv = document.getElementById('telegramStatus');
    statusDiv.className = 'test-result loading';
    statusDiv.textContent = 'Pinging local Bot API server… (save the URL + API ID/Hash first)';
    try {
        const response = await csrfFetch('/api/telegram/test-local-api', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        });
        const data = await response.json();
        if (response.ok) {
            statusDiv.className = 'test-result success';
            statusDiv.textContent = `Local server OK at ${data.api_base} — bot @${data.bot.username}. You can now enable "Use local Bot API server", Save, and Setup Webhook.`;
            applyLocalApiGate(true);  // unlock the checkbox
        } else {
            statusDiv.className = 'test-result error';
            statusDiv.textContent = data.detail || 'Local server test failed';
        }
    } catch (err) {
        statusDiv.className = 'test-result error';
        statusDiv.textContent = 'Error: ' + err.message;
    }
});

// Setup Telegram webhook
document.getElementById('setupWebhookBtn').addEventListener('click', async () => {
    const statusDiv = document.getElementById('telegramStatus');
    const tokenInput = document.getElementById('telegram_bot_token');
    const webhookUrl = document.getElementById('telegram_webhook_url').value.trim();
    const token = tokenInput.value.trim();

    if (!webhookUrl) {
        statusDiv.className = 'test-result error';
        statusDiv.textContent = 'Please enter a webhook URL';
        return;
    }

    if (!token) {
        statusDiv.className = 'test-result error';
        statusDiv.textContent = 'Please enter a bot token first';
        return;
    }

    statusDiv.className = 'test-result loading';
    statusDiv.textContent = 'Setting up webhook...';

    try {
        const response = await csrfFetch('/api/telegram/set-webhook', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ bot_token: token, webhook_url: webhookUrl, enabled: true })
        });

        const data = await response.json();

        if (response.ok) {
            statusDiv.className = 'test-result success';
            statusDiv.textContent = 'Webhook configured successfully!';
        } else {
            statusDiv.className = 'test-result error';
            statusDiv.textContent = data.detail || 'Failed to set webhook';
        }
    } catch (err) {
        statusDiv.className = 'test-result error';
        statusDiv.textContent = 'Error: ' + err.message;
    }
});

// View Telegram users
document.getElementById('viewTelegramUsersBtn').addEventListener('click', async () => {
    const statusDiv = document.getElementById('telegramStatus');

    statusDiv.className = 'test-result loading';
    statusDiv.textContent = 'Loading users...';

    try {
        const response = await csrfFetch('/api/telegram/users', {
            method: 'GET'
        });

        const data = await response.json();

        if (response.ok) {
            if (data.length === 0) {
                statusDiv.className = 'test-result success';   // base .test-result is display:none — need a state class to show
                statusDiv.textContent = 'No users have linked Telegram yet';
            } else {
                const userList = data.map(u => `@${u.username}: ${u.telegram_chat_id}`).join('\n');
                statusDiv.className = 'test-result success';
                statusDiv.style.whiteSpace = 'pre-line';        // keep one user per line
                statusDiv.textContent = `Linked users:\n${userList}`;
            }
        } else {
            statusDiv.className = 'test-result error';
            statusDiv.textContent = data.detail || 'Failed to get users';
        }
    } catch (err) {
        statusDiv.className = 'test-result error';
        statusDiv.textContent = 'Error: ' + err.message;
    }
});

// The local LLM is always native llama.cpp now — show both the native settings and the
// generation-parameters section (no backend dropdown to toggle).
function updateBackendUI() {
    const nativeSettings = document.getElementById('native-settings');
    const ollamaSettings = document.getElementById('ollama-settings');
    if (nativeSettings) nativeSettings.style.display = 'block';
    if (ollamaSettings) ollamaSettings.style.display = 'block';
}

// Reload model button
document.getElementById('reloadModelBtn').addEventListener('click', async () => {
    const statusDiv = document.getElementById('modelStatus');
    statusDiv.className = 'model-status loading';
    statusDiv.textContent = 'Reloading model...';
    statusDiv.style.display = 'block';

    try {
        const response = await csrfFetch('/api/admin/reload-model', {
            method: 'POST'
        });

        const data = await response.json();

        if (response.ok) {
            statusDiv.className = 'model-status success';
            statusDiv.textContent = data.message || 'Model reloaded successfully!';
        } else {
            statusDiv.className = 'model-status error';
            statusDiv.textContent = data.detail || 'Failed to reload model';
        }
    } catch (err) {
        statusDiv.className = 'model-status error';
        statusDiv.textContent = 'Error: ' + err.message;
    }
});

// Image generation is always native diffusers now — show the native image settings.
function updateImageBackendUI() {
    const nativeSettings = document.getElementById('native-image-settings');
    if (nativeSettings) nativeSettings.style.display = 'block';
}

// Reload image model button
document.getElementById('reloadImageModelBtn').addEventListener('click', async () => {
    const statusDiv = document.getElementById('imageModelStatus');
    statusDiv.className = 'model-status loading';
    statusDiv.textContent = 'Reloading image model...';
    statusDiv.style.display = 'block';

    try {
        const response = await csrfFetch('/api/admin/reload-image-model', {
            method: 'POST'
        });

        const data = await response.json();

        if (response.ok) {
            statusDiv.className = 'model-status success';
            statusDiv.textContent = data.message || 'Image model reloaded successfully!';
            refreshImageQueue();
        } else {
            statusDiv.className = 'model-status error';
            statusDiv.textContent = data.detail || 'Failed to reload image model';
        }
    } catch (err) {
        statusDiv.className = 'model-status error';
        statusDiv.textContent = 'Error: ' + err.message;
    }
});

// Refresh image queue and VRAM status
async function refreshImageQueue() {
    try {
        // Fetch image status
        const imgResponse = await fetch('/api/admin/image-status');
        if (imgResponse.ok) {
            const data = await imgResponse.json();
            document.getElementById('imageBackendStatus').textContent = data.loaded ? 'Ready' : 'Not loaded';
            document.getElementById('imageModelName').textContent = data.model_path || '-';
            document.getElementById('imageDeviceName').textContent = data.device || '-';
            document.getElementById('imageQueueCount').textContent = (data.queue_size || 0) + ' pending';
        }

        // Fetch VRAM status
        const vramResponse = await fetch('/api/admin/vram-status');
        if (vramResponse.ok) {
            const vram = await vramResponse.json();
            document.getElementById('llmLoadedStatus').textContent = vram.llm_loaded ? 'Yes' : 'No';
            document.getElementById('imageLoadedStatus').textContent = vram.image_loaded ? 'Yes' : 'No';
        }
    } catch (err) {
        console.error('Failed to refresh status:', err);
    }
}

document.getElementById('refreshImageQueueBtn').addEventListener('click', refreshImageQueue);

// Initialize
loadSettings();

// External Storage Management
let allUsers = [];

async function loadUsersForExternalStorage() {
    try {
        const response = await csrfFetch('/api/admin/users');
        if (response.ok) {
            allUsers = await response.json();
            const userSelect = document.getElementById('externalStorageUsers');
            if (userSelect) {
                userSelect.innerHTML = allUsers.map(user => 
                    `<option value="${user.id}">${escapeHtml(user.username || user.email || `User ${user.id}`)}</option>`
                ).join('');
            }
        }
    } catch (err) {
        console.error('Failed to load users:', err);
    }
}

async function loadExternalStorage() {
    try {
        const response = await csrfFetch('/api/admin/external-storage');
        if (response.ok) {
            const mounts = await response.json();
            const listDiv = document.getElementById('externalStorageList');
            if (!listDiv) return;
            
            if (mounts.length === 0) {
                listDiv.innerHTML = '<p style="color: var(--text-secondary);">No external storage mounts configured.</p>';
                return;
            }
            
            listDiv.innerHTML = mounts.map(mount => `
                <div class="external-storage-item" data-id="${mount.id}">
                    <div class="external-storage-info">
                        <div class="external-storage-header">
                            <strong>${escapeHtml(mount.name)}</strong>
                            <span class="external-storage-mount-point">${escapeHtml(mount.mount_point)}</span>
                            ${mount.is_active ? '<span class="badge badge-success">Active</span>' : '<span class="badge badge-inactive">Inactive</span>'}
                        </div>
                        <div class="external-storage-details">
                            <div><strong>Path:</strong> <code>${escapeHtml(mount.mount_path)}</code></div>
                            ${mount.description ? `<div><strong>Description:</strong> ${escapeHtml(mount.description)}</div>` : ''}
                            <div><strong>Allowed Users:</strong> ${mount.allowed_users && mount.allowed_users.length > 0 
                                ? mount.allowed_users.map(u => escapeHtml(u.username || u.email || `User ${u.id}`)).join(', ')
                                : '<span style="color: var(--text-secondary);">None (no access granted)</span>'}</div>
                        </div>
                    </div>
                    <div class="external-storage-actions">
                        <button class="btn-secondary btn-small" onclick="editExternalStorage(${mount.id})" title="Edit">✏️</button>
                        <button class="btn-secondary btn-small" onclick="deleteExternalStorage(${mount.id})" title="Delete">🗑️</button>
                    </div>
                </div>
            `).join('');
        }
    } catch (err) {
        console.error('Failed to load external storage:', err);
    }
}

window.editExternalStorage = async function(id) {
    try {
        // Load users first if not already loaded
        if (allUsers.length === 0) {
            await loadUsersForExternalStorage();
        }
        
        const response = await csrfFetch('/api/admin/external-storage');
        if (response.ok) {
            const mounts = await response.json();
            const mount = mounts.find(m => m.id === id);
            if (!mount) {
                console.error('Mount not found:', id);
                pcAlert('External storage mount not found');
                return;
            }
            
            // Populate form fields
            document.getElementById('externalStorageId').value = mount.id;
            document.getElementById('externalStorageName').value = mount.name || '';
            document.getElementById('externalStorageMountPath').value = mount.mount_path || '';
            document.getElementById('externalStorageMountPoint').value = mount.mount_point || '';
            document.getElementById('externalStorageDescription').value = mount.description || '';
            document.getElementById('externalStorageActive').checked = mount.is_active !== false;
            
            // Set allowed users - ensure userSelect is populated first
            const userSelect = document.getElementById('externalStorageUsers');
            if (userSelect) {
                // Clear previous selections
                Array.from(userSelect.options).forEach(option => {
                    option.selected = false;
                });
                
                // Set selected users if mount has allowed_user_ids
                if (mount.allowed_user_ids && mount.allowed_user_ids.length > 0) {
                    Array.from(userSelect.options).forEach(option => {
                        if (mount.allowed_user_ids.includes(parseInt(option.value))) {
                            option.selected = true;
                        }
                    });
                }
            } else {
                console.error('externalStorageUsers select element not found');
            }
            
            // Clear any previous errors
            const errorDiv = document.getElementById('externalStorageError');
            if (errorDiv) {
                errorDiv.style.display = 'none';
                errorDiv.textContent = '';
            }
            
            document.getElementById('externalStorageModalTitle').textContent = 'Edit External Storage';
            document.getElementById('externalStorageModal').style.display = 'flex';
        } else {
            const error = await response.json();
            console.error('Failed to load external storage:', error);
            pcAlert('Error loading external storage: ' + (error.detail || 'Unknown error'));
        }
    } catch (err) {
        console.error('Failed to edit external storage:', err);
        pcAlert('Error: ' + (err.message || 'Failed to load external storage'));
    }
}

window.deleteExternalStorage = async function(id) {
    if (!(await pcConfirm('Are you sure you want to delete this external storage mount?'))) {
        return;
    }
    
    try {
        const response = await csrfFetch(`/api/admin/external-storage/${id}`, {
            method: 'DELETE'
        });
        if (response.ok) {
            await loadExternalStorage();
            pcAlert('External storage mount deleted');
        } else {
            const error = await response.json();
            pcAlert('Error: ' + (error.detail || 'Failed to delete'));
        }
    } catch (err) {
        pcAlert('Error deleting external storage mount');
    }
}

document.getElementById('addExternalStorageBtn')?.addEventListener('click', async () => {
    document.getElementById('externalStorageId').value = '';
    document.getElementById('externalStorageName').value = '';
    document.getElementById('externalStorageMountPath').value = '';
    document.getElementById('externalStorageMountPoint').value = '';
    document.getElementById('externalStorageDescription').value = '';
    document.getElementById('externalStorageActive').checked = true;
    
    // Clear user selection
    const userSelect = document.getElementById('externalStorageUsers');
    if (userSelect) {
        Array.from(userSelect.options).forEach(option => {
            option.selected = false;
        });
    }
    
    // Load users if not already loaded
    if (allUsers.length === 0) {
        await loadUsersForExternalStorage();
    }
    
    document.getElementById('externalStorageModalTitle').textContent = 'Add External Storage';
    document.getElementById('externalStorageError').style.display = 'none';
    document.getElementById('externalStorageModal').style.display = 'flex';
});

document.getElementById('saveExternalStorageBtn')?.addEventListener('click', async () => {
    const id = document.getElementById('externalStorageId').value;
    const name = document.getElementById('externalStorageName').value.trim();
    const mountPath = document.getElementById('externalStorageMountPath').value.trim();
    const mountPoint = document.getElementById('externalStorageMountPoint').value.trim();
    const description = document.getElementById('externalStorageDescription').value.trim();
    const isActive = document.getElementById('externalStorageActive').checked;
    const userSelect = document.getElementById('externalStorageUsers');
    
    if (!name || !mountPath || !mountPoint) {
        const errorDiv = document.getElementById('externalStorageError');
        errorDiv.textContent = 'Please fill in all required fields';
        errorDiv.style.display = 'block';
        return;
    }
    
    // Get selected user IDs
    const allowedUserIds = userSelect ? Array.from(userSelect.selectedOptions).map(opt => parseInt(opt.value)) : [];
    
    if (allowedUserIds.length === 0) {
        const errorDiv = document.getElementById('externalStorageError');
        errorDiv.textContent = 'Please select at least one user who can access this storage';
        errorDiv.style.display = 'block';
        return;
    }
    
    const errorDiv = document.getElementById('externalStorageError');
    errorDiv.style.display = 'none';
    
    try {
        const data = {
            name,
            mount_path: mountPath,
            mount_point: mountPoint,
            description: description || null,
            is_active: isActive,
            allowed_user_ids: allowedUserIds
        };
        
        let response;
        if (id) {
            // Update
            response = await csrfFetch(`/api/admin/external-storage/${id}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data)
            });
        } else {
            // Create
            response = await csrfFetch('/api/admin/external-storage', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data)
            });
        }
        
        if (response.ok) {
            document.getElementById('externalStorageModal').style.display = 'none';
            await loadExternalStorage();
            pcAlert(id ? 'External storage mount updated' : 'External storage mount created');
        } else {
            const error = await response.json();
            errorDiv.textContent = error.detail || 'Failed to save';
            errorDiv.style.display = 'block';
        }
    } catch (err) {
        errorDiv.textContent = 'Error: ' + err.message;
        errorDiv.style.display = 'block';
    }
});

// Load external storage when the Storage tab is opened (it lived on Services before the split)
document.querySelector('[data-tab="storage"]')?.addEventListener('click', async () => {
    await loadUsersForExternalStorage();
    setTimeout(loadExternalStorage, 100);
});

// Load on page load if the Storage tab is the active one
if (document.getElementById('tab-storage')?.classList.contains('active')) {
    loadUsersForExternalStorage().then(() => loadExternalStorage());
}
loadUsers();


// Update backend UI after settings load
setTimeout(() => {
    updateBackendUI();
    updateImageBackendUI();
    refreshImageQueue();
}, 500);

// ── Ctrl/Cmd+F find-in-page for the Admin UI ──────────────────────────────────────────────────
// The desktop (Electron) app has no built-in browser Find, so provide our own: highlight matches in
// the visible tab, jump between them, live count. Enter = next, Shift+Enter = prev, Esc = close.
(function(){
  let bar=null,input=null,countEl=null,hits=[],cur=-1;
  const scopeEl=()=>document.querySelector('.admin-content')||document.body;
  function clearHits(){
    for(const m of hits){ const t=document.createTextNode(m.textContent); const pn=m.parentNode; if(pn){ pn.replaceChild(t,m); pn.normalize&&pn.normalize(); } }
    hits=[]; cur=-1;
  }
  function build(){
    if(bar) return;
    const st=document.createElement('style');
    st.textContent=`#admin-find{position:fixed;top:12px;right:16px;z-index:99999;display:none;align-items:center;gap:6px;
      background:#12151c;border:1px solid #2a3140;border-radius:10px;padding:6px 8px;box-shadow:0 8px 30px rgba(0,0,0,.5)}
      #admin-find.show{display:flex}
      #admin-find input{background:#0c0e13;border:1px solid #2a3140;border-radius:6px;color:#e6e8ee;padding:5px 8px;font-size:13px;width:180px;outline:none}
      #admin-find input:focus{border-color:#39d0d8}
      #admin-find #admin-find-ct{color:#8a93a6;font-size:12px;min-width:36px;text-align:center}
      #admin-find button{background:none;border:none;color:#8a93a6;cursor:pointer;font-size:13px;padding:3px 6px;border-radius:5px}
      #admin-find button:hover{background:#1c2130;color:#e6e8ee}
      mark.admin-find-hit{background:#664d00;color:#fff;border-radius:2px}
      mark.admin-find-hit.cur{background:#39d0d8;color:#08121a}`;
    document.head.appendChild(st);
    bar=document.createElement('div'); bar.id='admin-find';
    bar.innerHTML='<input type="text" id="admin-find-in" placeholder="Find in page…" autocomplete="off" spellcheck="false">'+
      '<span id="admin-find-ct">0/0</span>'+
      '<button id="admin-find-prev" title="Previous (Shift+Enter)">▲</button>'+
      '<button id="admin-find-next" title="Next (Enter)">▼</button>'+
      '<button id="admin-find-x" title="Close (Esc)">✕</button>';
    document.body.appendChild(bar);
    input=bar.querySelector('#admin-find-in'); countEl=bar.querySelector('#admin-find-ct');
    input.addEventListener('input',()=>run(input.value));
    input.addEventListener('keydown',e=>{ if(e.key==='Enter'){ e.preventDefault(); step(e.shiftKey?-1:1); } else if(e.key==='Escape'){ e.preventDefault(); close(); } });
    bar.querySelector('#admin-find-next').onclick=()=>step(1);
    bar.querySelector('#admin-find-prev').onclick=()=>step(-1);
    bar.querySelector('#admin-find-x').onclick=close;
  }
  function run(q){
    clearHits(); q=(q||'').trim();
    if(!q){ countEl.textContent='0/0'; return; }
    const rx=new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'),'gi');
    const w=document.createTreeWalker(scopeEl(),NodeFilter.SHOW_TEXT,{acceptNode(n){
      if(!n.nodeValue||!n.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
      const pe=n.parentElement; if(!pe) return NodeFilter.FILTER_REJECT;
      if(pe.closest('#admin-find,script,style,noscript,option')) return NodeFilter.FILTER_REJECT;
      // Search INACTIVE tabs too — with the settings spread over ~18 tabs, "which tab is
      // stream_clamp_bitrate on?" is the main thing you'd Ctrl+F for, and paint() switches to the
      // tab holding the current hit. Everything else that's hidden (closed modals, collapsed rows)
      // is still skipped: a hit you can't see and can't reveal is just a dead stop in the list.
      const pane=pe.closest('.tab-content');
      if(pane && !pane.classList.contains('active')){
        const m=pe.closest('.modal');
        return (m && getComputedStyle(m).display==='none') ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
      }
      if(pe.offsetParent===null) return NodeFilter.FILTER_REJECT;   // hidden (display:none)
      return NodeFilter.FILTER_ACCEPT; }});
    const nodes=[]; let n; while(n=w.nextNode()) nodes.push(n);
    for(const node of nodes){
      const txt=node.nodeValue; rx.lastIndex=0; if(!rx.test(txt)) continue; rx.lastIndex=0;
      const frag=document.createDocumentFragment(); let last=0,m;
      while(m=rx.exec(txt)){
        if(m.index>last) frag.appendChild(document.createTextNode(txt.slice(last,m.index)));
        const mk=document.createElement('mark'); mk.className='admin-find-hit'; mk.textContent=m[0];
        frag.appendChild(mk); hits.push(mk); last=m.index+m[0].length;
        if(m.index===rx.lastIndex) rx.lastIndex++;
      }
      if(last<txt.length) frag.appendChild(document.createTextNode(txt.slice(last)));
      node.parentNode.replaceChild(frag,node);
    }
    cur=hits.length?0:-1; paint(); update();
  }
  function paint(){
    hits.forEach((h,i)=>h.classList.toggle('cur',i===cur));
    if(cur<0||!hits[cur]) return;
    // The hit may be on a tab that isn't open — switch to it, then scroll (scrollIntoView on a
    // display:none pane does nothing, which used to look like "Find stopped working").
    const pane=hits[cur].closest('.tab-content');
    if(pane && !pane.classList.contains('active')){
      const btn=document.querySelector('.tab-btn[data-tab="'+pane.id.replace(/^tab-/,'')+'"]');
      if(btn) btn.click();
    }
    hits[cur].scrollIntoView({block:'center',behavior:'smooth'});
  }
  function step(d){ if(!hits.length) return; cur=(cur+d+hits.length)%hits.length; paint(); update(); }
  function update(){ countEl.textContent=(hits.length?cur+1:0)+'/'+hits.length; }
  function open(){ build(); bar.classList.add('show'); input.focus(); input.select(); if(input.value) run(input.value); }
  function close(){ if(bar) bar.classList.remove('show'); clearHits(); if(countEl) countEl.textContent='0/0'; }
  document.addEventListener('keydown',e=>{
    if((e.ctrlKey||e.metaKey)&&!e.altKey&&(e.key==='f'||e.key==='F')){ e.preventDefault(); e.stopPropagation(); open(); }
  }, true);
})();

/* ---- Blossom: does this node hold what it says it holds? -------------------------------------
 *
 * A blob row and the bytes behind it are two different things in two different places — a row in
 * Postgres, a file on a disk that may be another machine — and nothing compared them. When they
 * disagreed the symptom appeared on somebody's phone, as a download that fails on every sweep for
 * ever, and finding out meant reading the access log and hand-querying the database.
 *
 * THE REPAIR ONLY EVER DROPS ROWS. There is nothing in storage to delete — that is what "missing"
 * means — and dropping the row is what lets a client stop being told the file exists. It is offered
 * with the exact list the scan just produced, never a fresh probe: a second look can answer
 * differently in the seconds in between, and this deletes rows.
 */
(function () {
    const $id = (x) => document.getElementById(x);
    let lastScan = null;

    function fmt(n) {
        if (!n) return '0 B';
        const u = ['B', 'KB', 'MB', 'GB', 'TB'];
        let i = 0, v = n;
        while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
        return v.toFixed(v < 10 && i ? 1 : 0) + ' ' + u[i];
    }

    async function scan() {
        const btn = $id('bl_scan_btn'), out = $id('bl_scan_out'), st = $id('bl_scan_status');
        if (!btn) return;
        btn.disabled = true;
        st.textContent = 'scanning…';
        out.innerHTML = '';
        try {
            const r = await csrfFetch('/api/admin/blossom/scan', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ deep: !!($id('bl_scan_deep') || {}).checked })
            });
            const j = await r.json();
            if (!r.ok || !j.ok) throw new Error((j && j.detail) || ('HTTP ' + r.status));
            lastScan = j.scan;
            const s = j.scan;
            const lines = [
                `<div><b>${s.checked}</b> of <b>${s.rows}</b> row(s) checked · <b>${fmt(s.bytes)}</b> accounted for · backend <code>${s.backend}</code>${s.truncated ? ' · truncated' : ''}</div>`
            ];
            if (s.missing.length) {
                lines.push(`<div style="color:var(--danger)">⚠ <b>${s.missing.length}</b> row(s) whose bytes are NOT in storage. Clients are told these files exist and can never fetch them.</div>`);
            }
            if ((s.corrupt || []).length) {
                lines.push(`<div style="color:var(--danger)">⚠ <b>${s.corrupt.length}</b> file(s) whose bytes do not match the hash they are stored under.</div>`);
            }
            if (s.unreadable_store) {
                lines.push('<div style="color:var(--danger)">⚠ the storage directory could not be read at all — nothing here is a verdict about your files. Check the path and any mount, then scan again.</div>');
            }
            if (s.cannot) {
                lines.push(`<div style="color:var(--warning,#e6a700)">⚠ ${s.cannot}</div>`);
            }
            if (s.unknown) {
                lines.push(`<div>${s.unknown} could not be checked — the store did not answer. Not counted as missing.</div>`);
            }
            if (s.orphans) {
                lines.push(`<div>${s.orphans} file(s) (${fmt(s.orphan_bytes)}) in storage with no row. Left alone — a half-finished upload looks the same.</div>`);
            }
            // "nothing was found missing" and "nothing could be asked" are not the same sentence.
            if (!s.missing.length && !(s.corrupt || []).length && s.checked > s.unknown) lines.push('<div>Everything the database claims is there.</div>');
            // Never offered when the store itself could not be read: every row then looks missing.
            if (s.missing.length && !s.unreadable_store) {
                lines.push(`<div style="margin-top:8px"><button type="button" class="btn btn-danger" id="bl_scan_fix">Drop ${s.missing.length} row(s) whose bytes are gone</button></div>`);
            }
            out.innerHTML = lines.join('');
            st.textContent = '';
            const fix = $id('bl_scan_fix');
            if (fix) fix.onclick = forget;
        } catch (e) {
            st.textContent = '';
            out.innerHTML = '<div style="color:var(--danger)">could not scan: ' + (e.message || e) + '</div>';
        } finally {
            btn.disabled = false;
        }
    }

    async function forget() {
        if (!lastScan || !lastScan.missing.length) return;
        const n = lastScan.missing.length;
        if (!(await pcConfirm('Drop ' + n + ' row(s) whose bytes this node does not have?\n\n'
                     + 'Nothing is deleted from storage — there is nothing there to delete. It stops '
                     + 'this node claiming to hold files it does not, so clients can stop retrying '
                     + 'them.'))) return;
        const fix = $id('bl_scan_fix'), out = $id('bl_scan_out');
        fix.disabled = true;
        try {
            const r = await csrfFetch('/api/admin/blossom/forget-missing', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ shas: lastScan.missing })
            });
            const j = await r.json();
            if (!r.ok || !j.ok) throw new Error((j && j.detail) || ('HTTP ' + r.status));
            /* What actually happened, not what was asked. The repair asks the store again about
             * every row before dropping it, so some of them come back present — a scan that ran
             * while a mount was down, a prefix being moved, a re-upload in flight. Saying "dropped
             * N" when it kept some of them is the kind of small lie that costs an afternoon. */
            const bits = ['Dropped ' + j.removed + ' row(s).'];
            if (j.kept) bits.push(j.kept + ' turned out to be there and were left alone.');
            if (j.unknown) bits.push(j.unknown + ' could not be checked again and were left alone.');
            if (j.refused) bits.push('Refused: ' + j.refused);
            out.innerHTML = '<div>' + bits.join(' ') + ' Run the scan again to confirm.</div>';
            lastScan = null;
        } catch (e) {
            out.innerHTML = '<div style="color:var(--danger)">could not drop those: ' + (e.message || e) + '</div>';
        }
    }

    document.addEventListener('DOMContentLoaded', function () {
        const b = $id('bl_scan_btn');
        if (b) b.onclick = scan;
    });
})();
