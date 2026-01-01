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

        // Enter to send (Shift+Enter for new line)
        this.messageInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                this.sendMessage();
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

        // User settings modal
        this.initUserSettings();
    }

    initUserSettings() {
        const settingsBtn = document.getElementById('userSettingsBtn');
        const settingsModal = document.getElementById('userSettingsModal');
        const closeBtn = document.getElementById('closeUserSettingsModal');
        const saveBtn = document.getElementById('saveUserSettings');
        const emailInput = document.getElementById('notificationEmail');
        const statusEl = document.getElementById('settingsStatus');

        if (settingsBtn && settingsModal) {
            settingsBtn.addEventListener('click', async () => {
                // Load current settings
                try {
                    const response = await fetch('/api/auth/settings');
                    if (response.ok) {
                        const data = await response.json();
                        emailInput.value = data.notification_email || '';
                        this.notificationEmail = data.notification_email;
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

                try {
                    const response = await fetch('/api/auth/settings', {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ notification_email: email })
                    });

                    if (response.ok) {
                        this.notificationEmail = email;
                        statusEl.textContent = 'Settings saved!';
                        statusEl.className = 'settings-status success';
                        setTimeout(() => { statusEl.textContent = ''; }, 2000);
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

        // Check if we have an upload without text for img2img mode
        if (mode === 'img2img' && this.uploadedImage && !content) {
            this.addMessage('user', '[Uploaded image - please add a prompt]');
            return;
        }

        // Need either content or a file upload
        if (!content && !this.uploadedFile && !this.uploadedImage) return;

        if (mode) {
            content = `${mode} ${content}`;
        }

        // Build display message
        let displayMsg = displayContent;
        if (this.uploadedImage) {
            displayMsg = displayContent + ' [with image]';
        } else if (this.uploadedFile) {
            displayMsg = displayContent + ' [with file]';
        }

        // Add user message to UI (show what user typed, not the command)
        this.lastUserMessage = this.addMessage('user', displayMsg || '[File uploaded]');

        // Add action buttons to user message
        const userContentEl = this.lastUserMessage.querySelector('.message-content');

        // Remove action buttons from previous user messages
        const prevUserActionBtns = this.messagesContainer.querySelectorAll('.message.user .btn-regenerate, .message.user .btn-edit');
        prevUserActionBtns.forEach(btn => btn.remove());

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
            if (mode === 'geni' || mode === 'img2img') {
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

    handleMessage(data) {
        switch (data.type) {
            case 'stream':
                this.handleStreamChunk(data.content);
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
            // Show first chunk immediately for instant feedback
            const contentEl = this.streamingMessage.querySelector('.message-content');
            this.fullStreamContent = content;
            contentEl.innerHTML = this.formatMessage(content);
            this.scrollToBottom();
            return;
        }

        // Buffer subsequent chunks and batch DOM updates with requestAnimationFrame
        this.streamBuffer += content;
        this.fullStreamContent += content;

        if (!this.streamRafPending) {
            this.streamRafPending = true;
            requestAnimationFrame(() => {
                if (this.streamingMessage && this.streamBuffer) {
                    const contentEl = this.streamingMessage.querySelector('.message-content');
                    contentEl.innerHTML = this.formatMessage(this.fullStreamContent);
                    this.scrollToBottom();
                }
                this.streamBuffer = '';
                this.streamRafPending = false;
            });
        }
    }

    handleStreamEnd() {
        if (this.streamingMessage) {
            const contentEl = this.streamingMessage.querySelector('.message-content');
            // Use buffered content instead of reading from DOM
            const content = this.fullStreamContent;

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
            if (mode === 'geni' || mode === 'img2img') {
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
            if (mode === 'geni' || mode === 'img2img') {
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

        // Store payload for potential retry
        this.lastPayload = payload;

        // Show typing indicator
        this.showTypingIndicator();

        // Notify mascot
        if (window.mascotController) {
            if (mode === 'geni' || mode === 'img2img') {
                window.mascotController.onGeneratingImage();
            } else {
                window.mascotController.onUserMessage();
            }
        }

        // Send to server
        this.ws.send(JSON.stringify(payload));
    }

    addMessage(role, content, isHtml = false) {
        const messageEl = document.createElement('div');
        messageEl.className = `message ${role}`;

        const contentEl = document.createElement('div');
        contentEl.className = 'message-content';

        if (isHtml) {
            contentEl.innerHTML = content;
        } else {
            contentEl.innerHTML = this.formatMessage(content);
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

            // Add edit button for user messages (for editing and resubmitting)
            if (role === 'user') {
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
                    // Remove regenerate button from previous assistant messages
                    const prevRegenBtns = this.messagesContainer.querySelectorAll('.btn-regenerate');
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

        return messageEl;
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

        // Escape HTML
        let html = text
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');

        // Bold **text**
        html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');

        // Italic *text*
        html = html.replace(/\*([^*]+)\*/g, '<em>$1</em>');

        // Code `text`
        html = html.replace(/`([^`]+)`/g, '<code>$1</code>');

        // Links
        html = html.replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank">$1</a>');

        // Newlines
        html = html.replace(/\n/g, '<br>');

        return html;
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
            this.addMessage(msg.role, msg.content);
        }
    }

    clear() {
        this.messagesContainer.innerHTML = '';
        this.streamingMessage = null;
        this.lastPayload = null;
        this.lastUserMessage = null;
    }
}

// Initialize chat handler
window.chatHandler = new ChatHandler();
