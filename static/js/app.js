// Main Application
class App {
    constructor() {
        this.conversations = [];
        this.currentConversation = null;
        this.currentMode = ''; // '', 'search', 'images', 'geni'

        this.sidebar = document.getElementById('sidebar');
        this.conversationList = document.getElementById('conversationList');
        this.chatTitle = document.getElementById('chatTitle');
        this.quickActions = document.getElementById('quickActions');
        this.messageInput = document.getElementById('messageInput');

        this.conversationPollInterval = null;
        this.conversationPollIntervalMs = 25000;

        this.init();
    }

    async init() {
        // Set up event listeners FIRST (before async operations)
        // so buttons are interactive immediately on page load
        this.setupEventListeners();

        // Load conversations
        await this.loadConversations();

        this.startConversationPolling();

        // Check for search engine query parameter (?q=...)
        const urlParams = new URLSearchParams(window.location.search);
        const searchQuery = urlParams.get('q');

        if (searchQuery) {
            // Clear the URL parameter (prevents re-search on refresh)
            window.history.replaceState({}, document.title, window.location.pathname);

            // Create a new conversation for this search
            await this.createConversation();

            // Wait for WebSocket to actually be connected (not just timeout)
            const waitForConnection = () => new Promise((resolve) => {
                const checkConnection = () => {
                    if (window.chatHandler && window.chatHandler.ws &&
                        window.chatHandler.ws.readyState === WebSocket.OPEN) {
                        resolve();
                    } else {
                        setTimeout(checkConnection, 100);
                    }
                };
                checkConnection();
            });
            await waitForConnection();

            // Activate search mode and execute query
            this.setMode('search');
            this.messageInput.value = searchQuery;
            if (window.chatHandler) {
                window.chatHandler.sendMessage();
            }
        } else if (this.conversations.length > 0) {
            // Select first conversation or show welcome
            this.selectConversation(this.conversations[0].id);
        }
    }

    setupEventListeners() {
        // New chat button
        document.getElementById('newChatBtn').addEventListener('click', () => this.createConversation());

        // Delete chat button
        document.getElementById('deleteChatBtn').addEventListener('click', () => this.deleteCurrentConversation());

        // User menu toggle
        const userMenuBtn = document.getElementById('userMenuBtn');
        const userMenuContainer = document.querySelector('.user-menu-container');
        if (userMenuBtn && userMenuContainer) {
            userMenuBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                userMenuContainer.classList.toggle('open');
            });

