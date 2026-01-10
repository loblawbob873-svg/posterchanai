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
        this.commands = ['help', 'search', 'images', 'geni', 'flood', 'budget', 'firewall'];
        this.pluginActions = []; // Will be populated with plugin action hints

        // Load plugins for autocomplete
        this.loadPluginsForAutocomplete();

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

                    console.log('[PASTE] Image pasted from clipboard');
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
                    await fetch('/api/auth/settings', {
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
                    const response = await fetch('/api/auth/test-custom-ai', {
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
                    const response = await fetch('/api/auth/test-custom-image?url=' + encodeURIComponent(customImageUrl.value), {
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

                try {
                    const response = await fetch('/api/auth/settings', {
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
                        const response = await fetch('/api/auth/avatar', {
                            method: 'POST',
                            body: formData
                        });

                        if (response.ok) {
                            const data = await response.json();
                            this.updateAvatarPreview(data.avatar);
                            avatarStatus.textContent = 'Avatar uploaded!';
                            avatarStatus.className = 'settings-status success';
                            setTimeout(() => { avatarStatus.textContent = ''; }, 2000);
                        } else {
                            const data = await response.json();
                            avatarStatus.textContent = data.detail || 'Upload failed';
                            avatarStatus.className = 'settings-status error';
                        }
                    } catch (e) {
                        avatarStatus.textContent = 'Upload failed';
                        avatarStatus.className = 'settings-status error';
                    }
                    avatarInput.value = '';
                });

                if (deleteAvatarBtn) {
                    deleteAvatarBtn.addEventListener('click', async () => {
                        avatarStatus.textContent = 'Removing...';
                        try {
                            const response = await fetch('/api/auth/avatar', { method: 'DELETE' });
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
        console.log('[DEBUG] sendMessage called');
        let content = this.messageInput.value.trim();
        console.log('[DEBUG] content:', content, 'ws:', !!this.ws, 'wsState:', this.ws?.readyState);

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
        console.log('[DEBUG] sendMessage - mode:', mode, 'content:', content, 'hasImage:', !!this.uploadedImage);

        // Need either content or a file upload
        if (!content && !this.uploadedFile && !this.uploadedImage) return;

        // Save to message history for up arrow recall
        this.saveToHistory(displayContent);

        if (mode) {
            content = `${mode} ${content}`;
            console.log('[DEBUG] After mode prepend:', content);
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

        console.log('[DEBUG] Sending payload:', { type: payload.type, content: payload.content, hasImageData: !!payload.image_data });
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
            if (window.ttsController && window.ttsController.isEnabled()) {
                window.ttsController.speak(content);
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

    handleCommandResponse(data) {
        this.hideTypingIndicator();
        this.resetSendButton();

        let html = this.formatMessage(data.content || '');

        // Handle different response types
        if (data.type === 'images' && data.images) {
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
        }

        this.addMessage('assistant', html, true);

        // Notify mascot
        if (window.mascotController) {
            window.mascotController.onResponse(true);
        }

        // Speak if TTS enabled
        if (window.ttsController && window.ttsController.isEnabled() && data.content) {
            window.ttsController.speak(data.content);
        }
    }

    handleError(message) {
        this.hideTypingIndicator();
        this.resetSendButton();

        // Show error as assistant message
        this.addMessage('assistant', `Error: ${message}`);

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
            const response = await fetch('/api/chat/email-response', {
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
        processed = processed.replace(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g, (match, text, url) => {
            const index = links.length;
            links.push({ text, url });
            return `\x00LINK${index}\x00`;
        });
        processed = processed.replace(/\[([^\]]+)\]\((www\.[^)]+)\)/g, (match, text, url) => {
            const index = links.length;
            links.push({ text, url: 'https://' + url });
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
            return `<a href="${link.url}" target="_blank">${link.text}</a>`;
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
                console.log('[Autocomplete] Loaded plugins:', this.commands);
            }
        } catch (e) {
            console.error('Failed to load plugins for autocomplete:', e);
        }
    }

    // Tab autocomplete for commands
    autocompleteCommand() {
        const input = this.messageInput.value;
        const cursorPos = this.messageInput.selectionStart;

        // Only autocomplete at the start of input
        if (cursorPos > input.length) return;

        const textBeforeCursor = input.substring(0, cursorPos).toLowerCase();

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
