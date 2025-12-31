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
    }

    connect(conversationId) {
        if (this.ws) {
            this.ws.close();
        }

        this.currentConversationId = conversationId;
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws/chat/${conversationId}`;

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

    sendMessage() {
        const content = this.messageInput.value.trim();
        if (!content || !this.ws || this.ws.readyState !== WebSocket.OPEN) return;

        // Add user message to UI
        this.addMessage('user', content);

        // Clear input
        this.messageInput.value = '';
        this.messageInput.style.height = 'auto';

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

    handleCommandResponse(data) {
        this.hideTypingIndicator();

        let html = this.formatMessage(data.content || '');

        // Handle different response types
        if (data.type === 'images' && data.images) {
            html += '<div class="image-grid">';
            for (const img of data.images) {
                html += `<img src="${img.img_src}" alt="${img.title || ''}" onclick="window.open('${img.url}', '_blank')">`;
            }
            html += '</div>';
        } else if (data.type === 'generated_image' && data.image) {
            html += `<img src="data:image/png;base64,${data.image}" alt="Generated image" class="generated-image">`;

            // Notify mascot for image generation
            if (window.mascotController) {
                window.mascotController.onResponse(true);
            }
        } else if (data.type === 'search' && data.results) {
            html += '<div class="search-results">';
            for (const r of data.results) {
                html += `<div class="search-result">
                    <a href="${r.url}" target="_blank">${r.title}</a>
                    <p>${r.content}</p>
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
        this.messagesContainer.appendChild(messageEl);
        this.scrollToBottom();

        return messageEl;
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
