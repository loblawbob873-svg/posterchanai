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

        this.init();
    }

    async init() {
        // Load conversations
        await this.loadConversations();

        // Set up event listeners
        this.setupEventListeners();

        // Select first conversation or show welcome
        if (this.conversations.length > 0) {
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
                if (window.chatHandler && window.chatHandler.ws) {
                    window.chatHandler.sendMessageDirect(cmd);
                }
            });
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
                this.renderConversationList();
            }
        } catch (err) {
            console.error('Failed to load conversations:', err);
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
                window.chatHandler.loadMessages(messages);
            }
        } catch (err) {
            console.error('Failed to load messages:', err);
        }

        // Connect WebSocket
        window.chatHandler.connect(id);
    }

    async createConversation() {
        try {
            const response = await fetch('/api/conversations', {
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
                window.chatHandler.clear();  // Ensure clean slate
                window.chatHandler.connect(conv.id);
            }
        } catch (err) {
            console.error('Failed to create conversation:', err);
        }
    }

    async deleteCurrentConversation() {
        if (!this.currentConversation) return;
        if (!confirm('Delete this chat?')) return;

        try {
            const response = await fetch(`/api/conversations/${this.currentConversation.id}`, {
                method: 'DELETE'
            });

            if (response.ok) {
                // Disconnect WebSocket
                window.chatHandler.disconnect();

                // Remove from list
                this.conversations = this.conversations.filter(c => c.id !== this.currentConversation.id);
                this.currentConversation = null;

                // Clear chat
                window.chatHandler.clear();
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
            const response = await fetch('/api/conversations', {
                method: 'DELETE'
            });

            if (response.ok) {
                window.chatHandler.disconnect();
                this.conversations = [];
                this.currentConversation = null;
                window.chatHandler.clear();
                this.chatTitle.textContent = 'Select a chat or start a new one';
                this.renderConversationList();
            }
        } catch (err) {
            console.error('Failed to delete all conversations:', err);
        }
    }

    async logout() {
        try {
            await fetch('/api/auth/logout', { method: 'POST' });
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

        // Update placeholder
        const placeholders = {
            '': 'Type a message...',
            'search': 'Enter search query...',
            'images': 'Search for images...',
            'geni': 'Describe the image to generate...',
            'img2img': 'Describe what to create (upload an image first)...'
        };
        this.messageInput.placeholder = placeholders[mode] || 'Type a message...';
        this.messageInput.focus();
    }

    getMode() {
        return this.currentMode;
    }
}

// Initialize app when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    window.app = new App();
});