            // Close menu when clicking outside
            document.addEventListener('click', (e) => {
                if (!userMenuContainer.contains(e.target)) {
                    userMenuContainer.classList.remove('open');
                }
            });
        }

        // Delete all button
        document.getElementById('deleteAllBtn').addEventListener('click', () => this.deleteAllConversations());

        // Logout button
        document.getElementById('logoutBtn').addEventListener('click', () => this.logout());
        
        // Photo Gallery button handler (reusable function)
        const openPhotoGallery = () => {
            console.log('Photo Gallery button clicked');
            
            // Show viewer element directly first (don't wait for fileManager)
            const viewer = document.getElementById('cyberpunkPictureViewer');
            if (viewer) {
                console.log('Showing picture viewer directly');
                viewer.style.display = 'block';
                
                // Close the user menu
                const userMenuContainer = document.querySelector('.user-menu-container');
                if (userMenuContainer) {
                    userMenuContainer.classList.remove('open');
                }
                
                // Then load images via fileManager if available
                if (window.fileManager) {
                    window.fileManager.pictureViewerOpen = true;
                    if (typeof window.fileManager.loadAllImages === 'function') {
                        window.fileManager.loadAllImages();
                    }
                    if (typeof window.fileManager.setupInfiniteScroll === 'function') {
                        window.fileManager.setupInfiniteScroll();
                    }
                }
            } else {
                console.error('cyberpunkPictureViewer not found in DOM');
                alert('Photo Gallery not available. Please refresh.');
            }
        };
        
        // Picture viewer button (in sidebar - may not exist anymore)
        const pictureViewerBtn = document.getElementById('pictureViewerBtn');
        if (pictureViewerBtn) {
            pictureViewerBtn.addEventListener('click', openPhotoGallery);
        }
        
        // Photo Gallery button (in chat quick buttons)
        const photoGalleryBtn = document.getElementById('photoGalleryBtn');
        if (photoGalleryBtn) {
            photoGalleryBtn.addEventListener('click', openPhotoGallery);
            console.log('Photo Gallery button handler attached');
        } else {
            console.warn('photoGalleryBtn not found in DOM');
        }

        // Mobile menu
        document.getElementById('menuBtn').addEventListener('click', () => this.toggleSidebar());

        // Mode buttons (Chat, Search, Images, Generate)
        document.querySelectorAll('.mode-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                this.setMode(btn.dataset.mode);
            });
        });

        // Command buttons (Help)
        document.querySelectorAll('.quick-btn[data-cmd]').forEach(btn => {
            btn.addEventListener('click', () => {
                const cmd = btn.dataset.cmd;
                // Use executeCommand (not sendMessageDirect) so the button works even with no
                // chat window open — it auto-creates a conversation / reconnects the socket,
                // matching the dropdown command items. sendMessageDirect just bails with no ws.
                if (window.chatHandler && cmd) {
                    window.chatHandler.executeCommand(cmd);
                }
            });
        });

        // Initialize all dropdowns
        document.querySelectorAll('.quick-btn-dropdown').forEach(dropdown => {
            const toggleBtn = dropdown.querySelector('.dropdown-toggle');
            if (toggleBtn) {
                toggleBtn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    // Close other dropdowns
                    document.querySelectorAll('.quick-btn-dropdown.open').forEach(d => {
                        if (d !== dropdown) d.classList.remove('open');
                    });
                    dropdown.classList.toggle('open');
                });
            }

            // Handle command items (mail, cal, contacts)
            dropdown.querySelectorAll('.cmd-item').forEach(item => {
                item.addEventListener('click', () => {
                    dropdown.classList.remove('open');
                    const cmd = item.dataset.cmd;
                    if (window.chatHandler && cmd) {
                        window.chatHandler.executeCommand(cmd);
                    }
                });
            });

            // Handle Add Event button
            const addEventBtn = dropdown.querySelector('#addEventBtn');
            if (addEventBtn) {
                addEventBtn.addEventListener('click', () => {
                    dropdown.classList.remove('open');
                    if (window.openCalendarModal) {
                        window.openCalendarModal();
                    }
                });
            }

            // Handle mode items (search, images)
            dropdown.querySelectorAll('.dropdown-item.mode-btn').forEach(item => {
                item.addEventListener('click', () => {
                    dropdown.classList.remove('open');
                });
            });
        });

        // Close dropdowns when clicking outside
        document.addEventListener('click', (e) => {
            if (!e.target.closest('.quick-btn-dropdown')) {
                document.querySelectorAll('.quick-btn-dropdown.open').forEach(d => {
                    d.classList.remove('open');
                });
            }
        });

        // Close sidebar on overlay click (mobile)
        document.addEventListener('click', (e) => {
            if (e.target.classList.contains('sidebar-overlay')) {
                this.closeSidebar();
            }
        });
    }

    async loadConversations() {
        try {
            const response = await fetch('/api/conversations');
            if (response.ok) {
                this.conversations = await response.json();
                this.syncCurrentConversationFromList();
                this.renderConversationList();
            }
        } catch (err) {
            console.error('Failed to load conversations:', err);
        }
    }

    /** Sync currentConversation with the latest from this.conversations (e.g. after refresh). */
    syncCurrentConversationFromList() {
        if (!this.currentConversation) return;
        const updated = this.conversations.find(c => c.id === this.currentConversation.id);
        if (updated) {
            this.currentConversation = updated;
            this.chatTitle.textContent = updated.title;
        }
    }

    /** Background refresh: fetch conversations and update list without disrupting selection. */
    async refreshConversationsInBackground() {
        if (document.hidden) return;
        try {
            const response = await fetch('/api/conversations');
            if (!response.ok) return;
            const list = await response.json();
            this.conversations = list;
            this.syncCurrentConversationFromList();
            this.renderConversationList();
        } catch (err) {
            console.error('Background conversation refresh failed:', err);
        }
    }

    startConversationPolling() {
        if (this.conversationPollInterval) clearInterval(this.conversationPollInterval);
        this.conversationPollInterval = setInterval(() => {
            this.refreshConversationsInBackground();
        }, this.conversationPollIntervalMs);
        if (this._conversationPollVisibilityBound) return;
        this._conversationPollVisibilityBound = () => {
            if (document.hidden) {
                if (this.conversationPollInterval) {
                    clearInterval(this.conversationPollInterval);
                    this.conversationPollInterval = null;
                }
            } else {
                if (!this.conversationPollInterval) {
                    this.conversationPollInterval = setInterval(() => {
                        this.refreshConversationsInBackground();
                    }, this.conversationPollIntervalMs);
                    this.refreshConversationsInBackground();
                }
            }
        };
        document.addEventListener('visibilitychange', this._conversationPollVisibilityBound);
    }

    stopConversationPolling() {
        if (this.conversationPollInterval) {
            clearInterval(this.conversationPollInterval);
            this.conversationPollInterval = null;
        }
    }

    renderConversationList() {
        this.conversationList.innerHTML = this.conversations.map(conv => `
            <div class="conversation-item ${this.currentConversation?.id === conv.id ? 'active' : ''}"
                 data-id="${conv.id}">
                <span class="title">${this.escapeHtml(conv.title)}</span>
            </div>
        `).join('');

        // Add click handlers
        this.conversationList.querySelectorAll('.conversation-item').forEach(el => {
            el.addEventListener('click', () => {
                this.selectConversation(parseInt(el.dataset.id));
            });
        });
    }

    async selectConversation(id) {
        // Find conversation
        const conv = this.conversations.find(c => c.id === id);
        if (!conv) return;

        // Disconnect old WebSocket FIRST to prevent race conditions
        // (old messages arriving while loading new conversation)
        if (window.chatHandler) {
            window.chatHandler.disconnect();
        }

        this.currentConversation = conv;
        this.chatTitle.textContent = conv.title;

        // Update UI
        this.renderConversationList();
        this.closeSidebar();

        // Load messages
        try {
            const response = await fetch(`/api/conversations/${id}/messages`);
            if (response.ok) {
                const messages = await response.json();
                if (window.chatHandler) {
                    window.chatHandler.loadMessages(messages);
                }
            }
        } catch (err) {
            console.error('Failed to load messages:', err);
        }

        // Connect WebSocket for new conversation
        if (window.chatHandler) {
            window.chatHandler.connect(id);
        }
    }

    async createConversation() {
        try {
            const response = await csrfFetch('/api/conversations', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ title: 'New Chat' })
            });

            if (response.ok) {
                const conv = await response.json();
                this.conversations.unshift(conv);

                // For new conversations, just connect without loading messages
                // (selectConversation would fetch empty messages anyway)
                this.currentConversation = conv;
                this.chatTitle.textContent = conv.title;
                this.renderConversationList();
                if (window.chatHandler) {
                    window.chatHandler.clear();  // Ensure clean slate
                    window.chatHandler.connect(conv.id);
                }
                
                // Return the conversation so callers can wait for it
                return conv;
            } else {
                throw new Error(`Failed to create conversation: ${response.status}`);
            }
        } catch (err) {
            console.error('Failed to create conversation:', err);
            throw err;  // Re-throw so callers can handle it
        }
    }

    async deleteCurrentConversation() {
        if (!this.currentConversation) return;
        if (!confirm('Delete this chat?')) return;

        try {
            const response = await csrfFetch(`/api/conversations/${this.currentConversation.id}`, {
                method: 'DELETE'
            });

            if (response.ok) {
                // Disconnect WebSocket
                if (window.chatHandler) {
                    window.chatHandler.disconnect();
                }

                // Remove from list
                this.conversations = this.conversations.filter(c => c.id !== this.currentConversation.id);
                this.currentConversation = null;

                // Clear chat
                if (window.chatHandler) {
                    window.chatHandler.clear();
                }
                this.chatTitle.textContent = 'Select a chat or start a new one';

                // Render list
                this.renderConversationList();

                // Select first if available
                if (this.conversations.length > 0) {
                    this.selectConversation(this.conversations[0].id);
                }
            }
        } catch (err) {
            console.error('Failed to delete conversation:', err);
        }
    }

    async deleteAllConversations() {
        if (!confirm('Delete ALL chats? This cannot be undone.')) return;

        try {
            const response = await csrfFetch('/api/conversations', {
                method: 'DELETE'
            });

            if (response.ok) {
                if (window.chatHandler) {
                    window.chatHandler.disconnect();
                }
                this.conversations = [];
                this.currentConversation = null;
                if (window.chatHandler) {
                    window.chatHandler.clear();
                }
                this.chatTitle.textContent = 'Select a chat or start a new one';
                this.renderConversationList();
            }
        } catch (err) {
            console.error('Failed to delete all conversations:', err);
        }
    }

    async logout() {
        try {
            await csrfFetch('/api/auth/logout', { method: 'POST' });
            window.location.href = '/login';
        } catch (err) {
            console.error('Logout failed:', err);
            window.location.href = '/login';
        }
    }

    toggleSidebar() {
        this.sidebar.classList.toggle('open');

        // Create overlay if not exists
        let overlay = document.querySelector('.sidebar-overlay');
        if (!overlay) {
            overlay = document.createElement('div');
            overlay.className = 'sidebar-overlay';
            document.body.appendChild(overlay);
        }
        overlay.classList.toggle('visible', this.sidebar.classList.contains('open'));
    }

    closeSidebar() {
        this.sidebar.classList.remove('open');
        const overlay = document.querySelector('.sidebar-overlay');
        if (overlay) overlay.classList.remove('visible');
    }

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    setMode(mode) {
        this.currentMode = mode;

        // Update button states
        document.querySelectorAll('.mode-btn').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.mode === mode);
        });

        // Update search dropdown button state
        const searchDropdownBtn = document.getElementById('searchDropdownBtn');
        if (searchDropdownBtn) {
            const isSearchMode = mode === 'search' || mode === 'images';
            searchDropdownBtn.classList.toggle('active', isSearchMode);
            if (mode === 'search') {
                searchDropdownBtn.textContent = 'Web ▾';
            } else if (mode === 'images') {
                searchDropdownBtn.textContent = 'Images ▾';
            } else {
                searchDropdownBtn.textContent = 'Search ▾';
            }
        }

        // Update the Generate dropdown button state/label (Image/Music/Video all live under it).
        const generateDropdownBtn = document.getElementById('generateDropdownBtn');
        if (generateDropdownBtn) {
            const genLabels = { 'geni': '🖼️ Image ▾', 'musicgeni': '🎵 Music ▾', 'videogeni': '🎬 Video ▾' };
            const isGenMode = mode in genLabels;
            generateDropdownBtn.classList.toggle('active', isGenMode);
            generateDropdownBtn.textContent = isGenMode ? genLabels[mode] : '✨ Generate ▾';
        }

        // Update placeholder
        const placeholders = {
            '': 'Type a message...',
            'search': 'Enter search query...',
            'images': 'Search for images...',
            'geni': 'Describe the image to generate...',
            'musicgeni': 'Describe the song to generate (add | lyrics, or "instrumental")...',
            'videogeni': 'Describe the video to generate...'
        };
        this.messageInput.placeholder = placeholders[mode] || 'Type a message...';
        this.messageInput.focus();
    }

    getMode() {
        return this.currentMode;
    }
}

