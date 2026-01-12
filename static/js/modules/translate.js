/**
 * Translate Modal - Handles document translation functionality
 */
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

// Export for module systems or make available globally
if (typeof module !== 'undefined' && module.exports) {
    module.exports = initTranslateModal;
} else {
    window.initTranslateModal = initTranslateModal;
}
