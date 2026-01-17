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

        // Stored upload data
        this.uploadedImage = null;  // base64 image data
        this.uploadedFile = null;   // text file content
        this.uploadedPDF = null;    // base64 PDF data

        // Callback for when stream ends (used by news to delete prompt)
        this.onStreamEndCallback = null;
        this.uploadedDocument = null; // base64 office document data

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
        this.commands = ['help', 'search', 'images', 'geni', 'yt', 'ytdl', 'torrents', 'nyaa', 'budget', 'firewall', 'news', 'dailynews', 'logs', 'rss', 'cal', 'contacts', 'mail', 'music', 'todo', 'notes', 'files'];
        this.pluginActions = []; // Will be populated with plugin action hints

        // Load plugins and mail accounts for autocomplete
        this.loadPluginsForAutocomplete();
        // Chain mail accounts -> contact emails to avoid race condition
        this.loadMailAccountsForAutocomplete().then(() => {
            this.loadContactEmailsForAutocomplete();
        });
        // Load note titles for autocomplete
        this.loadNoteTitlesForAutocomplete();

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

        // Music shuffle button
        const musicShuffleBtn = document.getElementById('musicShuffleBtn');
        if (musicShuffleBtn) {
            musicShuffleBtn.addEventListener('click', () => this.startMusicShuffle());
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
                    this.uploadedImage = base64;
                    this.uploadedFile = null;
                    this.uploadedPDF = null;
                    this.uploadedDocument = null;

                    // Show preview
                    this.imagePreview.src = event.target.result;
                    this.imagePreview.style.display = 'block';
                    this.filePreview.textContent = '';
                    this.filePreview.style.display = 'none';
                    this.uploadPreview.style.display = 'flex';
                };
                reader.readAsDataURL(file);
                break;  // Only handle first image
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

        // Native RSS elements
        const rssEnabled = document.getElementById('rssEnabled');
        const rssSettings = document.getElementById('rssSettings');
        const rssFeedList = document.getElementById('rssFeedList');
        const addRssFeed = document.getElementById('addRssFeed');
        const newRssFeedUrl = document.getElementById('newRssFeedUrl');
        const newRssFeedName = document.getElementById('newRssFeedName');
        const rssSkipSummarization = document.getElementById('rssSkipSummarization');

        // RSS feed list management
        let rssFeeds = [];

        async function loadRssFeeds() {
            try {
                const resp = await fetch('/api/rss/feeds', { credentials: 'include' });
                if (resp.ok) {
                    rssFeeds = await resp.json();
                    renderRssFeedList();
                }
            } catch (e) {
                console.error('Failed to load RSS feeds:', e);
            }
        }

        function renderRssFeedList() {
            if (!rssFeedList) return;
            rssFeedList.innerHTML = rssFeeds.map(feed => `
                <div class="rss-feed-item" data-id="${feed.id}">
                    <div class="feed-info">
                        <span class="feed-status">${feed.enabled ? '✓' : '✗'}</span>
                        <strong>${feed.display_name}</strong>
                        <small class="feed-url">${feed.url}</small>
                        ${feed.last_error ? `<span class="feed-error">⚠️ ${feed.last_error}</span>` : ''}
                    </div>
                    <div class="feed-actions">
                        <button type="button" class="btn-small toggle-feed" data-id="${feed.id}">${feed.enabled ? 'Disable' : 'Enable'}</button>
                        <button type="button" class="btn-danger btn-small remove-feed" data-id="${feed.id}">Remove</button>
                    </div>
                </div>
            `).join('') || '<p class="empty-list">No RSS feeds configured. Add a feed URL below.</p>';

            // Add toggle handlers
            rssFeedList.querySelectorAll('.toggle-feed').forEach(btn => {
                btn.addEventListener('click', async () => {
                    const id = parseInt(btn.dataset.id);
                    const csrfToken = document.cookie.split('; ')
                        .find(row => row.startsWith('csrf_token='))?.split('=')[1];
                    try {
                        const resp = await fetch(`/api/rss/feeds/${id}/toggle`, {
                            method: 'POST',
                            credentials: 'include',
                            headers: { 'X-CSRF-Token': csrfToken || '' }
                        });
                        if (resp.ok) loadRssFeeds();
                    } catch (e) {
                        console.error('Failed to toggle feed:', e);
                    }
                });
            });

            // Add remove handlers
            rssFeedList.querySelectorAll('.remove-feed').forEach(btn => {
                btn.addEventListener('click', async () => {
                    const id = parseInt(btn.dataset.id);
                    if (!confirm('Remove this RSS feed?')) return;
                    const csrfToken = document.cookie.split('; ')
                        .find(row => row.startsWith('csrf_token='))?.split('=')[1];
                    try {
                        const resp = await fetch(`/api/rss/feeds/${id}`, {
                            method: 'DELETE',
                            credentials: 'include',
                            headers: { 'X-CSRF-Token': csrfToken || '' }
                        });
                        if (resp.ok) loadRssFeeds();
                    } catch (e) {
                        console.error('Failed to remove feed:', e);
                    }
                });
            });
        }

        if (addRssFeed) {
            addRssFeed.addEventListener('click', async () => {
                const url = newRssFeedUrl?.value.trim();
                const name = newRssFeedName?.value.trim();
                if (!url) {
                    alert('Please enter a feed URL');
                    return;
                }
                const csrfToken = document.cookie.split('; ')
                    .find(row => row.startsWith('csrf_token='))?.split('=')[1];
                try {
                    const resp = await fetch('/api/rss/feeds', {
                        method: 'POST',
                        headers: { 
                            'Content-Type': 'application/json',
                            'X-CSRF-Token': csrfToken || ''
                        },
                        credentials: 'include',
                        body: JSON.stringify({ url, custom_name: name || null })
                    });
                    if (resp.ok) {
                        if (newRssFeedUrl) newRssFeedUrl.value = '';
                        if (newRssFeedName) newRssFeedName.value = '';
                        loadRssFeeds();
                    } else {
                        alert('Failed to add feed');
                    }
                } catch (e) {
                    console.error('Failed to add feed:', e);
                    alert('Failed to add feed');
                }
            });
        }

        // Toggle RSS settings visibility
        if (rssEnabled && rssSettings) {
            rssEnabled.addEventListener('change', () => {
                rssSettings.style.display = rssEnabled.checked ? 'flex' : 'none';
                if (rssEnabled.checked && rssFeeds.length === 0) {
                    loadRssFeeds();
                }
            });
        }

        // OPML import
        const opmlFileInput = document.getElementById('opmlFileInput');
        const importOpmlBtn = document.getElementById('importOpmlBtn');
        const opmlImportStatus = document.getElementById('opmlImportStatus');

        if (importOpmlBtn && opmlFileInput) {
            importOpmlBtn.addEventListener('click', () => {
                opmlFileInput.click();
            });

            opmlFileInput.addEventListener('change', async (e) => {
                const file = e.target.files?.[0];
                if (!file) return;

                if (opmlImportStatus) {
                    opmlImportStatus.textContent = 'Importing...';
                    opmlImportStatus.className = 'test-result';
                }

                try {
                    const formData = new FormData();
                    formData.append('file', file);

                    // Get CSRF token
                    const csrfToken = document.cookie.split('; ')
                        .find(row => row.startsWith('csrf_token='))?.split('=')[1];

                    const resp = await fetch('/api/rss/import/opml', {
                        method: 'POST',
                        credentials: 'include',
                        headers: {
                            'X-CSRF-Token': csrfToken || ''
                        },
                        body: formData
                    });

                    const result = await resp.json();

                    if (resp.ok) {
                        if (opmlImportStatus) {
                            opmlImportStatus.textContent = result.message;
                            opmlImportStatus.className = 'test-result success';
                        }
                        loadRssFeeds();
                    } else {
                        if (opmlImportStatus) {
                            opmlImportStatus.textContent = result.detail || 'Import failed';
                            opmlImportStatus.className = 'test-result error';
                        }
                    }
                } catch (err) {
                    console.error('OPML import error:', err);
                    if (opmlImportStatus) {
                        opmlImportStatus.textContent = 'Import failed';
                        opmlImportStatus.className = 'test-result error';
                    }
                }

                // Clear file input for re-import
                opmlFileInput.value = '';
            });
        }

        // OPML export
        const exportOpmlBtn = document.getElementById('exportOpmlBtn');
        if (exportOpmlBtn) {
            exportOpmlBtn.addEventListener('click', async () => {
                try {
                    const resp = await fetch('/api/rss/export/opml', {
                        method: 'GET',
                        credentials: 'include'
                    });

                    if (resp.ok) {
                        const blob = await resp.blob();
                        const url = window.URL.createObjectURL(blob);
                        const a = document.createElement('a');
                        a.href = url;
                        a.download = 'feeds.opml';
                        document.body.appendChild(a);
                        a.click();
                        document.body.removeChild(a);
                        window.URL.revokeObjectURL(url);
                    } else {
                        const result = await resp.json();
                        alert(result.detail || 'Export failed');
                    }
                } catch (err) {
                    console.error('OPML export error:', err);
                    alert('Export failed');
                }
            });
        }

        // Calendar & Contacts elements
        const scheduleEnabled = document.getElementById('scheduleEnabled');
        const caldavCalendarList = document.getElementById('caldavCalendarList');
        const addCaldavCalendar = document.getElementById('addCaldavCalendar');
        const carddavUrl = document.getElementById('carddavUrl');
        const carddavUsername = document.getElementById('carddavUsername');
        const carddavPassword = document.getElementById('carddavPassword');

        // Calendar list management
        let caldavCalendars = [];

        function renderCalendarList() {
            if (!caldavCalendarList) return;
            caldavCalendarList.innerHTML = caldavCalendars.map((cal, idx) => `
                <div class="caldav-calendar-item" data-index="${idx}">
                    <div class="form-group">
                        <label>Calendar ${idx + 1} Name</label>
                        <input type="text" class="cal-name" value="${cal.name || ''}" placeholder="Work Calendar">
                    </div>
                    <div class="form-group">
                        <label>CalDAV URL</label>
                        <input type="url" class="cal-url" value="${cal.url || ''}" placeholder="https://cal.example.com/user/calendar/">
                    </div>
                    <div class="form-row">
                        <div class="form-group">
                            <label>Username</label>
                            <input type="text" class="cal-username" value="${cal.username || ''}" placeholder="username">
                        </div>
                        <div class="form-group">
                            <label>Password</label>
                            <input type="password" class="cal-password" value="${cal.password ? '********' : ''}" placeholder="password">
                        </div>
                    </div>
                    <button type="button" class="btn-danger btn-small remove-calendar" data-index="${idx}">Remove</button>
                </div>
            `).join('') || '<p class="empty-list">No calendars configured. Click "+ Add Calendar" to add one.</p>';

            // Add remove handlers
            caldavCalendarList.querySelectorAll('.remove-calendar').forEach(btn => {
                btn.addEventListener('click', () => {
                    const idx = parseInt(btn.dataset.index);
                    caldavCalendars.splice(idx, 1);
                    renderCalendarList();
                });
            });
        }

        if (addCaldavCalendar) {
            addCaldavCalendar.addEventListener('click', () => {
                caldavCalendars.push({ name: '', url: '', username: '', password: '' });
                renderCalendarList();
            });
        }

        function collectCalendarData() {
            const items = caldavCalendarList?.querySelectorAll('.caldav-calendar-item') || [];
            return Array.from(items).map(item => {
                const password = item.querySelector('.cal-password').value;
                return {
                    name: item.querySelector('.cal-name').value.trim(),
                    url: item.querySelector('.cal-url').value.trim(),
                    username: item.querySelector('.cal-username').value.trim(),
                    password: password === '********' ? null : password  // null means keep existing
                };
            });
        }

        // Mail account elements
        const mailAccountList = document.getElementById('mailAccountList');
        const addMailAccount = document.getElementById('addMailAccount');

        // WebDAV Music elements
        const webdavMusicUrl = document.getElementById('webdavMusicUrl');
        const webdavMusicUsername = document.getElementById('webdavMusicUsername');
        const webdavMusicPassword = document.getElementById('webdavMusicPassword');
        const testWebdavMusic = document.getElementById('testWebdavMusic');
        const testMusicResult = document.getElementById('testMusicResult');

        // Mail account list management
        let mailAccounts = [];

        function renderMailAccountList() {
            if (!mailAccountList) return;
            mailAccountList.innerHTML = mailAccounts.map((acc, idx) => `
                <div class="mail-account-item" data-index="${idx}">
                    <div class="form-group">
                        <label>Email Address</label>
                        <input type="email" class="mail-email" value="${acc.email || ''}" placeholder="user@example.com">
                    </div>
                    <div class="form-row">
                        <div class="form-group">
                            <label>IMAP Server</label>
                            <input type="text" class="mail-imap-server" value="${acc.imap_server || ''}" placeholder="imap.example.com">
                        </div>
                        <div class="form-group" style="max-width: 100px;">
                            <label>IMAP Port</label>
                            <input type="number" class="mail-imap-port" value="${acc.imap_port || 993}" placeholder="993">
                        </div>
                    </div>
                    <div class="form-row">
                        <div class="form-group">
                            <label>SMTP Server</label>
                            <input type="text" class="mail-smtp-server" value="${acc.smtp_server || ''}" placeholder="smtp.example.com">
                        </div>
                        <div class="form-group" style="max-width: 100px;">
                            <label>SMTP Port</label>
                            <input type="number" class="mail-smtp-port" value="${acc.smtp_port || 587}" placeholder="587">
                        </div>
                    </div>
                    <div class="form-group">
                        <label>Password</label>
                        <input type="password" class="mail-password" value="${acc.password ? '********' : ''}" placeholder="Email password">
                    </div>
                    <button type="button" class="btn-danger btn-small remove-mail-account" data-index="${idx}">Remove</button>
                </div>
            `).join('') || '<p class="empty-list">No email accounts configured. Click "+ Add Email Account" to add one.</p>';

            // Add remove handlers
            mailAccountList.querySelectorAll('.remove-mail-account').forEach(btn => {
                btn.addEventListener('click', () => {
                    const idx = parseInt(btn.dataset.index);
                    mailAccounts.splice(idx, 1);
                    renderMailAccountList();
                });
            });
        }

        if (addMailAccount) {
            addMailAccount.addEventListener('click', () => {
                mailAccounts.push({ email: '', imap_server: '', imap_port: 993, smtp_server: '', smtp_port: 587, password: '' });
                renderMailAccountList();
            });
        }

        function collectMailAccountData() {
            const items = mailAccountList?.querySelectorAll('.mail-account-item') || [];
            return Array.from(items).map(item => {
                const password = item.querySelector('.mail-password').value;
                return {
                    email: item.querySelector('.mail-email').value.trim(),
                    imap_server: item.querySelector('.mail-imap-server').value.trim(),
                    imap_port: parseInt(item.querySelector('.mail-imap-port').value) || 993,
                    smtp_server: item.querySelector('.mail-smtp-server').value.trim(),
                    smtp_port: parseInt(item.querySelector('.mail-smtp-port').value) || 587,
                    password: password === '********' ? null : password  // null means keep existing
                };
            });
        }

        // Quick AI Toggle elements (in user menu)
        const aiToggleItem = document.getElementById('aiToggleItem');
        const aiToggleLabel = document.getElementById('aiToggleLabel');
        const quickAiToggle = document.getElementById('quickAiToggle');

        // Load initial state for quick toggle
        this.loadQuickToggleState = async () => {
            try {
                const response = await fetch('/api/auth/settings');
                if (response.ok) {
                    const data = await response.json();
                    // Only show toggle if custom AI URL is configured
                    if (data.custom_ai_url) {
                        aiToggleItem.style.display = 'flex';
                        quickAiToggle.checked = data.custom_ai_enabled || false;
                        aiToggleLabel.textContent = data.custom_ai_enabled ? 'Using: Custom AI' : 'Using: Server AI';
                    } else {
                        aiToggleItem.style.display = 'none';
                    }
                }
            } catch (e) {
                console.error('Failed to load AI toggle state:', e);
            }
        };

        // Quick toggle handler
        if (quickAiToggle) {
            quickAiToggle.addEventListener('change', async () => {
                const enabled = quickAiToggle.checked;
                aiToggleLabel.textContent = enabled ? 'Using: Custom AI' : 'Using: Server AI';

                try {
                    await csrfFetch('/api/auth/settings', {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ custom_ai_enabled: enabled })
                    });
                    // Also update the modal checkbox if open
                    if (customAiEnabled) {
                        customAiEnabled.checked = enabled;
                    }
                } catch (e) {
                    console.error('Failed to toggle AI:', e);
                }
            });
        }

        // Load initial state
        this.loadQuickToggleState();

        // Toggle custom AI settings visibility
        if (customAiEnabled && customAiSettings) {
            customAiEnabled.addEventListener('change', () => {
                customAiSettings.style.display = customAiEnabled.checked ? 'flex' : 'none';
            });
        }

        // Update placeholders and API key hint based on service type
        const apiKeySection = document.getElementById('apiKeySection');
        const apiKeyHint = document.getElementById('apiKeyHint');
        const updatePlaceholders = () => {
            if (!customAiType || !customAiUrl || !customAiModel) return;
            if (customAiType.value === 'ollama') {
                customAiUrl.placeholder = 'http://192.168.1.100:11434';
                customAiModel.placeholder = 'llama3:latest';
                // Ollama typically doesn't need API key
                if (apiKeyHint) apiKeyHint.textContent = '(not required for Ollama)';
                if (apiKeySection) apiKeySection.style.display = 'none';
            } else {
                // OpenAI-compatible (Open-WebUI, Posterchanai)
                customAiUrl.placeholder = 'http://192.168.1.100:3051';
                customAiModel.placeholder = 'llama3';
                if (apiKeyHint) apiKeyHint.textContent = '(required for Open-WebUI/Posterchanai)';
                if (apiKeySection) apiKeySection.style.display = 'block';
            }
        };
        if (customAiType) {
            customAiType.addEventListener('change', updatePlaceholders);
            updatePlaceholders(); // Set initial placeholders
        }

        // Toggle custom image settings visibility
        if (customImageEnabled && customImageSettings) {
            customImageEnabled.addEventListener('change', () => {
                customImageSettings.style.display = customImageEnabled.checked ? 'flex' : 'none';
            });
        }

        // Toggle news schedule settings visibility
        if (newsScheduleEnabled && newsScheduleSettings) {
            newsScheduleEnabled.addEventListener('change', () => {
                newsScheduleSettings.style.display = newsScheduleEnabled.checked ? 'flex' : 'none';
            });
        }

        // Test custom AI connection
        if (testCustomAi) {
            testCustomAi.addEventListener('click', async () => {
                testAiResult.textContent = 'Testing...';
                testAiResult.className = 'test-result';
                try {
                    // Don't send placeholder '********' as actual key - use null to indicate "use stored key"
                    const apiKeyValue = customAiApiKey.value === '********' ? null : (customAiApiKey.value || null);
                    const response = await csrfFetch('/api/auth/test-custom-ai', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            api_type: customAiType.value,
                            url: customAiUrl.value,
                            model: customAiModel.value,
                            api_key: apiKeyValue,
                            use_stored_key: customAiApiKey.value === '********'  // Tell backend to use stored key
                        })
                    });
                    const data = await response.json();
                    testAiResult.textContent = data.message;
                    testAiResult.className = 'test-result ' + (data.success ? 'success' : 'error');
                } catch (e) {
                    testAiResult.textContent = 'Test failed';
                    testAiResult.className = 'test-result error';
                }
            });
        }

        // Test custom image connection
        if (testCustomImage) {
            testCustomImage.addEventListener('click', async () => {
                testImageResult.textContent = 'Testing...';
                testImageResult.className = 'test-result';
                try {
                    const response = await csrfFetch('/api/auth/test-custom-image?url=' + encodeURIComponent(customImageUrl.value), {
                        method: 'POST'
                    });
                    const data = await response.json();
                    testImageResult.textContent = data.message;
                    testImageResult.className = 'test-result ' + (data.success ? 'success' : 'error');
                } catch (e) {
                    testImageResult.textContent = 'Test failed';
                    testImageResult.className = 'test-result error';
                }
            });
        }

        // Test WebDAV Music connection
        if (testWebdavMusic) {
            testWebdavMusic.addEventListener('click', async () => {
                testMusicResult.textContent = 'Testing...';
                testMusicResult.className = 'test-result';
                try {
                    // Send form values directly to test endpoint (like custom AI test)
                    const passwordValue = webdavMusicPassword.value === '********' ? null : webdavMusicPassword.value;
                    const response = await csrfFetch('/api/music/test', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            url: webdavMusicUrl.value.trim(),
                            username: webdavMusicUsername.value.trim(),
                            password: passwordValue,
                            use_stored_password: webdavMusicPassword.value === '********'
                        })
                    });
                    const data = await response.json();
                    testMusicResult.textContent = data.message;
                    testMusicResult.className = 'test-result ' + (data.success ? 'success' : 'error');
                } catch (e) {
                    testMusicResult.textContent = 'Test failed';
                    testMusicResult.className = 'test-result error';
                }
            });
        }

        if (settingsBtn && settingsModal) {
            settingsBtn.addEventListener('click', async () => {
                // Load current settings
                try {
                    const response = await fetch('/api/auth/settings');
                    if (response.ok) {
                        const data = await response.json();
                        emailInput.value = data.notification_email || '';
                        this.notificationEmail = data.notification_email;
                        // Update avatar preview
                        this.updateAvatarPreview(data.avatar);

                        // Load custom AI settings
                        if (customAiEnabled) {
                            customAiEnabled.checked = data.custom_ai_enabled || false;
                            customAiSettings.style.display = data.custom_ai_enabled ? 'flex' : 'none';
                        }
                        if (customAiType) customAiType.value = data.custom_ai_type || 'ollama';
                        if (customAiUrl) customAiUrl.value = data.custom_ai_url || '';
                        if (customAiModel) customAiModel.value = data.custom_ai_model || '';
                        if (customAiApiKey) customAiApiKey.value = data.custom_ai_has_api_key ? '********' : '';
                        // Update placeholders based on loaded service type
                        updatePlaceholders();

                        // Load custom image settings
                        if (customImageEnabled) {
                            customImageEnabled.checked = data.custom_image_enabled || false;
                            customImageSettings.style.display = data.custom_image_enabled ? 'flex' : 'none';
                        }
                        if (customImageUrl) customImageUrl.value = data.custom_image_url || '';

                        // Load news schedule settings
                        if (newsScheduleEnabled) {
                            newsScheduleEnabled.checked = data.news_schedule_enabled || false;
                            newsScheduleSettings.style.display = data.news_schedule_enabled ? 'flex' : 'none';
                        }
                        if (newsScheduleTime) newsScheduleTime.value = data.news_schedule_time || '12:00';
                        if (newsSources) newsSources.value = data.news_sources || '';

                        // Load Native RSS settings
                        if (rssEnabled) {
                            rssEnabled.checked = data.rss_enabled || false;
                            if (rssSettings) rssSettings.style.display = data.rss_enabled ? 'flex' : 'none';
                            if (data.rss_enabled) loadRssFeeds();
                        }
                        if (rssSkipSummarization) {
                            rssSkipSummarization.checked = data.rss_skip_summarization || false;
                        }

                        // Load Calendar & Contacts settings
                        if (scheduleEnabled) {
                            scheduleEnabled.checked = data.schedule_enabled || false;
                        }
                        if (data.caldav_calendars) {
                            caldavCalendars = data.caldav_calendars;
                            renderCalendarList();
                        }
                        if (carddavUrl) carddavUrl.value = data.carddav_url || '';
                        if (carddavUsername) carddavUsername.value = data.carddav_username || '';
                        if (carddavPassword) carddavPassword.value = data.carddav_has_password ? '********' : '';

                        // Load Mail account settings
                        if (data.mail_accounts) {
                            mailAccounts = data.mail_accounts;
                            renderMailAccountList();
                        }

                        // Load WebDAV Music settings
                        console.log('Loading WebDAV Music settings from server:', {
                            url: data.webdav_music_url,
                            username: data.webdav_music_username,
                            hasPassword: data.webdav_music_has_password
                        });
                        if (webdavMusicUrl) webdavMusicUrl.value = data.webdav_music_url || '';
                        if (webdavMusicUsername) webdavMusicUsername.value = data.webdav_music_username || '';
                        if (webdavMusicPassword) webdavMusicPassword.value = data.webdav_music_has_password ? '********' : '';
                    }
                } catch (e) {
                    console.error('Failed to load settings:', e);
                }
                settingsModal.style.display = 'flex';
                // Close the user menu
                document.getElementById('userMenu').classList.remove('active');
            });

            closeBtn.addEventListener('click', () => {
                settingsModal.style.display = 'none';
            });

            // User Settings Tab Switching
            const userTabs = settingsModal.querySelectorAll('.user-tab-btn');
            const userTabContents = settingsModal.querySelectorAll('.user-tab-content');
            console.log('User Settings tabs found:', userTabs.length, 'contents:', userTabContents.length);

            userTabs.forEach(tab => {
                tab.addEventListener('click', () => {
                    const targetTab = tab.dataset.tab;
                    console.log('Tab clicked:', targetTab);

                    // Remove active from all tabs and content
                    userTabs.forEach(t => t.classList.remove('active'));
                    userTabContents.forEach(c => c.classList.remove('active'));

                    // Add active to clicked tab and corresponding content
                    tab.classList.add('active');
                    const targetContent = document.getElementById(`user-tab-${targetTab}`);
                    if (targetContent) {
                        targetContent.classList.add('active');
                    }

                    // Load API keys when switching to that tab
                    if (targetTab === 'apikeys' && typeof apiKeysManager !== 'undefined') {
                        apiKeysManager.loadKeys();
                    }
                    
                    // Load storage/cloud addresses when switching to that tab
                    if (targetTab === 'storage') {
                        this.loadStorageAddresses();
                        this.loadStorageUsage();
                    }
                });
            });

            settingsModal.addEventListener('click', (e) => {
                if (e.target === settingsModal) {
                    settingsModal.style.display = 'none';
                }
            });

            saveBtn.addEventListener('click', async () => {
                const email = emailInput.value.trim();
                statusEl.textContent = 'Saving...';
                statusEl.className = 'settings-status';

                // Build settings object
                const settingsData = {
                    notification_email: email
                };

                // Add custom AI settings
                if (customAiEnabled) {
                    settingsData.custom_ai_enabled = customAiEnabled.checked;
                }
                if (customAiType) {
                    settingsData.custom_ai_type = customAiType.value;
                }
                if (customAiUrl) {
                    settingsData.custom_ai_url = customAiUrl.value.trim();
                }
                if (customAiModel) {
                    settingsData.custom_ai_model = customAiModel.value.trim();
                }
                // Only update API key if it's not the placeholder
                if (customAiApiKey && customAiApiKey.value !== '********') {
                    settingsData.custom_ai_api_key = customAiApiKey.value;
                }

                // Add custom image settings
                if (customImageEnabled) {
                    settingsData.custom_image_enabled = customImageEnabled.checked;
                }
                if (customImageUrl) {
                    settingsData.custom_image_url = customImageUrl.value.trim();
                }

                // Add news schedule settings
                if (newsScheduleEnabled) {
                    settingsData.news_schedule_enabled = newsScheduleEnabled.checked;
                }
                if (newsScheduleTime) {
                    settingsData.news_schedule_time = newsScheduleTime.value;
                }
                if (newsSources) {
                    settingsData.news_sources = newsSources.value;
                }

                // Add Native RSS settings
                if (rssEnabled) {
                    settingsData.rss_enabled = rssEnabled.checked;
                }
                if (rssSkipSummarization) {
                    settingsData.rss_skip_summarization = rssSkipSummarization.checked;
                }

                // Add Calendar & Contacts settings
                if (scheduleEnabled) {
                    settingsData.schedule_enabled = scheduleEnabled.checked;
                }
                // Collect calendar data from the dynamic list
                settingsData.caldav_calendars = collectCalendarData();
                if (carddavUrl) {
                    settingsData.carddav_url = carddavUrl.value.trim();
                }
                if (carddavUsername) {
                    settingsData.carddav_username = carddavUsername.value.trim();
                }
                // Only update password if it's not the placeholder
                if (carddavPassword && carddavPassword.value !== '********') {
                    settingsData.carddav_password = carddavPassword.value;
                }

                // Add Mail account settings
                settingsData.mail_accounts = collectMailAccountData();

                // Add WebDAV Music settings
                console.log('WebDAV Music elements:', {
                    urlEl: !!webdavMusicUrl,
                    usernameEl: !!webdavMusicUsername,
                    passwordEl: !!webdavMusicPassword,
                    urlVal: webdavMusicUrl?.value,
                    usernameVal: webdavMusicUsername?.value,
                    passwordVal: webdavMusicPassword?.value?.substring(0, 3) + '...'
                });
                if (webdavMusicUrl) {
                    settingsData.webdav_music_url = webdavMusicUrl.value.trim();
                }
                if (webdavMusicUsername) {
                    settingsData.webdav_music_username = webdavMusicUsername.value.trim();
                }
                // Only update password if it's not the placeholder
                if (webdavMusicPassword && webdavMusicPassword.value !== '********') {
                    settingsData.webdav_music_password = webdavMusicPassword.value;
                }
                console.log('WebDAV Music in settingsData:', {
                    url: settingsData.webdav_music_url,
                    username: settingsData.webdav_music_username,
                    hasPassword: !!settingsData.webdav_music_password
                });

                try {
                    const response = await csrfFetch('/api/auth/settings', {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(settingsData)
                    });

                    if (response.ok) {
                        this.notificationEmail = email;
                        statusEl.textContent = 'Settings saved!';
                        statusEl.className = 'settings-status success';
                        setTimeout(() => { statusEl.textContent = ''; }, 2000);
                        // Update quick toggle in user menu
                        this.loadQuickToggleState();
                    } else {
                        const data = await response.json();
                        statusEl.textContent = data.detail || 'Failed to save';
                        statusEl.className = 'settings-status error';
                    }
                } catch (e) {
                    statusEl.textContent = 'Failed to save settings';
                    statusEl.className = 'settings-status error';
                }
            });

            // Avatar upload handlers
            const avatarInput = document.getElementById('avatarInput');
            const uploadAvatarBtn = document.getElementById('uploadAvatarBtn');
            const deleteAvatarBtn = document.getElementById('deleteAvatarBtn');
            const avatarStatus = document.getElementById('avatarStatus');

            if (uploadAvatarBtn && avatarInput) {
                uploadAvatarBtn.addEventListener('click', () => {
                    avatarInput.click();
                });

                avatarInput.addEventListener('change', async (e) => {
                    const file = e.target.files[0];
                    if (!file) return;

                    avatarStatus.textContent = 'Uploading...';
                    avatarStatus.className = 'settings-status';

                    const formData = new FormData();
                    formData.append('file', file);

                    try {
                        console.log('Uploading avatar...', file.name, file.type, file.size);
                        const response = await csrfFetch('/api/auth/avatar', {
                            method: 'POST',
                            body: formData
                            // Don't set Content-Type header - browser will set it with boundary for FormData
                        });

                        console.log('Avatar upload response status:', response.status);
                        
                        if (response.ok) {
                            const data = await response.json();
                            console.log('Avatar upload success:', data);
                            console.log('Avatar URL:', data.avatar);
                            // Force reload by adding cache bust
                            const avatarUrl = data.avatar + (data.avatar.includes('?') ? '&' : '?') + 't=' + Date.now();
                            console.log('Avatar URL with cache bust:', avatarUrl);
                            this.updateAvatarPreview(avatarUrl);
                            avatarStatus.textContent = 'Avatar uploaded!';
                            avatarStatus.className = 'settings-status success';
                            setTimeout(() => { avatarStatus.textContent = ''; }, 2000);
                        } else {
                            const data = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
                            console.error('Avatar upload failed:', data);
                            avatarStatus.textContent = data.detail || `Upload failed (${response.status})`;
                            avatarStatus.className = 'settings-status error';
                        }
                    } catch (e) {
                        console.error('Avatar upload error:', e);
                        avatarStatus.textContent = `Upload failed: ${e.message || 'Unknown error'}`;
                        avatarStatus.className = 'settings-status error';
                    }
                    avatarInput.value = '';
                });

                if (deleteAvatarBtn) {
                    deleteAvatarBtn.addEventListener('click', async () => {
                        avatarStatus.textContent = 'Removing...';
                        try {
                            const response = await csrfFetch('/api/auth/avatar', { method: 'DELETE' });
                            if (response.ok) {
                                this.updateAvatarPreview(null);
                                avatarStatus.textContent = 'Avatar removed';
                                avatarStatus.className = 'settings-status success';
                                setTimeout(() => { avatarStatus.textContent = ''; }, 2000);
                            }
                        } catch (e) {
                            avatarStatus.textContent = 'Failed to remove';
                            avatarStatus.className = 'settings-status error';
                        }
                    });
                }
            }
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
                
                // Update WebDAV address
                const webdavInput = document.getElementById('webdavAddress');
                const webdavUsername = document.getElementById('webdavUsername');
                console.log('WebDAV elements:', { input: !!webdavInput, username: !!webdavUsername, url: data.webdav_url });
                
                if (webdavInput) {
                    if (data.webdav_url) {
                        // Replace localhost with actual hostname
                        const webdavUrl = data.webdav_url.replace('localhost', hostname).replace('http://', `${protocol}//`);
                        webdavInput.value = webdavUrl;
                        console.log('Set WebDAV URL:', webdavUrl);
                    } else {
                        webdavInput.value = '';
                        console.log('WebDAV URL is empty (server not enabled?)');
                    }
                }
                if (webdavUsername && data.username) {
                    webdavUsername.textContent = data.username;
                    console.log('Set WebDAV username:', data.username);
                }
                
                // Update CalDAV address
                const caldavInput = document.getElementById('caldavAddress');
                const caldavUsername = document.getElementById('caldavUsername');
                console.log('CalDAV elements:', { input: !!caldavInput, username: !!caldavUsername, url: data.caldav_url });
                
                if (caldavInput) {
                    if (data.caldav_url) {
                        const caldavUrl = data.caldav_url.replace('localhost', hostname).replace('http://', `${protocol}//`);
                        caldavInput.value = caldavUrl;
                        console.log('Set CalDAV URL:', caldavUrl);
                    } else {
                        caldavInput.value = '';
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
                console.log('CardDAV elements:', { input: !!carddavInput, username: !!carddavUsername, url: data.carddav_url });
                
                if (carddavInput) {
                    if (data.carddav_url) {
                        const carddavUrl = data.carddav_url.replace('localhost', hostname).replace('http://', `${protocol}//`);
                        carddavInput.value = carddavUrl;
                        console.log('Set CardDAV URL:', carddavUrl);
                    } else {
                        carddavInput.value = '';
                        console.log('CardDAV URL is empty (server not enabled?)');
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
        const file = e.target.files[0];
        if (!file) return;

        const isImage = file.type.startsWith('image/');
        const isPDF = file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf');

        if (isImage) {
            // Handle image upload
            const reader = new FileReader();
            reader.onload = (e) => {
                const base64 = e.target.result.split(',')[1];  // Remove data:image/...;base64, prefix
                this.uploadedImage = base64;
                this.uploadedFile = null;
                this.uploadedPDF = null;

                // Show preview
                this.imagePreview.src = e.target.result;
                this.imagePreview.style.display = 'block';
                this.filePreview.textContent = '';
                this.filePreview.style.display = 'none';
                this.uploadPreview.style.display = 'flex';
            };
            reader.readAsDataURL(file);
        } else if (isPDF) {
            // Handle PDF upload - send as base64
            const reader = new FileReader();
            reader.onload = (e) => {
                const base64 = e.target.result.split(',')[1];
                this.uploadedPDF = base64;
                this.uploadedImage = null;
                this.uploadedFile = null;
                this.uploadedDocument = null;

                // Show preview
                this.imagePreview.style.display = 'none';
                this.filePreview.textContent = `📕 ${file.name} (${this.formatFileSize(file.size)})`;
                this.filePreview.style.display = 'block';
                this.uploadPreview.style.display = 'flex';
            };
            reader.readAsDataURL(file);
        } else if (this.isOfficeFile(file.name)) {
            // Handle Office documents - send as base64
            const reader = new FileReader();
            reader.onload = (e) => {
                const base64 = e.target.result.split(',')[1];
                this.uploadedDocument = base64;
                this.uploadedImage = null;
                this.uploadedFile = null;
                this.uploadedPDF = null;

                // Show preview with appropriate icon
                const icon = file.name.endsWith('.docx') ? '📝' :
                            file.name.endsWith('.xlsx') ? '📊' :
                            file.name.endsWith('.pptx') ? '📽️' : '📄';
                this.imagePreview.style.display = 'none';
                this.filePreview.textContent = `${icon} ${file.name} (${this.formatFileSize(file.size)})`;
                this.filePreview.style.display = 'block';
                this.uploadPreview.style.display = 'flex';
            };
            reader.readAsDataURL(file);
        } else {
            // Handle text file upload
            const reader = new FileReader();
            reader.onload = (e) => {
                this.uploadedFile = e.target.result;
                this.uploadedImage = null;
                this.uploadedPDF = null;

                // Show preview
                this.imagePreview.style.display = 'none';
                this.filePreview.textContent = `📄 ${file.name} (${this.formatFileSize(file.size)})`;
                this.filePreview.style.display = 'block';
                this.uploadPreview.style.display = 'flex';
            };
            reader.readAsText(file);
        }

        // Clear input so same file can be selected again
        this.fileInput.value = '';
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
        this.uploadedImage = null;
        this.uploadedFile = null;
        this.uploadedPDF = null;
        this.uploadedDocument = null;
        this.uploadPreview.style.display = 'none';
        this.imagePreview.src = '';
        this.filePreview.textContent = '';
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

        // Need either content or a file upload
        if (!content && !this.uploadedFile && !this.uploadedImage) return;

        // Save to message history for up arrow recall
        this.saveToHistory(displayContent);

        if (mode) {
            content = `${mode} ${content}`;
        }

        // Build display message
        let displayMsg = displayContent;
        if (this.uploadedFile) {
            displayMsg = displayContent + ' [with file]';
        }

        // Get image data URL before clearing (so we can show it inline)
        let imageDataUrl = null;
        if (this.uploadedImage) {
            imageDataUrl = this.imagePreview.src;  // This is the data:image/... URL
        }

        // Add user message to UI (show what user typed, not the command)
        // skipUserButtons=true because we add them manually below with proper cleanup
        this.lastUserMessage = this.addMessage('user', displayMsg || '[File uploaded]', false, true, imageDataUrl);

        // Add action buttons to user message
        const userContentEl = this.lastUserMessage.querySelector('.message-content');

        // Remove action buttons from previous user messages
        const prevUserActionBtns = this.messagesContainer.querySelectorAll('.message.user .btn-regenerate, .message.user .btn-edit');
        prevUserActionBtns.forEach(btn => btn.remove());

        // Store image data on message element for editing later
        if (this.uploadedImage) {
            this.lastUserMessage._imageData = this.uploadedImage;
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

        // Include uploaded data
        if (this.uploadedImage) {
            payload.image_data = this.uploadedImage;
        }
        if (this.uploadedFile) {
            payload.file_content = this.uploadedFile;
        }
        if (this.uploadedPDF) {
            payload.pdf_data = this.uploadedPDF;
        }
        if (this.uploadedDocument) {
            payload.document_data = this.uploadedDocument;
        }

        // Store payload for potential retry
        this.lastPayload = payload;

        // Send to server (with command prepended)
        this.ws.send(JSON.stringify(payload));

        // Clear upload after sending
        this.clearUpload();

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

        // Intercept notes command to open notes browser
        const notesCmdMatch = decodedCmd.trim().toLowerCase();
        if (notesCmdMatch === 'notes' || notesCmdMatch.startsWith('notes ')) {
            if (window.notesBrowser) {
                window.notesBrowser.open();
                // Handle subcommands
                if (notesCmdMatch.startsWith('notes search ')) {
                    const query = decodedCmd.substring('notes search '.length).trim();
                    if (query && window.notesBrowser) {
                        setTimeout(() => {
                            document.getElementById('notesBrowserSearchInput').value = query;
                            window.notesBrowser.searchQuery = query;
                            window.notesBrowser.loadNotes();
                        }, 100);
                    }
                } else if (notesCmdMatch.startsWith('notes folder ')) {
                    const folderName = decodedCmd.substring('notes folder '.length).trim();
                    // Folder filtering would need folder name lookup - for now just open browser
                }
                return;  // Don't send to server
            } else if (window.openNotesModal) {
                // Fallback to modal if browser not available
                window.openNotesModal();
                return;  // Don't send to server
            }
        }

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
                this.handleStreamChunk(data.content);
                break;
            case 'stream_clear':
                // Clear current streaming content for follow-up (e.g., after plugin execution)
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

        // Buffer content
        this.fullStreamContent += content;

        // Strip thinking tags from display
        let displayContent = this.stripThinkingTags(this.fullStreamContent);

        // Only show content after thinking is done
        if (displayContent) {
            const contentEl = this.streamingMessage.querySelector('.message-content');
            contentEl.innerHTML = this.formatMessage(displayContent);
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
        // Wait for enough content before showing (in case <think comes later)
        if (text.trim().length < 50) {
            return '';
        }
        return text;
    }

    handleStreamEnd() {
        if (this.streamingMessage) {
            const contentEl = this.streamingMessage.querySelector('.message-content');
            // Use buffered content instead of reading from DOM, with thinking stripped
            const content = this.stripThinkingTags(this.fullStreamContent);

            // Final render with complete content
            contentEl.innerHTML = this.formatMessage(content);

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
        if (!text) return '';
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

    async startMusicShuffle() {
        // Send the music shuffle command
        try {
            const response = await csrfFetch('/api/command', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ command: 'music shuffle' })
            });

            if (response.ok) {
                const data = await response.json();
                if (data.type === 'music_playlist' && data.tracks && window.musicPlayer) {
                    // Shuffle the tracks
                    const shuffled = [...data.tracks].sort(() => Math.random() - 0.5);
                    window.musicPlayer.clearQueue();
                    shuffled.forEach(t => window.musicPlayer.addToQueue(t));
                    if (shuffled.length > 0) {
                        window.musicPlayer.play(shuffled[0]);
                    }
                    this.showToast(`Shuffling ${shuffled.length} tracks`);
                } else if (data.type === 'music_play' && data.track && window.musicPlayer) {
                    window.musicPlayer.play(data.track);
                } else if (data.content) {
                    this.showToast(data.content);
                }
            }
        } catch (e) {
            console.error('Music shuffle error:', e);
            this.showToast('Failed to start shuffle');
        }
    }

    handleCommandResponse(data) {
        // Always hide typing indicator and reset button first
        this.hideTypingIndicator();
        this.resetSendButton();
        
        // Ensure we're not in streaming mode
        this.isStreaming = false;
        this.streamingMessage = null;

        let html = '';
        let contentHtml = '';

        // Format content separately - don't mix with structured data
        if (data.content) {
            contentHtml = this.formatMessage(data.content);
        }

        // Handle different response types
        if (data.type === 'images' && data.images) {
            html = contentHtml;
            html += '<div class="image-grid">';
            for (const img of data.images) {
                const safeSrc = this.escapeUrl(img.img_src);
                const safeUrl = this.escapeUrl(img.url);
                const safeTitle = this.escapeHtml(img.title || '');
                html += `<a href="${safeUrl}" target="_blank" class="image-link">
                    <img src="${safeSrc}" alt="${safeTitle}"
                         onerror="this.parentElement.style.display='none';"
                         loading="lazy">
                </a>`;
            }
            html += '</div>';
        } else if (data.type === 'generated_image' && data.image) {
            html = contentHtml;
            const imageId = 'img_' + Date.now();
            html += `<div class="image-wrapper">
                <img src="data:image/png;base64,${data.image}" alt="Generated image" class="generated-image" id="${imageId}">
                <div class="image-actions">
                    <button class="btn-action" onclick="window.chatHandler.downloadImage('${imageId}')" title="Download">⬇️</button>
                    <button class="btn-action" onclick="window.chatHandler.copyImage('${imageId}')" title="Copy to clipboard">📋</button>
                </div>
            </div>`;

            // Notify mascot for image generation
            if (window.mascotController) {
                window.mascotController.onResponse(true);
            }
        } else if (data.type === 'search' && data.results) {
            html = contentHtml;
            html += '<div class="search-results">';
            for (const r of data.results) {
                const safeUrl = this.escapeUrl(r.url);
                const safeTitle = this.escapeHtml(r.title);
                const safeContent = this.escapeHtml(r.content);
                html += `<div class="search-result">
                    <a href="${safeUrl}" target="_blank">${safeTitle}</a>
                    <p>${safeContent}</p>
                </div>`;
            }
            html += '</div>';
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
                    
                    html += `<div class="file-result-item" data-file-path="${this.escapeHtml(file.path)}" data-file-name="${safeName}">
                        <div class="file-result-header">
                            ${file.thumbnail ? `<img src="${file.thumbnail}" alt="${safeName}" class="file-thumbnail">` : '<div class="file-icon">📄</div>'}
                            <div class="file-info">
                                <div class="file-name">${safeName}</div>
                                <div class="file-meta">${safePath} • ${fileSize}${modifiedDate ? ' • ' + modifiedDate : ''}</div>
                            </div>
                        </div>
                        <div class="file-actions">
                            <button class="file-action-btn" data-action="open" data-path="${escapedPath}" data-name="${escapedName}" title="Open">👁️ Open</button>
                            <button class="file-action-btn" data-action="download" data-path="${escapedPath}" data-name="${escapedName}" title="Download">⬇️ Download</button>
                            <button class="file-action-btn" data-action="preview" data-path="${escapedPath}" data-name="${escapedName}" title="Preview URL (Quick Share)">🔍 Preview URL</button>
                            <button class="file-action-btn" data-action="share" data-path="${escapedPath}" data-name="${escapedName}" title="Share Public URL">🔗 Share</button>
                            <button class="file-action-btn" data-action="email" data-path="${escapedPath}" data-name="${escapedName}" title="Email">✉️ Email</button>
                            <button class="file-action-btn file-action-delete" data-action="delete" data-path="${escapedPath}" data-name="${escapedName}" title="Delete">🗑️ Delete</button>
                        </div>
                    </div>`;
                }
            }
            html += '</div>';
        } else if (data.type === 'music_play' && data.track && window.musicPlayer) {
            // Play single track
            window.musicPlayer.play(data.track);
        } else if (data.type === 'music_playlist' && data.tracks && window.musicPlayer) {
            // Play playlist (multiple tracks)
            window.musicPlayer.clearQueue();
            data.tracks.forEach(t => window.musicPlayer.addToQueue(t));
            if (data.tracks.length > 0) {
                window.musicPlayer.play(data.tracks[0]);
            }
        } else if (data.type === 'music_next' && window.musicPlayer) {
            // Skip to next track
            window.musicPlayer.next();
        } else if (data.type === 'music_prev' && window.musicPlayer) {
            // Go to previous track
            window.musicPlayer.prev();
        } else if (data.type === 'music_stop' && window.musicPlayer) {
            // Stop playback
            window.musicPlayer.stop();
        } else {
            // Default: just use formatted content
            html = contentHtml;
        }

        const messageEl = this.addMessage('assistant', html, true);
        
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
                console.log(`Attached event delegation listener for file action buttons. Found ${buttons.length} buttons.`);
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
        contentEl.innerHTML = this.formatMessage(newContent);

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

        const contentEl = document.createElement('div');
        contentEl.className = 'message-content';

        if (isHtml) {
            contentEl.innerHTML = content;
        } else {
            contentEl.innerHTML = this.formatMessage(content);
        }

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
            copyBtn.onclick = () => this.copyText(contentEl.textContent);
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

    addSummarizeIcons(contentEl) {
        // Find all external links (not local paths)
        const links = contentEl.querySelectorAll('a[href^="http"]');
        links.forEach(link => {
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
        const url = `/api/files/view/${encodeURIComponent(filePath)}`;
        window.open(url, '_blank');
        this.showToast(`Opening ${fileName}...`);
    }
    
    async downloadFile(filePath, fileName) {
        try {
            const response = await fetch(`/api/files/view/${encodeURIComponent(filePath)}`);
            if (!response.ok) {
                throw new Error('Failed to download file');
            }
            const blob = await response.blob();
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = fileName;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            window.URL.revokeObjectURL(url);
            this.showToast('File downloaded');
        } catch (error) {
            console.error('Error downloading file:', error);
            this.showToast('Error downloading file', 'error');
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
        // Use file manager's share modal if available, otherwise create share directly
        if (window.fileManager && typeof window.fileManager.shareFile === 'function') {
            window.fileManager.shareFile(filePath, fileName);
        } else {
            // Fallback: create share directly
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
                this.showToast(`Share URL copied to clipboard: ${shareUrl}`);
            } catch (error) {
                console.error('Error sharing file:', error);
                this.showToast('Error sharing file', 'error');
            }
        }
    }

    async emailFile(filePath, fileName) {
        // Open email modal with file pre-selected
        if (window.fileManager) {
            window.fileManager.emailFile(filePath, fileName);
        } else {
            // Fallback: prompt for email address
            const to = prompt('Enter email address:');
            if (!to) return;
            
            try {
                const response = await csrfFetch('/api/files/email', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        file_paths: [filePath],
                        to: to,
                        subject: `Shared file: ${fileName}`,
                        body: `Please find the attached file: ${fileName}`
                    })
                });
                if (!response.ok) {
                    throw new Error('Failed to send email');
                }
                this.showToast('Email sent successfully');
            } catch (error) {
                console.error('Error sending email:', error);
                this.showToast('Error sending email', 'error');
            }
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
        if (!text) return '';

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

        // Escape HTML
        let html = processed
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');

        // Restore markdown links as HTML
        html = html.replace(/\x00LINK(\d+)\x00/g, (match, index) => {
            const link = links[parseInt(index)];
            if (link.isCommand) {
                // Command button - clicking executes the command
                const escapedCmd = this.escapeHtml(link.cmd);
                return `<button class="cmd-btn" data-cmd="${escapedCmd}" onclick="window.chatHandler.executeCommand('${escapedCmd.replace(/'/g, "\\'")}')">${this.escapeHtml(link.text)}</button>`;
            }
            if (link.isEditEvent) {
                // Edit event button - opens calendar modal
                const escapedUid = this.escapeHtml(link.uid);
                return `<button class="cmd-btn" onclick="window.chatHandler.openEditEventModal('${escapedUid.replace(/'/g, "\\'")}')">${this.escapeHtml(link.text)}</button>`;
            }
            if (link.isCopy) {
                // Copy button - copies content to clipboard
                const escapedContent = this.escapeHtml(link.content).replace(/'/g, "\\'").replace(/\n/g, '\\n');
                return `<button class="cmd-btn copy-btn" onclick="window.chatHandler.copyToClipboard('${escapedContent}')">${this.escapeHtml(link.text)}</button>`;
            }
            const target = link.external ? ' target="_blank"' : '';
            const download = link.download ? ' download' : '';
            // Don't encode mailto: or tel: URLs, they use different escaping
            let safeUrl = link.url;
            if (link.url.startsWith('/')) {
                safeUrl = link.url;
            } else if (link.url.startsWith('mailto:') || link.url.startsWith('tel:')) {
                safeUrl = link.url;  // Keep as-is
            } else {
                safeUrl = encodeURI(link.url);
            }
            return `<a href="${safeUrl}"${target}${download}>${this.escapeHtml(link.text)}</a>`;
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
            const escapedCode = block.code
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

    // Load plugins for autocomplete
    async loadPluginsForAutocomplete() {
        try {
            const response = await fetch('/api/plugins');
            if (response.ok) {
                const plugins = await response.json();
                this.pluginActions = [];
                for (const plugin of plugins) {
                    if (plugin.enabled) {
                        // Add plugin name as a keyword
                        const pluginKeyword = plugin.name.toLowerCase().replace(/\s+/g, '');
                        if (!this.commands.includes(pluginKeyword)) {
                            this.commands.push(pluginKeyword);
                        }
                        // Store plugin action hints
                        if (plugin.actions) {
                            for (const action of plugin.actions) {
                                this.pluginActions.push({
                                    plugin: plugin.name,
                                    action: action.name,
                                    description: action.description,
                                    keyword: `${pluginKeyword} ${action.name}`.toLowerCase()
                                });
                            }
                        }
                    }
                }
            }
        } catch (e) {
            console.error('Failed to load plugins for autocomplete:', e);
        }
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
                    // Also set up placeholders for send/forward recipient hints (populated by loadContactEmailsForAutocomplete)
                    for (const account of accountHints) {
                        this.subcommands[`mail folder ${account}`] = folderHints.map(f => f.replace(':', ''));
                        this.subcommands[`mail read ${account}`] = folderHints;
                        this.subcommands[`mail summary ${account}`] = folderHints;
                        this.subcommands[`mail send ${account}`] = [];  // Will be populated with contact emails
                        this.subcommands[`mail forward ${account}`] = [];  // Will be populated with contact emails
                    }
                    this.subcommands['mail translate'] = accountHints; // language first, then account

                    // Store account hints for contact email loading
                    this.mailAccountHints = accountHints;
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

    async loadContactEmailsForAutocomplete() {
        try {
            const response = await fetch('/api/contacts/emails');
            if (response.ok) {
                const contacts = await response.json();
                if (contacts && contacts.length > 0) {
                    // Create email hints - use name or email prefix for easy matching
                    const emailHints = contacts.map(c => c.email);
                    console.log('Contact email hints loaded:', emailHints.length);

                    // Store for use in autocomplete
                    this.contactEmails = emailHints;

                    // Add contact emails to mail send and mail forward subcommands
                    // Use stored account hints from loadMailAccountsForAutocomplete
                    const accountHints = this.mailAccountHints || [];
                    for (const account of accountHints) {
                        this.subcommands[`mail send ${account}`] = emailHints;
                        this.subcommands[`mail forward ${account}`] = emailHints;
                    }
                    console.log('Contact emails added to mail subcommands for accounts:', accountHints);
                }
            }
        } catch (e) {
            console.debug('Could not load contact emails:', e);
        }
    }

    async loadNoteTitlesForAutocomplete() {
        try {
            const response = await fetch('/api/notes?limit=100'); // Get recent notes for autocomplete
            if (response.ok) {
                const notes = await response.json();
                if (notes && notes.length > 0) {
                    // Extract note titles and create search hints
                    const noteTitles = notes.map(n => n.title).filter(t => t && t.length > 0);
                    console.log('Note titles loaded for autocomplete:', noteTitles.length);

                    // Store for use in autocomplete
                    this.noteTitles = noteTitles;

                    // Add note titles to notes search subcommand for autocomplete
                    // Use a copy to avoid modifying the original array
                    this.subcommands['notes search'] = [...noteTitles];
                    this.subcommands['note find'] = [...noteTitles];
                    this.subcommands['find note'] = [...noteTitles];
                    
                    console.log('Note titles added to autocomplete:', noteTitles.length, 'titles');
                }
            }
        } catch (e) {
            console.debug('Could not load note titles:', e);
        }
    }

    // Subcommands that can be autocompleted
    subcommands = {
        'torrents': ['download', 'list', 'add', 'start', 'stop', 'delete', 'movies', 'tv', 'music', 'anime'],
        'torrents download': ['movies', 'tv', 'music', 'anime'],
        'nyaa': ['download'],
        'budget': ['bills', 'add', 'pay'],
        'firewall': ['search', 'analyze'],
        'news': ['refresh'],
        'cal': ['today', 'week', 'add'],
        'contacts': ['all', 'add', 'search'],
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
        // Music subcommands
        'music': ['browse', 'search', 'play', 'random', 'skip', 'next', 'prev', 'queue', 'mood', 'stop'],
        'music mood': ['chill', 'upbeat', 'focus', 'workout', 'relaxing', 'energetic', 'party', 'calm'],
        'music queue': ['add', 'clear'],
        // Todo subcommands
        'todo': ['add', 'rm', 'list'],
        // Notes subcommands
        'notes': ['search', 'folder', 'new', 'list'],
        // RSS subcommands
        'rss': ['sync', 'add', 'remove', 'list', 'search'],
        // YouTube download subcommands
        'ytdl': ['video']
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
                this.showToast('Type your message (optional), press Enter to forward');
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
            
            // Special case: notes search / note find / find note - show note title suggestions
            if (/^(notes\s+search|note\s+find|find\s+note)$/i.test(cmdPrefix) && this.noteTitles && this.noteTitles.length > 0) {
                // Filter note titles that match the current input
                const searchTerm = lastPart.toLowerCase();
                const matches = this.noteTitles.filter(title => 
                    title.toLowerCase().includes(searchTerm)
                ).slice(0, 5); // Limit to 5 suggestions
                
                if (matches.length === 1 && matches[0].toLowerCase().startsWith(searchTerm)) {
                    // Auto-complete if single match
                    const completed = cmdPrefix + ' ' + matches[0];
                    this.messageInput.value = completed + input.substring(cursorPos);
                    this.messageInput.setSelectionRange(completed.length, completed.length);
                } else if (matches.length > 0) {
                    this.showToast(`Note suggestions: ${matches.join(', ')}`);
                }
                return;
            }
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

        // Find matching plugin actions
        const actionMatches = this.pluginActions.filter(pa =>
            pa.keyword.startsWith(textBeforeCursor) ||
            pa.action.toLowerCase().startsWith(textBeforeCursor)
        );

        // Combine matches
        const allMatches = [...commandMatches];
        for (const am of actionMatches) {
            const actionHint = `${am.plugin.toLowerCase().replace(/\s+/g, '')} ${am.action}`;
            if (!allMatches.includes(actionHint)) {
                allMatches.push(actionHint);
            }
        }

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
