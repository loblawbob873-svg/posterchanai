/**
 * MessageFormatter - Handles markdown/code formatting for chat messages
 * Can be used standalone or integrated into ChatHandler
 */
class MessageFormatter {
    /**
     * Strip LLM thinking tags from text
     */
    static stripThinkingTags(text) {
        if (!text) return '';
        // Remove complete think blocks
        text = text.replace(/<think>[\s\S]*?<\/think>/gi, '');
        // Remove unclosed think tags (during streaming)
        text = text.replace(/<think>[\s\S]*$/gi, '');
        return text.trim();
    }

    /**
     * Escape HTML special characters
     */
    static escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    /**
     * Escape URL for safe use in href attributes
     */
    static escapeUrl(url) {
        // Don't encode mailto: or tel: URLs
        if (url.startsWith('mailto:') || url.startsWith('tel:')) {
            return url;
        }
        try {
            return encodeURI(url);
        } catch {
            return url;
        }
    }

    /**
     * Format a message with markdown support
     * @param {string} text - Raw message text
     * @param {string} codeBlockIdPrefix - Prefix for code block IDs (default: code)
     * @returns {string} - HTML formatted message
     */
    static format(text, codeBlockIdPrefix = 'code') {
        if (!text) return '';

        // Strip thinking tags first
        text = this.stripThinkingTags(text);

        // Treat ```markdown blocks as regular text (just strip the fence)
        text = text.replace(/```markdown\n?([\s\S]*?)```/gi, '$1');

        // Extract and preserve fenced code blocks before escaping
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
        // Match relative URL links (starting with /)
        processed = processed.replace(/\[([^\]]+)\]\(\s*(\/[^)\s]+)\s*\)/g, (match, text, url) => {
            const index = links.length;
            links.push({ text: text.trim(), url: url.trim(), external: false, download: true });
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
            const target = link.external ? ' target="_blank"' : '';
            const download = link.download ? ' download' : '';
            return `<a href="${this.escapeUrl(link.url)}"${target}${download}>${this.escapeHtml(link.text)}</a>`;
        });

        // Bold **text**
        html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');

        // Italic *text*
        html = html.replace(/\*([^*]+)\*/g, '<em>$1</em>');

        // Inline code `text` (but not inside code blocks)
        html = html.replace(/`([^`]+)`/g, '<code>$1</code>');

        // Plain URLs (not already in a link) - exclude trailing emojis, emoji variation selector, and common punctuation
        // Emoji regex pattern: excludes emoji characters and variation selectors
        html = html.replace(/(https?:\/\/[^\s<]+?)([\u0000-\u1FFFF]*?)(?![^<]*<\/a>)/g, (match, url, trailing) => {
            // Only keep trailing if it's not emoji-related (whitespace, letters, numbers, basic punctuation)
            if (/^[\s\w.,;:!?()[\]{}'"\-]*$/.test(trailing)) {
                return '<a href="' + url + '" target="_blank">' + url + trailing + '</a>';
            }
            return '<a href="' + url + '" target="_blank">' + url + '</a>';
        });

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
            const blockId = `${codeBlockIdPrefix}-${Date.now()}-${index}`;
            return `<div class="code-block-wrapper">
                ${langLabel}
                <button class="code-copy-btn" onclick="window.chatHandler.copyCodeBlock('${blockId}')" title="Copy code">Copy</button>
                <pre${langClass}><code id="${blockId}">${escapedCode}</code></pre>
            </div>`;
        });

        return html;
    }
}

// Export for module systems or make available globally
if (typeof module !== 'undefined' && module.exports) {
    module.exports = MessageFormatter;
} else {
    window.MessageFormatter = MessageFormatter;
}
