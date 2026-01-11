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

// Load settings
async function loadSettings() {
    try {
        const response = await fetch('/api/admin/settings');
        if (response.ok) {
            const settings = await response.json();
            for (const [key, value] of Object.entries(settings)) {
                const el = document.getElementById(key);
                if (el) {
                    if (el.type === 'checkbox') {
                        el.checked = value === 'true' || value === true;
                    } else if (el.tagName === 'SELECT') {
                        el.value = value || el.options[0].value;
                    } else {
                        // Only update if there's a value - preserve HTML defaults
                        if (value !== null && value !== undefined && value !== '') {
                            el.value = value;
                        }
                    }
                }
            }
        }
    } catch (err) {
        console.error('Failed to load settings:', err);
    }
}

// Reset news sources to defaults
document.getElementById('resetNewsSourcesBtn').addEventListener('click', () => {
    document.getElementById('news_sources').value = DEFAULT_NEWS_SOURCES;
});

// Save settings
document.getElementById('settingsForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const form = e.target;
    const settings = {};
    for (const el of form.querySelectorAll('input, textarea, select')) {
        if (el.name) {
            if (el.type === 'checkbox') {
                settings[el.name] = el.checked ? 'true' : 'false';
            } else {
                settings[el.name] = el.value;
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
            list.innerHTML = users.map(u => `
                <div class="user-item">
                    <span class="username">${escapeHtml(u.username)}</span>
                    <span class="badge ${u.is_admin ? 'admin' : ''}">${u.is_admin ? 'Admin' : 'User'}</span>
                    <button class="btn-secondary btn-small" onclick="resetPassword(${u.id}, '${escapeHtml(u.username)}')">Reset Password</button>
                    <button class="btn-danger btn-small" onclick="deleteUser(${u.id})">Delete</button>
                </div>
            `).join('');
        }
    } catch (err) {
        console.error('Failed to load users:', err);
    }
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
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

// Backend type switching
function updateBackendUI() {
    const backend = document.getElementById('llm_backend').value;
    const nativeSettings = document.getElementById('native-settings');
    const ollamaSettings = document.getElementById('ollama-settings');

    if (backend === 'native' || backend === 'ipex') {
        nativeSettings.style.display = 'block';
        ollamaSettings.style.display = 'none';
    } else {
        nativeSettings.style.display = 'none';
        ollamaSettings.style.display = 'block';
    }
}

document.getElementById('llm_backend').addEventListener('change', updateBackendUI);

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

// Image backend type switching
function updateImageBackendUI() {
    const backend = document.getElementById('image_backend').value;
    const nativeSettings = document.getElementById('native-image-settings');
    const comfyuiSettings = document.getElementById('comfyui-settings');

    if (backend === 'native') {
        nativeSettings.style.display = 'block';
        comfyuiSettings.style.display = 'none';
    } else {
        nativeSettings.style.display = 'none';
        comfyuiSettings.style.display = 'block';
    }
}

document.getElementById('image_backend').addEventListener('change', updateImageBackendUI);

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
            document.getElementById('imageModelName').textContent = data.model_path || data.comfyui_url || '-';
            document.getElementById('imageDeviceName').textContent = data.device || data.backend || '-';
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

// === Plugin Management ===

let pluginActionCounter = 0;
let allUsersCache = [];

async function loadPlugins() {
    try {
        const response = await fetch('/api/plugins');
        if (response.ok) {
            const plugins = await response.json();
            const list = document.getElementById('pluginList');

            if (plugins.length === 0) {
                list.innerHTML = '<div class="rag-empty">No plugins configured yet.<br>Add a plugin below to enable AI integrations.</div>';
                return;
            }

            list.innerHTML = plugins.map(p => {
                // Build allowed users display
                let usersDisplay = '';
                if (p.is_global && p.allowed_users && p.allowed_users.length > 0) {
                    const userNames = p.allowed_users.map(id => {
                        const user = allUsersCache.find(u => u.id === id);
                        return user ? user.username : `User ${id}`;
                    });
                    usersDisplay = `<div class="plugin-users-badge">Users: ${userNames.join(', ')}</div>`;
                } else if (p.is_global) {
                    usersDisplay = '<div class="plugin-users-badge">All users</div>';
                }

                return `
                <div class="plugin-item ${p.is_global ? 'global' : ''}" data-plugin-id="${p.id}">
                    <div class="plugin-header">
                        <span class="plugin-name">
                            ${escapeHtml(p.name)}
                            ${p.is_global ? '<span class="badge">Global</span>' : ''}
                            ${!p.enabled ? '<span class="badge disabled">Disabled</span>' : ''}
                        </span>
                        <div class="plugin-controls">
                            <button class="btn-secondary btn-small" onclick="editPlugin(${p.id})">Edit</button>
                            <button class="btn-secondary btn-small" onclick="togglePlugin(${p.id}, ${p.enabled})">${p.enabled ? 'Disable' : 'Enable'}</button>
                            ${!p.is_global || window.isAdmin ? `<button class="btn-danger btn-small" onclick="deletePlugin(${p.id})">Delete</button>` : ''}
                        </div>
                    </div>
                    <div class="plugin-description">${escapeHtml(p.description)}</div>
                    <div class="plugin-url">${escapeHtml(p.base_url)}</div>
                    ${usersDisplay}
                    <div class="plugin-actions-list">
                        ${p.actions.map(a => `<span class="plugin-action-tag">${escapeHtml(a.name)}</span>`).join('')}
                    </div>
                </div>
            `}).join('');
        }
    } catch (err) {
        console.error('Failed to load plugins:', err);
    }
}

async function loadUsersForPlugins() {
    // Only show global plugin option for admins
    if (!window.isAdmin) {
        document.getElementById('globalPluginGroup').style.display = 'none';
        return;
    }

    try {
        const response = await fetch('/api/admin/users');
        if (response.ok) {
            allUsersCache = await response.json();
            const list = document.getElementById('allowedUsersList');
            list.innerHTML = allUsersCache.map(u => `
                <label class="user-checkbox">
                    <input type="checkbox" name="allowedUser" value="${u.id}">
                    ${escapeHtml(u.username)}
                </label>
            `).join('');
        }
    } catch (err) {
        console.error('Failed to load users:', err);
    }
}

document.getElementById('pluginIsGlobal').addEventListener('change', (e) => {
    document.getElementById('allowedUsersGroup').style.display = e.target.checked ? 'block' : 'none';
});

async function togglePlugin(id, currentState) {
    try {
        const response = await csrfFetch(`/api/plugins/${id}/toggle`, { method: 'POST' });
        if (response.ok) {
            loadPlugins();
        }
    } catch (err) {
        console.error('Failed to toggle plugin:', err);
    }
}

async function deletePlugin(id) {
    if (!confirm('Delete this plugin?')) return;
    try {
        const response = await csrfFetch(`/api/plugins/${id}`, { method: 'DELETE' });
        if (response.ok) {
            loadPlugins();
        } else {
            alert('Failed to delete plugin');
        }
    } catch (err) {
        console.error('Failed to delete plugin:', err);
    }
}

// Edit mode state
let editingPluginId = null;
let pluginsCache = [];

async function editPlugin(id) {
    try {
        // Fetch the plugin data
        const response = await fetch(`/api/plugins/${id}`);
        if (!response.ok) {
            alert('Failed to load plugin');
            return;
        }
        const plugin = await response.json();

        // Set edit mode
        editingPluginId = id;

        // Populate form fields
        document.getElementById('pluginName').value = plugin.name;
        document.getElementById('pluginDescription').value = plugin.description;
        document.getElementById('pluginBaseUrl').value = plugin.base_url;
        document.getElementById('pluginAuthType').value = plugin.auth_type || 'none';
        document.getElementById('pluginAuthHeader').value = plugin.auth_header || 'X-API-Key';
        document.getElementById('pluginAuthValue').value = plugin.auth_value || '';

        // Show auth fields based on type
        const authType = plugin.auth_type || 'none';
        document.getElementById('authHeaderGroup').style.display = authType === 'header' ? 'block' : 'none';
        document.getElementById('authValueGroup').style.display = authType !== 'none' ? 'block' : 'none';

        // Handle global plugin settings
        const isGlobal = plugin.is_global || false;
        document.getElementById('pluginIsGlobal').checked = isGlobal;
        document.getElementById('allowedUsersGroup').style.display = isGlobal ? 'block' : 'none';

        // Set allowed users checkboxes
        document.querySelectorAll('input[name="allowedUser"]').forEach(cb => {
            cb.checked = plugin.allowed_users && plugin.allowed_users.includes(parseInt(cb.value));
        });

        // Clear and populate actions
        document.getElementById('pluginActions').innerHTML = '';
        if (plugin.actions && plugin.actions.length > 0) {
            plugin.actions.forEach(a => {
                addActionRow(a.name, a.description, a.method, a.path, a.body, a.params);
            });
        }

        // Update form UI for edit mode
        document.querySelector('#createPluginForm h3') && document.querySelector('#createPluginForm h3').remove();
        const formTitle = document.querySelector('#createPluginForm').previousElementSibling;
        if (formTitle && formTitle.tagName === 'H3') {
            formTitle.innerHTML = `Edit Plugin: ${escapeHtml(plugin.name)} <button type="button" class="btn-secondary btn-small" onclick="cancelEditPlugin()" style="margin-left: 10px;">Cancel</button>`;
        }

        // Update submit button text
        const submitBtn = document.querySelector('#createPluginForm button[type="submit"]');
        if (submitBtn) {
            submitBtn.textContent = 'Update Plugin';
        }

        // Scroll to form
        document.getElementById('createPluginForm').scrollIntoView({ behavior: 'smooth' });

    } catch (err) {
        console.error('Failed to edit plugin:', err);
        alert('Failed to load plugin for editing');
    }
}

function cancelEditPlugin() {
    editingPluginId = null;
    resetPluginForm();
}

function resetPluginForm() {
    document.getElementById('pluginName').value = '';
    document.getElementById('pluginDescription').value = '';
    document.getElementById('pluginBaseUrl').value = '';
    document.getElementById('pluginAuthType').value = 'none';
    document.getElementById('pluginAuthHeader').value = 'X-API-Key';
    document.getElementById('pluginAuthValue').value = '';
    document.getElementById('pluginActions').innerHTML = '';
    document.getElementById('authHeaderGroup').style.display = 'none';
    document.getElementById('authValueGroup').style.display = 'none';
    document.getElementById('pluginIsGlobal').checked = false;
    document.getElementById('allowedUsersGroup').style.display = 'none';
    document.querySelectorAll('input[name="allowedUser"]').forEach(cb => cb.checked = false);

    // Reset form title
    const formTitle = document.querySelector('#tab-plugins h3:nth-of-type(2)');
    if (formTitle) {
        formTitle.textContent = 'Add Plugin';
    }

    // Reset submit button
    const submitBtn = document.querySelector('#createPluginForm button[type="submit"]');
    if (submitBtn) {
        submitBtn.textContent = 'Create Plugin';
    }
}

function addActionRow(name = '', description = '', method = 'GET', path = '', body = null, params = []) {
    const container = document.getElementById('pluginActions');
    const id = pluginActionCounter++;
    const bodyStr = body ? JSON.stringify(body, null, 2) : '';
    const paramsStr = params && params.length > 0 ? params.join(', ') : '';
    const html = `
        <div class="action-item" data-action-id="${id}">
            <span class="remove-action" onclick="this.parentElement.remove()">&times;</span>
            <div class="form-row">
                <div class="form-group">
                    <label>Action Name</label>
                    <input type="text" class="action-name" value="${escapeHtml(name)}" placeholder="get_summary" required>
                </div>
                <div class="form-group">
                    <label>Method</label>
                    <select class="action-method">
                        <option value="GET" ${method === 'GET' ? 'selected' : ''}>GET</option>
                        <option value="POST" ${method === 'POST' ? 'selected' : ''}>POST</option>
                        <option value="PUT" ${method === 'PUT' ? 'selected' : ''}>PUT</option>
                        <option value="DELETE" ${method === 'DELETE' ? 'selected' : ''}>DELETE</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>Path</label>
                    <input type="text" class="action-path" value="${escapeHtml(path)}" placeholder="/endpoint" required>
                </div>
            </div>
            <div class="form-group">
                <label>Description</label>
                <input type="text" class="action-description" value="${escapeHtml(description)}" placeholder="What this action does" required>
            </div>
            <div class="form-group">
                <label>Parameters (comma-separated)</label>
                <input type="text" class="action-params" value="${escapeHtml(paramsStr)}" placeholder="url, name, limit">
                <small>Variable names that will be substituted in the body using {{name}} syntax</small>
            </div>
            <div class="form-group">
                <label>Request Body (JSON, for POST/PUT)</label>
                <textarea class="action-body" rows="3" placeholder='{"urls": ["{{url}}"], "destination": "/downloads"}'>${escapeHtml(bodyStr)}</textarea>
                <small>Use {{param_name}} for variable substitution</small>
            </div>
        </div>
    `;
    container.insertAdjacentHTML('beforeend', html);
}

document.getElementById('addActionBtn').addEventListener('click', () => addActionRow());

document.getElementById('pluginAuthType').addEventListener('change', (e) => {
    const type = e.target.value;
    document.getElementById('authHeaderGroup').style.display = type === 'header' ? 'block' : 'none';
    document.getElementById('authValueGroup').style.display = type !== 'none' ? 'block' : 'none';
});

document.getElementById('createPluginForm').addEventListener('submit', async (e) => {
    e.preventDefault();

    const actions = [];
    document.querySelectorAll('.action-item').forEach(item => {
        const action = {
            name: item.querySelector('.action-name').value,
            description: item.querySelector('.action-description').value,
            method: item.querySelector('.action-method').value,
            path: item.querySelector('.action-path').value
        };

        // Add params if specified
        const paramsStr = item.querySelector('.action-params')?.value?.trim();
        if (paramsStr) {
            action.params = paramsStr.split(',').map(p => p.trim()).filter(p => p);
        }

        // Add body if specified (for POST/PUT)
        const bodyStr = item.querySelector('.action-body')?.value?.trim();
        if (bodyStr) {
            try {
                action.body = JSON.parse(bodyStr);
            } catch (e) {
                alert(`Invalid JSON in action "${action.name}" body`);
                return;
            }
        }

        actions.push(action);
    });

    if (actions.length === 0) {
        alert('Please add at least one action');
        return;
    }

    const data = {
        name: document.getElementById('pluginName').value,
        description: document.getElementById('pluginDescription').value,
        base_url: document.getElementById('pluginBaseUrl').value,
        auth_type: document.getElementById('pluginAuthType').value,
        auth_header: document.getElementById('pluginAuthHeader').value,
        auth_value: document.getElementById('pluginAuthValue').value,
        actions: actions
    };

    // Handle global plugin with allowed users (admin only)
    const isGlobal = document.getElementById('pluginIsGlobal').checked;
    if (isGlobal) {
        const selectedUsers = [];
        document.querySelectorAll('input[name="allowedUser"]:checked').forEach(cb => {
            selectedUsers.push(parseInt(cb.value));
        });
        data.allowed_users = selectedUsers.length > 0 ? selectedUsers : [];
    }

    try {
        // Use PUT for edit, POST for create
        const url = editingPluginId ? `/api/plugins/${editingPluginId}` : '/api/plugins';
        const method = editingPluginId ? 'PUT' : 'POST';

        const response = await csrfFetch(url, {
            method: method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        });

        if (response.ok) {
            // Reset form and edit state
            editingPluginId = null;
            resetPluginForm();
            loadPlugins();
        } else {
            const err = await response.json();
            alert(err.detail || `Failed to ${editingPluginId ? 'update' : 'create'} plugin`);
        }
    } catch (err) {
        console.error(`Failed to ${editingPluginId ? 'update' : 'create'} plugin:`, err);
        alert(`Failed to ${editingPluginId ? 'update' : 'create'} plugin`);
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
loadUsers();
loadUsersForPlugins().then(() => loadPlugins());

// Update backend UI after settings load
setTimeout(() => {
    updateBackendUI();
    updateImageBackendUI();
    refreshImageQueue();
    loadRagCollections();
}, 500);