// Initialize translate modal
function initTranslateModal() {
    const translateBtn = document.getElementById('translateBtn');
    const translateModal = document.getElementById('translateModal');
    const closeBtn = document.getElementById('closeTranslateModal');
    const fileInput = document.getElementById('translateFileInput');
    const fileNameEl = document.getElementById('translateFileName');
    const languageSelect = document.getElementById('targetLanguage');

    if (!translateBtn || !translateModal) {
        console.log('Translate elements not found');
        return;
    }

    translateBtn.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        translateModal.style.display = 'flex';
    });

    closeBtn.addEventListener('click', () => {
        translateModal.style.display = 'none';
        fileNameEl.textContent = '';
        fileInput.value = '';
    });

    translateModal.addEventListener('click', (e) => {
        if (e.target === translateModal) {
            translateModal.style.display = 'none';
            fileNameEl.textContent = '';
            fileInput.value = '';
        }
    });

    fileInput.addEventListener('change', (e) => {
        const file = e.target.files[0];
        if (!file) return;

        fileNameEl.textContent = `Processing: ${file.name}...`;
        let targetLang = languageSelect.value.trim();
        if (!targetLang) {
            targetLang = 'English';  // Default to English if empty
        }

        const reader = new FileReader();
        reader.onload = (event) => {
            const base64 = event.target.result.split(',')[1];
            const isImage = file.type.startsWith('image/');
            const isPDF = file.type === 'application/pdf';

            // Close modal
            translateModal.style.display = 'none';
            fileNameEl.textContent = '';
            fileInput.value = '';

            // Build translation request
            const payload = {
                type: 'message',
                content: `IMPORTANT: Translate the ENTIRE document below to ${targetLang}. You MUST translate every single word, sentence, and paragraph completely. Do not summarize, do not skip any sections, do not add commentary. Preserve all original formatting. Output ONLY the full translated text, nothing else.`
            };

            if (isImage) {
                payload.image_data = base64;
            } else if (isPDF) {
                payload.pdf_data = base64;
            } else {
                // Try as text
                const textReader = new FileReader();
                textReader.onload = (te) => {
                    payload.file_content = te.target.result;
                    sendTranslation(payload, file.name, targetLang);
                };
                textReader.readAsText(file);
                return;
            }

            sendTranslation(payload, file.name, targetLang);
        };
        reader.readAsDataURL(file);
    });

    function sendTranslation(payload, fileName, targetLang) {
        if (window.chatHandler) {
            window.chatHandler.addMessage('user', `Translate "${fileName}" to ${targetLang}`);
            window.chatHandler.showTypingIndicator();
            if (window.chatHandler.ws && window.chatHandler.ws.readyState === WebSocket.OPEN) {
                window.chatHandler.ws.send(JSON.stringify(payload));
            }
        }
    }
}

