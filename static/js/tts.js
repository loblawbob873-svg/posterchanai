// TTS Controller
class TTSController {
    constructor() {
        this.enabled = localStorage.getItem('ttsEnabled') !== 'false';
        this.currentAudio = null;
        this.toggle = document.getElementById('ttsToggle');

        this.init();
    }

    init() {
        this.updateToggleUI();

        if (this.toggle) {
            this.toggle.addEventListener('click', () => this.toggleTTS());
        }
    }

    toggleTTS() {
        this.enabled = !this.enabled;
        localStorage.setItem('ttsEnabled', this.enabled);
        this.updateToggleUI();

        if (!this.enabled && this.currentAudio) {
            this.stop();
        }
    }

    updateToggleUI() {
        if (this.toggle) {
            this.toggle.classList.toggle('active', this.enabled);
            this.toggle.textContent = this.enabled ? '🔊' : '🔇';
            this.toggle.title = this.enabled ? 'TTS On (click to disable)' : 'TTS Off (click to enable)';
        }
    }

    async speak(text) {
        if (!this.enabled || !text) return;

        // Skip very long text (likely document translations)
        if (text.length > 5000) {
            console.log('TTS: Skipping very long text');
            return;
        }

        // Stop any currently playing audio
        this.stop();

        try {
            const response = await fetch('/api/tts', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({ text })
            });

            if (!response.ok) {
                // Silently fail for TTS - not critical functionality
                return;
            }

            const data = await response.json();
            if (data.audio) {
                this.currentAudio = new Audio(`data:audio/mp3;base64,${data.audio}`);
                await this.currentAudio.play();
            }
        } catch (err) {
            // Silently fail - TTS is optional
        }
    }

    stop() {
        if (this.currentAudio) {
            this.currentAudio.pause();
            this.currentAudio.currentTime = 0;
            this.currentAudio = null;
        }
    }

    isEnabled() {
        return this.enabled;
    }
}

// Initialize TTS controller
window.ttsController = new TTSController();
