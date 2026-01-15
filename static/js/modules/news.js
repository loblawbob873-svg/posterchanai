/**
 * News Modal - Handles news headlines fetching and display
 */
function initNewsModal() {
    const newsBtn = document.getElementById('newsBtn');
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

// Export for module systems or make available globally
if (typeof module !== 'undefined' && module.exports) {
    module.exports = initNewsModal;
} else {
    window.initNewsModal = initNewsModal;
}
