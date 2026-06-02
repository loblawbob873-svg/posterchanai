// Chat Handler
class ChatHandler {
    constructor() {
        this.ws = null;
        this.currentConversationId = null;
        this.reconnectAttempts = 0;
        this.maxReconnectAttempts = 5;
        this.messagesContainer = document.getElementById('messages');
        this.messageInput = document.getElementById('messageInput');
        this.sendBtn = document.getElementById('sendBtn');
        this.streamingMessage = null;

        // Streaming optimization - batch chunks and use RAF
        this.streamBuffer = '';
        this.streamRafPending = false;
        this.fullStreamContent = '';

        // File upload elements
        this.fileInput = document.getElementById('fileInput');
        this.cameraInput = document.getElementById('cameraInput');
        this.uploadPreview = document.getElementById('uploadPreview');
        this.imagePreview = document.getElementById('imagePreview');
        this.filePreview = document.getElementById('filePreview');
        this.removeUpload = document.getElementById('removeUpload');

        // User settings
        this.notificationEmail = null;

        // Stored upload data - now arrays to support multiple attachments
        this.uploadedImages = [];  // Array of {base64, filename, dataUrl}
        this.uploadedFiles = [];   // Array of {content, filename}
        this.uploadedPDFs = [];    // Array of {base64, filename}
        this.uploadedDocuments = []; // Array of {base64, filename, type}
        this.uploadedVideos = [];  // Array of {base64, filename} (for compress)

        // Callback for when stream ends (used by news to delete prompt)
        this.onStreamEndCallback = null;

        // Last payload for retry functionality
        this.lastPayload = null;
        this.lastUserMessage = null;  // Reference to last user message element

        // Streaming state
        this.isStreaming = false;

        // Track last read email for "this email" voice commands
        this.lastReadEmail = null;  // {account: "...", id: "..."}

        this.init();
    }

