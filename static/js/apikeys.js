// API Keys Management
class APIKeysManager {
    constructor() {
        this.modal = document.getElementById('apiKeysModal');
        this.keyList = document.getElementById('apiKeyList');
        this.init();
    }

    init() {
        // Open modal button
        document.getElementById('apiKeysBtn').addEventListener('click', () => this.openModal());

        // Close modal
        document.getElementById('closeApiKeysModal').addEventListener('click', () => this.closeModal());
        this.modal.addEventListener('click', (e) => {
            if (e.target === this.modal) this.closeModal();
        });

        // Create key button
        document.getElementById('createApiKey').addEventListener('click', () => this.createKey());
    }

    openModal() {
        this.modal.style.display = 'flex';
        this.loadKeys();
    }

    closeModal() {
        this.modal.style.display = 'none';
    }

    async loadKeys() {
        try {
            const response = await fetch('/api/auth/api-keys');
            if (response.ok) {
                const keys = await response.json();
                this.renderKeys(keys);
            }
        } catch (err) {
            console.error('Failed to load API keys:', err);
        }
    }

    renderKeys(keys) {
        if (keys.length === 0) {
            this.keyList.innerHTML = '<p class="no-keys">No API keys yet. Create one to get started.</p>';
            return;
        }

        this.keyList.innerHTML = keys.map(key => `
            <div class="api-key-item ${key.is_active ? '' : 'disabled'}">
                <div class="key-info">
                    <span class="key-name">${this.escapeHtml(key.name)}</span>
                    <span class="key-preview">${key.key_preview}</span>
                    <span class="key-date">Created: ${new Date(key.created_at).toLocaleDateString()}</span>
                    ${key.last_used_at ? `<span class="key-used">Last used: ${new Date(key.last_used_at).toLocaleDateString()}</span>` : ''}
                </div>
                <div class="key-actions">
                    <button class="btn-small ${key.is_active ? 'btn-warning' : 'btn-success'}" onclick="apiKeysManager.toggleKey(${key.id})">
                        ${key.is_active ? 'Disable' : 'Enable'}
                    </button>
                    <button class="btn-small btn-danger" onclick="apiKeysManager.deleteKey(${key.id})">Delete</button>
                </div>
            </div>
        `).join('');
    }

    async createKey() {
        const nameInput = document.getElementById('newKeyName');
        const name = nameInput.value.trim() || 'Default';

        try {
            const response = await fetch('/api/auth/api-keys', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name })
            });

            if (response.ok) {
                const data = await response.json();
                // Show the key once (it won't be shown again)
                this.showNewKey(data.key);
                nameInput.value = '';
                this.loadKeys();
            } else {
                const error = await response.json();
                alert(error.detail || 'Failed to create API key');
            }
        } catch (err) {
            alert('Error creating API key');
        }
    }

    showNewKey(key) {
        const keyDisplay = document.createElement('div');
        keyDisplay.className = 'new-key-display';
        keyDisplay.innerHTML = `
            <div class="new-key-alert">
                <div class="new-key-header">
                    <strong>Your new API key:</strong>
                    <p class="warning">Save this key! It won't be shown again.</p>
                </div>
                <div class="new-key-content">
                    <code class="key-full">${key}</code>
                </div>
                <div class="new-key-actions">
                    <button class="btn-primary copy-key-btn" onclick="apiKeysManager.copyKey('${key}', this)">Copy to Clipboard</button>
                </div>
            </div>
        `;

        // Insert at the top of the key list
        this.keyList.insertBefore(keyDisplay, this.keyList.firstChild);

        // Auto-remove after 120 seconds
        setTimeout(() => keyDisplay.remove(), 120000);
    }

    copyKey(key, btn) {
        navigator.clipboard.writeText(key).then(() => {
            btn.textContent = 'Copied!';
            setTimeout(() => btn.textContent = 'Copy', 2000);
        }).catch(() => {
            // Fallback for non-HTTPS
            const textarea = document.createElement('textarea');
            textarea.value = key;
            document.body.appendChild(textarea);
            textarea.select();
            document.execCommand('copy');
            document.body.removeChild(textarea);
            btn.textContent = 'Copied!';
            setTimeout(() => btn.textContent = 'Copy', 2000);
        });
    }

    async toggleKey(id) {
        try {
            const response = await fetch(`/api/auth/api-keys/${id}/toggle`, {
                method: 'PUT'
            });
            if (response.ok) {
                this.loadKeys();
            }
        } catch (err) {
            alert('Error toggling API key');
        }
    }

    async deleteKey(id) {
        if (!confirm('Delete this API key? Any applications using it will stop working.')) return;

        try {
            const response = await fetch(`/api/auth/api-keys/${id}`, {
                method: 'DELETE'
            });
            if (response.ok) {
                this.loadKeys();
            }
        } catch (err) {
            alert('Error deleting API key');
        }
    }

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
}

// Initialize
const apiKeysManager = new APIKeysManager();