// Initialize news modal
function initNewsModal() {
    const newsBtn = document.getElementById('newsBtn') || document.getElementById('newsDropdownBtn');
    const newsModal = document.getElementById('newsModal');
    const closeBtn = document.getElementById('closeNewsModal');
    const sourcesContainer = document.querySelector('.news-sources');

    if (!newsBtn || !newsModal) {
        console.log('News modal elements not found');
        return;
    }

    // Load news sources from API and populate modal
    let allSources = [];

    async function loadNewsSources() {
        // Default sources as fallback
        const defaultSources = [
            { url: 'drudgereport.com', name: 'Drudge Report' },
            { url: 'npr.org/sections/news', name: 'NPR' },
            { url: 'nypost.com', name: 'NY Post' },
            { url: 'foxnews.com', name: 'Fox News' }
        ];

        try {
            const response = await fetch('/api/news/sources');
            if (response.ok) {
                const data = await response.json();
                allSources = data.sources || defaultSources;
            } else {
                console.warn('News sources API returned', response.status, '- using defaults');
                allSources = defaultSources;
            }
        } catch (err) {
            console.error('Failed to load news sources:', err);
            allSources = defaultSources;
        }

        // Render buttons
        sourcesContainer.innerHTML = '';

        // Add "All" button first
        if (allSources.length > 1) {
            const allBtn = document.createElement('button');
            allBtn.className = 'news-source-btn news-source-all';
            allBtn.textContent = 'All Sources';
            allBtn.addEventListener('click', () => {
                newsModal.style.display = 'none';
                sendAllNewsRequests();
            });
            sourcesContainer.appendChild(allBtn);
        }

        for (const source of allSources) {
            const btn = document.createElement('button');
            btn.className = 'news-source-btn';
            btn.dataset.url = source.url;
            btn.textContent = source.name;
            btn.addEventListener('click', async () => {
                newsModal.style.display = 'none';
                await sendNewsRequest(source.url, source.name);
            });
            sourcesContainer.appendChild(btn);
        }
    }

    async function sendAllNewsRequests() {
        // Process sources one at a time to avoid overloading
        for (const source of allSources) {
            await sendNewsRequest(source.url, source.name);
        }
    }

    function waitForConnection() {
        return new Promise(resolve => {
            let attempts = 0;
            const maxAttempts = 50;
            const checkConnection = () => {
                attempts++;
                if (window.chatHandler && window.chatHandler.ws &&
                    window.chatHandler.ws.readyState === WebSocket.OPEN) {
                    resolve(true);
                } else if (attempts >= maxAttempts) {
                    resolve(false);
                } else {
                    setTimeout(checkConnection, 100);
                }
            };
            checkConnection();
        });
    }

    newsBtn.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        // Close dropdown if in one
        const dropdown = newsBtn.closest('.quick-btn-dropdown');
        if (dropdown) dropdown.classList.remove('open');
        loadNewsSources();
        newsModal.style.display = 'flex';
    });

    closeBtn.addEventListener('click', () => {
        newsModal.style.display = 'none';
    });

    newsModal.addEventListener('click', (e) => {
        if (e.target === newsModal) {
            newsModal.style.display = 'none';
        }
    });

    async function sendNewsRequest(url, name) {
        // Use the /api/news/headlines endpoint with AI summarization
        if (!window.chatHandler) return;

        // Ensure we have a conversation
        if (!window.chatHandler.ws || window.chatHandler.ws.readyState !== WebSocket.OPEN) {
            await window.app.createConversation();
            await waitForConnection();
        }

        // Get current conversation ID
        const conversationId = window.app.currentConversation?.id;

        // Show loading indicator
        window.chatHandler.showTypingIndicator();

        try {
            // Include conversation_id to persist the message
            let apiUrl = `/api/news/headlines/${encodeURIComponent(url)}`;
            if (conversationId) {
                apiUrl += `?conversation_id=${conversationId}`;
            }

            const response = await fetch(apiUrl);
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }

            const data = await response.json();

            // Hide typing indicator and show results
            window.chatHandler.hideTypingIndicator();

            if (data.markdown) {
                window.chatHandler.addMessage('assistant', data.markdown);
            } else {
                window.chatHandler.addMessage('assistant', `**${name}:** Could not fetch headlines`);
            }
        } catch (err) {
            console.error('Failed to fetch news:', err);
            window.chatHandler.hideTypingIndicator();
            window.chatHandler.addMessage('assistant', `**${name}:** Failed to fetch headlines`);
        }
    }
}