    init() {
        // Send button click (also handles stop when streaming)
        this.sendBtn.addEventListener('click', () => {
            if (this.isStreaming) {
                this.stopStreaming();
            } else {
                this.sendMessage();
            }
        });

        // Message history for up arrow recall
        this.messageHistory = [];
        this.historyIndex = -1;

        // Available commands for tab autocomplete
        this.commands = ['help', 'search', 'images', 'geni', 'screenshot', 'yt', 'ytdl', 'torrents', 'nyaa', 'budget', 'bills', 'pay', 'addbill', 'firewall', 'news', 'dailynews', 'logs', 'mail', 'todo', 'files', '4chan', 'compress', 'clip', 'convert'];
        

        // Load mail accounts for autocomplete
        this.loadMailAccountsForAutocomplete();
        // Notes autocomplete removed

        // Enter to send (Shift+Enter for new line)
        this.messageInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                this.sendMessage();
            }
            // Up arrow - recall last message
            else if (e.key === 'ArrowUp' && this.messageInput.selectionStart === 0) {
                e.preventDefault();
                this.recallPreviousMessage();
            }
            // Down arrow - navigate forward in history
            else if (e.key === 'ArrowDown' && this.historyIndex >= 0) {
                e.preventDefault();
                this.recallNextMessage();
            }
            // Tab - autocomplete commands
            else if (e.key === 'Tab') {
                e.preventDefault();
                this.autocompleteCommand();
            }
        });

        // Auto-resize textarea
        this.messageInput.addEventListener('input', () => {
            this.messageInput.style.height = 'auto';
            this.messageInput.style.height = Math.min(this.messageInput.scrollHeight, 120) + 'px';
        });

        // File input change
        if (this.fileInput) {
            this.fileInput.addEventListener('change', (e) => this.handleFileSelect(e));
        }

        // Camera input change
        if (this.cameraInput) {
            this.cameraInput.addEventListener('change', (e) => this.handleFileSelect(e));
        }

        // Remove upload button
        if (this.removeUpload) {
            this.removeUpload.addEventListener('click', () => this.clearUpload());
        }


        // Paste image from clipboard
        document.addEventListener('paste', (e) => this.handlePaste(e));

        // User settings modal
        this.initUserSettings();
    }

    handlePaste(e) {
        const items = e.clipboardData?.items;
        if (!items) return;

        for (const item of items) {
            if (item.type.startsWith('image/')) {
                e.preventDefault();
                const file = item.getAsFile();
                if (!file) continue;

                const reader = new FileReader();
                reader.onload = (event) => {
                    const base64 = event.target.result.split(',')[1];
                    const dataUrl = event.target.result;
                    this.uploadedImages.push({ base64, filename: file.name, dataUrl });
                    this.updateAttachmentsPreview();
                };
                reader.readAsDataURL(file);
                // Don't break - allow multiple images
            }
        }
    }

    initUserSettings() {
        const settingsBtn = document.getElementById('userSettingsBtn');
        const settingsModal = document.getElementById('userSettingsModal');
        const closeBtn = document.getElementById('closeUserSettingsModal');
        const saveBtn = document.getElementById('saveUserSettings');
        const emailInput = document.getElementById('notificationEmail');
        const statusEl = document.getElementById('settingsStatus');

        // Open settings modal when button clicked
        if (settingsBtn && settingsModal) {
            settingsBtn.addEventListener('click', async () => {
                settingsModal.style.display = 'flex';
                await this.loadUserSettings();
            });
        }

        // Close modal when close button clicked
        if (closeBtn && settingsModal) {
            closeBtn.addEventListener('click', () => {
                settingsModal.style.display = 'none';
            });
        }

        // Close modal when clicking outside
        if (settingsModal) {
            settingsModal.addEventListener('click', (e) => {
                if (e.target === settingsModal) {
                    settingsModal.style.display = 'none';
                }
            });
        }

        // Tab switching
        const tabButtons = settingsModal?.querySelectorAll('.user-tab-btn');
        const tabContents = settingsModal?.querySelectorAll('.user-tab-content');
        
        if (tabButtons && tabContents) {
            tabButtons.forEach(btn => {
                btn.addEventListener('click', () => {
                    const tabId = btn.dataset.tab;
                    
                    // Remove active class from all buttons and contents
                    tabButtons.forEach(b => b.classList.remove('active'));
                    tabContents.forEach(c => c.classList.remove('active'));
                    
                    // Add active class to clicked button
                    btn.classList.add('active');
                    
                    // Show corresponding content
                    const targetContent = settingsModal.querySelector(`#user-tab-${tabId}`);
                    if (targetContent) {
                        targetContent.classList.add('active');
                    }
                });
            });
        }

        // Custom AI elements
        const customAiEnabled = document.getElementById('customAiEnabled');
        const customAiSettings = document.getElementById('customAiSettings');
        const customAiType = document.getElementById('customAiType');
        const customAiUrl = document.getElementById('customAiUrl');
        const customAiModel = document.getElementById('customAiModel');
        const customAiApiKey = document.getElementById('customAiApiKey');
        const testCustomAi = document.getElementById('testCustomAi');
        const testAiResult = document.getElementById('testAiResult');

        // Custom Image elements
        const customImageEnabled = document.getElementById('customImageEnabled');
        const customImageSettings = document.getElementById('customImageSettings');
        const customImageUrl = document.getElementById('customImageUrl');
        const testCustomImage = document.getElementById('testCustomImage');
        const testImageResult = document.getElementById('testImageResult');

        // News Schedule elements
        const newsScheduleEnabled = document.getElementById('newsScheduleEnabled');
        const newsScheduleSettings = document.getElementById('newsScheduleSettings');
        const newsScheduleTime = document.getElementById('newsScheduleTime');
        const newsSources = document.getElementById('newsSources');

        // Telegram key generation
        const generateTelegramKeyBtn = document.getElementById('generateTelegramKeyBtn');
        const revokeTelegramKeyBtn = document.getElementById('revokeTelegramKeyBtn');
        if (generateTelegramKeyBtn) {
            generateTelegramKeyBtn.addEventListener('click', async () => {
                const statusEl = document.getElementById('telegramKeyStatus');
                // Warn if a key is already pending (visible in the key display)
                const existingKeyDisplay = document.getElementById('telegramKeyDisplay');
                if (existingKeyDisplay && existingKeyDisplay.style.display !== 'none') {
                    if (!confirm('You already have a pending key. Generating a new one will invalidate it. Continue?')) return;
                }
                try {
                    const resp = await csrfFetch('/api/telegram/generate-key', { method: 'POST' });
                    const data = await resp.json();
                    if (resp.ok && data.key) {
                        const keyDisplay = document.getElementById('telegramKeyDisplay');
                        const keyValue = document.getElementById('telegramKeyValue');
                        const startCmd = document.getElementById('telegramStartCommand');
                        if (keyDisplay) keyDisplay.style.display = 'block';
                        if (keyValue) keyValue.textContent = data.key;
                        if (startCmd) startCmd.textContent = `/start ${data.key}`;
                        if (revokeTelegramKeyBtn) revokeTelegramKeyBtn.style.display = 'inline-block';
                        const expiry = data.expires_at
                            ? new Date(data.expires_at + 'Z').toLocaleString()
                            : null;
                        const msg = expiry
                            ? `Key generated — expires ${expiry}. Send the command above to the bot.`
                            : 'Key generated. Send the command above to the bot.';
                        if (statusEl) { statusEl.textContent = msg; statusEl.className = 'test-result success'; }
                    } else {
                        if (statusEl) { statusEl.textContent = data.detail || 'Failed to generate key'; statusEl.className = 'test-result error'; }
                    }
                } catch (e) {
                    if (statusEl) { statusEl.textContent = 'Error: ' + e.message; statusEl.className = 'test-result error'; }
                }
            });
        }
        if (revokeTelegramKeyBtn) {
            revokeTelegramKeyBtn.addEventListener('click', async () => {
                const statusEl = document.getElementById('telegramKeyStatus');
                try {
                    const resp = await csrfFetch('/api/telegram/generate-key', { method: 'DELETE' });
                    if (resp.ok) {
                        const keyDisplay = document.getElementById('telegramKeyDisplay');
                        if (keyDisplay) keyDisplay.style.display = 'none';
                        revokeTelegramKeyBtn.style.display = 'none';
                        if (statusEl) { statusEl.textContent = 'Key revoked.'; statusEl.className = 'test-result'; }
                    } else {
                        const data = await resp.json().catch(() => ({}));
                        if (statusEl) { statusEl.textContent = data.detail || 'Failed to revoke key'; statusEl.className = 'test-result error'; }
                    }
                } catch (e) {
                    if (statusEl) { statusEl.textContent = 'Error: ' + e.message; statusEl.className = 'test-result error'; }
                }
            });
        }

        // Misskey MiAuth connect button
        const misskeyConnectBtn = document.getElementById('misskeyConnectBtn');
        const misskeyDisconnectBtn = document.getElementById('misskeyDisconnectBtn');
        if (misskeyConnectBtn) {
            misskeyConnectBtn.addEventListener('click', async () => {
                const instanceUrl = document.getElementById('misskeyInstanceUrl').value.trim();
                if (!instanceUrl) {
                    alert('Please enter your Misskey instance URL first.');
                    return;
                }
                const statusEl = document.getElementById('misskeyStatus');
                try {
                    const resp = await csrfFetch('/api/misskey/miauth/start', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ instance_url: instanceUrl })
                    });
                    const data = await resp.json();
                    if (!resp.ok) {
                        if (statusEl) { statusEl.textContent = data.detail || 'Failed to start MiAuth'; statusEl.className = 'test-result error'; }
                        return;
                    }
                    // Open MiAuth authorization in a new tab
                    const authWindow = window.open(data.auth_url, '_blank');
                    if (statusEl) { statusEl.textContent = 'Waiting for authorization…'; statusEl.className = 'test-result'; }
                    // Listen for postMessage from the callback page
                    const onMessage = async (event) => {
                        if (event.data === 'misskey_connected') {
                            window.removeEventListener('message', onMessage);
                            if (statusEl) { statusEl.textContent = 'Connected!'; statusEl.className = 'test-result success'; }
                            await this.loadUserSettings();
                        }
                    };
                    window.addEventListener('message', onMessage);
                } catch (e) {
                    if (statusEl) { statusEl.textContent = 'Error: ' + e.message; statusEl.className = 'test-result error'; }
                }
            });
        }
        if (misskeyDisconnectBtn) {
            misskeyDisconnectBtn.addEventListener('click', async () => {
                if (!confirm('Disconnect your Misskey account?')) return;
                try {
                    const resp = await csrfFetch('/api/misskey/disconnect', { method: 'DELETE' });
                    if (resp.ok) {
                        await this.loadUserSettings();
                    } else {
                        const d = await resp.json();
                        alert(d.detail || 'Failed to disconnect');
                    }
                } catch (e) {
                    alert('Error: ' + e.message);
                }
            });
        }

        // Pleroma OAuth connect / disconnect buttons
        const pleromaConnectBtn = document.getElementById('pleromaConnectBtn');
        const pleromaDisconnectBtn = document.getElementById('pleromaDisconnectBtn');
        if (pleromaConnectBtn) {
            pleromaConnectBtn.addEventListener('click', async () => {
                const instanceUrl = document.getElementById('pleromaInstanceUrl').value.trim();
                const statusEl = document.getElementById('pleromaStatus');
                if (!instanceUrl) {
                    if (statusEl) { statusEl.textContent = 'Please enter your Pleroma instance URL first.'; statusEl.className = 'test-result error'; }
                    return;
                }
                if (statusEl) { statusEl.textContent = 'Registering app…'; statusEl.className = 'test-result'; }
                try {
                    const resp = await csrfFetch('/api/pleroma/oauth/start', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ instance_url: instanceUrl })
                    });
                    const data = await resp.json();
                    if (!resp.ok) {
                        if (statusEl) { statusEl.textContent = data.detail || 'Failed to start OAuth'; statusEl.className = 'test-result error'; }
                        return;
                    }
                    // Open Pleroma authorization in a new tab
                    window.open(data.auth_url, '_blank');
                    if (statusEl) { statusEl.textContent = 'Waiting for authorization…'; statusEl.className = 'test-result'; }
                    // Listen for postMessage from the callback page
                    const onMessage = async (event) => {
                        if (event.data === 'pleroma_connected') {
                            window.removeEventListener('message', onMessage);
                            if (statusEl) { statusEl.textContent = 'Connected!'; statusEl.className = 'test-result success'; }
                            await this.loadUserSettings();
                        }
                    };
                    window.addEventListener('message', onMessage);
                } catch (e) {
                    if (statusEl) { statusEl.textContent = 'Error: ' + e.message; statusEl.className = 'test-result error'; }
                }
            });
        }
        if (pleromaDisconnectBtn) {
            pleromaDisconnectBtn.addEventListener('click', async () => {
                if (!confirm('Disconnect your Pleroma account?')) return;
                try {
                    const resp = await csrfFetch('/api/pleroma/disconnect', { method: 'POST' });
                    if (resp.ok) {
                        await this.loadUserSettings();
                    } else {
                        const d = await resp.json();
                        alert(d.detail || 'Failed to disconnect');
                    }
                } catch (e) {
                    alert('Error: ' + e.message);
                }
            });
        }

        // Matrix connect / disconnect buttons
        const matrixConnectBtn = document.getElementById('matrixConnectBtn');
        const matrixDisconnectBtn = document.getElementById('matrixDisconnectBtn');
        if (matrixConnectBtn) {
            matrixConnectBtn.addEventListener('click', async () => {
                const homeserver = document.getElementById('matrixHomeserver').value.trim();
                const username = document.getElementById('matrixUsername').value.trim();
                const password = document.getElementById('matrixPassword').value;
                const statusEl = document.getElementById('matrixStatus');
                if (!homeserver) {
                    if (statusEl) { statusEl.textContent = 'Please enter your homeserver URL.'; statusEl.className = 'test-result error'; }
                    return;
                }
                if (!username || !password) {
                    if (statusEl) { statusEl.textContent = 'Please enter your username and password.'; statusEl.className = 'test-result error'; }
                    return;
                }
                if (statusEl) { statusEl.textContent = 'Connecting…'; statusEl.className = 'test-result'; }
                try {
                    const resp = await csrfFetch('/api/matrix/connect', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ homeserver, username, password })
                    });
                    const data = await resp.json();
                    if (!resp.ok) {
                        if (statusEl) { statusEl.textContent = data.detail || 'Connection failed'; statusEl.className = 'test-result error'; }
                        return;
                    }
                    document.getElementById('matrixPassword').value = '';
                    if (statusEl) { statusEl.textContent = 'Connected!'; statusEl.className = 'test-result success'; }
                    await this.loadUserSettings();
                } catch (e) {
                    if (statusEl) { statusEl.textContent = 'Error: ' + e.message; statusEl.className = 'test-result error'; }
                }
            });
        }
        if (matrixDisconnectBtn) {
            matrixDisconnectBtn.addEventListener('click', async () => {
                if (!confirm('Disconnect your Matrix account?')) return;
                try {
                    const resp = await csrfFetch('/api/matrix/disconnect', { method: 'POST' });
                    if (resp.ok) {
                        await this.loadUserSettings();
                    } else {
                        const d = await resp.json();
                        alert(d.detail || 'Failed to disconnect');
                    }
                } catch (e) {
                    alert('Error: ' + e.message);
                }
            });
        }

        const matrixTestDmBtn = document.getElementById('matrixTestDmBtn');
        if (matrixTestDmBtn) {
            matrixTestDmBtn.addEventListener('click', async () => {
                const botUserId = document.getElementById('matrixDmBotUserId').value.trim();
                const statusEl = document.getElementById('matrixDmStatus');
                if (!botUserId) {
                    if (statusEl) { statusEl.textContent = 'Please enter a bot user ID.'; statusEl.className = 'test-result error'; }
                    return;
                }
                if (statusEl) { statusEl.textContent = 'Sending…'; statusEl.className = 'test-result'; }
                try {
                    const resp = await csrfFetch('/api/matrix/test-dm', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ bot_user_id: botUserId })
                    });
                    const data = await resp.json();
                    if (resp.ok) {
                        if (statusEl) { statusEl.textContent = 'DM sent! Check your Matrix client.'; statusEl.className = 'test-result success'; }
                    } else {
                        if (statusEl) { statusEl.textContent = data.detail || 'Failed to send DM'; statusEl.className = 'test-result error'; }
                    }
                } catch (e) {
                    if (statusEl) { statusEl.textContent = 'Error: ' + e.message; statusEl.className = 'test-result error'; }
                }
            });
        }

        // Telegram unlink button
        const unlinkTelegramBtn = document.getElementById('unlinkTelegramBtn');
        if (unlinkTelegramBtn) {
            unlinkTelegramBtn.addEventListener('click', async () => {
                if (!confirm('Are you sure you want to unlink your Telegram account?')) return;
                try {
                    const response = await csrfFetch('/api/telegram/unlink', { method: 'POST' });
                    const data = await response.json();
                    if (response.ok) {
                        await this.loadUserSettings();
                    } else {
                        alert(data.detail || 'Failed to unlink Telegram');
                    }
                } catch (e) {
                    alert('Error: ' + e.message);
                }
            });
        }

        // Save settings button
        if (saveBtn) {
            saveBtn.addEventListener('click', async () => {
                const settings = {};

                // Profile settings
                if (emailInput) settings.notification_email = emailInput.value;

                // Custom AI settings
                if (customAiEnabled) settings.custom_ai_enabled = customAiEnabled.checked;
                if (customAiType) settings.custom_ai_type = customAiType.value;
                if (customAiUrl) settings.custom_ai_url = customAiUrl.value;
                if (customAiModel) settings.custom_ai_model = customAiModel.value;
                if (customAiApiKey) settings.custom_ai_api_key = customAiApiKey.value;

                // Custom Image settings
                if (customImageEnabled) settings.custom_image_enabled = customImageEnabled.checked;
                if (customImageUrl) settings.custom_image_url = customImageUrl.value;

                // Schedule settings
                const scheduleEnabled = document.getElementById('scheduleEnabled');
                if (scheduleEnabled) settings.schedule_enabled = scheduleEnabled.checked;

                // News sources
                const userNewsSourcesEl = document.getElementById('userNewsSources');
                if (userNewsSourcesEl) settings.news_sources = userNewsSourcesEl.value;

                // Telegram settings
                const telegramNotificationsEl = document.getElementById('telegramNotifications');
                if (telegramNotificationsEl) settings.telegram_notifications = telegramNotificationsEl.value;

                // Relay social notifications (Misskey/Pleroma/Matrix) to Telegram
                const socialNotifEnabledEl = document.getElementById('socialNotifEnabled');
                if (socialNotifEnabledEl) settings.social_notif_enabled = socialNotifEnabledEl.checked;

                // Nitter RSS feeds → image cards → Telegram
                const nitterFeedsEl = document.getElementById('nitterFeeds');
                if (nitterFeedsEl) settings.nitter_feeds = nitterFeedsEl.value;

                // Misskey — only persist the instance URL from the form (token is managed via MiAuth)
                const misskeyInstanceUrlEl = document.getElementById('misskeyInstanceUrl');
                if (misskeyInstanceUrlEl) settings.misskey_instance_url = misskeyInstanceUrlEl.value.trim();

                // Matrix bot settings
                const matrixDmBotUserIdEl = document.getElementById('matrixDmBotUserId');
                if (matrixDmBotUserIdEl) settings.matrix_dm_bot_user_id = matrixDmBotUserIdEl.value.trim();

                // Finance (Budget Manager) API key — the field is blanked on load and the
                // saved key is never echoed back, so any non-empty value means the user just
                // typed a new key. Send it whenever it's non-empty (a blank field leaves the
                // existing key untouched). Don't gate on a dirty flag: a settings refresh
                // between typing and saving resets that flag and silently drops the key.
                const financeApiKeyEl = document.getElementById('financeApiKey');
                if (financeApiKeyEl && financeApiKeyEl.value.trim()) {
                    settings.finance_api_key = financeApiKeyEl.value.trim();
                }

                try {
                    const response = await csrfFetch('/api/auth/settings', {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(settings)
                    });

                    if (response.ok) {
                        if (statusEl) {
                            statusEl.textContent = 'Settings saved!';
                            statusEl.className = 'settings-status success';
                            setTimeout(() => {
                                statusEl.textContent = '';
                                statusEl.className = 'settings-status';
                            }, 3000);
                        }
                        // Close modal after successful save
                        if (settingsModal) settingsModal.style.display = 'none';
                    } else {
                        const data = await response.json();
                        if (statusEl) {
                            statusEl.textContent = data.detail || 'Failed to save settings';
                            statusEl.className = 'settings-status error';
                        }
                    }
                } catch (e) {
                    console.error('Error saving settings:', e);
                    if (statusEl) {
                        statusEl.textContent = 'Error saving settings';
                        statusEl.className = 'settings-status error';
                    }
                }
            });
        }

        // Add Mail Account button
        const addMailAccountBtn = document.getElementById('addMailAccount');
        if (addMailAccountBtn) {
            addMailAccountBtn.addEventListener('click', () => {
                // Simple prompt-based account addition
                const email = prompt('Email address:');
                if (!email) return;
                const password = prompt('Email password:');
                if (!password) return;
                const imapServer = prompt('IMAP server (e.g., imap.gmail.com):');
                if (!imapServer) return;
                const smtpServer = prompt('SMTP server (e.g., smtp.gmail.com):');
                if (!smtpServer) return;

                this.addMailAccount({
                    email,
                    password,
                    imap_server: imapServer,
                    smtp_server: smtpServer,
                    imap_port: 993,
                    smtp_port: 587,
                    use_ssl: true
                });
            });
        }
    }

    async addMailAccount(account) {
        try {
            // Get existing accounts first
            const response = await csrfFetch('/api/auth/settings');
            let accounts = [];
            if (response.ok) {
                const settings = await response.json();
                accounts = settings.mail_accounts || [];
            }
            
            // Add new account
            accounts.push(account);
            
            // Save all accounts
            const saveResponse = await csrfFetch('/api/auth/settings', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ mail_accounts: accounts })
            });

            if (saveResponse.ok) {
                await this.loadUserSettings();
            } else {
                const data = await saveResponse.json();
                alert(data.detail || 'Failed to add account');
            }
        } catch (e) {
            console.error('Error adding mail account:', e);
            alert('Error adding account');
        }
    }

    async deleteMailAccount(index) {
        if (!confirm('Delete this email account?')) return;
        
        try {
            // Get existing accounts
            const response = await csrfFetch('/api/auth/settings');
            if (!response.ok) {
                alert('Failed to load accounts');
                return;
            }
            
            const settings = await response.json();
            let accounts = settings.mail_accounts || [];
            
            // Remove account at index
            accounts.splice(index, 1);
            
            // Save updated accounts
            const saveResponse = await csrfFetch('/api/auth/settings', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ mail_accounts: accounts })
            });

            if (saveResponse.ok) {
                await this.loadUserSettings();
            } else {
                const data = await saveResponse.json();
                alert(data.detail || 'Failed to delete account');
            }
        } catch (e) {
            console.error('Error deleting mail account:', e);
            alert('Error deleting account');
        }
    }

    async loadUserSettings() {
        try {
            const response = await csrfFetch('/api/auth/settings');
            if (response.ok) {
                const settings = await response.json();
                console.log('User settings loaded:', settings);

                // Profile settings
                const notificationEmail = document.getElementById('notificationEmail');
                if (notificationEmail) notificationEmail.value = settings.notification_email || '';

                // Avatar
                const avatarImage = document.getElementById('avatarImage');
                const avatarPlaceholder = document.getElementById('avatarPlaceholder');
                const deleteAvatarBtn = document.getElementById('deleteAvatarBtn');
                if (settings.avatar && avatarImage && avatarPlaceholder) {
                    avatarImage.src = settings.avatar;
                    avatarImage.style.display = 'block';
                    avatarPlaceholder.style.display = 'none';
                    if (deleteAvatarBtn) deleteAvatarBtn.style.display = 'inline-block';
                } else if (avatarImage && avatarPlaceholder) {
                    avatarImage.style.display = 'none';
                    avatarPlaceholder.style.display = 'block';
                    if (deleteAvatarBtn) deleteAvatarBtn.style.display = 'none';
                }

                // Custom AI settings
                const customAiEnabled = document.getElementById('customAiEnabled');
                const customAiSettings = document.getElementById('customAiSettings');
                if (customAiEnabled) customAiEnabled.checked = settings.custom_ai_enabled || false;
                if (customAiSettings) customAiSettings.style.display = (settings.custom_ai_enabled) ? 'block' : 'none';

                const customAiType = document.getElementById('customAiType');
                if (customAiType) customAiType.value = settings.custom_ai_type || 'ollama';

                const customAiUrl = document.getElementById('customAiUrl');
                if (customAiUrl) customAiUrl.value = settings.custom_ai_url || '';

                const customAiModel = document.getElementById('customAiModel');
                if (customAiModel) customAiModel.value = settings.custom_ai_model || '';

                const customAiApiKey = document.getElementById('customAiApiKey');
                if (customAiApiKey) customAiApiKey.value = '';

                // Custom Image settings
                const customImageEnabled = document.getElementById('customImageEnabled');
                const customImageSettings = document.getElementById('customImageSettings');
                if (customImageEnabled) customImageEnabled.checked = settings.custom_image_enabled || false;
                if (customImageSettings) customImageSettings.style.display = (settings.custom_image_enabled) ? 'block' : 'none';

                const customImageUrl = document.getElementById('customImageUrl');
                if (customImageUrl) customImageUrl.value = settings.custom_image_url || '';

                // Schedule settings
                const scheduleEnabled = document.getElementById('scheduleEnabled');
                if (scheduleEnabled) scheduleEnabled.checked = settings.schedule_enabled || false;

                // News sources
                const userNewsSources = document.getElementById('userNewsSources');
                if (userNewsSources) userNewsSources.value = settings.news_sources || '';

                // Mail accounts
                if (settings.mail_accounts) {
                    this.renderMailAccountList(settings.mail_accounts);
                }

                // Telegram settings
                const telegramLinkedInfo = document.getElementById('telegramLinkedInfo');
                const telegramLinkedMsg = document.getElementById('telegramLinkedMsg');
                if (settings.telegram_enabled && settings.telegram_chat_id) {
                    if (telegramLinkedInfo) telegramLinkedInfo.style.display = 'block';
                    if (telegramLinkedMsg) telegramLinkedMsg.textContent = `Linked (Chat ID: ${settings.telegram_chat_id})`;
                } else {
                    if (telegramLinkedInfo) telegramLinkedInfo.style.display = 'none';
                }

                // Restore pending key display if one exists
                const keyDisplay = document.getElementById('telegramKeyDisplay');
                const keyValue = document.getElementById('telegramKeyValue');
                const startCmd = document.getElementById('telegramStartCommand');
                const revokeBtn = document.getElementById('revokeTelegramKeyBtn');
                const keyStatus = document.getElementById('telegramKeyStatus');
                if (settings.telegram_pending_key) {
                    const isExpired = settings.telegram_key_expires_at
                        && new Date(settings.telegram_key_expires_at + 'Z') < new Date();
                    if (isExpired) {
                        // Key exists in DB but is past its expiry — tell user to regenerate
                        if (keyDisplay) keyDisplay.style.display = 'none';
                        if (revokeBtn) revokeBtn.style.display = 'none';
                        if (keyStatus) {
                            keyStatus.textContent = 'Your previous key expired. Generate a new one.';
                            keyStatus.className = 'test-result error';
                        }
                    } else {
                        if (keyDisplay) keyDisplay.style.display = 'block';
                        if (keyValue) keyValue.textContent = settings.telegram_pending_key;
                        if (startCmd) startCmd.textContent = `/start ${settings.telegram_pending_key}`;
                        if (revokeBtn) revokeBtn.style.display = 'inline-block';
                        const expiry = settings.telegram_key_expires_at
                            ? new Date(settings.telegram_key_expires_at + 'Z').toLocaleString()
                            : null;
                        if (keyStatus) {
                            keyStatus.textContent = expiry ? `Key expires: ${expiry}` : '';
                            keyStatus.className = 'test-result';
                        }
                    }
                } else {
                    if (keyDisplay) keyDisplay.style.display = 'none';
                    if (revokeBtn) revokeBtn.style.display = 'none';
                    if (keyStatus) { keyStatus.textContent = ''; keyStatus.className = 'test-result'; }
                }

                const telegramNotifications = document.getElementById('telegramNotifications');
                if (telegramNotifications) telegramNotifications.value = settings.telegram_notifications || 'news,downloads,mentions';

                const socialNotifEnabled = document.getElementById('socialNotifEnabled');
                if (socialNotifEnabled) socialNotifEnabled.checked = !!settings.social_notif_enabled;

                const nitterFeeds = document.getElementById('nitterFeeds');
                if (nitterFeeds) nitterFeeds.value = settings.nitter_feeds || '';

                // Misskey settings
                const misskeyInstanceUrl = document.getElementById('misskeyInstanceUrl');
                const misskeyConnectBtn = document.getElementById('misskeyConnectBtn');
                const misskeyDisconnectBtn = document.getElementById('misskeyDisconnectBtn');
                const misskeyConnectedInfo = document.getElementById('misskeyConnectedInfo');
                const misskeyConnectedMsg = document.getElementById('misskeyConnectedMsg');
                if (misskeyInstanceUrl) misskeyInstanceUrl.value = settings.misskey_instance_url || '';
                if (settings.misskey_enabled && settings.misskey_has_api_token) {
                    if (misskeyConnectBtn) misskeyConnectBtn.style.display = 'none';
                    if (misskeyDisconnectBtn) misskeyDisconnectBtn.style.display = 'inline-block';
                    if (misskeyConnectedInfo) misskeyConnectedInfo.style.display = 'block';
                    if (misskeyConnectedMsg) misskeyConnectedMsg.textContent = `Connected to ${settings.misskey_instance_url}`;
                } else {
                    if (misskeyConnectBtn) misskeyConnectBtn.style.display = 'inline-block';
                    if (misskeyDisconnectBtn) misskeyDisconnectBtn.style.display = 'none';
                    if (misskeyConnectedInfo) misskeyConnectedInfo.style.display = 'none';
                }

                // Pleroma settings
                const pleromaInstanceUrlEl = document.getElementById('pleromaInstanceUrl');
                const pleromaConnectBtnEl = document.getElementById('pleromaConnectBtn');
                const pleromaDisconnectBtnEl = document.getElementById('pleromaDisconnectBtn');
                const pleromaConnectedInfo = document.getElementById('pleromaConnectedInfo');
                const pleromaConnectedMsg = document.getElementById('pleromaConnectedMsg');
                if (pleromaInstanceUrlEl) pleromaInstanceUrlEl.value = settings.pleroma_instance_url || '';
                if (settings.pleroma_enabled && settings.pleroma_has_access_token) {
                    if (pleromaConnectBtnEl) pleromaConnectBtnEl.style.display = 'none';
                    if (pleromaDisconnectBtnEl) pleromaDisconnectBtnEl.style.display = 'inline-block';
                    if (pleromaConnectedInfo) pleromaConnectedInfo.style.display = 'block';
                    if (pleromaConnectedMsg) pleromaConnectedMsg.textContent = `Connected to ${settings.pleroma_instance_url}`;
                } else {
                    if (pleromaConnectBtnEl) pleromaConnectBtnEl.style.display = 'inline-block';
                    if (pleromaDisconnectBtnEl) pleromaDisconnectBtnEl.style.display = 'none';
                    if (pleromaConnectedInfo) pleromaConnectedInfo.style.display = 'none';
                }

                // Matrix
                const matrixHomeserverEl = document.getElementById('matrixHomeserver');
                const matrixConnectBtnEl = document.getElementById('matrixConnectBtn');
                const matrixDisconnectBtnEl = document.getElementById('matrixDisconnectBtn');
                const matrixConnectedInfo = document.getElementById('matrixConnectedInfo');
                const matrixConnectedMsg = document.getElementById('matrixConnectedMsg');
                const matrixLoginSection = document.getElementById('matrixLoginSection');
                if (matrixHomeserverEl) matrixHomeserverEl.value = settings.matrix_homeserver || '';
                if (settings.matrix_enabled && settings.matrix_has_access_token) {
                    if (matrixConnectBtnEl) matrixConnectBtnEl.style.display = 'none';
                    if (matrixDisconnectBtnEl) matrixDisconnectBtnEl.style.display = 'inline-block';
                    if (matrixConnectedInfo) matrixConnectedInfo.style.display = 'block';
                    if (matrixConnectedMsg) matrixConnectedMsg.textContent = `Connected as ${settings.matrix_user_id || settings.matrix_homeserver}`;
                    if (matrixLoginSection) {
                        const usernameEl = matrixLoginSection.querySelector('#matrixUsername');
                        const passwordEl = matrixLoginSection.querySelector('#matrixPassword');
                        if (usernameEl) usernameEl.style.display = 'none';
                        if (passwordEl) passwordEl.closest('.form-row') && (passwordEl.closest('.form-row').style.display = 'none');
                    }
                } else {
                    if (matrixConnectBtnEl) matrixConnectBtnEl.style.display = 'inline-block';
                    if (matrixDisconnectBtnEl) matrixDisconnectBtnEl.style.display = 'none';
                    if (matrixConnectedInfo) matrixConnectedInfo.style.display = 'none';
                    if (matrixLoginSection) {
                        const formRow = matrixLoginSection.querySelector('.form-row');
                        if (formRow) formRow.style.display = '';
                    }
                }
                const matrixDmBotUserIdEl = document.getElementById('matrixDmBotUserId');
                if (matrixDmBotUserIdEl) matrixDmBotUserIdEl.value = settings.matrix_dm_bot_user_id || '';

                // Finance — never echo the key; just clear the field and show status. On save,
                // any non-empty value is treated as a new key (see the save handler above).
                const financeApiKeyEl = document.getElementById('financeApiKey');
                const financeKeyStatus = document.getElementById('financeKeyStatus');
                if (financeApiKeyEl) {
                    financeApiKeyEl.value = '';
                    financeApiKeyEl.placeholder = settings.finance_has_api_key
                        ? '•••••••• (key saved — type to replace)'
                        : 'Paste your Budget Manager API key';
                }
                if (financeKeyStatus) {
                    financeKeyStatus.textContent = settings.finance_has_api_key
                        ? '✅ Connected. Type a new key here and save to replace it.'
                        : 'Not connected.';
                }

                // News settings
                const newsScheduleEnabled = document.getElementById('newsScheduleEnabled');
                const newsScheduleSettings = document.getElementById('newsScheduleSettings');
                if (newsScheduleEnabled) newsScheduleEnabled.checked = settings.news_schedule_enabled || false;
                if (newsScheduleSettings) newsScheduleSettings.style.display = (settings.news_schedule_enabled) ? 'block' : 'none';

                // Load storage usage
                await this.loadStorageUsage();

                // Load API keys
                if (window.apiKeysManager) {
                    window.apiKeysManager.loadApiKeys();
                }
            } else {
                console.error('Failed to load user settings:', response.status);
            }
        } catch (e) {
            console.error('Error loading user settings:', e);
        }
    }

    async loadStorageAddresses() {
        try {
            console.log('Loading storage addresses...');
            const response = await fetch('/api/auth/storage-addresses');
            console.log('Storage addresses response status:', response.status);
            
            if (response.ok) {
                const data = await response.json();
                console.log('Storage addresses data:', data);
                
                // Get base URL from current location
                const protocol = window.location.protocol;
                const hostname = window.location.hostname;
                
                // WebDAV address code removed
                
                // Update CalDAV address
                const caldavInput = document.getElementById('caldavAddress');
                const caldavUsername = document.getElementById('caldavUsername');
                console.log('CalDAV elements:', { input: !!caldavInput, username: !!caldavUsername, url: data.caldav_url });
                
                if (caldavInput) {
                    if (data.caldav_url) {
                        let caldavUrl = data.caldav_url;
                        // Only replace localhost if it's actually localhost
                        if (caldavUrl.includes('localhost')) {
                            caldavUrl = caldavUrl.replace('localhost', hostname);
                        }
                        // Ensure protocol matches current page
                        if (caldavUrl.startsWith('http://') && protocol === 'https:') {
                            caldavUrl = caldavUrl.replace('http://', 'https://');
                        }
                        caldavInput.value = caldavUrl;
                        console.log('Set CalDAV URL:', caldavUrl);
                    } else {
                        caldavInput.value = '';
                        caldavInput.placeholder = 'CalDAV server not enabled (enable in Admin → Site Settings)';
                        console.log('CalDAV URL is empty (server not enabled?)');
                    }
                }
                if (caldavUsername && data.username) {
                    caldavUsername.textContent = data.username;
                    console.log('Set CalDAV username:', data.username);
                }
                
                // Update CardDAV address
                const carddavInput = document.getElementById('carddavAddress');
                const carddavUsername = document.getElementById('carddavUsername');
                // Check both carddav_url and cardav_url for backwards compatibility
                // Also check for empty strings (API might return "" instead of null/undefined)
                const carddavUrl = (data.carddav_url && data.carddav_url.trim()) || (data.cardav_url && data.cardav_url.trim());
                console.log('CardDAV elements:', { input: !!carddavInput, username: !!carddavUsername, url: carddavUrl, raw_data: { carddav_url: data.carddav_url, cardav_url: data.cardav_url } });
                
                if (carddavInput) {
                    if (carddavUrl && carddavUrl.trim()) {
                        let url = carddavUrl.trim();
                        // Only replace localhost if it's actually localhost
                        if (url.includes('localhost')) {
                            url = url.replace('localhost', hostname);
                        }
                        // Ensure protocol matches current page
                        if (url.startsWith('http://') && protocol === 'https:') {
                            url = url.replace('http://', 'https://');
                        }
                        carddavInput.value = url;
                        console.log('Set CardDAV URL:', url);
                    } else {
                        carddavInput.value = '';
                        carddavInput.placeholder = 'CardDAV server not enabled (enable in Admin → Site Settings)';
                        console.log('CardDAV URL is empty (server not enabled?)', { carddav_url: data.carddav_url, cardav_url: data.cardav_url });
                    }
                }
                if (carddavUsername && data.username) {
                    carddavUsername.textContent = data.username;
                    console.log('Set CardDAV username:', data.username);
                }
            } else {
                const errorText = await response.text();
                console.error('Failed to load storage addresses:', response.status, errorText);
            }
        } catch (e) {
            console.error('Failed to load storage addresses:', e);
        }
    }
    
    async loadStorageUsage() {
        try {
            const response = await fetch('/api/files/list');
            if (response.ok) {
                const data = await response.json();
                const storage = data.storage;
                
                const storageText = document.getElementById('storageText');
                const storageBarFill = document.getElementById('storageBarFill');
                
                if (storageText) {
                    const used_mb = storage.used_mb.toFixed(1);
                    const quota_mb = storage.unlimited ? '∞' : storage.quota_mb.toFixed(1);
                    storageText.textContent = `${used_mb} MB / ${quota_mb} MB`;
                }
                
                if (storageBarFill && !storage.unlimited) {
                    const percent = Math.min(100, (storage.used / storage.quota) * 100);
                    storageBarFill.style.width = `${percent}%`;
                } else if (storageBarFill) {
                    storageBarFill.style.width = '0%';
                }
            }
        } catch (e) {
            console.error('Failed to load storage usage:', e);
        }
    }
    
    // Scan user storage button - moved inside initUserSettings to have proper context

    updateAvatarPreview(avatarUrl) {
        const avatarImage = document.getElementById('avatarImage');
        const avatarPlaceholder = document.getElementById('avatarPlaceholder');
        const deleteAvatarBtn = document.getElementById('deleteAvatarBtn');
        const sidebarAvatar = document.getElementById('sidebarAvatar');
        const sidebarInitial = document.getElementById('sidebarInitial');

        if (avatarUrl) {
            const cacheBust = '?t=' + Date.now();
            avatarImage.src = avatarUrl + cacheBust;
            avatarImage.style.display = 'block';
            avatarPlaceholder.style.display = 'none';
            deleteAvatarBtn.style.display = 'inline-block';
            // Update sidebar
            if (sidebarAvatar) {
                sidebarAvatar.src = avatarUrl + cacheBust;
                sidebarAvatar.style.display = 'block';
                if (sidebarInitial) sidebarInitial.style.display = 'none';
            }
        } else {
            avatarImage.style.display = 'none';
            avatarPlaceholder.style.display = 'block';
            deleteAvatarBtn.style.display = 'none';
            // Update sidebar
            if (sidebarAvatar) {
                sidebarAvatar.style.display = 'none';
                if (sidebarInitial) sidebarInitial.style.display = 'block';
            }
        }
    }

    handleFileSelect(e) {
        const files = Array.from(e.target.files || []);
        if (files.length === 0) return;

        // Process each file
        files.forEach(file => {
            const isImage = file.type.startsWith('image/');
            const isPDF = file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf');
            const isVideo = file.type.startsWith('video/') || /\.(mp4|mov|mkv|webm|avi|m4v)$/i.test(file.name);

            if (isVideo) {
                // Handle video upload (for the compress command) - send as base64
                const reader = new FileReader();
                reader.onload = (e) => {
                    const base64 = e.target.result.split(',')[1];
                    this.uploadedVideos.push({ base64, filename: file.name });
                    this.updateAttachmentsPreview();
                };
                reader.readAsDataURL(file);
            } else if (isImage) {
                // Handle image upload
                const reader = new FileReader();
                reader.onload = (e) => {
                    const base64 = e.target.result.split(',')[1];
                    const dataUrl = e.target.result;
                    this.uploadedImages.push({ base64, filename: file.name, dataUrl });
                    this.updateAttachmentsPreview();
                };
                reader.readAsDataURL(file);
            } else if (isPDF) {
                // Handle PDF upload - send as base64
                const reader = new FileReader();
                reader.onload = (e) => {
                    const base64 = e.target.result.split(',')[1];
                    this.uploadedPDFs.push({ base64, filename: file.name });
                    this.updateAttachmentsPreview();
                };
                reader.readAsDataURL(file);
            } else if (this.isOfficeFile(file.name)) {
                // Handle Office documents - send as base64
                const reader = new FileReader();
                reader.onload = (e) => {
                    const base64 = e.target.result.split(',')[1];
                    const type = file.name.endsWith('.docx') ? 'docx' :
                                file.name.endsWith('.xlsx') ? 'xlsx' :
                                file.name.endsWith('.pptx') ? 'pptx' : 'docx';
                    this.uploadedDocuments.push({ base64, filename: file.name, type });
                    this.updateAttachmentsPreview();
                };
                reader.readAsDataURL(file);
            } else {
                // Handle text file upload
                const reader = new FileReader();
                reader.onload = (e) => {
                    this.uploadedFiles.push({ content: e.target.result, filename: file.name });
                    this.updateAttachmentsPreview();
                };
                reader.readAsText(file);
            }
        });

        // Clear input so same file can be selected again
        this.fileInput.value = '';
    }

    updateAttachmentsPreview() {
        const attachmentsList = document.getElementById('attachmentsList');
        const uploadPreview = document.getElementById('uploadPreview');
        if (!attachmentsList || !uploadPreview) return;

        const totalAttachments = this.uploadedImages.length + this.uploadedPDFs.length +
                                this.uploadedDocuments.length + this.uploadedVideos.length +
                                this.uploadedFiles.length;

        if (totalAttachments === 0) {
            uploadPreview.style.display = 'none';
            attachmentsList.innerHTML = '';
            return;
        }

        uploadPreview.style.display = 'flex';
        attachmentsList.innerHTML = '';

        // Add images
        this.uploadedImages.forEach((img, index) => {
            const item = document.createElement('div');
            item.className = 'attachment-item attachment-image';
            item.innerHTML = `
                <img src="${img.dataUrl}" alt="${this.escapeHtml(img.filename)}" class="attachment-thumb">
                <span class="attachment-name">${this.escapeHtml(img.filename)}</span>
                <button class="btn-icon attachment-remove" onclick="window.chatHandler.removeAttachment('image', ${index})" title="Remove">×</button>
            `;
            attachmentsList.appendChild(item);
        });

        // Add PDFs
        this.uploadedPDFs.forEach((pdf, index) => {
            const item = document.createElement('div');
            item.className = 'attachment-item attachment-pdf';
            item.innerHTML = `
                <span class="attachment-icon">📕</span>
                <span class="attachment-name">${this.escapeHtml(pdf.filename)}</span>
                <button class="btn-icon attachment-remove" onclick="window.chatHandler.removeAttachment('pdf', ${index})" title="Remove">×</button>
            `;
            attachmentsList.appendChild(item);
        });

        // Add documents
        this.uploadedDocuments.forEach((doc, index) => {
            const icon = doc.type === 'docx' ? '📝' : doc.type === 'xlsx' ? '📊' : '📽️';
            const item = document.createElement('div');
            item.className = 'attachment-item attachment-document';
            item.innerHTML = `
                <span class="attachment-icon">${icon}</span>
                <span class="attachment-name">${this.escapeHtml(doc.filename)}</span>
                <button class="btn-icon attachment-remove" onclick="window.chatHandler.removeAttachment('document', ${index})" title="Remove">×</button>
            `;
            attachmentsList.appendChild(item);
        });

        // Add videos
        this.uploadedVideos.forEach((vid, index) => {
            const item = document.createElement('div');
            item.className = 'attachment-item attachment-video';
            item.innerHTML = `
                <span class="attachment-icon">🎬</span>
                <span class="attachment-name">${this.escapeHtml(vid.filename)}</span>
                <button class="btn-icon attachment-remove" onclick="window.chatHandler.removeAttachment('video', ${index})" title="Remove">×</button>
            `;
            attachmentsList.appendChild(item);
        });

        // Add text files
        this.uploadedFiles.forEach((file, index) => {
            const item = document.createElement('div');
            item.className = 'attachment-item attachment-file';
            item.innerHTML = `
                <span class="attachment-icon">📄</span>
                <span class="attachment-name">${this.escapeHtml(file.filename)}</span>
                <button class="btn-icon attachment-remove" onclick="window.chatHandler.removeAttachment('file', ${index})" title="Remove">×</button>
            `;
            attachmentsList.appendChild(item);
        });

        // Quick action: translate the OCR'd text of an attached image/PDF.
        if (this.uploadedImages.length > 0 || this.uploadedPDFs.length > 0) {
            const action = document.createElement('button');
            action.type = 'button';
            action.className = 'attachment-action';
            action.textContent = '🌐 Translate';
            action.title = 'OCR the attachment and translate it (edit the language, then Enter)';
            action.style.cssText = 'align-self:center;padding:4px 10px;border-radius:14px;border:1px solid var(--border-color,#ccc);background:transparent;color:inherit;cursor:pointer;font-size:0.85em;white-space:nowrap;';
            action.onclick = () => window.chatHandler.translateAttachment();
            attachmentsList.appendChild(action);
        }
    }

    // Prefill the composer with a translate command (keeping the attachment) so the
    // user can pick a language and send. Routes through the `translate` command, which
    // OCRs the upload and translates the full text.
    translateAttachment() {
        if (this.messageInput) {
            this.messageInput.value = 'translate to english';
            this.messageInput.focus();
            // Select "english" so the user can type another language and hit Enter.
            const v = this.messageInput.value;
            this.messageInput.setSelectionRange(v.length - 'english'.length, v.length);
        }
    }

    removeAttachment(type, index) {
        if (type === 'image') {
            this.uploadedImages.splice(index, 1);
        } else if (type === 'pdf') {
            this.uploadedPDFs.splice(index, 1);
        } else if (type === 'document') {
            this.uploadedDocuments.splice(index, 1);
        } else if (type === 'video') {
            this.uploadedVideos.splice(index, 1);
        } else if (type === 'file') {
            this.uploadedFiles.splice(index, 1);
        }
        this.updateAttachmentsPreview();
    }

    formatFileSize(bytes) {
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
        return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    }

    isOfficeFile(filename) {
        const ext = filename.toLowerCase();
        return ext.endsWith('.docx') || ext.endsWith('.xlsx') || ext.endsWith('.pptx') ||
               ext.endsWith('.doc') || ext.endsWith('.xls') || ext.endsWith('.ppt');
    }

    clearUpload() {
        this.uploadedImages = [];
        this.uploadedFiles = [];
        this.uploadedPDFs = [];
        this.uploadedDocuments = [];
        this.uploadedVideos = [];
        this.updateAttachmentsPreview();
    }

    getCookie(name) {
        const value = `; ${document.cookie}`;
        const parts = value.split(`; ${name}=`);
        if (parts.length === 2) return parts.pop().split(';').shift();
        return null;
    }

    connect(conversationId) {
        // Close existing connection and clear state
        if (this.ws) {
            this.ws.onclose = null; // Prevent reconnect attempts
            this.ws.close();
            this.ws = null;
        }
        this.streamingMessage = null;
        this.hideTypingIndicator();

        this.currentConversationId = conversationId;
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';

        // Get token from cookie and pass as query param
        const token = this.getCookie('access_token');
        let wsUrl = `${protocol}//${window.location.host}/api/ws/chat/${conversationId}`;
        if (token) {
            wsUrl += `?token=${encodeURIComponent(token)}`;
        }

        this.ws = new WebSocket(wsUrl);

        this.ws.onopen = () => {
            console.log('WebSocket connected');
            this.reconnectAttempts = 0;
        };

        this.ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            this.handleMessage(data);
        };

        this.ws.onclose = (event) => {
            console.log('WebSocket closed:', event.code, event.reason);
            // 4001 = auth error - redirect to login
            if (event.code === 4001) {
                window.location.href = '/login';
                return;
            }
            if (event.code !== 1000 && this.reconnectAttempts < this.maxReconnectAttempts) {
                this.reconnectAttempts++;
                setTimeout(() => this.connect(conversationId), 2000 * this.reconnectAttempts);
            }
        };

        this.ws.onerror = (error) => {
            console.error('WebSocket error:', error);
        };
    }

    disconnect() {
        if (this.ws) {
            this.ws.onclose = null;  // Prevent reconnect attempts
            this.ws.close();
            this.ws = null;
        }
        this.currentConversationId = null;
        this.lastPayload = null;
        this.lastUserMessage = null;
        this.streamingMessage = null;
        this.reconnectAttempts = 0;
    }

    async sendMessage() {
        let content = this.messageInput.value.trim();

        // Auto-create conversation if none exists
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
            if (window.app && !window.app.currentConversation) {
                await window.app.createConversation();
                // Wait a bit for WebSocket to connect
                await new Promise(resolve => setTimeout(resolve, 300));
            }
            if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
        }

        // Get current mode and prepend command if needed
        const mode = window.app ? window.app.getMode() : '';
        const displayContent = content;

        // Need either content or at least one attachment
        const hasAttachments = this.uploadedImages.length > 0 || this.uploadedPDFs.length > 0 ||
                              this.uploadedDocuments.length > 0 || this.uploadedVideos.length > 0 ||
                              this.uploadedFiles.length > 0;
        if (!content && !hasAttachments) return;

        // Save to message history for up arrow recall
        this.saveToHistory(displayContent);

        if (mode) {
            // Don't prepend mode if user explicitly typed a known command
            const contentLower = content.toLowerCase();
            const startsWithCmd = (this.commands || []).some(cmd => contentLower === cmd || contentLower.startsWith(cmd + ' '));
            if (!startsWithCmd) {
                content = `${mode} ${content}`;
            }
        }

        // Build display message
        let displayMsg = displayContent;
        if (hasAttachments) {
            const count = this.uploadedImages.length + this.uploadedPDFs.length +
                         this.uploadedDocuments.length + this.uploadedVideos.length +
                         this.uploadedFiles.length;
            displayMsg = displayContent + (displayContent ? ' ' : '') + `[with ${count} attachment${count > 1 ? 's' : ''}]`;
        }

        // Get first image data URL for preview (if any)
        let imageDataUrl = null;
        if (this.uploadedImages.length > 0) {
            imageDataUrl = this.uploadedImages[0].dataUrl;
        }

        // Add user message to UI (show what user typed, not the command)
        // skipUserButtons=true because we add them manually below with proper cleanup
        this.lastUserMessage = this.addMessage('user', displayMsg || '[File uploaded]', false, true, imageDataUrl);

        // Add action buttons to user message
        const userContentEl = this.lastUserMessage.querySelector('.message-content');

        // Remove action buttons from previous user messages
        const prevUserActionBtns = this.messagesContainer.querySelectorAll('.message.user .btn-regenerate, .message.user .btn-edit');
        prevUserActionBtns.forEach(btn => btn.remove());

        // Store attachment data on message element for editing later
        if (this.uploadedImages.length > 0) {
            this.lastUserMessage._imageData = this.uploadedImages[0].base64;
            this.lastUserMessage._allAttachments = {
                images: this.uploadedImages,
                pdfs: this.uploadedPDFs,
                documents: this.uploadedDocuments,
                videos: this.uploadedVideos,
                files: this.uploadedFiles
            };
        }

        // Add edit button
        const editBtn = document.createElement('button');
        editBtn.className = 'btn-edit';
        editBtn.innerHTML = '✏️';
        editBtn.title = 'Edit and resubmit';
        editBtn.onclick = () => this.editMessage(this.lastUserMessage, displayMsg);
        userContentEl.appendChild(editBtn);

        const userRegenBtn = document.createElement('button');
        userRegenBtn.className = 'btn-regenerate';
        userRegenBtn.innerHTML = '🔄';
        userRegenBtn.title = 'Retry this message';
        userRegenBtn.onclick = () => this.retryLastMessage();
        userContentEl.appendChild(userRegenBtn);

        // Clear input
        this.messageInput.value = '';
        this.messageInput.style.height = 'auto';

        // Notify mascot
        if (window.mascotController) {
            if (mode === 'geni') {
                window.mascotController.onGeneratingImage();
            } else {
                window.mascotController.onUserMessage();
            }
        }

        // Build message payload
        const payload = {
            type: 'message',
            content: content
        };

        // Include uploaded data - send first of each type for backward compatibility
        // Backend will process all attachments from the arrays
        if (this.uploadedImages.length > 0) {
            payload.image_data = this.uploadedImages[0].base64;  // First image for backward compat
            payload.images = this.uploadedImages.map(img => ({ base64: img.base64, filename: img.filename }));
        }
        if (this.uploadedPDFs.length > 0) {
            payload.pdf_data = this.uploadedPDFs[0].base64;  // First PDF for backward compat
            payload.pdfs = this.uploadedPDFs.map(pdf => ({ base64: pdf.base64, filename: pdf.filename }));
        }
        if (this.uploadedDocuments.length > 0) {
            payload.document_data = this.uploadedDocuments[0].base64;  // First document for backward compat
            payload.documents = this.uploadedDocuments.map(doc => ({ base64: doc.base64, filename: doc.filename, type: doc.type }));
        }
        if (this.uploadedVideos.length > 0) {
            payload.videos = this.uploadedVideos.map(vid => ({ base64: vid.base64, filename: vid.filename }));
        }
        if (this.uploadedFiles.length > 0) {
            payload.file_content = this.uploadedFiles[0].content;  // First file for backward compat
            payload.files = this.uploadedFiles.map(file => ({ content: file.content, filename: file.filename }));
        }

        // Store payload for potential retry
        this.lastPayload = payload;

        // Send to server (with command prepended)
        this.ws.send(JSON.stringify(payload));

        // Clear upload after sending
        this.clearUpload();
        
        // Make removeAttachment available globally for onclick handlers
        if (!window.chatHandler) {
            window.chatHandler = this;
        }

        // Show typing indicator
        this.showTypingIndicator();
    }

    sendMessageDirect(content) {
        if (!content || !this.ws || this.ws.readyState !== WebSocket.OPEN) return;

        // Add to UI
        this.addMessage('user', content);

        // Notify mascot
        if (window.mascotController) {
            window.mascotController.onUserMessage();
        }

        // Send to server
        this.ws.send(JSON.stringify({
            type: 'message',
            content: content
        }));

        // Show typing indicator
        this.showTypingIndicator();
    }

    copyToClipboard(text) {
        // Copy text to clipboard
        if (!text) {
            console.error('copyToClipboard: No text provided');
            return;
        }

        // Decode escaped newlines
        const decodedText = text.replace(/\\n/g, '\n');

        navigator.clipboard.writeText(decodedText).then(() => {
            // Show brief feedback
            this.showNotification('Copied to clipboard!', 'success');
        }).catch(err => {
            console.error('Failed to copy:', err);
            // Fallback: select text in a temporary textarea
            const textarea = document.createElement('textarea');
            textarea.value = decodedText;
            textarea.style.position = 'fixed';
            textarea.style.opacity = '0';
            document.body.appendChild(textarea);
            textarea.select();
            try {
                document.execCommand('copy');
                this.showNotification('Copied to clipboard!', 'success');
            } catch (e) {
                this.showNotification('Failed to copy', 'error');
            }
            document.body.removeChild(textarea);
        });
    }

    openEditEventModal(uid) {
        // Fetch event data and open calendar modal for editing
        if (!uid) {
            console.error('openEditEventModal: No UID provided');
            return;
        }

        // For now, open modal with just the UID - user fills in new values
        // TODO: Fetch event data via API and pre-fill the form
        if (window.openCalendarModal) {
            window.openCalendarModal({ uid: uid });
        } else {
            console.error('openEditEventModal: Calendar modal not available');
        }
    }

    showNotification(message, type = 'info') {
        // Show a brief notification toast
        const existing = document.querySelector('.copy-toast');
        if (existing) existing.remove();

        const toast = document.createElement('div');
        toast.className = `copy-toast ${type}`;
        toast.textContent = message;
        toast.style.cssText = `
            position: fixed;
            bottom: 80px;
            left: 50%;
            transform: translateX(-50%);
            background: ${type === 'success' ? '#00ffff' : '#ff3366'};
            color: #000;
            padding: 8px 16px;
            border-radius: 4px;
            font-size: 14px;
            z-index: 10000;
            animation: fadeInOut 2s ease-in-out;
        `;
        document.body.appendChild(toast);
        setTimeout(() => toast.remove(), 2000);
    }

    async executeCommand(cmd) {
        // Execute a command from a button click
        // Validate command
        if (!cmd || typeof cmd !== 'string') {
            console.error('executeCommand: Invalid command', cmd);
            return;
        }

        // Decode HTML entities that may have been escaped
        const textarea = document.createElement('textarea');
        textarea.innerHTML = cmd;
        const decodedCmd = textarea.value;

        // Notes command removed - no longer intercepting notes commands

        // Intercept cal get commands to open edit modal in WebUI
        const calGetMatch = decodedCmd.match(/^cal\s+get\s+(\S+)/i);
        if (calGetMatch && window.openCalendarModal) {
            const uid = calGetMatch[1];
            // Fetch event data from API before opening modal
            fetch(`/api/auth/calendar/event/${encodeURIComponent(uid)}`)
                .then(response => {
                    if (!response.ok) throw new Error('Event not found');
                    return response.json();
                })
                .then(eventData => {
                    window.openCalendarModal(eventData);
                })
                .catch(err => {
                    console.error('Failed to fetch event:', err);
                    // Fall back to opening modal with just UID
                    window.openCalendarModal({ uid: uid });
                });
            return;  // Don't send to server
        }

        // Intercept contacts edit commands to open edit modal in WebUI
        const contactsEditMatch = decodedCmd.match(/^contacts\s+edit\s+(\S+)/i);
        if (contactsEditMatch && window.openContactsModal) {
            const uid = contactsEditMatch[1];
            window.openContactsModal(uid);
            return;  // Don't send to server
        }

        // Track mail read commands for "this email" voice support
        // Format: "mail read <id>" or "mail read <account> <id>"
        const mailReadMatch = decodedCmd.match(/^mail\s+read\s+(\S+)(?:\s+(\S+))?/i);
        if (mailReadMatch) {
            // If second group exists, first is account and second is ID
            // Otherwise first is just the ID
            const account = mailReadMatch[2] ? mailReadMatch[1] : 'default';
            const id = mailReadMatch[2] || mailReadMatch[1];
            this.lastReadEmail = { account, id };
            console.log('Tracked last read email:', this.lastReadEmail);
        }

        // Ensure we have a conversation - create one if needed
        if (!this.currentConversationId || !window.app?.currentConversation) {
            console.log('executeCommand: No conversation exists, creating one...');
            try {
                if (window.app && window.app.createConversation) {
                    await window.app.createConversation();
                    // Wait for WebSocket to actually connect (not just start connecting)
                    await new Promise((resolve, reject) => {
                        let attempts = 0;
                        const maxAttempts = 50; // 5 seconds max wait
                        const checkConnection = () => {
                            attempts++;
                            if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                                resolve();
                            } else if (attempts >= maxAttempts) {
                                reject(new Error('WebSocket connection timeout'));
                            } else {
                                setTimeout(checkConnection, 100);
                            }
                        };
                        checkConnection();
                    });
                } else {
                    console.error('executeCommand: Cannot create conversation - app.createConversation not available');
                    alert('Please create a conversation first');
                    return;
                }
            } catch (err) {
                console.error('executeCommand: Failed to create conversation:', err);
                alert('Failed to create conversation. Please refresh the page.');
                return;
            }
        }

        // Check WebSocket connection
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
            console.warn('executeCommand: WebSocket not connected, attempting reconnect...');
            // Try to reconnect and wait for connection
            if (this.currentConversationId) {
                this.connect(this.currentConversationId);
                // Wait for WebSocket to connect
                await new Promise((resolve, reject) => {
                    let attempts = 0;
                    const maxAttempts = 50; // 5 seconds max wait
                    const checkConnection = () => {
                        attempts++;
                        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                            resolve();
                        } else if (attempts >= maxAttempts) {
                            reject(new Error('WebSocket connection timeout'));
                        } else {
                            setTimeout(checkConnection, 100);
                        }
                    };
                    checkConnection();
                });
                // Retry the command after connection is established
                return this.executeCommand(decodedCmd);
            } else {
                // This shouldn't happen if the above check worked, but handle it anyway
                console.error('executeCommand: No conversation ID after creation');
                alert('Failed to establish connection. Please try again.');
                return;
            }
        }

        // If command ends with space, put in input for user to complete (e.g., reply)
        if (decodedCmd.endsWith(' ')) {
            this.messageInput.value = decodedCmd;
            this.messageInput.focus();
            // Place cursor at end
            this.messageInput.setSelectionRange(decodedCmd.length, decodedCmd.length);
            // Show autocomplete suggestions
            this.autocompleteCommand();
            // Show hint to user
            this.showToast("Type recipient and press Tab for autocomplete");
        } else {
            // Clear any mode (search, images, etc.) so command runs as-is
            if (window.app) {
                window.app.setMode('');
            }
            // Execute immediately
            this.messageInput.value = decodedCmd;
            this.sendMessage();
        }
    }

    handleMessage(data) {
        switch (data.type) {
            case 'stream':
                this.handleStreamChunk(typeof (data.data?.content ?? data.content) === 'string' ? (data.data?.content ?? data.content) : '');
                break;
            case 'stream_clear':
                // Clear current streaming content for follow-up 
                this.fullStreamContent = '';
                if (this.streamingMessage) {
                    const contentEl = this.streamingMessage.querySelector('.message-content');
                    contentEl.innerHTML = '';
                }
                break;
            case 'stream_end':
                this.handleStreamEnd();
                break;
            case 'response':
                this.handleCommandResponse(data.data);
                break;
            case 'error':
                this.handleError(data.message);
                break;
        }
    }

    handleStreamChunk(content) {
        // Ignore if no active connection
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;

        this.hideTypingIndicator();

        if (!this.streamingMessage) {
            this.streamingMessage = this.addMessage('assistant', '');
            this.fullStreamContent = '';
            this.thinkingMode = null; // null=unknown, true=in thinking, false=not thinking
        }

        // Buffer content (ensure string to avoid "undefined" when chunk is missing)
        this.fullStreamContent += (content != null && typeof content === 'string' ? content : '');

        // Strip thinking tags from display
        let displayContent = this.stripThinkingTags(this.fullStreamContent);

        // Only show content after thinking is done
        if (displayContent) {
            const contentEl = this.streamingMessage.querySelector('.message-content');
            contentEl.innerHTML = `<div class="message-body">${this.formatMessage(displayContent)}</div>`;
            this.scrollToBottom();
        }
    }

    stripThinkingTags(text) {
        // Find the last </think> or </thinking> tag and return everything after it
        const thinkEndMatch = text.match(/.*<\/think(?:ing)?>/is);
        if (thinkEndMatch) {
            return text.substring(thinkEndMatch[0].length).trim();
        }
        // If we see <think anywhere (with possible leading whitespace) but no closing tag yet, hide everything
        if (/^\s*<think/i.test(text) || /<think/i.test(text)) {
            return '';
        }
        // For short responses, show immediately (don't wait for 50 chars)
        // Only wait if we're not sure if thinking tag is coming (very short content)
        if (text.trim().length < 10) {
            // Very short - might have thinking tag, wait a bit
            return '';
        }
        // Show content if it's 10+ chars or if we're at stream end
        return text;
    }

    handleStreamEnd() {
        if (this.streamingMessage) {
            const contentEl = this.streamingMessage.querySelector('.message-content');
            // Use buffered content instead of reading from DOM, with thinking stripped
            const content = this.stripThinkingTags(this.fullStreamContent);

            // Always show content at stream end, even if it's short
            // Final render with complete content
            if (content || this.fullStreamContent.trim()) {
                // If stripThinkingTags returned empty but we have content, show it anyway
                const displayContent = content || this.fullStreamContent.trim();
                contentEl.innerHTML = `<div class="message-body">${this.formatMessage(displayContent)}</div>`;
            } else {
                contentEl.innerHTML = `<div class="message-body">${this.formatMessage('')}</div>`;
            }

            // Add copy button
            const copyBtn = document.createElement('button');
            copyBtn.className = 'btn-copy';
            copyBtn.innerHTML = '📋';
            copyBtn.title = 'Copy to clipboard';
            copyBtn.onclick = () => this.copyText(content);
            contentEl.appendChild(copyBtn);

            // Add email button
            const emailBtn = document.createElement('button');
            emailBtn.className = 'btn-email';
            emailBtn.innerHTML = '📧';
            emailBtn.title = 'Email this response';
            emailBtn.onclick = () => this.emailResponse(content);
            contentEl.appendChild(emailBtn);

            // Add regenerate button (only on latest message)
            if (this.lastPayload) {
                // Remove regenerate button from previous assistant messages
                const prevRegenBtns = this.messagesContainer.querySelectorAll('.message.assistant .btn-regenerate');
                prevRegenBtns.forEach(btn => btn.remove());

                const messageEl = this.streamingMessage;
                const regenBtn = document.createElement('button');
                regenBtn.className = 'btn-regenerate';
                regenBtn.innerHTML = '🔄';
                regenBtn.title = 'Regenerate response';
                regenBtn.onclick = () => this.regenerateResponse(messageEl);
                contentEl.appendChild(regenBtn);
            }

            // Add summarize icons to external links
            this.addSummarizeIcons(contentEl);

            // Notify mascot
            if (window.mascotController) {
                window.mascotController.onResponse(true);
            }

            // Speak if TTS enabled
            if (window.ttsController && window.ttsController.isEnabled() && content) {
                // Strip markdown formatting for TTS
                let ttsText = content
                    .replace(/^##?\s+[^\n]+\n+/gm, '')  // Remove markdown headers
                    .replace(/\*\*([^*]+)\*\*/g, '$1')   // Remove bold
                    .replace(/\*([^*]+)\*/g, '$1')       // Remove italic
                    .replace(/`([^`]+)`/g, '$1')         // Remove inline code
                    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')  // Remove links, keep text
                    .replace(/^---+$/gm, '')             // Remove horizontal rules
                    .trim();
                if (ttsText) {
                    window.ttsController.speak(ttsText);
                }
            }

            // Reset streaming state
            this.streamingMessage = null;
            this.streamBuffer = '';
            this.fullStreamContent = '';
            this.streamRafPending = false;
        }

        // Call stream end callback if set (used by news to delete prompt)
        if (this.onStreamEndCallback) {
            this.onStreamEndCallback();
            this.onStreamEndCallback = null;
        }

        this.resetSendButton();
    }

    escapeHtml(text) {
        if (text == null || typeof text !== 'string') return '';
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    escapeUrl(url) {
        if (!url) return '';
        // Only allow http/https URLs
        if (!url.match(/^https?:\/\//i)) return '#';
        return encodeURI(url);
    }

    handleCommandResponse(data) {
        // Always hide typing indicator and reset button first
        this.hideTypingIndicator();
        this.resetSendButton();
        
        // Remove streaming placeholder so we don't show it and the final response (e.g. 10 empty + 10 images)
        if (this.streamingMessage) {
            this.streamingMessage.remove();
            this.streamingMessage = null;
        }
        this.isStreaming = false;

        if (!data || typeof data !== 'object') {
            this.addMessage('assistant', 'No response data received.');
            return;
        }

        let html = '';
        let contentHtml = '';

        // Format content separately - don't mix with structured data (guard non-string to avoid "undefined" in output)
        if (data.content != null) {
            contentHtml = this.formatMessage(typeof data.content === 'string' ? data.content : String(data.content));
        }

        // Handle different response types
        if (data.type === 'images' && data.images) {
            // Remove any recent assistant message that could be showing image search (grid) or streamed image placeholders (message-body img)
            const recent = this.messagesContainer.querySelectorAll('.message.assistant');
            const now = Date.now();
            for (const el of recent) {
                const addedAt = parseInt(el.dataset.addedAt, 10);
                if (!addedAt || (now - addedAt) > 10000) continue;
                const hasGrid = el.querySelector('.image-grid');
                const bodyImgs = el.querySelectorAll('.message-body img');
                if (hasGrid || bodyImgs.length >= 10) el.remove();
            }
            // Build grid with DOM only: one message, one grid, exactly N image links (max 10)
            const srcKey = (img) => (img.thumb_id ? `/api/proxy-image/${img.thumb_id}` : (img.img_src || img.thumbnail_src || img.thumbnail || '').trim());
            const textLine = data.content ? String(data.content) : '';
            const rawList = Array.isArray(data.images) ? data.images : [];
            const seen = new Set();
            const imagesList = [];
            for (const img of rawList) {
                if (imagesList.length >= 10) break;
                const src = srcKey(img);
                if (!src) continue;
                if (!img.thumb_id && !src.startsWith('http') && !src.startsWith('data:')) continue;
                if (seen.has(src)) continue;
                seen.add(src);
                imagesList.push(img);
            }
            this.addImagesMessage(textLine, imagesList);
            return;
        } else if (data.type === 'generated_image') {
            console.log('[IMAGE] Received generated_image response:', {
                hasImage: !!data.image,
                imageLength: data.image ? data.image.length : 0,
                prompt: data.prompt,
                content: data.content
            });
            
            if (!data.image) {
                console.error('[IMAGE] No image data in response!');
                html = contentHtml || '<p>Image generation failed: No image data received</p>';
            } else {
                html = contentHtml;
                const imageId = 'img_' + Date.now();
                const saveButtonId = 'save_img_' + Date.now();
                html += `<div class="image-wrapper">
                    <img src="data:image/png;base64,${data.image}" alt="Generated image" class="generated-image" id="${imageId}">
                    <div class="image-actions">
                        <button class="btn-action" onclick="window.chatHandler.saveGeneratedImage('${imageId}', '${this.escapeHtml(data.prompt || '')}', '${saveButtonId}')" id="${saveButtonId}" title="Save to storage">💾</button>
                        <button class="btn-action" onclick="window.chatHandler.downloadImage('${imageId}')" title="Download">⬇️</button>
                        <button class="btn-action" onclick="window.chatHandler.copyImage('${imageId}')" title="Copy to clipboard">📋</button>
                    </div>
                </div>`;

                // Notify mascot for image generation
                if (window.mascotController) {
                    window.mascotController.onResponse(true);
                }
            }
        } else if (data.type === 'search' && data.results) {
            html = contentHtml;
            html += '<div class="search-results">';
            for (const r of data.results) {
                const safeUrl = this.escapeUrl(r.url ?? '');
                const safeTitle = this.escapeHtml(r.title ?? '');
                const safeContent = this.escapeHtml(r.content ?? '');
                html += `<div class="search-result">
                    <a href="${safeUrl}" target="_blank">${safeTitle}</a>
                    <p>${safeContent}</p>
                </div>`;
            }
            html += '</div>';
        } else if (data.type === 'mail_attachment' && data.data) {
            html = contentHtml;
            const attachmentId = 'attach_' + Date.now();
            const saveButtonId = 'save_attach_' + Date.now();
            
            if (data.mime_type && data.mime_type.startsWith('image/')) {
                // Display image preview
                html += `<div class="attachment-wrapper">
                    <img src="data:${data.mime_type};base64,${data.data}" alt="${this.escapeHtml(data.filename)}" class="attachment-image" id="${attachmentId}">
                    <div class="attachment-actions">
                        <button class="btn-action" onclick="window.chatHandler.saveMailAttachment('${data.data}', '${this.escapeHtml(data.filename)}', '${saveButtonId}')" id="${saveButtonId}" title="Save to storage">💾</button>
                        <button class="btn-action" onclick="window.chatHandler.downloadAttachment('${data.data}', '${this.escapeHtml(data.filename)}', '${data.mime_type}')" title="Download">⬇️</button>
                    </div>
                </div>`;
            } else {
                // Display download button for non-image attachments
                html += `<div class="attachment-wrapper">
                    <div class="attachment-info">
                        <strong>${this.escapeHtml(data.filename)}</strong> (${(data.size / 1024).toFixed(1)} KB)
                    </div>
                    <div class="attachment-actions">
                        <button class="btn-action" onclick="window.chatHandler.saveMailAttachment('${data.data}', '${this.escapeHtml(data.filename)}', '${saveButtonId}')" id="${saveButtonId}" title="Save to storage">💾</button>
                        <button class="btn-action" onclick="window.chatHandler.downloadAttachment('${data.data}', '${this.escapeHtml(data.filename)}', '${data.mime_type}')" title="Download">⬇️</button>
                    </div>
                </div>`;
            }
        } else if (data.type === 'files' && data.files) {
            // For files, put content first, then file results in a separate container
            // Ensure contentHtml doesn't break the structure - extract text from p tags if needed
            let cleanContent = contentHtml || '';
            // If contentHtml is wrapped in p tags, extract the content to avoid nesting issues
            if (cleanContent.trim().startsWith('<p') && cleanContent.trim().endsWith('</p>')) {
                const pMatch = cleanContent.match(/^<p[^>]*>(.*?)<\/p>$/s);
                if (pMatch) {
                    cleanContent = pMatch[1].trim();
                }
            }
            // Build HTML with proper structure - content and file results in separate containers
            html = '';
            if (cleanContent) {
                html += `<div class="file-command-content">${cleanContent}</div>`;
            }
            html += '<div class="file-search-results">';
            if (data.files.length === 0) {
                html += '<p class="no-results">No files found.</p>';
            } else {
                for (const file of data.files) {
                    const safeName = this.escapeHtml(file.name);
                    const safePath = this.escapeHtml(file.path);
                    const fileSize = this.formatFileSize(file.size || 0);
                    const modifiedDate = file.modified ? new Date(file.modified * 1000).toLocaleString() : '';
                    const fileId = 'file_' + Date.now() + '_' + Math.random().toString(36).substr(2, 9);
                    
                    // Escape file path and name for use in onclick handlers
                    const escapedPath = this.escapeHtml(file.path).replace(/'/g, "\\'").replace(/"/g, '&quot;');
                    const escapedName = safeName.replace(/'/g, "\\'").replace(/"/g, '&quot;');
                    
                    // Format path for display (show directory if different from filename)
                    let displayPath = '';
                    if (safePath !== safeName && safePath.includes('/')) {
                        const pathParts = safePath.split('/');
                        if (pathParts.length > 1) {
                            displayPath = pathParts.slice(0, -1).join('/') + '/';
                        }
                    }
                    
                    html += `<div class="file-result-item" data-file-path="${this.escapeHtml(file.path)}" data-file-name="${safeName}">
                        <div class="file-result-header">
                            ${file.thumbnail ? `<img src="${file.thumbnail}" alt="${safeName}" class="file-thumbnail">` : '<div class="file-icon">📄</div>'}
                            <div class="file-info">
                                <div class="file-name">${safeName}</div>
                                <div class="file-meta">
                                    ${displayPath ? `<span class="file-path" title="${safePath}">${displayPath}</span>` : ''}
                                    <span class="file-size">${fileSize}</span>
                                    ${modifiedDate ? `<span class="file-date">${modifiedDate}</span>` : ''}
                                </div>
                            </div>
                        </div>
                        <div class="file-actions" style="display:flex;flex-wrap:wrap;gap:8px;padding-top:12px;border-top:1px solid #2a2a3e;">
                            <button type="button" class="file-action-btn" data-action="open" data-path="${escapedPath}" data-name="${escapedName}" title="Open" style="padding:8px 10px;background:#4a4a6e;border:1px solid #6a6a8e;border-radius:6px;color:#fff;font-size:20px;line-height:1;cursor:pointer;min-width:36px;min-height:36px;-webkit-tap-highlight-color:transparent;">👁️</button>
                            <button type="button" class="file-action-btn" data-action="download" data-path="${escapedPath}" data-name="${escapedName}" title="Download" style="padding:8px 10px;background:#4a4a6e;border:1px solid #6a6a8e;border-radius:6px;color:#fff;font-size:20px;line-height:1;cursor:pointer;min-width:36px;min-height:36px;-webkit-tap-highlight-color:transparent;" onclick="var b=this,p=b.getAttribute('data-path')||'',n=b.getAttribute('data-name')||'';if(window.PosterchanAndroid&&typeof window.PosterchanAndroid.downloadFile==='function'){window.PosterchanAndroid.downloadFile(p,n);if(window.chatHandler&&window.chatHandler.showToast)window.chatHandler.showToast('Download started...');return false;}if(window.chatHandler&&window.chatHandler.downloadFile)window.chatHandler.downloadFile(p,n);return false;">⬇️</button>
                            <button type="button" class="file-action-btn" data-action="preview" data-path="${escapedPath}" data-name="${escapedName}" title="Preview URL (Quick Share)" style="padding:8px 10px;background:#4a4a6e;border:1px solid #6a6a8e;border-radius:6px;color:#fff;font-size:20px;line-height:1;cursor:pointer;min-width:36px;min-height:36px;-webkit-tap-highlight-color:transparent;">🔍</button>
                            <button type="button" class="file-action-btn" data-action="share" data-path="${escapedPath}" data-name="${escapedName}" title="Share Public URL" style="padding:8px 10px;background:#4a4a6e;border:1px solid #6a6a8e;border-radius:6px;color:#fff;font-size:20px;line-height:1;cursor:pointer;min-width:36px;min-height:36px;-webkit-tap-highlight-color:transparent;">🔗</button>
                            <button type="button" class="file-action-btn" data-action="email" data-path="${escapedPath}" data-name="${escapedName}" title="Email" style="padding:8px 10px;background:#4a4a6e;border:1px solid #6a6a8e;border-radius:6px;color:#fff;font-size:20px;line-height:1;cursor:pointer;min-width:36px;min-height:36px;-webkit-tap-highlight-color:transparent;">✉️</button>
                            <button type="button" class="file-action-btn file-action-delete" data-action="delete" data-path="${escapedPath}" data-name="${escapedName}" title="Delete" style="padding:8px 10px;background:#6a2a2a;border:1px solid #8a3a3a;border-radius:6px;color:#fff;font-size:20px;line-height:1;cursor:pointer;min-width:36px;min-height:36px;-webkit-tap-highlight-color:transparent;">🗑️</button>
                        </div>
                    </div>`;
                }
            }
            html += '</div>';
            
            // Debug: Log HTML structure for files command
            if (data.files && data.files.length > 0) {
                console.log('Files command HTML generated:', {
                    hasFileResults: html.includes('file-search-results'),
                    hasFileActions: html.includes('file-actions'),
                    hasButtons: html.includes('file-action-btn'),
                    htmlPreview: html.substring(0, 1000)
                });
            }
        } else if (data.type === 'text' && data.content && data.content.includes('🔍 **Search Results:**')) {
            html = contentHtml;
        } else if (data.type === '4chan') {
            html = contentHtml || '<p>Opening 4chan catalog.</p>';
        } else {
            // Default: just use formatted content
            html = contentHtml;
        }

        const messageEl = this.addMessage('assistant', html, true);

        if (data.type === '4chan' && data.board && typeof window.openFourchanModal === 'function') {
            setTimeout(() => window.openFourchanModal(data.board), 100);
        }
        
        // Attach event listeners to file action buttons using event delegation
        // This is more reliable than attaching to individual buttons
        if (messageEl && data.type === 'files') {
            // Wait a tick to ensure DOM is fully rendered
            setTimeout(() => {
                // Use event delegation on the message element
                messageEl.addEventListener('click', (e) => {
                    const btn = e.target.closest('.file-action-btn[data-action]');
                    if (!btn) return;
                    
                    e.preventDefault();
                    e.stopPropagation();
                    
                    const action = btn.dataset.action;
                    const filePath = btn.dataset.path;
                    const fileName = btn.dataset.name;
                    
                    console.log('File action clicked:', action, filePath, fileName);
                    
                    if (!window.chatHandler) {
                        console.error('chatHandler not available');
                        return;
                    }
                    
                    switch(action) {
                        case 'open':
                            window.chatHandler.openFile(filePath, fileName);
                            break;
                        case 'download':
                            window.chatHandler.downloadFile(filePath, fileName);
                            break;
                        case 'preview':
                            window.chatHandler.previewUrl(filePath, fileName);
                            break;
                        case 'share':
                            window.chatHandler.shareFile(filePath, fileName);
                            break;
                        case 'email':
                            window.chatHandler.emailFile(filePath, fileName);
                            break;
                        case 'delete':
                            window.chatHandler.deleteFile(filePath, fileName);
                            break;
                    }
                });
                
                // Debug: Check if buttons exist
                const buttons = messageEl.querySelectorAll('.file-action-btn');
                const fileResults = messageEl.querySelectorAll('.file-result-item');
                const fileActions = messageEl.querySelectorAll('.file-actions');
                console.log(`File command debug:`, {
                    buttons: buttons.length,
                    fileResults: fileResults.length,
                    fileActions: fileActions.length,
                    htmlLength: html.length
                });
                
                // If no buttons found, log the actual HTML structure
                if (buttons.length === 0) {
                    const fileSearchResults = messageEl.querySelector('.file-search-results');
                    if (fileSearchResults) {
                        console.warn('File search results found but no buttons:', fileSearchResults.innerHTML.substring(0, 500));
                    } else {
                        console.warn('No file-search-results container found in message');
                    }
                }
            }, 0);
        }
        
        // Ensure UI is updated and scroll to bottom
        this.scrollToBottom();

        // Notify mascot
        if (window.mascotController) {
            window.mascotController.onResponse(true);
        }

        // Speak if TTS enabled
        if (window.ttsController && window.ttsController.isEnabled() && data.content) {
            // Strip markdown formatting for TTS
            let ttsText = data.content
                .replace(/^##?\s+[^\n]+\n+/gm, '')  // Remove markdown headers
                .replace(/\*\*([^*]+)\*\*/g, '$1')   // Remove bold
                .replace(/\*([^*]+)\*/g, '$1')       // Remove italic
                .replace(/`([^`]+)`/g, '$1')         // Remove inline code
                .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')  // Remove links, keep text
                .replace(/^---+$/gm, '')             // Remove horizontal rules
                .trim();
            if (ttsText) {
                window.ttsController.speak(ttsText);
            }
        }
    }

    handleError(message) {
        // Always hide typing indicator and reset button
        this.hideTypingIndicator();
        this.resetSendButton();
        
        // Ensure we're not in streaming mode
        this.isStreaming = false;
        this.streamingMessage = null;

        // Show error as assistant message
        this.addMessage('assistant', `Error: ${message}`);
        
        // Ensure UI is updated
        this.scrollToBottom();

        // Add retry button to the last user message
        if (this.lastUserMessage && this.lastPayload) {
            // Remove existing retry button if any
            const existingBtn = this.lastUserMessage.querySelector('.btn-retry');
            if (existingBtn) existingBtn.remove();

            const retryBtn = document.createElement('button');
            retryBtn.className = 'btn-retry';
            retryBtn.innerHTML = '🔄 Retry';
            retryBtn.onclick = () => this.retryLastMessage();
            this.lastUserMessage.querySelector('.message-content').appendChild(retryBtn);
        }

        if (window.mascotController) {
            window.mascotController.onError();
        }
    }

    retryLastMessage() {
        if (!this.lastPayload) {
            this.showToast('No message to retry');
            return;
        }

        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
            this.showToast('Not connected');
            return;
        }

        // Remove retry button from user message
        if (this.lastUserMessage) {
            const retryBtn = this.lastUserMessage.querySelector('.btn-retry');
            if (retryBtn) retryBtn.remove();
        }

        // Show user that we're retrying
        this.showTypingIndicator();

        // Notify mascot
        if (window.mascotController) {
            const mode = window.app ? window.app.getMode() : '';
            if (mode === 'geni') {
                window.mascotController.onGeneratingImage();
            } else {
                window.mascotController.onUserMessage();
            }
        }

        // Resend the last payload
        this.ws.send(JSON.stringify(this.lastPayload));
    }

    regenerateResponse(messageEl) {
        if (!this.lastPayload) {
            this.showToast('No message to regenerate');
            return;
        }

        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
            this.showToast('Not connected');
            return;
        }

        // Remove the current assistant message
        if (messageEl) {
            messageEl.remove();
        }

        // Show typing indicator
        this.showTypingIndicator();

        // Notify mascot
        if (window.mascotController) {
            const mode = window.app ? window.app.getMode() : '';
            if (mode === 'geni') {
                window.mascotController.onGeneratingImage();
            } else {
                window.mascotController.onUserMessage();
            }
        }

        // Resend the last payload
        this.ws.send(JSON.stringify(this.lastPayload));
    }

    editMessage(messageEl, originalContent) {
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
            this.showToast('Not connected');
            return;
        }

        const contentEl = messageEl.querySelector('.message-content');
        if (!contentEl || contentEl.classList.contains('editing')) return;

        contentEl.classList.add('editing');

        // Store original HTML for cancel
        const originalHtml = contentEl.innerHTML;

        // Clean the content - remove button text and [with file/image] suffixes
        let cleanContent = originalContent
            .replace(/ \[with image\]$/, '')
            .replace(/ \[with file\]$/, '')
            .replace(/🔄$/, '')
            .replace(/📋$/, '')
            .replace(/✏️$/, '')
            .trim();

        // Create edit form
        const editForm = document.createElement('div');
        editForm.className = 'edit-form';
        editForm.innerHTML = `
            <textarea class="edit-textarea">${this.escapeHtml(cleanContent)}</textarea>
            <div class="edit-actions">
                <button class="btn-save" title="Save and resubmit">Save & Resubmit</button>
                <button class="btn-cancel" title="Cancel">Cancel</button>
            </div>
        `;

        contentEl.innerHTML = '';
        contentEl.appendChild(editForm);

        const textarea = editForm.querySelector('.edit-textarea');
        textarea.focus();
        textarea.setSelectionRange(textarea.value.length, textarea.value.length);

        // Auto-resize textarea
        textarea.style.height = 'auto';
        textarea.style.height = Math.min(textarea.scrollHeight, 200) + 'px';
        textarea.addEventListener('input', () => {
            textarea.style.height = 'auto';
            textarea.style.height = Math.min(textarea.scrollHeight, 200) + 'px';
        });

        // Handle save
        editForm.querySelector('.btn-save').onclick = () => {
            const newContent = textarea.value.trim();
            if (!newContent) {
                this.showToast('Message cannot be empty');
                return;
            }
            this.submitEditedMessage(messageEl, newContent);
        };

        // Handle cancel
        editForm.querySelector('.btn-cancel').onclick = () => {
            contentEl.innerHTML = originalHtml;
            contentEl.classList.remove('editing');
        };

        // Handle Escape to cancel, Ctrl+Enter to save
        textarea.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                contentEl.innerHTML = originalHtml;
                contentEl.classList.remove('editing');
            } else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
                e.preventDefault();
                const newContent = textarea.value.trim();
                if (newContent) {
                    this.submitEditedMessage(messageEl, newContent);
                }
            }
        });
    }

    submitEditedMessage(messageEl, newContent) {
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
            this.showToast('Not connected');
            return;
        }

        // Find and remove all messages after this one (including the AI response)
        let nextSibling = messageEl.nextElementSibling;
        while (nextSibling) {
            const toRemove = nextSibling;
            nextSibling = nextSibling.nextElementSibling;
            // Don't remove typing indicator
            if (!toRemove.classList.contains('typing')) {
                toRemove.remove();
            }
        }

        // Update the message content in the UI
        const contentEl = messageEl.querySelector('.message-content');
        contentEl.classList.remove('editing');
        contentEl.innerHTML = `<div class="message-body">${this.formatMessage(newContent)}</div>`;

        // Add action buttons back
        const editBtn = document.createElement('button');
        editBtn.className = 'btn-edit';
        editBtn.innerHTML = '✏️';
        editBtn.title = 'Edit and resubmit';
        editBtn.onclick = () => this.editMessage(messageEl, newContent);
        contentEl.appendChild(editBtn);

        const regenBtn = document.createElement('button');
        regenBtn.className = 'btn-regenerate';
        regenBtn.innerHTML = '🔄';
        regenBtn.title = 'Retry this message';
        regenBtn.onclick = () => this.retryLastMessage();
        contentEl.appendChild(regenBtn);

        // Update lastUserMessage reference
        this.lastUserMessage = messageEl;

        // Get current mode and prepend command if needed
        const mode = window.app ? window.app.getMode() : '';
        let content = newContent;
        if (mode) {
            content = `${mode} ${content}`;
        }

        // Build and send new payload
        const payload = {
            type: 'message',
            content: content
        };

        // Include stored image data
        if (messageEl._imageData) {
            payload.image_data = messageEl._imageData;
        } else if (messageEl._imagePath) {
            // For historical messages, use the stored image path
            payload.image_path = messageEl._imagePath;
        }

        // Store payload for potential retry
        this.lastPayload = payload;

        // Show typing indicator
        this.showTypingIndicator();

        // Notify mascot
        if (window.mascotController) {
            if (mode === 'geni') {
                window.mascotController.onGeneratingImage();
            } else {
                window.mascotController.onUserMessage();
            }
        }

        // Send to server
        this.ws.send(JSON.stringify(payload));
    }

    addMessage(role, content, isHtml = false, skipUserButtons = false, imagePath = null) {
        const messageEl = document.createElement('div');
        messageEl.className = `message ${role}`;
        messageEl.dataset.addedAt = String(Date.now());

        const contentEl = document.createElement('div');
        contentEl.className = 'message-content';

        // Ensure we never insert undefined (would render as literal "undefined")
        const safeContent = content != null && typeof content === 'string' ? content : (content != null ? String(content) : '');
        const bodyHtml = isHtml ? safeContent : this.formatMessage(safeContent);
        const safeBodyHtml = bodyHtml != null && typeof bodyHtml === 'string' ? bodyHtml : '';
        contentEl.innerHTML = `<div class="message-body">${safeBodyHtml}</div>`;

        // Add stored image if present
        if (imagePath) {
            // Store image path on element for editing later
            messageEl._imagePath = imagePath;

            const imgContainer = document.createElement('div');
            imgContainer.className = 'generated-image';
            const img = document.createElement('img');
            img.src = imagePath;
            img.alt = 'Stored image';
            img.style.maxWidth = '100%';
            img.style.borderRadius = '8px';
            img.style.marginTop = '10px';
            img.onclick = () => window.open(imagePath, '_blank');
            img.style.cursor = 'pointer';
            imgContainer.appendChild(img);
            contentEl.appendChild(imgContainer);
        }

        // Add buttons (skip for empty assistant messages - those are streaming placeholders)
        // But not if isHtml is true (that's a command response, not streaming)
        const isStreamingPlaceholder = role === 'assistant' && !content && !isHtml;

        if (!isStreamingPlaceholder) {
            // Add copy button to all messages
            const copyBtn = document.createElement('button');
            copyBtn.className = 'btn-copy';
            copyBtn.innerHTML = '📋';
            copyBtn.title = 'Copy to clipboard';
            copyBtn.onclick = () => {
                const body = contentEl.querySelector('.message-body');
                this.copyText(body ? body.textContent : contentEl.textContent);
            };
            contentEl.appendChild(copyBtn);

            // Add edit button for user messages (only when loading history, not when sending)
            if (role === 'user' && !skipUserButtons) {
                const editBtn = document.createElement('button');
                editBtn.className = 'btn-edit';
                editBtn.innerHTML = '✏️';
                editBtn.title = 'Edit and resubmit';
                editBtn.onclick = () => this.editMessage(messageEl, content);
                contentEl.appendChild(editBtn);
            }

            // Add email and regenerate buttons for assistant messages
            if (role === 'assistant') {
                // Add email button
                const emailBtn = document.createElement('button');
                emailBtn.className = 'btn-email';
                emailBtn.innerHTML = '📧';
                emailBtn.title = 'Email this response';
                emailBtn.onclick = () => this.emailResponse(content);
                contentEl.appendChild(emailBtn);

                if (this.lastPayload) {
                    // Remove regenerate button from previous assistant messages (not user messages)
                    const prevRegenBtns = this.messagesContainer.querySelectorAll('.message.assistant .btn-regenerate');
                    prevRegenBtns.forEach(btn => btn.remove());

                    const regenBtn = document.createElement('button');
                    regenBtn.className = 'btn-regenerate';
                    regenBtn.innerHTML = '🔄';
                    regenBtn.title = 'Regenerate response';
                    regenBtn.onclick = () => this.regenerateResponse(messageEl);
                    contentEl.appendChild(regenBtn);
                }
            }
        }

        messageEl.appendChild(contentEl);

        this.messagesContainer.appendChild(messageEl);
        this.scrollToBottom();

        // Add summarize icons to external links in assistant messages
        if (role === 'assistant') {
            this.addSummarizeIcons(contentEl);
        }

        return messageEl;
    }

    /** Image search: one message, one grid, exactly N image links (max 10). No innerHTML for grid to avoid any duplicate/placeholder issues. */
    addImagesMessage(captionText, imagesList) {
        const messageEl = document.createElement('div');
        messageEl.className = 'message assistant';
        messageEl.dataset.addedAt = String(Date.now());

        const contentEl = document.createElement('div');
        contentEl.className = 'message-content';

        const bodyEl = document.createElement('div');
        bodyEl.className = 'message-body';

        if (captionText && captionText.trim()) {
            const p = document.createElement('p');
            p.className = 'image-search-caption';
            p.textContent = captionText.trim();
            bodyEl.appendChild(p);
        }

        const gridEl = document.createElement('div');
        gridEl.className = 'image-grid';
        const srcKey = (img) => (img.thumb_id ? `/api/proxy-image/${img.thumb_id}` : (img.img_src || img.thumbnail_src || img.thumbnail || '').trim());
        for (const img of imagesList) {
            const src = srcKey(img);
            if (!src) continue;
            const a = document.createElement('a');
            a.href = img.url || src;
            a.target = '_blank';
            a.className = 'image-link';
            a.style.display = 'inline-block';
            const im = document.createElement('img');
            im.src = src;
            im.alt = (img.title || '').trim() || 'Image';
            im.loading = 'lazy';
            im.referrerPolicy = 'no-referrer';
            im.onerror = function () { if (a.parentNode) a.style.display = 'none'; };
            a.appendChild(im);
            gridEl.appendChild(a);
        }
        bodyEl.appendChild(gridEl);
        contentEl.appendChild(bodyEl);

        const copyBtn = document.createElement('button');
        copyBtn.className = 'btn-copy';
        copyBtn.innerHTML = '📋';
        copyBtn.title = 'Copy to clipboard';
        copyBtn.onclick = () => this.copyText(bodyEl.textContent);
        contentEl.appendChild(copyBtn);

        const emailBtn = document.createElement('button');
        emailBtn.className = 'btn-email';
        emailBtn.innerHTML = '📧';
        emailBtn.title = 'Email this response';
        emailBtn.onclick = () => this.emailResponse(bodyEl.textContent);
        contentEl.appendChild(emailBtn);

        if (this.lastPayload) {
            const prevRegenBtns = this.messagesContainer.querySelectorAll('.message.assistant .btn-regenerate');
            prevRegenBtns.forEach(btn => btn.remove());
            const regenBtn = document.createElement('button');
            regenBtn.className = 'btn-regenerate';
            regenBtn.innerHTML = '🔄';
            regenBtn.title = 'Regenerate response';
            regenBtn.onclick = () => this.regenerateResponse(messageEl);
            contentEl.appendChild(regenBtn);
        }

        messageEl.appendChild(contentEl);
        this.messagesContainer.appendChild(messageEl);
        const grid = messageEl.querySelector('.image-grid');
        if (grid) {
            const links = grid.querySelectorAll('.image-link');
            for (let i = 10; i < links.length; i++) links[i].remove();
        }
        // Ensure only one image-search message: remove any other assistant message that has .image-grid (duplicate response)
        const allWithGrid = this.messagesContainer.querySelectorAll('.message.assistant .image-grid');
        for (const g of allWithGrid) {
            const msg = g.closest('.message');
            if (msg && msg !== messageEl) msg.remove();
        }
        this.scrollToBottom();
        // Do not add summarize icons to image-search messages (would add 10 extra elements)
        return messageEl;
    }

    addSummarizeIcons(contentEl) {
        // Find all external links (not local paths). Skip image-search grid links (they are image thumbnails, not articles).
        const links = contentEl.querySelectorAll('a[href^="http"]');
        links.forEach(link => {
            if (link.closest('.image-grid')) return; // no summarize on image search results
            // Skip if already has summarize button
            if (link.nextElementSibling?.classList?.contains('btn-summarize')) return;

            const summarizeBtn = document.createElement('button');
            summarizeBtn.className = 'btn-summarize';
            summarizeBtn.innerHTML = '📄';
            summarizeBtn.title = 'Summarize article';
            summarizeBtn.onclick = async (e) => {
                e.preventDefault();
                e.stopPropagation();
                await this.summarizeArticle(link.href, link.textContent);
            };
            link.parentNode.insertBefore(summarizeBtn, link.nextSibling);
        });
    }

    async summarizeArticle(url, title) {
        // Show loading
        this.showTypingIndicator();

        try {
            const response = await fetch(`/api/news/summarize?url=${encodeURIComponent(url)}`);
            if (!response.ok) throw new Error(`HTTP ${response.status}`);

            const data = await response.json();
            this.hideTypingIndicator();

            if (data.summary) {
                this.addMessage('assistant', `**Summary of "${title}":**\n\n${data.summary}`);
            } else {
                this.addMessage('assistant', `Could not summarize the article.`);
            }
        } catch (err) {
            console.error('Failed to summarize:', err);
            this.hideTypingIndicator();
            this.addMessage('assistant', `Error summarizing article: ${err.message}`);
        }
    }

    async saveGeneratedImage(imageId, prompt, buttonId) {
        const img = document.getElementById(imageId);
        if (!img) {
            console.error('Image not found:', imageId);
            return;
        }
        
        const button = document.getElementById(buttonId);
        if (button) {
            button.disabled = true;
            button.textContent = '💾 Saving...';
        }
        
        try {
            // Get base64 data from image src
            const imageData = img.src.replace('data:image/png;base64,', '');
            
            const response = await fetch('/api/save-generated-image', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    image: imageData,
                    prompt: prompt || ''
                })
            });
            
            const result = await response.json();
            
            if (result.success) {
                if (button) {
                    button.textContent = '✅ Saved';
                    button.title = `Saved to: ${result.path}`;
                    setTimeout(() => {
                        button.textContent = '💾';
                        button.disabled = false;
                    }, 2000);
                }
                // Show success message
                this.showNotification(`Image saved to: ${result.path}`, 'success');
            } else {
                throw new Error(result.error || 'Failed to save image');
            }
        } catch (error) {
            console.error('Error saving image:', error);
            if (button) {
                button.textContent = '❌ Error';
                button.disabled = false;
                setTimeout(() => {
                    button.textContent = '💾';
                }, 2000);
            }
            this.showNotification('Failed to save image: ' + error.message, 'error');
        }
    }

    async saveMailAttachment(attachmentData, filename, buttonId) {
        const button = document.getElementById(buttonId);
        if (button) {
            button.disabled = true;
            button.textContent = '💾 Saving...';
        }
        
        try {
            const response = await fetch('/api/save-mail-attachment', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    data: attachmentData,
                    filename: filename
                })
            });
            
            const result = await response.json();
            
            if (result.success) {
                if (button) {
                    button.textContent = '✅ Saved';
                    button.title = `Saved to: ${result.path}`;
                    setTimeout(() => {
                        button.textContent = '💾';
                        button.disabled = false;
                    }, 2000);
                }
                // Show success message with link
                this.showNotification(`Attachment saved to: ${result.path}`, 'success');
            } else {
                throw new Error(result.error || 'Failed to save attachment');
            }
        } catch (error) {
            console.error('Error saving attachment:', error);
            if (button) {
                button.textContent = '❌ Error';
                button.disabled = false;
                setTimeout(() => {
                    button.textContent = '💾';
                }, 2000);
            }
            this.showNotification('Failed to save attachment: ' + error.message, 'error');
        }
    }

    downloadImage(imageId) {
        const img = document.getElementById(imageId);
        if (!img) return;

        const link = document.createElement('a');
        link.download = `posterchanai_${Date.now()}.png`;
        link.href = img.src;
        link.click();
    }

    async openFile(filePath, fileName) {
        // Open file in new tab/window
        // Encode path segments separately to preserve slashes
        const pathSegments = filePath.split('/').map(seg => encodeURIComponent(seg));
        const encodedPath = pathSegments.join('/');
        const url = `/api/files/view/${encodedPath}`;
        
        // Check if it's an image or PDF that should open in browser
        const ext = fileName.split('.').pop().toLowerCase();
        const isImage = ['jpg', 'jpeg', 'png', 'gif', 'webp', 'svg', 'bmp'].includes(ext);
        const isPdf = ext === 'pdf';
        
        if (isImage || isPdf) {
            // Open directly in new tab - browser will display it
            window.open(url, '_blank');
            this.showToast(`Opening ${fileName}...`);
        } else {
            // For other files, open in new tab (browser may download or display)
            window.open(url, '_blank');
            this.showToast(`Opening ${fileName}...`);
        }
    }
    
    async downloadFile(filePath, fileName) {
        try {
            // Android WebView: use native bridge so download runs with auth (link click often doesn't navigate)
            if (typeof window.PosterchanAndroid !== 'undefined' && typeof window.PosterchanAndroid.downloadFile === 'function') {
                window.PosterchanAndroid.downloadFile(filePath, fileName || '');
                this.showToast('Download started...');
                return;
            }

            // Encode path segments separately to preserve slashes
            const pathSegments = filePath.split('/').map(seg => encodeURIComponent(seg));
            const encodedPath = pathSegments.join('/');
            const url = `/api/files/view/${encodedPath}`;
            
            console.log('Downloading file:', { filePath, fileName, url });
            
            // Use a simple approach: create a download link and trigger it
            // This is more reliable than fetch+blob for downloads
            const a = document.createElement('a');
            a.href = url;
            a.download = fileName;
            a.style.display = 'none';
            a.target = '_blank'; // Open in new tab as fallback
            
            // Add to DOM, click, then remove
            document.body.appendChild(a);
            a.click();
            
            // Clean up after a short delay
            setTimeout(() => {
                if (document.body.contains(a)) {
                    document.body.removeChild(a);
                }
            }, 1000);
            
            this.showToast('Download started...');
        } catch (error) {
            console.error('Error downloading file:', error);
            const errorMsg = error.message || 'Unknown error';
            this.showToast(`Error downloading file: ${errorMsg}`, 'error');
        }
    }

    async previewUrl(filePath, fileName) {
        // Check for existing share first, then create if needed
        try {
            // Check for existing shares
            const sharesResponse = await fetch('/api/files/shares');
            if (sharesResponse.ok) {
                const sharesData = await sharesResponse.json();
                // API returns {"shares": [...]}, so extract the array
                let shares = [];
                if (Array.isArray(sharesData)) {
                    shares = sharesData;
                } else if (sharesData && typeof sharesData === 'object') {
                    // API returns {shares: [...]}
                    shares = sharesData.shares || sharesData.items || [];
                }
                
                // Ensure shares is an array before using .find()
                if (!Array.isArray(shares)) {
                    console.warn('Shares response is not an array:', sharesData);
                    shares = [];
                }
                
                const existingShare = shares.find(s => s && s.file_path === filePath);
                
                if (existingShare) {
                    const url = window.location.origin + existingShare.share_url;
                    await navigator.clipboard.writeText(url);
                    this.showUrlPreview(url, fileName, true);
                    return;
                }
            }
            
            // Create new share
            const response = await csrfFetch('/api/files/share', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ file_path: filePath })
            });
            
            if (!response.ok) {
                throw new Error('Failed to create share');
            }
            
            const data = await response.json();
            const url = window.location.origin + data.share_url;
            await navigator.clipboard.writeText(url);
            this.showUrlPreview(url, fileName, false);
        } catch (error) {
            console.error('Error getting preview URL:', error);
            this.showToast('Error getting preview URL', 'error');
        }
    }
    
    showUrlPreview(url, fileName, isExisting) {
        const message = isExisting 
            ? `Existing public URL for "${fileName}" (copied to clipboard):\n\n${url}`
            : `Public URL for "${fileName}" (copied to clipboard):\n\n${url}`;
        alert(message);
    }

    async shareFile(filePath, fileName) {
        // Check if fileShareModal exists (file manager page)
        const modal = document.getElementById('fileShareModal');
        if (modal && window.fileManager && typeof window.fileManager.shareFile === 'function') {
            window.fileManager.shareFile(filePath, fileName);
            return;
        }

        // Create share directly for chat interface (no modal available)
        try {
            const response = await csrfFetch('/api/files/share', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ file_path: filePath })
            });
            if (!response.ok) {
                throw new Error('Failed to create share');
            }
            const data = await response.json();
            const shareUrl = window.location.origin + data.share_url;

            // Copy to clipboard
            await navigator.clipboard.writeText(shareUrl);
            this.showToast(`Public share URL copied to clipboard!\n${shareUrl}`, 'success');
        } catch (error) {
            console.error('Error sharing file:', error);
            this.showToast('Error creating share link: ' + error.message, 'error');
        }
    }

    async emailFile(filePath, fileName, apiUrl = null) {
        // Use chat command to email file via AI
        const input = document.getElementById('message-input');
        if (input && this.sendMessage) {
            input.value = `email ${fileName}`;
            // Directly call sendMessage instead of clicking button (works better on mobile)
            await this.sendMessage();
            return;
        }

        // Fallback: Try to use FileManager if available
        if (window.fileManager && typeof window.fileManager.emailFile === 'function') {
            try {
                await window.fileManager.emailFile(filePath, fileName, apiUrl);
                return;
            } catch (e) {
                console.warn('FileManager.emailFile failed:', e);
            }
        }

        // Last resort: Show email modal directly if it exists
        const modal = document.getElementById('fileEmailModal');
        if (modal) {
            const emailFilesList = document.getElementById('emailFilesList');
            if (emailFilesList) {
                // Add file to the list (support multiple files)
                const fileItem = document.createElement('div');
                fileItem.className = 'email-file-item';
                fileItem.dataset.filePath = filePath || '';
                fileItem.dataset.fileName = fileName || 'Unknown file';
                if (apiUrl) {
                    fileItem.dataset.apiUrl = apiUrl;
                }
                const escapedName = this.escapeHtml ? this.escapeHtml(fileName || 'Unknown file') : (fileName || 'Unknown file').replace(/</g, '&lt;').replace(/>/g, '&gt;');
                fileItem.innerHTML = `
                    <span class="email-file-name">${escapedName}</span>
                    <button class="email-file-remove" onclick="this.parentElement.remove(); if(window.fileManager && window.fileManager.updateEmailSubjectAndBody) window.fileManager.updateEmailSubjectAndBody();" title="Remove">✕</button>
                `;
                emailFilesList.appendChild(fileItem);
                
                // Update subject and body
                if (window.fileManager && typeof window.fileManager.updateEmailSubjectAndBody === 'function') {
                    window.fileManager.updateEmailSubjectAndBody();
                } else {
                    // Fallback update
                    const fileItems = emailFilesList.querySelectorAll('.email-file-item');
                    const fileCount = fileItems.length;
                    const emailSubjectInput = document.getElementById('emailSubject');
                    const emailBodyInput = document.getElementById('emailBody');
                    if (emailSubjectInput && emailBodyInput) {
                        if (fileCount === 1) {
                            emailSubjectInput.value = `Shared file: ${fileName}`;
                            emailBodyInput.value = `Please find the attached file: ${fileName}`;
                        } else {
                            emailSubjectInput.value = `Shared files (${fileCount} files)`;
                            const fileNames = Array.from(fileItems).map(item => item.dataset.fileName).join(', ');
                            emailBodyInput.value = `Please find the attached files:\n${fileNames}`;
                        }
                    }
                }
            }
            
            const emailToInput = document.getElementById('emailTo');
            if (emailToInput) emailToInput.value = '';
            
            // Show modal
            modal.style.display = 'flex';
            setTimeout(() => {
                if (emailToInput) emailToInput.focus();
            }, 100);
        } else {
            // Last resort: prompt
            console.warn('Email modal not found, using prompt');
            const to = prompt('Enter email address:');
            if (!to) return;
            
            try {
                const requestBody = {
                    to: to,
                    subject: `Shared file: ${fileName}`,
                    body: `Please find the attached file: ${fileName}`
                };
                
                if (apiUrl) {
                    requestBody.file_urls = [apiUrl];
                } else {
                    requestBody.file_paths = [filePath];
                }
                
                const response = await fetch('/api/files/email', {
                    method: 'POST',
                    headers: { 
                        'Content-Type': 'application/json',
                        'X-CSRFToken': document.querySelector('meta[name="csrf-token"]')?.content || ''
                    },
                    credentials: 'include',
                    body: JSON.stringify(requestBody)
                });
                
                if (!response.ok) {
                    const error = await response.json().catch(() => ({ detail: 'Failed to send email' }));
                    throw new Error(error.detail || 'Failed to send email');
                }
                
                this.showToast('Email sent successfully');
            } catch (error) {
                console.error('Error sending email:', error);
                this.showToast(`Error sending email: ${error.message}`, 'error');
            }
        }
    }
    
    async sendEmailFromModal() {
        // Fallback sendEmail function when FileManager is not available
        const emailFilesList = document.getElementById('emailFilesList');
        const emailToInput = document.getElementById('emailTo');
        const emailSubjectInput = document.getElementById('emailSubject');
        const emailBodyInput = document.getElementById('emailBody');
        
        if (!emailFilesList || !emailToInput) {
            alert('Email form error. Please refresh the page.');
            return;
        }
        
        // Get all selected files
        const fileItems = emailFilesList.querySelectorAll('.email-file-item');
        if (fileItems.length === 0) {
            alert('Please select at least one file to email');
            return;
        }
        
        let to = emailToInput.value.trim();
        const subject = emailSubjectInput?.value.trim() || 'Shared files';
        const body = emailBodyInput?.value.trim() || 'Please find the attached files.';
        
        if (!to) {
            alert('Please enter recipient email address');
            return;
        }
        
        // Extract email from "Name <email>" format if present
        const emailMatch = to.match(/<([^>]+)>/);
        if (emailMatch) {
            to = emailMatch[1];
        }
        
        // Basic email validation
        const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
        if (!emailRegex.test(to)) {
            alert('Please enter a valid email address');
            return;
        }
        
        try {
            // Collect file paths and URLs
            const filePaths = [];
            const fileUrls = [];
            
            fileItems.forEach(item => {
                const filePath = item.dataset.filePath;
                const apiUrl = item.dataset.apiUrl;
                
                if (apiUrl) {
                    fileUrls.push(apiUrl);
                } else if (filePath) {
                    filePaths.push(filePath);
                }
            });
            
            const requestBody = {
                to: to,
                subject: subject,
                body: body
            };
            
            if (fileUrls.length > 0) {
                requestBody.file_urls = fileUrls;
            }
            if (filePaths.length > 0) {
                requestBody.file_paths = filePaths;
            }
            
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || '';
            const response = await fetch('/api/files/email', {
                method: 'POST',
                headers: { 
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken
                },
                credentials: 'include',
                body: JSON.stringify(requestBody)
            });
            
            const data = await response.json();
            if (response.ok) {
                const fileCount = filePaths.length + fileUrls.length;
                alert(`Email sent successfully with ${fileCount} file(s)!`);
                const emailModal = document.getElementById('fileEmailModal');
                if (emailModal) {
                    emailFilesList.innerHTML = '';
                    emailModal.style.display = 'none';
                }
            } else {
                alert('Error: ' + (data.detail || 'Failed to send email'));
            }
        } catch (error) {
            console.error('Error sending email:', error);
            alert('Error sending email. Please try again.');
        }
    }

    async deleteFile(filePath, fileName) {
        if (!confirm(`Are you sure you want to delete "${fileName}"? This action cannot be undone.`)) {
            return;
        }
        
        try {
            const response = await csrfFetch(`/api/files/delete?file_path=${encodeURIComponent(filePath)}`, {
                method: 'DELETE'
            });
            if (!response.ok) {
                throw new Error('Failed to delete file');
            }
            this.showToast('File deleted successfully');
            
            // Remove the file result item from the UI
            const fileItems = document.querySelectorAll(`.file-result-item[data-file-path="${this.escapeHtml(filePath)}"]`);
            fileItems.forEach(item => {
                item.style.opacity = '0.5';
                item.style.textDecoration = 'line-through';
                setTimeout(() => item.remove(), 500);
            });
        } catch (error) {
            console.error('Error deleting file:', error);
            this.showToast('Error deleting file', 'error');
        }
    }

    async copyImage(imageId) {
        const img = document.getElementById(imageId);
        if (!img) return;

        try {
            const response = await fetch(img.src);
            const blob = await response.blob();
            await navigator.clipboard.write([
                new ClipboardItem({ 'image/png': blob })
            ]);
            this.showToast('Image copied to clipboard!');
        } catch (err) {
            console.error('Failed to copy image:', err);
            this.showToast('Failed to copy image');
        }
    }

    downloadAttachment(base64Data, filename, mimeType) {
        try {
            // Convert base64 to blob
            const byteCharacters = atob(base64Data);
            const byteNumbers = new Array(byteCharacters.length);
            for (let i = 0; i < byteCharacters.length; i++) {
                byteNumbers[i] = byteCharacters.charCodeAt(i);
            }
            const byteArray = new Uint8Array(byteNumbers);
            const blob = new Blob([byteArray], { type: mimeType || 'application/octet-stream' });
            
            // Create download link
            const url = URL.createObjectURL(blob);
            const link = document.createElement('a');
            link.href = url;
            link.download = filename;
            link.style.display = 'none';
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
            URL.revokeObjectURL(url);
            
            this.showToast(`Downloading ${filename}...`);
        } catch (err) {
            console.error('Failed to download attachment:', err);
            this.showToast('Failed to download attachment');
        }
    }

    async emailResponse(content) {
        if (!this.notificationEmail) {
            // Try to load settings first
            try {
                const response = await fetch('/api/auth/settings');
                if (response.ok) {
                    const data = await response.json();
                    this.notificationEmail = data.notification_email;
                }
            } catch (e) {
                console.error('Failed to load settings:', e);
            }
        }

        if (!this.notificationEmail) {
            this.showToast('Please set your email in Settings first');
            // Open settings modal
            const settingsModal = document.getElementById('userSettingsModal');
            if (settingsModal) settingsModal.style.display = 'flex';
            return;
        }

        try {
            const response = await csrfFetch('/api/chat/email-response', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ content: content })
            });

            if (response.ok) {
                this.showToast('Response sent to your email!');
            } else {
                const data = await response.json();
                this.showToast(data.detail || 'Failed to send email');
            }
        } catch (e) {
            console.error('Failed to email response:', e);
            this.showToast('Failed to send email');
        }
    }

    copyText(text) {
        // Clean up text - remove button text that might be included
        const cleanText = text.replace(/🔄$/, '').replace(/📋$/, '').replace(/📧$/, '').replace(/🔄 Retry$/, '').trim();

        // Use fallback method that works in all contexts
        try {
            const textArea = document.createElement('textarea');
            textArea.value = cleanText;
            textArea.style.position = 'fixed';
            textArea.style.left = '-9999px';
            textArea.style.top = '0';
            document.body.appendChild(textArea);
            textArea.focus();
            textArea.select();
            const success = document.execCommand('copy');
            document.body.removeChild(textArea);
            if (success) {
                this.showToast('Copied to clipboard!');
            } else {
                this.showToast('Failed to copy');
            }
        } catch (err) {
            console.error('Failed to copy:', err);
            this.showToast('Failed to copy');
        }
    }

    showToast(message) {
        const toast = document.createElement('div');
        toast.className = 'toast';
        toast.textContent = message;
        document.body.appendChild(toast);
        setTimeout(() => toast.remove(), 2000);
    }

    formatMessage(text) {
        if (text == null || typeof text !== 'string') return '';

        // Strip LLM thinking tags (Qwen and other models use <think>...</think>)
        // First remove complete think blocks
        text = text.replace(/<think>[\s\S]*?<\/think>/gi, '');
        // Also remove unclosed think tags (during streaming)
        text = text.replace(/<think>[\s\S]*$/gi, '');
        text = text.trim();

        // First, extract and preserve fenced code blocks before escaping
        // But treat ```markdown blocks as regular text (just strip the fence)
        text = text.replace(/```markdown\n?([\s\S]*?)```/gi, '$1');

        const codeBlocks = [];
        let processed = text.replace(/```(\w*)\n?([\s\S]*?)```/g, (match, lang, code) => {
            const index = codeBlocks.length;
            codeBlocks.push({ lang: lang || '', code: code.trimEnd() });
            return `\x00CODEBLOCK${index}\x00`;
        });

        // Auto-detect: if entire message starts with shebang, treat it all as code
        if (/^#!\//.test(processed.trim()) && !processed.includes('\x00CODEBLOCK')) {
            const trimmed = processed.trim();
            let lang = 'bash';
            if (trimmed.includes('python')) lang = 'python';
            else if (trimmed.includes('node')) lang = 'javascript';
            else if (trimmed.includes('ruby')) lang = 'ruby';
            else if (trimmed.includes('perl')) lang = 'perl';

            const index = codeBlocks.length;
            codeBlocks.push({ lang, code: trimmed });
            processed = `\x00CODEBLOCK${index}\x00`;
        }

        // Extract markdown images ![alt](url) before link processing (so link regex doesn't consume them)
        const images = [];
        // Absolute URLs (http/https) - e.g. https://img.youtube.com/vi/xyz/hqdefault.jpg
        processed = processed.replace(/!\[([^\]]*)\]\(\s*(https?:\/\/[^)\s]+)\s*\)/g, (match, alt, url) => {
            const index = images.length;
            images.push({ alt: alt.trim(), url: url.trim() });
            return `\x00IMG${index}\x00`;
        });
        // Relative URLs (e.g. /api/youtube-thumbnail?video_id=...) used by YouTube thumbnails
        processed = processed.replace(/!\[([^\]]*)\]\(\s*(\/[^)\s]+)\s*\)/g, (match, alt, url) => {
            const index = images.length;
            images.push({ alt: alt.trim(), url: url.trim() });
            return `\x00IMG${index}\x00`;
        });

        // Extract and preserve HTML links BEFORE processing markdown (so they don't get escaped)
        const htmlLinks = [];
        processed = processed.replace(/<a\s+([^>]*?)href=["']([^"']+)["']([^>]*?)>(.*?)<\/a>/gi, (match, before, url, after, text) => {
            const index = htmlLinks.length;
            // Extract target and rel attributes if present
            const targetMatch = (before + after).match(/target=["']([^"']+)["']/i);
            const relMatch = (before + after).match(/rel=["']([^"']+)["']/i);
            htmlLinks.push({
                url: url.trim(),
                text: text.trim(),
                target: targetMatch ? targetMatch[1] : null,
                rel: relMatch ? relMatch[1] : null
            });
            return `\x00HTMLLINK${index}\x00`;
        });
        
        // Process markdown links BEFORE escaping (preserve URLs)
        const links = [];
        // First, fix malformed links where URL is on a new line (most common issue)
        // Pattern: [text](\nhttps://... or [text](\nwww....
        // This handles cases where the URL starts on the next line after the opening paren
        processed = processed.replace(/\[([^\]]+)\]\(\s*\n\s*(https?:\/\/[^\s\)\n]+)/g, (match, text, url) => {
            const index = links.length;
            links.push({ text: text.trim(), url: url.trim(), external: true });
            return `\x00LINK${index}\x00`;
        });
        processed = processed.replace(/\[([^\]]+)\]\(\s*\n\s*(www\.[^\s\)\n]+)/g, (match, text, url) => {
            const index = links.length;
            links.push({ text: text.trim(), url: 'https://' + url.trim(), external: true });
            return `\x00LINK${index}\x00`;
        });
        // Also handle cases where there's a closing paren on a later line: [text](\nurl\n)
        processed = processed.replace(/\[([^\]]+)\]\(\s*\n\s*(https?:\/\/[^\s\)\n]+)\s*\n\s*\)/g, (match, text, url) => {
            const index = links.length;
            links.push({ text: text.trim(), url: url.trim(), external: true });
            return `\x00LINK${index}\x00`;
        });
        processed = processed.replace(/\[([^\]]+)\]\(\s*\n\s*(www\.[^\s\)\n]+)\s*\n\s*\)/g, (match, text, url) => {
            const index = links.length;
            links.push({ text: text.trim(), url: 'https://' + url.trim(), external: true });
            return `\x00LINK${index}\x00`;
        });
        // Match http/https links - handle URLs with balanced parens (e.g., Wikipedia)
        // Allow whitespace around the URL
        processed = processed.replace(/\[([^\]]+)\]\(\s*(https?:\/\/[^)\s]+(?:\([^)]*\)[^)\s]*)*)\s*\)/g, (match, text, url) => {
            const index = links.length;
            links.push({ text: text.trim(), url: url.trim(), external: true });
            return `\x00LINK${index}\x00`;
        });
        // Match www. links
        processed = processed.replace(/\[([^\]]+)\]\(\s*(www\.[^)\s]+(?:\([^)]*\)[^)\s]*)*)\s*\)/g, (match, text, url) => {
            const index = links.length;
            links.push({ text: text.trim(), url: 'https://' + url.trim(), external: true });
            return `\x00LINK${index}\x00`;
        });
        // Match tel: links (phone numbers)
        processed = processed.replace(/\[([^\]]+)\]\(\s*(tel:[^)\s]+)\s*\)/g, (match, text, url) => {
            const index = links.length;
            links.push({ text: text.trim(), url: url.trim(), external: false });
            return `\x00LINK${index}\x00`;
        });
        // Match mailto: links (email addresses)
        processed = processed.replace(/\[([^\]]+)\]\(\s*(mailto:[^)\s]+)\s*\)/g, (match, text, url) => {
            const index = links.length;
            links.push({ text: text.trim(), url: url.trim(), external: false });
            return `\x00LINK${index}\x00`;
        });
        // Match relative URL links (starting with /) - for attachments, etc.
        processed = processed.replace(/\[([^\]]+)\]\(\s*(\/[^)\s]+)\s*\)/g, (match, text, url) => {
            const index = links.length;
            links.push({ text: text.trim(), url: url.trim(), external: true });  // Open in new tab
            return `\x00LINK${index}\x00`;
        });
        // Match cmd: links (command execution buttons)
        processed = processed.replace(/\[([^\]]+)\]\(cmd:([^)]+)\)/g, (match, text, cmd) => {
            const index = links.length;
            links.push({ text, cmd, isCommand: true });
            return `\x00LINK${index}\x00`;
        });
        // Match edit-event: links (calendar event edit buttons)
        processed = processed.replace(/\[([^\]]+)\]\(edit-event:([^)]+)\)/g, (match, text, uid) => {
            const index = links.length;
            links.push({ text, uid, isEditEvent: true });
            return `\x00LINK${index}\x00`;
        });
        // Match copy: links (clipboard copy buttons)
        processed = processed.replace(/\[([^\]]+)\]\(copy:([^)]+)\)/g, (match, text, content) => {
            const index = links.length;
            links.push({ text, content: decodeURIComponent(content), isCopy: true });
            return `\x00LINK${index}\x00`;
        });
        // Catch-all for any remaining markdown links (e.g., tracking URLs, other protocols)
        processed = processed.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (match, text, url) => {
            const index = links.length;
            // Determine if external based on URL format
            const isExternal = url.startsWith('http') || url.startsWith('//') || url.includes('.');
            links.push({ text, url, external: isExternal });
            return `\x00LINK${index}\x00`;
        });

        // Convert markdown headers and blockquotes BEFORE escaping
        // Headers: ### Header -> <h3>Header</h3>
        processed = processed.replace(/^###\s+(.+)$/gm, '<h3>$1</h3>');
        processed = processed.replace(/^##\s+(.+)$/gm, '<h2>$1</h2>');
        processed = processed.replace(/^#\s+(.+)$/gm, '<h1>$1</h1>');
        // Blockquotes: > text -> <blockquote>text</blockquote>
        // Handle single or multiple > at start of line (including nested blockquotes)
        processed = processed.replace(/^(>+\s*)(.+)$/gm, (match, prefix, content) => {
            return `<blockquote>${content}</blockquote>`;
        });

        // Also handle raw HTML tags already in the output (LLM sometimes generates them directly)
        // Convert <h3>, <h2>, <h1>, <blockquote> tags to placeholders before escaping
        processed = processed.replace(/<(h[123]|blockquote)\b[^>]*>(.*?)<\/\1>/gi, (match, tag, content) => {
            return `\x00HTMLTAG_${tag}\x00${content}\x00/HTMLTAG_${tag}\x00`;
        });

        // Handle LLM output that contains placeholder strings like "HTMLTAG_h3" without < >
        processed = processed.replace(/HTMLTAG_(h[123]|blockquote)(.+?)\/HTMLTAG_\1/gi, (match, tag, content) => {
            return `\x00HTMLTAG_${tag}\x00${content}\x00/HTMLTAG_${tag}\x00`;
        });

        // Escape HTML
        let html = processed
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');

        // Restore preserved HTML tags from escaped form
        html = html.replace(/&lt;(h[123]|blockquote)\b[^&]*&gt;(.*?)&lt;\/\1&gt;/gi, (match, tag, content) => {
            return `<${tag}>${content}</${tag}>`;
        });

        // Fallback: directly replace any remaining HTMLTAG placeholders in HTML output
        html = html.replace(/HTMLTAG_(h[123]|blockquote)(.+?)\/HTMLTAG_\1/gi, (match, tag, content) => {
            return `<${tag}>${content}</${tag}>`;
        });

        // Restore markdown images as <img> (before links so order is preserved)
        html = html.replace(/\x00IMG(\d+)\x00/g, (match, index) => {
            const img = images[parseInt(index)];
            if (!img || !img.url) return '';
            const imgAlt = img.alt ?? '';
            let src = img.url;
            // Use same-origin proxy for YouTube thumbnails so they load in chat
            const ytMatch = src.match(/^https?:\/\/img\.youtube\.com\/vi\/([a-zA-Z0-9_-]{11})\//);
            if (ytMatch) src = '/api/youtube-thumbnail?video_id=' + encodeURIComponent(ytMatch[1]);
            else if (src.startsWith('/')) src = src;  // Same-origin (e.g. /api/youtube-thumbnail?video_id=...)
            else src = encodeURI(src);
            const safeAlt = this.escapeHtml(imgAlt);
            return `<img src="${src}" alt="${safeAlt}" class="message-inline-image" loading="lazy" style="max-width:100%;height:auto;border-radius:8px;margin:8px 0;">`;
        });

        // Restore HTML links first (before markdown links)
        html = html.replace(/\x00HTMLLINK(\d+)\x00/g, (match, index) => {
            const htmlLink = htmlLinks[parseInt(index)];
            if (!htmlLink) return '';
            const target = htmlLink.target ? ` target="${htmlLink.target}"` : '';
            const rel = htmlLink.rel ? ` rel="${htmlLink.rel}"` : '';
            const safeUrl = htmlLink.url.startsWith('/') ? htmlLink.url : encodeURI(htmlLink.url);
            return `<a href="${safeUrl}"${target}${rel}>${this.escapeHtml(htmlLink.text ?? '')}</a>`;
        });
        
        // Restore markdown links as HTML
        html = html.replace(/\x00LINK(\d+)\x00/g, (match, index) => {
            const link = links[parseInt(index)];
            if (!link) return '';
            const linkText = link.text ?? '';
            if (link.isCommand) {
                // Command button - inline style so it looks correct in WebView (torrent Download, etc.)
                const escapedCmd = this.escapeHtml(link.cmd ?? '');
                const btnStyle = 'display:inline-block;padding:8px 14px;margin:2px 4px 2px 0;background:rgba(0,200,200,0.25);border:1px solid #00c8c8;color:#00e5e5;border-radius:6px;cursor:pointer;font-size:14px;font-family:inherit;-webkit-tap-highlight-color:transparent;';
                return `<button type="button" class="cmd-btn" data-cmd="${escapedCmd}" style="${btnStyle}" onclick="if(window.chatHandler&&window.chatHandler.executeCommand){window.chatHandler.executeCommand('${escapedCmd.replace(/'/g, "\\'")}');}return false;">${this.escapeHtml(linkText)}</button>`;
            }
            if (link.isEditEvent) {
                const escapedUid = this.escapeHtml(link.uid ?? '');
                const btnStyle = 'display:inline-block;padding:6px 12px;margin:2px 4px 2px 0;background:rgba(0,200,200,0.2);border:1px solid #00c8c8;color:#00e5e5;border-radius:6px;cursor:pointer;font-size:13px;font-family:inherit;';
                return `<button type="button" class="cmd-btn" style="${btnStyle}" onclick="if(window.chatHandler&&window.chatHandler.openEditEventModal){window.chatHandler.openEditEventModal('${escapedUid.replace(/'/g, "\\'")}');}return false;">${this.escapeHtml(linkText)}</button>`;
            }
            if (link.isCopy) {
                const escapedContent = this.escapeHtml(link.content ?? '').replace(/'/g, "\\'").replace(/\n/g, '\\n');
                const btnStyle = 'display:inline-block;padding:6px 12px;margin:2px 4px 2px 0;background:rgba(0,200,200,0.2);border:1px solid #00c8c8;color:#00e5e5;border-radius:6px;cursor:pointer;font-size:13px;font-family:inherit;';
                return `<button type="button" class="cmd-btn copy-btn" style="${btnStyle}" onclick="if(window.chatHandler&&window.chatHandler.copyToClipboard){window.chatHandler.copyToClipboard('${escapedContent}')};return false;">${this.escapeHtml(linkText)}</button>`;
            }
            const target = link.external ? ' target="_blank"' : '';
            const download = link.download ? ' download' : '';
            if (!link.url) return this.escapeHtml(linkText);
            // Don't encode mailto: or tel: URLs, they use different escaping
            let safeUrl = link.url;
            if (link.url.startsWith('/')) {
                safeUrl = link.url;
            } else if (link.url.startsWith('mailto:') || link.url.startsWith('tel:')) {
                safeUrl = link.url;  // Keep as-is
            } else {
                safeUrl = encodeURI(link.url);
            }
            return `<a href="${safeUrl}"${target}${download}>${this.escapeHtml(linkText)}</a>`;
        });

        // Bold **text**
        html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');

        // Italic *text*
        html = html.replace(/\*([^*]+)\*/g, '<em>$1</em>');

        // Inline code `text` (but not inside code blocks)
        html = html.replace(/`([^`]+)`/g, '<code>$1</code>');

        // Plain URLs (not already in a link)
        html = html.replace(/(https?:\/\/[^\s<]+)(?![^<]*<\/a>)/g, '<a href="$1" target="_blank">$1</a>');

        // Newlines
        html = html.replace(/\n/g, '<br>');

        // Restore code blocks with proper formatting
        html = html.replace(/\x00CODEBLOCK(\d+)\x00/g, (match, index) => {
            const block = codeBlocks[parseInt(index)];
            if (!block) return '';
            const blockCode = block.code != null && typeof block.code === 'string' ? block.code : '';
            const escapedCode = blockCode
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;');
            const langClass = block.lang ? ` class="language-${block.lang}"` : '';
            const langLabel = block.lang ? `<span class="code-lang">${block.lang}</span>` : '';
            const blockId = `code-${Date.now()}-${index}`;
            return `<div class="code-block-wrapper">
                ${langLabel}
                <button class="code-copy-btn" onclick="window.chatHandler.copyCodeBlock('${blockId}')" title="Copy code">Copy</button>
                <pre${langClass}><code id="${blockId}">${escapedCode}</code></pre>
            </div>`;
        });

        // Remove trailing emojis that LLM adds after links (📋, 📧, 🔄, ✏️, etc.)
        html = html.replace(/((?:📋|📧|🔄|✏️|💬|📁|🌐|✨|🗂️|📎|📷|🎤|🏠|🔍|📝)\s*)+$/g, '');

        return html;
    }

    copyCodeBlock(blockId) {
        const codeEl = document.getElementById(blockId);
        if (codeEl) {
            navigator.clipboard.writeText(codeEl.textContent).then(() => {
                const btn = codeEl.parentElement.parentElement.querySelector('.code-copy-btn');
                if (btn) {
                    const originalText = btn.textContent;
                    btn.textContent = 'Copied!';
                    setTimeout(() => { btn.textContent = originalText; }, 2000);
                }
            }).catch(err => {
                console.error('Failed to copy code:', err);
            });
        }
    }

    showTypingIndicator() {
        this.hideTypingIndicator();
        this.isStreaming = true;
        this.sendBtn.textContent = 'Stop';
        this.sendBtn.classList.add('stop-btn');
        const indicator = document.createElement('div');
        indicator.className = 'message assistant typing';
        indicator.id = 'typingIndicator';
        indicator.innerHTML = `
            <div class="message-content">
                <div class="typing-indicator">
                    <span></span><span></span><span></span>
                </div>
            </div>
        `;
        this.messagesContainer.appendChild(indicator);
        this.scrollToBottom();
    }

    hideTypingIndicator() {
        const indicator = document.getElementById('typingIndicator');
        if (indicator) indicator.remove();
    }

    resetSendButton() {
        this.isStreaming = false;
        this.sendBtn.textContent = 'Send';
        this.sendBtn.classList.remove('stop-btn');
    }

    stopStreaming() {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify({ type: 'stop' }));
        }
        this.hideTypingIndicator();
        this.resetSendButton();
        // Finalize the current streaming message if exists
        if (this.streamingMessage && this.fullStreamContent) {
            this.handleStreamEnd();
        }
    }

    scrollToBottom() {
        const container = document.getElementById('messagesContainer');
        container.scrollTop = container.scrollHeight;
    }

    loadMessages(messages) {
        this.messagesContainer.innerHTML = '';
        for (const msg of messages) {
            this.addMessage(msg.role, msg.content, false, false, msg.image_path);
        }
    }

    clear() {
        this.messagesContainer.innerHTML = '';
        this.streamingMessage = null;
        this.lastPayload = null;
        this.lastUserMessage = null;
    }

    // Save message to history (called when sending)
    saveToHistory(message) {
        if (message && message.trim()) {
            // Don't add duplicates at the end
            if (this.messageHistory.length === 0 || this.messageHistory[this.messageHistory.length - 1] !== message) {
                this.messageHistory.push(message);
                // Keep history manageable
                if (this.messageHistory.length > 50) {
                    this.messageHistory.shift();
                }
            }
        }
        this.historyIndex = -1; // Reset index after sending
    }

    // Recall previous message (up arrow)
    recallPreviousMessage() {
        if (this.messageHistory.length === 0) return;

        if (this.historyIndex === -1) {
            // First press - save current input and go to last message
            this.savedInput = this.messageInput.value;
            this.historyIndex = this.messageHistory.length - 1;
        } else if (this.historyIndex > 0) {
            // Navigate further back
            this.historyIndex--;
        }

        this.messageInput.value = this.messageHistory[this.historyIndex];
        // Move cursor to end
        this.messageInput.setSelectionRange(this.messageInput.value.length, this.messageInput.value.length);
    }

    // Recall next message (down arrow)
    recallNextMessage() {
        if (this.historyIndex === -1) return;

        this.historyIndex++;

        if (this.historyIndex >= this.messageHistory.length) {
            // Past the end - restore saved input
            this.historyIndex = -1;
            this.messageInput.value = this.savedInput || '';
        } else {
            this.messageInput.value = this.messageHistory[this.historyIndex];
        }
        // Move cursor to end
        this.messageInput.setSelectionRange(this.messageInput.value.length, this.messageInput.value.length);
    }


    async loadMailAccountsForAutocomplete() {
        try {
            const response = await csrfFetch('/api/auth/settings');
            if (response.ok) {
                const settings = await response.json();
                console.debug('Settings loaded for autocomplete:', { mail_accounts: settings.mail_accounts });
                if (settings.mail_accounts && settings.mail_accounts.length > 0) {
                    // Extract account hints (first part of email before @)
                    const accountHints = settings.mail_accounts.map(acc => {
                        const email = acc.email || '';
                        return email.split('@')[0].toLowerCase();
                    }).filter(h => h);

                    console.log('Mail account hints loaded:', accountHints);

                    // Common IMAP folder prefixes for folder:uid format
                    const folderHints = ['INBOX:', 'INBOX.Archive:', 'INBOX.Sent:', 'INBOX.Drafts:', 'INBOX.spam:', 'Sent Messages:', 'Trash:'];

                    // Update subcommands with account hints
                    this.subcommands['mail folders'] = accountHints;
                    this.subcommands['mail folder'] = accountHints;
                    this.subcommands['mail search'] = accountHints;
                    this.subcommands['mail read'] = accountHints;
                    this.subcommands['mail summary'] = accountHints;
                    this.subcommands['mail sum'] = accountHints;
                    this.subcommands['mail translate'] = accountHints;
                    this.subcommands['mail reply'] = accountHints;
                    this.subcommands['mail forward'] = accountHints;
                    this.subcommands['mail delete'] = accountHints;
                    this.subcommands['mail deleteall'] = accountHints;
                    this.subcommands['mail archive'] = accountHints;
                    this.subcommands['mail send'] = accountHints;

                    // Add folder hints after account for folder/read/summary/translate
                    for (const account of accountHints) {
                        this.subcommands[`mail folder ${account}`] = folderHints.map(f => f.replace(':', ''));
                        this.subcommands[`mail read ${account}`] = folderHints;
                        this.subcommands[`mail summary ${account}`] = folderHints;
                    }
                    this.subcommands['mail translate'] = accountHints; // language first, then account

                    console.log('Mail subcommands set:', Object.keys(this.subcommands).filter(k => k.startsWith('mail')));
                } else {
                    console.debug('No mail accounts configured');
                }
            } else {
                console.warn('Failed to load settings:', response.status);
            }
        } catch (e) {
            console.error('Error loading mail accounts for autocomplete:', e);
        }
    }

    // Render mail account list in user settings
    renderMailAccountList(accounts) {
        const listContainer = document.getElementById('mailAccountList');
        if (!listContainer) return;

        if (!accounts || accounts.length === 0) {
            listContainer.innerHTML = '<p class="no-accounts">No email accounts configured. Click "+ Add Email Account" to add one.</p>';
            return;
        }

        listContainer.innerHTML = accounts.map((acc, index) => `
            <div class="mail-account-item">
                <div class="account-info">
                    <strong>${this.escapeHtml(acc.email || acc.username || 'Unknown')}</strong>
                    <span class="account-server">${this.escapeHtml(acc.imap_server || '')}</span>
                </div>
                <div class="account-actions">
                    <button class="btn-small btn-danger" onclick="window.chatHandler.deleteMailAccount(${index})">Delete</button>
                </div>
            </div>
        `).join('');
    }

    async deleteMailAccount(index) {
        if (!confirm('Delete this email account?')) return;
        
        try {
            const response = await csrfFetch('/api/auth/mail-accounts', {
                method: 'DELETE',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ index })
            });

            if (response.ok) {
                // Reload settings to refresh the list
                await this.loadUserSettings();
            } else {
                const data = await response.json();
                alert(data.detail || 'Failed to delete account');
            }
        } catch (e) {
            console.error('Error deleting mail account:', e);
            alert('Error deleting account');
        }
    }

    // Subcommands that can be autocompleted
    subcommands = {
        'torrents': ['download', 'list', 'add', 'start', 'stop', 'delete', 'movies', 'tv', 'anime'],
        'torrents download': ['movies', 'tv', 'anime'],
        'nyaa': ['download'],
        'bills': ['all', 'paid'],
        'firewall': ['search', 'analyze'],
        'news': ['refresh'],
        'mail': ['inbox', 'unread', 'folders', 'folder', 'sum', 'search', 'read', 'summary', 'translate', 'reply', 'forward', 'delete', 'deleteall', 'archive', 'send'],
        // Mail subcommands - will be populated with account names dynamically
        'mail folders': [],
        'mail folder': [],
        'mail search': [],
        'mail read': [],
        'mail summary': [],
        'mail sum': [],
        'mail translate': [],
        'mail reply': [],
        'mail forward': [],
        'mail delete': [],
        'mail deleteall': [],
        'mail archive': [],
        'mail send': [],
        // Todo subcommands
        'todo': ['add', 'rm', 'list'],
        // YouTube download subcommands
        'ytdl': ['mp3', 'video'],
        // 4chan catalog boards
        '4chan': ['g', 'pol']
    };

    // Tab autocomplete for commands
    autocompleteCommand() {
        const input = this.messageInput.value;
        const cursorPos = this.messageInput.selectionStart;

        // Only autocomplete at the start of input
        if (cursorPos > input.length) return;

        const textBeforeCursor = input.substring(0, cursorPos).toLowerCase();

        // Check if we're after a command (has space) - handle subcommand completion
        const spaceIndex = textBeforeCursor.indexOf(' ');
        if (spaceIndex > 0) {
            const parts = textBeforeCursor.split(' ');
            const cmd = parts[0];
            const afterCmd = parts.slice(1).join(' ');

            // Check for multi-level subcommands (e.g., "torrents download")
            const cmdPrefix = parts.slice(0, -1).join(' ');
            const lastPart = parts[parts.length - 1];

            // Try multi-level first (e.g., "torrents download" -> ["movies", "tv", ...])
            // Special case: mail send <account> <email> -> recipient filled, show message hint
            if (/^mail\s+send\s+\S+\s+\S+@\S+$/i.test(cmdPrefix)) {
                this.showToast('"Subject" message | Or just: message (auto-subject)');
                return;
            }
            // Special case: mail forward <account> <id> <email> -> recipient filled, show message hint
            if (/^mail\s+forward\s+\S+\s+\d+\s+\S+@\S+$/i.test(cmdPrefix)) {
                this.showToast('Type your message (optional, can be multi-line). Original attachments will be included. Press Enter to forward');
                return;
            }
            
            // Special case: files - show search hint
            if (/^files$/i.test(cmdPrefix)) {
                if (lastPart === '') {
                    this.showToast('Type a search query to find files (e.g., "files document" or "files .pdf")');
                } else {
                    // User is typing a search query, let them continue
                    this.showToast('Press Enter to search for files');
                }
                return;
            }
            
            // Notes autocomplete removed
            // Special case: mail forward/send <account> <id> -> suggest recipient emails
            let effectiveCmdPrefix = cmdPrefix;
            if (/^mail\s+(forward|send)\s+\S+\s+\d+$/i.test(cmdPrefix)) {
                // Strip the ID to get mail forward/send <account>
                effectiveCmdPrefix = cmdPrefix.replace(/\s+\d+$/, '');
            }
            console.log('Autocomplete lookup:', { cmdPrefix, effectiveCmdPrefix, lastPart, hasSubs: !!this.subcommands[effectiveCmdPrefix], subs: this.subcommands[effectiveCmdPrefix] });
            if (this.subcommands[effectiveCmdPrefix] && this.subcommands[effectiveCmdPrefix].length > 0) {
                const subs = this.subcommands[effectiveCmdPrefix];
                // Case-insensitive matching for folder hints, note titles, etc.
                const matches = subs.filter(s => s.toLowerCase().startsWith(lastPart.toLowerCase()) || s.toLowerCase().includes(lastPart.toLowerCase()));

                console.log('Autocomplete matches:', matches.length, matches);
                // Check for empty lastPart first to show all options
                if (lastPart === '') {
                    this.showToast(`${subs.join(' | ')}`);
                    return;
                } else if (matches.length === 1) {
                    // Don't add trailing space if match ends with : (like folder hints)
                    const trailingSpace = matches[0].endsWith(':') ? '' : ' ';
                    const completed = cmdPrefix + ' ' + matches[0] + trailingSpace;
                    this.messageInput.value = completed + input.substring(cursorPos);
                    this.messageInput.setSelectionRange(completed.length, completed.length);
                    return;
                } else if (matches.length > 1) {
                    // Find common prefix among matches
                    let commonPrefix = matches[0];
                    for (let i = 1; i < matches.length; i++) {
                        while (!matches[i].toLowerCase().startsWith(commonPrefix.toLowerCase())) {
                            commonPrefix = commonPrefix.slice(0, -1);
                            if (!commonPrefix) break;
                        }
                    }
                    // Fill in common prefix if longer than what user typed
                    if (commonPrefix.length > lastPart.length) {
                        // Don't add trailing space if prefix ends with : (folder hints)
                        const trailingSpace = commonPrefix.endsWith(':') ? '' : '';
                        const completed = cmdPrefix + ' ' + commonPrefix + trailingSpace;
                        this.messageInput.value = completed + input.substring(cursorPos);
                        this.messageInput.setSelectionRange(completed.length, completed.length);
                    }
                    this.showToast(`${matches.join(' | ')}`);
                    return;
                }
            }

            // Try single-level (e.g., "torrents" -> ["download"])
            if (this.subcommands[cmd]) {
                const subs = this.subcommands[cmd];
                const matches = subs.filter(s => s.startsWith(afterCmd));

                if (matches.length === 1) {
                    const completed = cmd + ' ' + matches[0] + ' ';
                    this.messageInput.value = completed + input.substring(cursorPos);
                    this.messageInput.setSelectionRange(completed.length, completed.length);
                    return;
                } else if (matches.length > 1) {
                    this.showToast(`${cmd}: ${matches.join(' | ')}`);
                    return;
                } else if (afterCmd === '') {
                    this.showToast(`${cmd}: ${subs.join(' | ')}`);
                    return;
                }
            }
            
            // Special case: files command - show helpful hint
            if (cmd === 'files' && afterCmd === '') {
                this.showToast('Type a search query to find files (e.g., "files document" or "files .pdf")');
                return;
            }
            
            return;
        }

        // Find matching commands
        const commandMatches = this.commands.filter(cmd => cmd.startsWith(textBeforeCursor));

        // Combine matches
        const allMatches = [...commandMatches];

        if (allMatches.length === 1) {
            // Single match - complete it
            const completed = allMatches[0] + ' ';
            this.messageInput.value = completed + input.substring(cursorPos);
            this.messageInput.setSelectionRange(completed.length, completed.length);
        } else if (allMatches.length > 1) {
            // Multiple matches - complete common prefix
            let commonPrefix = allMatches[0];
            for (const match of allMatches) {
                while (!match.startsWith(commonPrefix)) {
                    commonPrefix = commonPrefix.slice(0, -1);
                }
            }
            if (commonPrefix.length > textBeforeCursor.length) {
                this.messageInput.value = commonPrefix + input.substring(cursorPos);
                this.messageInput.setSelectionRange(commonPrefix.length, commonPrefix.length);
            }
            // Show available options as a toast (limit to 5)
            const displayMatches = allMatches.slice(0, 5);
            const more = allMatches.length > 5 ? ` (+${allMatches.length - 5} more)` : '';
            this.showToast(`Options: ${displayMatches.join(', ')}${more}`);
        }
    }
}

// Initialize chat handler
window.chatHandler = new ChatHandler();

// Expose sendMessage globally for other scripts
window.sendMessage = function(text) {
    if (window.chatHandler && window.chatHandler.messageInput) {
        window.chatHandler.messageInput.value = text;
        window.chatHandler.sendMessage();
    }
};

// 4chan catalog modal + thread viewer
(function() {
    const overlay = document.getElementById('fourchanOverlay');
    const grid = document.getElementById('fourchanGrid');
    const loading = document.getElementById('fourchanLoading');
    const closeBtn = document.getElementById('fourchanCloseBtn');
    const tabs = document.querySelectorAll('.fourchan-tab');

    const threadOverlay = document.getElementById('fourchanThreadOverlay');
    const threadTitleEl = document.getElementById('fourchanThreadTitle');
    const threadOpenLink = document.getElementById('fourchanThreadOpenLink');
    const threadSummarizeBtn = document.getElementById('fourchanSummarizeBtn');
    const threadCloseBtn = document.getElementById('fourchanThreadCloseBtn');
    const threadLoading = document.getElementById('fourchanThreadLoading');
    const threadPostsEl = document.getElementById('fourchanThreadPosts');
    const threadSummarySection = document.getElementById('fourchanThreadSummarySection');
    const threadSummaryEl = document.getElementById('fourchanThreadSummary');
    const fourchanImageOverlay = document.getElementById('fourchanImageOverlay');
    const fourchanImageOverlayClose = document.getElementById('fourchanImageOverlayClose');
    const fourchanImageOverlayImg = document.getElementById('fourchanImageOverlayImg');
    const fourchanImageOverlayVideo = document.getElementById('fourchanImageOverlayVideo');

    let currentThreadBoard = 'g';
    let currentThreadId = '';

    // Decode HTML entities (e.g., &gt; -> >, &lt; -> <, &quot; -> ", etc.)
    function decodeHtmlEntities(s) {
        if (!s) return '';
        const textarea = document.createElement('textarea');
        textarea.innerHTML = s;
        return textarea.value;
    }

    // Clean 4chan HTML content: decode entities then escape for display
    function clean4chanHtml(s) {
        if (!s) return '';
        // First decode HTML entities, then escape for safe display
        return escapeHtml(decodeHtmlEntities(s));
    }

    function setLoading(show) {
        if (loading) loading.style.display = show ? 'block' : 'none';
    }

    function setActiveBoard(board) {
        (tabs || []).forEach(t => {
            t.classList.toggle('active', (t.dataset.board || '') === board);
        });
    }

    async function loadCatalog(board) {
        if (!grid) return;
        setLoading(true);
        if (grid) grid.innerHTML = '';
        try {
            const r = await fetch(`/api/4chan/catalog?board=${encodeURIComponent(board)}`);
            const data = await r.json();
            setLoading(false);
            if (data.error) {
                grid.innerHTML = `<div class="fourchan-error">${escapeHtml(data.error)}</div>`;
                return;
            }
            const threads = data.threads || [];
            grid.innerHTML = '';
            threads.forEach(t => {
                const card = document.createElement('a');
                card.className = 'fourchan-card';
                card.href = t.link;
                card.target = '_blank';
                card.rel = 'noopener noreferrer';
                card.dataset.board = board;
                card.dataset.threadId = String(t.thread_id || t.no || '');
                const title = clean4chanHtml(t.title);
                const thumbSrc = t.thumb_url ? ('/api/4chan/proxy?url=' + encodeURIComponent(t.thumb_url)) : '';
                const thumbPlaceholder = '<div class="fourchan-card-thumb" style="background:#222;"></div>';
                const thumbPlaceholderForAttr = thumbPlaceholder.replace(/'/g, "\\'").replace(/"/g, '&quot;');
                const thumb = thumbSrc ? `<img class="fourchan-card-thumb" src="${escapeHtml(thumbSrc)}" alt="" loading="lazy" onerror="this.outerHTML='${thumbPlaceholderForAttr}'">` : thumbPlaceholder;
                card.innerHTML = `${thumb}<div class="fourchan-card-title">${title}</div><div class="fourchan-card-meta">${t.replies || 0} replies · ${t.images || 0} images</div>`;
                card.addEventListener('click', function(e) {
                    e.preventDefault();
                    const b = this.dataset.board || 'g';
                    const tid = this.dataset.threadId;
                    if (tid && window.openFourchanThreadModal) window.openFourchanThreadModal(b, tid);
                });
                grid.appendChild(card);
            });
        } catch (e) {
            setLoading(false);
            loading.style.display = 'none';
            grid.innerHTML = `<div class="fourchan-error">Failed to load catalog: ${escapeHtml(String(e))}</div>`;
        }
    }

    function openFourchanModal(board) {
        board = (board || 'g').toString().toLowerCase();
        if (board !== 'g' && board !== 'pol') board = 'g';
        if (overlay) {
            overlay.style.display = 'flex';
            setActiveBoard(board);
            loadCatalog(board);
        }
    }

    function closeFourchanModal() {
        if (overlay) overlay.style.display = 'none';
    }

    function openFourchanThreadModal(board, threadId) {
        currentThreadBoard = (board || 'g').toString().toLowerCase();
        currentThreadId = String(threadId || '');
        if (!threadOverlay || !currentThreadId) return;
        threadSummarySection.style.display = 'none';
        threadSummaryEl.textContent = '';
        threadOverlay.style.display = 'flex';
        threadTitleEl.textContent = 'Thread ' + currentThreadId;
        threadOpenLink.href = 'https://boards.4chan.org/' + currentThreadBoard + '/thread/' + currentThreadId;
        threadPostsEl.innerHTML = '';
        if (threadLoading) threadLoading.style.display = 'block';
        fetch('/api/4chan/thread?board=' + encodeURIComponent(currentThreadBoard) + '&thread_id=' + encodeURIComponent(currentThreadId))
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (threadLoading) threadLoading.style.display = 'none';
                if (data.error) {
                    threadPostsEl.innerHTML = '<div class="fourchan-error">' + escapeHtml(data.error) + '</div>';
                    return;
                }
                threadTitleEl.textContent = decodeHtmlEntities(data.title) || ('Thread ' + currentThreadId);
                if (data.link) threadOpenLink.href = data.link;
                threadPostsEl.innerHTML = (data.posts || []).map(function(p) {
                    const name = clean4chanHtml(p.name || 'Anonymous');
                    // Handle 4chan post HTML: decode entities, convert <br> to newlines, strip other tags
                    let com = decodeHtmlEntities(p.com || '');
                    // Convert <br> tags to newlines
                    com = com.replace(/<br\s*\/?>/gi, '\n');
                    // Remove other HTML tags
                    com = com.replace(/<[^>]+>/g, '');
                    // Clean up whitespace (collapse multiple spaces, preserve newlines)
                    com = com.split('\n').map(function(line) { return line.replace(/\s+/g, ' ').trim(); }).filter(function(line) { return line; }).join('\n');
                    // Escape for display
                    com = escapeHtml(com);
                    // Convert newlines to <br> for HTML display
                    com = com.replace(/\n/g, '<br>');
                    let thumb = '';
                    if (p.thumb_url) {
                        const img = '<img src="' + escapeHtml(p.thumb_url) + '" alt="" class="fourchan-thread-post-thumb" loading="lazy">';
                        thumb = p.image_url
                            ? '<a href="#" class="fourchan-thread-post-thumb-link" data-image-url="' + escapeHtml(p.image_url) + '" title="Open full image">' + img + '</a>'
                            : img;
                    }
                    return '<div class="fourchan-thread-post">' +
                        '<div class="fourchan-thread-post-header"><strong>#' + escapeHtml(String(p.no)) + '</strong> ' + name + '</div>' +
                        (thumb ? '<div class="fourchan-thread-post-thumb-wrap">' + thumb + '</div>' : '') +
                        '<div class="fourchan-thread-post-body">' + com + '</div></div>';
                }).join('');
            })
            .catch(function(e) {
                if (threadLoading) threadLoading.style.display = 'none';
                threadPostsEl.innerHTML = '<div class="fourchan-error">Failed to load thread: ' + escapeHtml(String(e)) + '</div>';
            });
    }

    function closeFourchanThreadModal() {
        if (threadOverlay) threadOverlay.style.display = 'none';
    }

    if (threadCloseBtn) threadCloseBtn.addEventListener('click', closeFourchanThreadModal);
    if (threadOverlay) threadOverlay.addEventListener('click', function(e) { if (e.target === threadOverlay) closeFourchanThreadModal(); });

    function isVideoUrl(url) {
        return /\.(webm|mp4|mov|ogg|webm\?|mp4\?|mov\?|ogg\?)/i.test(url || '');
    }
    function openFourchanImage(url) {
        if (!fourchanImageOverlay) return;
        if (isVideoUrl(url)) {
            if (fourchanImageOverlayImg) fourchanImageOverlayImg.style.display = 'none';
            if (fourchanImageOverlayVideo) {
                fourchanImageOverlayVideo.src = url;
                fourchanImageOverlayVideo.style.display = 'block';
            }
        } else {
            if (fourchanImageOverlayVideo) {
                fourchanImageOverlayVideo.pause();
                fourchanImageOverlayVideo.removeAttribute('src');
                fourchanImageOverlayVideo.style.display = 'none';
            }
            if (fourchanImageOverlayImg) {
                fourchanImageOverlayImg.src = url;
                fourchanImageOverlayImg.style.display = 'block';
            }
        }
        fourchanImageOverlay.style.display = 'flex';
    }
    function closeFourchanImageOverlay() {
        if (fourchanImageOverlay) fourchanImageOverlay.style.display = 'none';
        if (fourchanImageOverlayImg) {
            fourchanImageOverlayImg.src = '';
            fourchanImageOverlayImg.style.display = 'none';
        }
        if (fourchanImageOverlayVideo) {
            fourchanImageOverlayVideo.pause();
            fourchanImageOverlayVideo.removeAttribute('src');
            fourchanImageOverlayVideo.style.display = 'none';
        }
    }
    if (threadPostsEl) {
        threadPostsEl.addEventListener('click', function(e) {
            const link = e.target.closest('a.fourchan-thread-post-thumb-link');
            if (!link) return;
            e.preventDefault();
            const url = link.getAttribute('data-image-url');
            if (url) openFourchanImage(url);
        });
    }
    if (fourchanImageOverlayClose) fourchanImageOverlayClose.addEventListener('click', function(e) { e.stopPropagation(); closeFourchanImageOverlay(); });
    if (fourchanImageOverlay) {
        fourchanImageOverlay.addEventListener('click', function(e) {
            if (e.target === fourchanImageOverlay) closeFourchanImageOverlay();
        });
    }
    if (fourchanImageOverlayImg) fourchanImageOverlayImg.addEventListener('click', function(e) { e.stopPropagation(); });
    if (fourchanImageOverlayVideo) fourchanImageOverlayVideo.addEventListener('click', function(e) { e.stopPropagation(); });

    if (threadSummarizeBtn) {
        var summarizeBtnLabel = threadSummarizeBtn.textContent || 'Summarize';
        threadSummarizeBtn.addEventListener('click', function() {
            if (!currentThreadId) return;
            threadSummarizeBtn.disabled = true;
            threadSummarizeBtn.textContent = 'Summarizing…';
            threadSummarizeBtn.classList.add('fourchan-summarize-loading');
            threadSummarySection.style.display = 'block';
            threadSummaryEl.textContent = '';
            threadSummaryEl.classList.add('fourchan-summary-loading');
            threadSummaryEl.innerHTML = '<span class="fourchan-summary-spinner"></span> Working on it…';
            threadSummarySection.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            fetch('/api/4chan/summarize?board=' + encodeURIComponent(currentThreadBoard) + '&thread_id=' + encodeURIComponent(currentThreadId), { credentials: 'same-origin' })
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    threadSummarizeBtn.disabled = false;
                    threadSummarizeBtn.textContent = summarizeBtnLabel;
                    threadSummarizeBtn.classList.remove('fourchan-summarize-loading');
                    threadSummaryEl.classList.remove('fourchan-summary-loading');
                    threadSummaryEl.innerHTML = '';
                    if (data.error) {
                        threadSummaryEl.textContent = 'Error: ' + data.error;
                        return;
                    }
                    threadSummaryEl.textContent = data.summary || '';
                })
                .catch(function(e) {
                    threadSummarizeBtn.disabled = false;
                    threadSummarizeBtn.textContent = summarizeBtnLabel;
                    threadSummarizeBtn.classList.remove('fourchan-summarize-loading');
                    threadSummaryEl.classList.remove('fourchan-summary-loading');
                    threadSummaryEl.innerHTML = '';
                    threadSummaryEl.textContent = 'Failed: ' + String(e);
                });
        });
    }

    if (closeBtn) closeBtn.addEventListener('click', closeFourchanModal);
    (tabs || []).forEach(t => {
        t.addEventListener('click', () => {
            const board = t.dataset.board || 'g';
            setActiveBoard(board);
            loadCatalog(board);
        });
    });
    if (overlay) {
        overlay.addEventListener('click', (e) => { if (e.target === overlay) closeFourchanModal(); });
    }

    window.openFourchanModal = openFourchanModal;
    window.closeFourchanModal = closeFourchanModal;
    window.openFourchanThreadModal = openFourchanThreadModal;
    window.closeFourchanThreadModal = closeFourchanThreadModal;
})();
