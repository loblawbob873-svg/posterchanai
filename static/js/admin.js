// Admin Panel JavaScript
// Extracted from admin.html for modularity

// Tab switching
document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        // Update button states
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');

        // Update content
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
        document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
    });
});

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

// Reset news sources to defaults
document.getElementById('resetNewsSourcesBtn').addEventListener('click', () => {
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
            // Apply MCP settings (start/stop based on enabled setting)
            try {
                const mcpResponse = await csrfFetch('/api/admin/mcp-apply', { method: 'POST' });
                const mcpData = await mcpResponse.json();
                // Update MCP status display
                const statusDiv = document.getElementById('mcpStatus');
                if (mcpData.message && mcpData.message !== 'No change needed') {
                    statusDiv.className = 'model-status success';
                    statusDiv.textContent = mcpData.message;
                    statusDiv.style.display = 'block';
                    alert('Settings saved! ' + mcpData.message);
                } else {
                    alert('Settings saved!');
                }
            } catch (mcpErr) {
                alert('Settings saved! (MCP apply failed: ' + mcpErr.message + ')');
            }
        } else {
            alert('Failed to save settings');
        }
    } catch (err) {
        alert('Error saving settings');
    }
});

// Load users
async function loadUsers() {
    try {
        const response = await fetch('/api/admin/users');
        if (response.ok) {
            const users = await response.json();
            const list = document.getElementById('userList');
            list.innerHTML = users.map(u => {
                const quota_mb = u.storage_quota > 0 ? (u.storage_quota / (1024 * 1024)).toFixed(1) : '∞';
                return `
                <div class="user-item">
                    <span class="username">${escapeHtml(u.username)}</span>
                    <span class="badge ${u.is_admin ? 'admin' : ''}">${u.is_admin ? 'Admin' : 'User'}</span>

                    <div class="storage-quota-control" style="display: inline-flex; align-items: center; gap: 8px;">
                        <label style="font-size: 0.9em;">Quota:</label>
                        <input type="number" 
                               id="quota_${u.id}" 
                               value="${quota_mb === '∞' ? '0' : quota_mb}" 
                               step="0.1" 
                               min="0" 
                               style="width: 80px; padding: 4px;"
                               placeholder="MB (0=unlimited)">
                        <button class="btn-secondary btn-small" onclick="updateStorageQuota(${u.id}, '${escapeHtml(u.username)}')">Set</button>
                        <!-- Storage scanning moved to user-level - users can scan from User Settings -->
                        <button class="btn-secondary btn-small" onclick="generateThumbnailsForUser(${u.id}, '${escapeHtml(u.username)}')" title="Generate thumbnails for this user's images">🖼️ Thumbnails</button>
                    </div>
                    <button class="btn-secondary btn-small" onclick="resetPassword(${u.id}, '${escapeHtml(u.username)}')">Reset Password</button>
                    <button class="btn-danger btn-small" onclick="deleteUser(${u.id})">Delete</button>
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
    const newPassword = prompt(`Enter new password for ${username}:`);
    if (!newPassword) return;

    try {
        const response = await csrfFetch(`/api/admin/users/${userId}/password`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ password: newPassword })
        });
        if (response.ok) {
            alert('Password updated!');
        } else {
            const data = await response.json();
            alert(data.detail || 'Failed to update password');
        }
    } catch (err) {
        alert('Error updating password');
    }
}

// Delete user
async function deleteUser(id) {
    if (!confirm('Delete this user?')) return;
    try {
        const response = await csrfFetch(`/api/admin/users/${id}`, { method: 'DELETE' });
        if (response.ok) {
            loadUsers();
        } else {
            const data = await response.json();
            alert(data.detail || 'Failed to delete user');
        }
    } catch (err) {
        alert('Error deleting user');
    }
}

// Storage scanning moved to user-level - users can scan their own storage from User Settings

async function updateStorageQuota(userId, username) {
    const quotaInput = document.getElementById(`quota_${userId}`);
    const quota_mb = parseFloat(quotaInput.value);
    
    if (isNaN(quota_mb) || quota_mb < 0) {
        alert('Please enter a valid quota (MB, 0 for unlimited)');
        return;
    }
    
    try {
        const response = await csrfFetch(`/api/admin/users/${userId}/storage-quota?quota_mb=${quota_mb}`, {
            method: 'PUT'
        });
        if (response.ok) {
            alert(`Storage quota updated for ${username}: ${quota_mb === 0 ? 'Unlimited' : quota_mb + 'MB'}`);
            loadUsers();
        } else {
            const data = await response.json();
            alert(data.detail || 'Failed to update quota');
            loadUsers(); // Reload to reset
        }
    } catch (err) {
        alert('Error updating quota');
        loadUsers();
    }
}

// Storage scanning moved to user-level - users can scan their own storage from User Settings
// Removed rescanUserStorage function - users should use the scan button in their own User Settings

async function generateThumbnailsForUser(userId, username) {
    if (!confirm(`Generate thumbnails for all images for user "${username}"? This may take a moment if there are many images.`)) {
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
                    alert(`Thumbnails generated for ${username}:\n• ${result.successful.toLocaleString()} generated\n• ${result.failed.toLocaleString()} failed`);
                } else {
                    alert(`Error rescanning storage for ${username}: ${result.error || 'Unknown error'}`);
                }
            } else {
                alert(data.message || 'Storage rescanned');
            }
        } else {
            const error = await response.json();
            alert(`Error: ${error.detail || 'Failed to rescan storage'}`);
        }
    } catch (err) {
        console.error('Storage rescan error:', err);
        alert(`Error: ${err.message}`);
    }
}

// Generate thumbnails for a specific user
async function generateThumbnailsForUser(userId, username) {
    if (!confirm(`Generate thumbnails for all images for user "${username}"? This may take a moment if there are many images.`)) {
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
                    alert(`Thumbnails generated for ${username}:\n• ${result.successful.toLocaleString()} generated\n• ${result.failed.toLocaleString()} failed`);
                } else {
                    alert(`Error generating thumbnails for ${username}: ${result.error || 'Unknown error'}`);
                }
            } else {
                alert(data.message || 'Thumbnail generation completed');
            }
        } else {
            const error = await response.json();
            alert(`Error: ${error.detail || 'Failed to generate thumbnails'}`);
        }
    } catch (err) {
        console.error('Thumbnail generation error:', err);
        alert(`Error: ${err.message}`);
    }
}

// Create user
document.getElementById('createUserForm').addEventListener('submit', async (e) => {
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
            alert(data.detail || 'Failed to create user');
        }
    } catch (err) {
        alert('Error creating user');
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
                statusDiv.className = 'test-result';
                statusDiv.textContent = 'No users have linked Telegram yet';
            } else {
                const userList = data.map(u => `@${u.username}: ${u.telegram_chat_id}`).join('\n');
                statusDiv.className = 'test-result';
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

// Reload embedding model button (for RAG)
document.getElementById('reloadEmbeddingModelBtn').addEventListener('click', async () => {
    const statusDiv = document.getElementById('embeddingModelStatus');
    statusDiv.className = 'model-status loading';
    statusDiv.textContent = 'Reloading embedding model...';
    statusDiv.style.display = 'block';

    try {
        const response = await csrfFetch('/api/admin/reload-embedding-model', {
            method: 'POST'
        });

        const data = await response.json();

        if (response.ok) {
            statusDiv.className = 'model-status success';
            statusDiv.textContent = data.message || 'Embedding model reloaded successfully!';
        } else {
            statusDiv.className = 'model-status error';
            statusDiv.textContent = data.detail || 'Failed to reload embedding model';
        }
    } catch (err) {
        statusDiv.className = 'model-status error';
        statusDiv.textContent = 'Error: ' + err.message;
    }
});

// Clear RAG caches button
document.getElementById('clearRagCacheBtn').addEventListener('click', async () => {
    const statusDiv = document.getElementById('ragCacheStatus');
    statusDiv.className = 'model-status loading';
    statusDiv.textContent = 'Clearing RAG caches...';
    statusDiv.style.display = 'block';

    try {
        const response = await csrfFetch('/api/admin/clear-rag-cache', {
            method: 'POST'
        });

        const data = await response.json();

        if (response.ok) {
            statusDiv.className = 'model-status success';
            statusDiv.textContent = data.message || 'RAG caches cleared successfully!';
        } else {
            statusDiv.className = 'model-status error';
            statusDiv.textContent = data.detail || 'Failed to clear RAG caches';
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

// MCP Server controls
document.getElementById('mcpRestartBtn').addEventListener('click', async () => {
    const statusDiv = document.getElementById('mcpStatus');
    statusDiv.className = 'model-status loading';
    statusDiv.textContent = 'Restarting MCP server...';
    statusDiv.style.display = 'block';

    try {
        const response = await csrfFetch('/api/admin/mcp-restart', { method: 'POST' });
        const data = await response.json();

        if (response.ok && data.success) {
            statusDiv.className = 'model-status success';
            statusDiv.textContent = data.message || 'MCP server restarted!';
        } else {
            statusDiv.className = 'model-status error';
            statusDiv.textContent = data.message || data.detail || 'Failed to restart MCP server';
        }
    } catch (err) {
        statusDiv.className = 'model-status error';
        statusDiv.textContent = 'Error: ' + err.message;
    }
});

document.getElementById('mcpWarmupBtn').addEventListener('click', async () => {
    const statusDiv = document.getElementById('mcpStatus');
    statusDiv.className = 'model-status loading';
    statusDiv.textContent = 'Running warmup...';
    statusDiv.style.display = 'block';

    try {
        const response = await csrfFetch('/api/admin/mcp-warmup', { method: 'POST' });
        const data = await response.json();

        if (response.ok && data.success) {
            const result = data.result || {};
            statusDiv.className = 'model-status success';
            statusDiv.textContent = `Warmup complete: ${result.collections_loaded || 0} collections, ${result.chunks_cached || 0} chunks cached`;
        } else {
            statusDiv.className = 'model-status error';
            statusDiv.textContent = data.detail || 'Failed to warmup';
        }
    } catch (err) {
        statusDiv.className = 'model-status error';
        statusDiv.textContent = 'Error: ' + err.message;
    }
});

document.getElementById('mcpStatusBtn').addEventListener('click', async () => {
    const statusDiv = document.getElementById('mcpStatus');
    statusDiv.className = 'model-status loading';
    statusDiv.textContent = 'Checking status...';
    statusDiv.style.display = 'block';

    try {
        const response = await fetch('/api/admin/mcp-status');
        const data = await response.json();

        if (response.ok) {
            const running = data.running ? 'Running' : 'Stopped';
            const warmup = data.warmup_complete ? 'Yes' : 'No';
            const cacheSize = data.mcp_query_cache_size || 0;
            const docChunks = data.document_cache?.chunks || 0;
            statusDiv.className = 'model-status success';
            statusDiv.innerHTML = `<strong>Status:</strong> ${running}<br>
                <strong>Warmed up:</strong> ${warmup}<br>
                <strong>Query cache:</strong> ${cacheSize} entries<br>
                <strong>Document cache:</strong> ${docChunks} chunks`;
        } else {
            statusDiv.className = 'model-status error';
            statusDiv.textContent = data.detail || 'Failed to get status';
        }
    } catch (err) {
        statusDiv.className = 'model-status error';
        statusDiv.textContent = 'Error: ' + err.message;
    }
});

// === RAG Management ===

let currentWizardMethod = null;
let uploadedFile = null;

// Load collections
async function loadRagCollections() {
    try {
        const response = await fetch('/api/rag/collections');
        if (response.ok) {
            const collections = await response.json();
            const list = document.getElementById('ragCollectionList');

            if (collections.length === 0) {
                list.innerHTML = '<div class="rag-empty">No codebases indexed yet.<br>Click "Add Codebase" to get started.</div>';
                return;
            }

            list.innerHTML = collections.map(c => `
                <div class="rag-collection-item" data-id="${c.id}" data-type="${c.collection_type}" data-source="${escapeHtml(c.source_path || '')}" data-branch="${escapeHtml(c.git_branch || '')}" data-patterns="${escapeHtml(c.file_patterns || '')}">
                    <div class="rag-collection-header">
                        <span class="rag-collection-name">${escapeHtml(c.name)}</span>
                        <span class="rag-collection-type">${c.collection_type}</span>
                    </div>
                    <div class="rag-collection-stats">
                        <span>📄 ${c.document_count || 0} files</span>
                        ${c.collection_type === 'git' ? `<span>🌿 ${escapeHtml(c.git_branch || 'main')}</span>` : ''}
                        <span>📅 ${c.last_indexed_at ? new Date(c.last_indexed_at).toLocaleDateString() : 'Not indexed'}</span>
                    </div>
                    ${c.source_path ? `<div class="rag-collection-source" style="font-size: 12px; color: var(--text-secondary); margin-bottom: 10px; word-break: break-all;">📎 ${escapeHtml(c.source_path)}</div>` : ''}
                    <div class="rag-collection-actions">
                        <button class="btn-secondary" onclick="editCollection(${c.id})">Edit</button>
                        ${c.collection_type === 'watcher' ? `<button class="btn-secondary" onclick="showApiKey(${c.id})">Show API Key</button>` : ''}
                        ${c.collection_type === 'git' ? `<button class="btn-secondary" onclick="pullCollection(${c.id})">Pull & Re-index</button>` : ''}
                        <button class="btn-secondary" onclick="reindexCollection(${c.id})">Re-index</button>
                        <button class="btn-danger" onclick="deleteCollection(${c.id})">Delete</button>
                    </div>
                </div>
            `).join('');
        }
    } catch (err) {
        console.error('Failed to load RAG collections:', err);
    }
}

// Wizard controls
document.getElementById('addCodebaseBtn').addEventListener('click', () => {
    document.getElementById('ragWizard').style.display = 'block';
    document.getElementById('wizardStep1').style.display = 'block';
    document.getElementById('wizardStep2').style.display = 'none';
    document.getElementById('wizardStep3').style.display = 'none';
    document.getElementById('addCodebaseBtn').style.display = 'none';
});

document.getElementById('closeWizard').addEventListener('click', closeWizard);
document.getElementById('wizardBack').addEventListener('click', () => {
    document.getElementById('wizardStep1').style.display = 'block';
    document.getElementById('wizardStep2').style.display = 'none';
    hideAllForms();
});
document.getElementById('wizardDone').addEventListener('click', () => {
    closeWizard();
    loadRagCollections();
});

function closeWizard() {
    document.getElementById('ragWizard').style.display = 'none';
    document.getElementById('addCodebaseBtn').style.display = 'block';
    hideAllForms();
    clearWizardForms();
}

function hideAllForms() {
    document.getElementById('wizardGitForm').style.display = 'none';
    document.getElementById('wizardUploadForm').style.display = 'none';
    document.getElementById('wizardVscodeForm').style.display = 'none';
}

function clearWizardForms() {
    document.getElementById('wizardGitUrl').value = '';
    document.getElementById('wizardGitBranch').value = 'main';
    document.getElementById('wizardGitName').value = '';
    document.getElementById('wizardUploadName').value = '';
    document.getElementById('wizardVscodeName').value = '';
    document.getElementById('wizardFileName').textContent = '';
    document.getElementById('wizardStatus').className = 'rag-status-message';
    uploadedFile = null;
}

// Method selection
document.querySelectorAll('.wizard-option').forEach(btn => {
    btn.addEventListener('click', () => {
        currentWizardMethod = btn.dataset.method;
        document.getElementById('wizardStep1').style.display = 'none';
        document.getElementById('wizardStep2').style.display = 'block';
        hideAllForms();

        if (currentWizardMethod === 'git') {
            document.getElementById('wizardStep2Title').textContent = 'Clone Git Repository';
            document.getElementById('wizardGitForm').style.display = 'flex';
        } else if (currentWizardMethod === 'upload') {
            document.getElementById('wizardStep2Title').textContent = 'Upload Zip File';
            document.getElementById('wizardUploadForm').style.display = 'flex';
        } else if (currentWizardMethod === 'vscode') {
            document.getElementById('wizardStep2Title').textContent = 'VS Code Extension';
            document.getElementById('wizardVscodeForm').style.display = 'flex';
        }
    });
});

// File upload handling
const dropzone = document.getElementById('wizardDropzone');
const fileInput = document.getElementById('wizardUploadFile');

dropzone.addEventListener('click', () => fileInput.click());
dropzone.addEventListener('dragover', (e) => { e.preventDefault(); dropzone.classList.add('dragover'); });
dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));
dropzone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropzone.classList.remove('dragover');
    if (e.dataTransfer.files.length) handleFileSelect(e.dataTransfer.files[0]);
});
fileInput.addEventListener('change', () => {
    if (fileInput.files.length) handleFileSelect(fileInput.files[0]);
});

function handleFileSelect(file) {
    if (!file.name.endsWith('.zip')) {
        alert('Please select a .zip file');
        return;
    }
    uploadedFile = file;
    document.getElementById('wizardFileName').textContent = file.name;
    document.getElementById('wizardUploadSubmit').disabled = false;
}

// Submit handlers
document.getElementById('wizardGitSubmit').addEventListener('click', async () => {
    const statusDiv = document.getElementById('wizardStatus');
    const gitUrl = document.getElementById('wizardGitUrl').value.trim();
    const branch = document.getElementById('wizardGitBranch').value.trim() || 'main';
    const name = document.getElementById('wizardGitName').value.trim() || gitUrl.split('/').pop().replace('.git', '');
    const patterns = document.getElementById('wizardGitPatterns').value.trim();

    if (!gitUrl) {
        statusDiv.className = 'rag-status-message error';
        statusDiv.textContent = 'Please enter a repository URL';
        return;
    }

    statusDiv.className = 'rag-status-message loading';
    statusDiv.textContent = 'Cloning repository...';

    try {
        const response = await csrfFetch('/api/rag/collections/git', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, git_url: gitUrl, branch, file_patterns: patterns })
        });
        const data = await response.json();

        if (response.ok) {
            showSuccess('git', data);
        } else {
            statusDiv.className = 'rag-status-message error';
            statusDiv.textContent = data.detail || 'Failed to clone repository';
        }
    } catch (err) {
        statusDiv.className = 'rag-status-message error';
        statusDiv.textContent = 'Error: ' + err.message;
    }
});

document.getElementById('wizardUploadSubmit').addEventListener('click', async () => {
    const statusDiv = document.getElementById('wizardStatus');
    const name = document.getElementById('wizardUploadName').value.trim();
    const patterns = document.getElementById('wizardUploadPatterns').value.trim();

    if (!name) {
        statusDiv.className = 'rag-status-message error';
        statusDiv.textContent = 'Please enter a name';
        return;
    }
    if (!uploadedFile) {
        statusDiv.className = 'rag-status-message error';
        statusDiv.textContent = 'Please select a zip file';
        return;
    }

    statusDiv.className = 'rag-status-message loading';
    statusDiv.textContent = 'Uploading and indexing...';

    const formData = new FormData();
    formData.append('name', name);
    formData.append('file', uploadedFile);
    formData.append('file_patterns', patterns);

    try {
        const response = await csrfFetch('/api/rag/collections/upload', {
            method: 'POST',
            body: formData
        });
        const data = await response.json();

        if (response.ok) {
            showSuccess('upload', data);
        } else {
            statusDiv.className = 'rag-status-message error';
            statusDiv.textContent = data.detail || 'Failed to upload';
        }
    } catch (err) {
        statusDiv.className = 'rag-status-message error';
        statusDiv.textContent = 'Error: ' + err.message;
    }
});

document.getElementById('wizardVscodeSubmit').addEventListener('click', async () => {
    const statusDiv = document.getElementById('wizardStatus');
    const name = document.getElementById('wizardVscodeName').value.trim();
    const patterns = document.getElementById('wizardVscodePatterns').value.trim();

    if (!name) {
        statusDiv.className = 'rag-status-message error';
        statusDiv.textContent = 'Please enter a name';
        return;
    }

    statusDiv.className = 'rag-status-message loading';
    statusDiv.textContent = 'Creating collection...';

    try {
        // Create collection
        const colResponse = await csrfFetch('/api/rag/collections', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, collection_type: 'watcher', file_patterns: patterns })
        });
        const colData = await colResponse.json();

        if (!colResponse.ok) {
            statusDiv.className = 'rag-status-message error';
            statusDiv.textContent = colData.detail || 'Failed to create collection';
            return;
        }

        // Create watcher
        const watchResponse = await csrfFetch('/api/rag/watchers', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ collection_id: colData.id })
        });
        const watchData = await watchResponse.json();

        if (watchResponse.ok) {
            showSuccess('vscode', { ...colData, api_key: watchData.api_key });
        } else {
            statusDiv.className = 'rag-status-message error';
            statusDiv.textContent = watchData.detail || 'Failed to create watcher';
        }
    } catch (err) {
        statusDiv.className = 'rag-status-message error';
        statusDiv.textContent = 'Error: ' + err.message;
    }
});

function showSuccess(method, data) {
    document.getElementById('wizardStep2').style.display = 'none';
    document.getElementById('wizardStep3').style.display = 'block';

    let content = '';
    if (method === 'git') {
        content = `
            <h4>Repository Cloned!</h4>
            <p>"${escapeHtml(data.name)}" is being indexed in the background.</p>
            <p>This may take a few minutes for large repositories.</p>
        `;
    } else if (method === 'upload') {
        content = `
            <h4>Files Uploaded!</h4>
            <p>"${escapeHtml(data.name)}" is being indexed.</p>
        `;
    } else if (method === 'vscode') {
        content = `
            <h4>Ready for VS Code!</h4>
            <p>Use this API key in the VS Code extension:</p>
            <div class="api-key-box">
                <code id="successApiKey">${data.api_key}</code>
                <button class="btn-secondary btn-small" onclick="copyApiKey()">Copy</button>
            </div>
            <div class="instructions">
                <strong>Setup Instructions:</strong>
                <ol>
                    <li>Install the Posterchanai RAG Sync extension in VS Code</li>
                    <li>Click the "RAG Sync" button in the status bar</li>
                    <li>Enter this server URL: <code>${window.location.origin}</code></li>
                    <li>Paste the API key above</li>
                    <li>Your code will sync automatically as you work!</li>
                </ol>
            </div>
        `;
    }
    document.getElementById('wizardSuccessContent').innerHTML = content;
}

function copyApiKey() {
    const key = document.getElementById('successApiKey').textContent;
    navigator.clipboard.writeText(key).then(() => {
        alert('API key copied!');
    });
}

// Collection actions
async function deleteCollection(id) {
    if (!confirm('Delete this codebase and all indexed files?')) return;
    try {
        const response = await csrfFetch(`/api/rag/collections/${id}`, { method: 'DELETE' });
        if (response.ok) {
            loadRagCollections();
        } else {
            alert('Failed to delete');
        }
    } catch (err) {
        alert('Error deleting');
    }
}

async function reindexCollection(id) {
    try {
        const response = await csrfFetch(`/api/rag/collections/${id}/reindex`, { method: 'POST' });
        if (response.ok) {
            alert('Re-indexing started');
            loadRagCollections();
        } else {
            alert('Failed to re-index');
        }
    } catch (err) {
        alert('Error');
    }
}

async function pullCollection(id) {
    // Get button and original state
    const btn = event.target;
    const originalText = btn.textContent;

    // Get current last_indexed_at before starting
    let originalIndexTime = null;
    try {
        const preResp = await fetch('/api/rag/collections');
        if (preResp.ok) {
            const preCols = await preResp.json();
            const preCol = preCols.find(c => c.id === id);
            if (preCol && preCol.last_indexed_at) {
                originalIndexTime = new Date(preCol.last_indexed_at).getTime();
            }
        }
    } catch (e) {}

    try {
        btn.disabled = true;
        btn.textContent = '⏳ Pulling...';
        const response = await csrfFetch(`/api/rag/collections/${id}/pull`, { method: 'POST' });
        if (response.ok) {
            btn.textContent = '⏳ Indexing...';
            // Poll for completion
            let attempts = 0;
            const checkInterval = setInterval(async () => {
                attempts++;
                try {
                    const checkResp = await fetch('/api/rag/collections');
                    if (checkResp.ok) {
                        const cols = await checkResp.json();
                        const col = cols.find(c => c.id === id);
                        if (col && col.last_indexed_at) {
                            const indexedTime = new Date(col.last_indexed_at).getTime();
                            if (originalIndexTime === null || indexedTime > originalIndexTime) {
                                clearInterval(checkInterval);
                                btn.textContent = '✓ Done!';
                                btn.style.background = '#28a745';
                                btn.style.color = 'white';
                                setTimeout(() => {
                                    loadRagCollections();
                                }, 1500);
                                return;
                            }
                        }
                    }
                } catch (e) {}
                btn.textContent = `⏳ Indexing... (${attempts * 3}s)`;
                // Timeout after 5 minutes
                if (attempts >= 100) {
                    clearInterval(checkInterval);
                    btn.textContent = 'Timeout';
                    btn.disabled = false;
                    setTimeout(() => loadRagCollections(), 2000);
                }
            }, 3000);
        } else {
            const data = await response.json();
            alert(data.detail || 'Failed to pull');
            btn.disabled = false;
            btn.textContent = originalText;
        }
    } catch (err) {
        alert('Error pulling repository');
        btn.disabled = false;
        btn.textContent = originalText;
    }
}

async function showApiKey(collectionId) {
    try {
        const response = await fetch('/api/rag/watchers');
        if (response.ok) {
            const watchers = await response.json();
            const watcher = watchers.find(w => w.collection_id === collectionId);
            if (watcher) {
                const key = watcher.api_key;
                if (confirm(`API Key:\n\n${key}\n\nCopy to clipboard?`)) {
                    navigator.clipboard.writeText(key);
                }
            } else {
                alert('No watcher found. Creating one...');
                const createResp = await csrfFetch('/api/rag/watchers', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ collection_id: collectionId })
                });
                if (createResp.ok) {
                    const data = await createResp.json();
                    if (confirm(`New API Key:\n\n${data.api_key}\n\nCopy to clipboard?`)) {
                        navigator.clipboard.writeText(data.api_key);
                    }
                }
            }
        }
    } catch (err) {
        alert('Error getting API key');
    }
}

// Edit collection
function editCollection(id) {
    const item = document.querySelector(`.rag-collection-item[data-id="${id}"]`);
    if (!item) return;

    const type = item.dataset.type;
    const name = item.querySelector('.rag-collection-name').textContent;
    const source = item.dataset.source;
    const branch = item.dataset.branch;
    const patterns = item.dataset.patterns;

    document.getElementById('editCollectionId').value = id;
    document.getElementById('editCollectionName').value = name;
    document.getElementById('editCollectionGitUrl').value = source;
    document.getElementById('editCollectionBranch').value = branch;
    document.getElementById('editCollectionPatterns').value = patterns;

    // Show/hide git-specific fields
    const gitUrlGroup = document.getElementById('editGitUrlGroup');
    const branchGroup = document.getElementById('editBranchGroup');
    if (type === 'git') {
        gitUrlGroup.style.display = 'block';
        branchGroup.style.display = 'block';
    } else {
        gitUrlGroup.style.display = 'none';
        branchGroup.style.display = type === 'folder' ? 'none' : 'none';
    }

    document.getElementById('editStatus').className = 'rag-status-message';
    document.getElementById('ragEditModal').style.display = 'block';
}

document.getElementById('closeEditModal').addEventListener('click', () => {
    document.getElementById('ragEditModal').style.display = 'none';
});

async function saveCollection(andReindex = false) {
    const id = document.getElementById('editCollectionId').value;
    const statusDiv = document.getElementById('editStatus');

    const data = {
        name: document.getElementById('editCollectionName').value.trim(),
        source_path: document.getElementById('editCollectionGitUrl').value.trim(),
        git_branch: document.getElementById('editCollectionBranch').value.trim(),
        file_patterns: document.getElementById('editCollectionPatterns').value.trim()
    };

    statusDiv.className = 'rag-status-message loading';
    statusDiv.textContent = 'Saving...';

    try {
        const response = await csrfFetch(`/api/rag/collections/${id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        });

        if (response.ok) {
            if (andReindex) {
                statusDiv.textContent = 'Saved! Starting re-index...';
                const reindexResp = await csrfFetch(`/api/rag/collections/${id}/reindex`, { method: 'POST' });
                if (reindexResp.ok) {
                    statusDiv.className = 'rag-status-message success';
                    statusDiv.textContent = 'Saved and re-indexing started!';
                } else {
                    statusDiv.className = 'rag-status-message error';
                    statusDiv.textContent = 'Saved but failed to start re-index';
                }
            } else {
                statusDiv.className = 'rag-status-message success';
                statusDiv.textContent = 'Saved successfully!';
            }
            loadRagCollections();
            setTimeout(() => {
                document.getElementById('ragEditModal').style.display = 'none';
            }, 1500);
        } else {
            const err = await response.json();
            statusDiv.className = 'rag-status-message error';
            statusDiv.textContent = err.detail || 'Failed to save';
        }
    } catch (err) {
        statusDiv.className = 'rag-status-message error';
        statusDiv.textContent = 'Error: ' + err.message;
    }
}

document.getElementById('saveEditCollection').addEventListener('click', () => saveCollection(false));
document.getElementById('saveAndReindex').addEventListener('click', () => saveCollection(true));

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
                alert('External storage mount not found');
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
            alert('Error loading external storage: ' + (error.detail || 'Unknown error'));
        }
    } catch (err) {
        console.error('Failed to edit external storage:', err);
        alert('Error: ' + (err.message || 'Failed to load external storage'));
    }
}

window.deleteExternalStorage = async function(id) {
    if (!confirm('Are you sure you want to delete this external storage mount?')) {
        return;
    }
    
    try {
        const response = await csrfFetch(`/api/admin/external-storage/${id}`, {
            method: 'DELETE'
        });
        if (response.ok) {
            await loadExternalStorage();
            alert('External storage mount deleted');
        } else {
            const error = await response.json();
            alert('Error: ' + (error.detail || 'Failed to delete'));
        }
    } catch (err) {
        alert('Error deleting external storage mount');
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
            alert(id ? 'External storage mount updated' : 'External storage mount created');
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

// Load external storage when services tab is opened
document.querySelector('[data-tab="services"]')?.addEventListener('click', async () => {
    await loadUsersForExternalStorage();
    setTimeout(loadExternalStorage, 100);
});

// Load on page load if services tab is active
if (document.getElementById('tab-services')?.classList.contains('active')) {
    loadUsersForExternalStorage().then(() => loadExternalStorage());
}
loadUsers();


// Update backend UI after settings load
setTimeout(() => {
    updateBackendUI();
    updateImageBackendUI();
    refreshImageQueue();
    loadRagCollections();
}, 500);