// Initialize calendar modal
function initCalendarModal() {
    const calendarModal = document.getElementById('calendarModal');
    const closeBtn = document.getElementById('closeCalendarModal');
    const saveBtn = document.getElementById('saveEventBtn');
    const cancelBtn = document.getElementById('cancelEventBtn');

    if (!calendarModal) {
        console.log('Calendar modal not found');
        return;
    }

    // Close modal handlers
    closeBtn?.addEventListener('click', () => {
        calendarModal.style.display = 'none';
        clearCalendarForm();
    });

    cancelBtn?.addEventListener('click', () => {
        calendarModal.style.display = 'none';
        clearCalendarForm();
    });

    calendarModal.addEventListener('click', (e) => {
        if (e.target === calendarModal) {
            calendarModal.style.display = 'none';
            clearCalendarForm();
        }
    });

    // Save event handler
    saveBtn?.addEventListener('click', () => {
        const title = document.getElementById('eventTitle').value.trim();
        const date = document.getElementById('eventDate').value;
        let time = document.getElementById('eventTime').value;
        const endTime = document.getElementById('eventEndTime').value;
        const location = document.getElementById('eventLocation').value.trim();
        const description = document.getElementById('eventDescription').value.trim();
        const recurrence = document.getElementById('eventRecurrence').value.trim();
        const uid = document.getElementById('eventUid').value;

        console.log('Calendar save:', { title, date, time, endTime, location, uid });

        if (!title || !date) {
            alert('Please fill in required fields: Title and Date');
            return;
        }

        // Default time to 9:00 AM if not provided
        if (!time) {
            time = '09:00';
        }

        // Build the command
        let command;
        if (uid) {
            // Editing existing event - detect what changed
            // Store original values when opening modal for comparison
            const origTitle = calendarModal.dataset.origTitle || '';
            const origDate = calendarModal.dataset.origDate || '';
            const origTime = calendarModal.dataset.origTime || '';
            const origLocation = calendarModal.dataset.origLocation || '';
            const origDescription = calendarModal.dataset.origDescription || '';
            const origRecurrence = calendarModal.dataset.origRecurrence || '';

            // Collect ALL changes
            const commands = [];

            if (title !== origTitle) {
                commands.push(`cal edit ${uid} title ${title}`);
            }

            if (date !== origDate || time !== origTime) {
                // Time/date changed - use move command
                const dateObj = new Date(date + 'T' + time);
                const dateStr = dateObj.toLocaleDateString('en-US', { month: 'long', day: 'numeric', year: 'numeric' });
                const timeStr = dateObj.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
                let moveDesc = `${dateStr} at ${timeStr}`;
                if (endTime) {
                    const endTimeStr = new Date('1970-01-01T' + endTime).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
                    moveDesc += ` until ${endTimeStr}`;
                }
                commands.push(`cal edit ${uid} move to ${moveDesc}`);
            }

            if (location !== origLocation) {
                commands.push(`cal edit ${uid} location ${location || ''}`);
            }

            if (description !== origDescription) {
                commands.push(`cal edit ${uid} description ${description || ''}`);
            }

            if (recurrence !== origRecurrence) {
                // Recurrence changed
                if (recurrence) {
                    commands.push(`cal edit ${uid} repeat ${recurrence}`);
                } else {
                    commands.push(`cal edit ${uid} repeat none`);
                }
            }

            if (commands.length === 0) {
                // No changes detected
                calendarModal.style.display = 'none';
                clearCalendarForm();
                return;
            }

            // Send all commands with delay between them
            command = commands[0];
            // Send remaining commands with delays
            for (let i = 1; i < commands.length; i++) {
                ((cmd, delay) => {
                    setTimeout(() => {
                        if (window.sendMessage) {
                            window.sendMessage(cmd);
                        }
                    }, delay);
                })(commands[i], i * 1500);  // 1.5 second delay between commands
            }
        } else {
            // Adding new event - build natural language description
            let eventDesc = title;
            const dateObj = new Date(date + 'T' + time);
            const dateStr = dateObj.toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric' });
            const timeStr = dateObj.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
            eventDesc += ` on ${dateStr} at ${timeStr}`;
            if (endTime) {
                const endTimeStr = new Date('1970-01-01T' + endTime).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
                eventDesc += ` until ${endTimeStr}`;
            }
            if (location) {
                eventDesc += ` at ${location}`;
            }
            if (recurrence) {
                eventDesc += `, repeating ${recurrence}`;
            }
            command = `cal add ${eventDesc}`;
        }

        // Close modal
        calendarModal.style.display = 'none';
        clearCalendarForm();

        // Send command
        if (window.chatHandler && window.chatHandler.ws) {
            window.chatHandler.addMessage('user', command);
            window.chatHandler.ws.send(JSON.stringify({
                type: 'message',
                content: command
            }));
        }
    });

    function clearCalendarForm() {
        document.getElementById('eventTitle').value = '';
        document.getElementById('eventDate').value = '';
        document.getElementById('eventTime').value = '';
        document.getElementById('eventEndTime').value = '';
        document.getElementById('eventLocation').value = '';
        document.getElementById('eventDescription').value = '';
        document.getElementById('eventRecurrence').value = '';
        document.getElementById('eventUid').value = '';
        document.getElementById('calendarModalTitle').textContent = 'Add Event';
    }

    // Expose function to open modal for adding
    window.openCalendarModal = function(eventData = null) {
        const today = new Date().toISOString().split('T')[0];
        if (eventData && eventData.uid) {
            // Edit mode - set defaults for fields not provided
            document.getElementById('calendarModalTitle').textContent = 'Edit Event';
            document.getElementById('eventTitle').value = eventData.title || '';
            document.getElementById('eventDate').value = eventData.date || today;
            document.getElementById('eventTime').value = eventData.time || '';
            document.getElementById('eventEndTime').value = eventData.endTime || '';
            document.getElementById('eventLocation').value = eventData.location || '';
            document.getElementById('eventDescription').value = eventData.description || '';
            document.getElementById('eventRecurrence').value = eventData.recurrence || '';
            document.getElementById('eventUid').value = eventData.uid || '';
            // Store original values for change detection
            calendarModal.dataset.origTitle = eventData.title || '';
            calendarModal.dataset.origDate = eventData.date || today;
            calendarModal.dataset.origTime = eventData.time || '';
            calendarModal.dataset.origLocation = eventData.location || '';
            calendarModal.dataset.origDescription = eventData.description || '';
            calendarModal.dataset.origRecurrence = eventData.recurrence || '';
        } else {
            clearCalendarForm();
            // Set default date to today
            document.getElementById('eventDate').value = today;
            // Clear original values
            calendarModal.dataset.origTitle = '';
            calendarModal.dataset.origDate = '';
            calendarModal.dataset.origTime = '';
            calendarModal.dataset.origLocation = '';
            calendarModal.dataset.origDescription = '';
            calendarModal.dataset.origRecurrence = '';
        }
        calendarModal.style.display = 'flex';
        document.getElementById('eventTitle').focus();
    };
}

// Initialize app when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    window.app = new App();
    initTranslateModal();
    initNewsModal();
    initCalendarModal();
});
