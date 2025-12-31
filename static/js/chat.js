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

        // File upload elements
        this.fileInput = document.getElementById('fileInput');
        this.uploadPreview = document.getElementById('uploadPreview');
        this.imagePreview = document.getElementById('imagePreview');
        this.filePreview = document.getElementById('filePreview');
        this.removeUpload = document.getElementById('removeUpload');

        // Stored upload data
        this.uploadedImage = null;  // base64 image data
        this.uploadedFile = null;   // text file content
        this.uploadedPDF = null;    // base64 PDF data
        this.uploadedDocument = null; // base64 office document data

        this.init();
    }

    init() {
        // Send button click
        this.sendBtn.addEventListener('click', () => this.sendMessage());

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

        // Remove upload button
        if (this.removeUpload) {
            this.removeUpload.addEventListener('click', () => this.clearUpload());
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
            this.ws.close();
            this.ws = null;
        }
        this.currentConversationId = null;
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
        this.addMessage('user', displayMsg || '[File uploaded]');

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
        }

        const contentEl = this.streamingMessage.querySelector('.message-content');
        contentEl.innerHTML = this.formatMessage(contentEl.textContent + content);
        this.scrollToBottom();
    }

    handleStreamEnd() {
        if (this.streamingMessage) {
            const content = this.streamingMessage.querySelector('.message-content').textContent;

            // Notify mascot
            if (window.mascotController) {
                window.mascotController.onResponse(true);
            }

            // Speak if TTS enabled
            if (window.ttsController && window.ttsController.isEnabled()) {
                window.ttsController.speak(content);
            }

            this.streamingMessage = null;
        }
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
                html += `<img src="${safeSrc}" alt="${safeTitle}" onclick="window.open('${safeUrl}', '_blank')">`;
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
        this.addMessage('assistant', `Error: ${message}`);

        if (window.mascotController) {
            window.mascotController.onError();
        }
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

        messageEl.appendChild(contentEl);

        // Add copy button for assistant messages
        if (role === 'assistant') {
            const copyBtn = document.createElement('button');
            copyBtn.className = 'btn-copy';
            copyBtn.innerHTML = '📋';
            copyBtn.title = 'Copy to clipboard';
            copyBtn.onclick = () => this.copyText(contentEl.textContent);
            messageEl.appendChild(copyBtn);
        }

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

    async copyText(text) {
        try {
            await navigator.clipboard.writeText(text);
            this.showToast('Copied to clipboard!');
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
    }
}

// Initialize chat handler
window.chatHandler = new ChatHandler();
