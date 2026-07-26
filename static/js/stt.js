// Voice command mappings - natural phrases to commands
// Patterns are checked in order - first match wins
const VOICE_COMMANDS = [
    // ==================== EMAIL ====================
    // Check inbox - including common mishearings
    { patterns: [/^(check|show|get|open|read)?\s*(my\s+)?(e-?)?mail\.?$/i], command: 'mail' },
    { patterns: [/^(check|show|get|open)?\s*(my\s+)?(inbox|messages?)\.?$/i], command: 'mail' },
    { patterns: [/^(any\s+)?(new|unread)\s*(mail|messages?)?\.?$/i], command: 'mail unread' },
    // "read email" without number = show inbox
    { patterns: [/^read\s+(e-?mail|e-?moo|emoo?|imoo?|message)s?\.?$/i], command: 'mail' },
    // Mishearings: "check my mail" -> "check new", "check male", "check mail"
    { patterns: [/^check\.?\s*new\.?$/i], command: 'mail' },
    { patterns: [/^check\s+(male|nail|mell|mel)\.?$/i], command: 'mail' },

    // Read/delete/archive by number - SIMPLE: "read 2", "delete 3", "archive 1"
    // Note: normalizeNumbers() converts word numbers (four, forward, etc) to digits before matching
    { patterns: [/^read\s+(\d+)$/i], command: 'mail read $1' },
    { patterns: [/^(delete|remove|trash)\s+(\d+)$/i], command: 'mail delete $2' },
    { patterns: [/^archive\s+(\d+)$/i], command: 'mail archive $1' },
    // With "email/mail/message": "read email 2", "delete message 3", "delete mail 5"
    // Mishearings: "email" -> "e-moo", "emoo", "emo", "imoo"
    // Note: normalizeNumbers() converts word numbers to digits before matching
    { patterns: [/^read\s+(e-?mail|mail|e-?moo|emoo?|imoo?|message)\s+(\d+)$/i], command: 'mail read $2' },
    { patterns: [/^(open|show)\s+(e-?mail|mail|e-?moo|emoo?|imoo?|message)\s+(\d+)$/i], command: 'mail read $3' },
    { patterns: [/^(delete|remove|trash)\s+(e-?mail|mail|e-?moo|emoo?|imoo?|message)\s+(\d+)$/i], command: 'mail delete $3' },
    { patterns: [/^archive\s+(e-?mail|mail|e-?moo|emoo?|imoo?|message)\s+(\d+)$/i], command: 'mail archive $2' },

    // "This email" commands - after reading an email
    { patterns: [/^reply(\s+to)?(\s+this)?$/i], command: 'mail reply THIS_EMAIL ' },
    { patterns: [/^reply(\s+to\s+this)?\s+(.+)$/i], command: 'mail reply THIS_EMAIL $2' },
    { patterns: [/^delete\s+this$/i], command: 'mail delete THIS_EMAIL' },
    { patterns: [/^archive\s+this$/i], command: 'mail archive THIS_EMAIL' },
    { patterns: [/^summarize(\s+this)?$/i], command: 'mail summary THIS_EMAIL' },
    { patterns: [/^translate(\s+this)?\s+(to\s+)?(\w+)$/i], command: 'mail translate THIS_EMAIL $3' },

    // Forward email - "forward this to john" or "forward to john@example.com"
    { patterns: [/^forward(\s+this)?(\s+to)?\s+(\S+)$/i], command: 'mail forward THIS_EMAIL $3' },
    { patterns: [/^forward(\s+this)?(\s+to)?\s+(\S+)\s+(.+)$/i], command: 'mail forward THIS_EMAIL $3 $4' },

    // Other email actions
    { patterns: [/^(send|write|compose)\s*(e-?mail|message)?$/i], command: 'mail send' },
    // Send email to contact - "email john hello" or "send john a message saying hello"
    { patterns: [/^(e-?mail|message)\s+(\S+)\s+(.+)$/i], command: 'mail send $2 $3' },
    { patterns: [/^(send|write)\s+(e-?mail|message)?\s*to\s+(\S+)\s+(saying\s+)?(.+)$/i], command: 'mail send $3 $5' },
    { patterns: [/^(search|find)\s+(e-?)?mail\s+(for\s+)?(.+)$/i], command: 'mail search $4' },
    { patterns: [/^folders?$/i], command: 'mail folders' },

    // ==================== TODO ====================
    // "todo", "my todos", "show my to do list", "to do" (two words)
    { patterns: [/^(show\s+)?(my\s+)?to-?\s?do('?s)?(\s+list)?\.?$/i], command: 'todo' },
    { patterns: [/^(add|new)\s+to-?\s?do\s+(.+)$/i], command: 'todo add $2' },
    { patterns: [/^add\s+(.+)\s+to\s+(my\s+)?to-?\s?do(\s+list)?$/i], command: 'todo add $1' },
    { patterns: [/^remind\s+me\s+to\s+(.+)$/i], command: 'todo add $1' },
    { patterns: [/^(delete|remove|done)\s+to-?\s?do\s+(\d+)$/i], command: 'todo rm $2' },
    { patterns: [/^(complete|finish)\s+task\s+(\d+)$/i], command: 'todo rm $2' },
    // ==================== MUSIC ====================
    // Music commands removed
    // ==================== NEWS ====================
    { patterns: [/^(the\s+)?news$/i], command: 'news' },
    { patterns: [/^(refresh|update)\s+news$/i], command: 'news refresh' },
    { patterns: [/^daily\s*news$/i], command: 'dailynews' },

    // ==================== SEARCH & IMAGES ====================
    // Note: "search anime X" goes to nyaa, not web search (handled below)
    { patterns: [/^search\s+anime\s+(.+)$/i], command: 'nyaa $1' },
    { patterns: [/^search\s+(for\s+)?(.+)$/i], command: 'search $2' },
    { patterns: [/^google\s+(.+)$/i], command: 'search $1' },
    { patterns: [/^(show\s+)?(images?|pictures?)\s+(of\s+)?(.+)$/i], command: 'images $4' },
    { patterns: [/^(generate|create|draw)\s+(image|picture)?\s*(of\s+)?(.+)$/i], command: 'geni $4' },

    // ==================== TORRENTS ====================
    // Common mishearings: "torrents" -> "torrance", "toronto", "terrance", "torrent"
    // Catch-all: any phrase containing these words
    { patterns: [/\b(torrance|toronto|terrance)\b/i], command: 'torrents' },
    { patterns: [/^(show\s+me\s+)?(the\s+)?(my\s+)?(torrents?|downloads?)$/i], command: 'torrents' },
    { patterns: [/^downloads?$/i], command: 'torrents list' },
    { patterns: [/^(show\s+(me\s+)?)?(the\s+)?movies?$/i], command: 'torrents movies' },
    { patterns: [/^(show\s+(me\s+)?)?(the\s+)?tv(\s+shows?)?$/i], command: 'torrents tv' },
    { patterns: [/^(show\s+(me\s+)?)?(the\s+)?anime$/i], command: 'torrents anime' },
    // Download: "download movie 3", "download tv 5", "download anime 2"
    { patterns: [/^download\s+(movie|film)\s+(\d+)$/i], command: 'torrents download movies $2' },
    { patterns: [/^download\s+(tv|show)\s+(\d+)$/i], command: 'torrents download tv $2' },
    { patterns: [/^download\s+anime\s+(\d+)$/i], command: 'torrents download anime $1' },
    // Torrent controls
    { patterns: [/^pause\s+(\d+)$/i], command: 'torrents pause $1' },
    { patterns: [/^resume\s+(\d+)$/i], command: 'torrents resume $1' },
    { patterns: [/^(delete|remove)\s+torrent\s+(\d+)$/i], command: 'torrents rm $2' },

    // Nyaa anime search
    { patterns: [/^(search\s+)?anime\s+(.+)$/i], command: 'nyaa $2' },
    { patterns: [/^nyaa\s+(.+)$/i], command: 'nyaa $1' },
    

    // ==================== YOUTUBE ====================
    { patterns: [/^summarize\s+(video\s+)?(.+)$/i], command: 'yt $2' },
    { patterns: [/^download\s+video\s+(.+)$/i], command: 'ytdl video $1' },
    { patterns: [/^download\s+(song|music|audio)\s+(.+)$/i], command: 'ytdl $2' },
    { patterns: [/^download\s+(youtube|youtube\s+video)\s+(.+)$/i], command: 'ytdl video $2' },
    { patterns: [/^download\s+(.+)$/i], command: 'ytdl $1' }, // Default to audio

    // ==================== TRANSLATE ====================
    { patterns: [/^translate\s+to\s+(\w+)$/i], command: 'translate $1' },
    { patterns: [/^(say\s+)?that\s+in\s+(\w+)$/i], command: 'translate $2' },

    // ==================== SYSTEM ====================
    { patterns: [/^firewall$/i], command: 'firewall' },
    { patterns: [/^(check|lookup)\s+(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})$/i], command: 'firewall search $2' },
    { patterns: [/^logs?$/i], command: 'logs' },
    { patterns: [/^help$/i], command: 'help' },
];

/**
 * Convert number words to digits
 */
function normalizeNumbers(text) {
    const wordToNum = {
        'one': '1', 'won': '1',
        'two': '2', 'to': '2', 'too': '2',
        'three': '3', 'tree': '3', 'free': '3',
        'four': '4', 'for': '4', 'forward': '4', 'fore': '4',
        'five': '5', 'fife': '5',
        'six': '6', 'sicks': '6',
        'seven': '7',
        'eight': '8', 'ate': '8',
        'nine': '9', 'nein': '9',
        'ten': '10'
    };
    // Only convert at end of string (where numbers typically appear in commands)
    return text.replace(/\b(one|won|two|to|too|three|tree|free|four|for|forward|fore|five|fife|six|sicks|seven|eight|ate|nine|nein|ten)\s*$/i,
        (match) => wordToNum[match.toLowerCase().trim()] || match);
}

/**
 * Convert natural language voice input to a command
 * @param {string} text - The transcribed voice input
 * @returns {string} - The command or original text if no match
 */
function parseVoiceCommand(text) {
    // Strip emojis, special characters, and extra whitespace
    let cleaned = text
        .replace(/[\u{1F300}-\u{1F9FF}]/gu, '')  // Emojis
        .replace(/[\u{2600}-\u{26FF}]/gu, '')    // Misc symbols
        .replace(/[\u{2700}-\u{27BF}]/gu, '')    // Dingbats
        .replace(/[^\w\s\-\.\$\#\@]/g, '')       // Keep only word chars, spaces, common punctuation
        .replace(/\s+/g, ' ')
        .replace(/[.,!?;:]+$/g, '')              // Strip trailing punctuation (Whisper often adds these)
        .trim();

    // Convert number words to digits
    cleaned = normalizeNumbers(cleaned);

    // Filter out common Whisper hallucinations (output when audio unclear)
    const hallucinations = [
        /^thank(s|\s+you)?\.?$/i,
        /^thanks\s+for\s+(watching|listening)\.?$/i,
        /^please\s+subscribe\.?$/i,
        /^(bye|goodbye)\.?$/i,
        /^you\.?$/i,
        /^\.+$/,
        /^$/
    ];
    for (const pattern of hallucinations) {
        if (pattern.test(cleaned)) {
            console.warn('Filtered Whisper hallucination:', text);
            return null;  // Signal to ignore this transcription
        }
    }

    for (const { patterns, command } of VOICE_COMMANDS) {
        for (const pattern of patterns) {
            const match = cleaned.match(pattern);
            if (match) {
                // Replace $1, $2, etc. with captured groups
                let result = command;
                for (let i = 1; i < match.length; i++) {
                    if (match[i]) {
                        result = result.replace(`$${i}`, match[i].trim());
                    }
                }
                // Clean up any unreplaced placeholders and extra spaces
                result = result.replace(/\$\d+/g, '').replace(/\s+/g, ' ').trim();

                // Handle THIS_EMAIL placeholder - substitute with last read email
                if (result.includes('THIS_EMAIL')) {
                    const lastEmail = window.chatHandler?.lastReadEmail;
                    if (lastEmail && lastEmail.id) {
                        if (lastEmail.account && lastEmail.account !== 'default') {
                            result = result.replace('THIS_EMAIL', `${lastEmail.account} ${lastEmail.id}`);
                        } else {
                            result = result.replace('THIS_EMAIL', lastEmail.id);
                        }
                    } else {
                        console.warn('No email tracked for "this" command');
                        return 'Please read an email first, then say "reply" or "delete this"';
                    }
                }

                return result;
            }
        }
    }

    // No match - return cleaned text for LLM
    return cleaned;
}

// Speech-to-Text Controller with Whisper fallback for Brave/Firefox
class STTController {
    constructor() {
        this.recognition = null;
        this.isListening = false;
        this.voiceBtn = null;
        this.messageInput = null;
        this.interimTranscript = '';
        this.finalTranscript = '';
        this.autoSendTimeout = null;
        this.autoSendDelay = 1500;
        this.userStopped = false;

        // Whisper fallback
        this.useWhisper = false;
        this.whisperAvailable = false;
        this.mediaRecorder = null;
        this.audioChunks = [];
        this.recordingMimeType = 'audio/webm';

        // Check browser support
        this.SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
        this.webSpeechSupported = !!this.SpeechRecognition;

        this.init();
    }

    async init() {
        this.voiceBtn = document.getElementById('voiceInputBtn');
        this.messageInput = document.getElementById('messageInput');

        // Check if Whisper backend is available
        await this.checkWhisperAvailable();

        if (!this.webSpeechSupported && !this.whisperAvailable) {
            console.warn('No speech recognition available');
            if (this.voiceBtn) {
                this.voiceBtn.title = 'Voice not available';
                this.voiceBtn.style.opacity = '0.5';
                this.voiceBtn.addEventListener('click', () => {
                    this.showNotification('Voice requires Chrome, or install faster-whisper on server', 'error');
                });
            }
            return;
        }

        // Setup Web Speech API if available
        if (this.webSpeechSupported) {
            this.setupWebSpeech();
        }

        if (this.voiceBtn) {
            this.voiceBtn.addEventListener('click', (e) => {
                e.preventDefault();
                this.toggle();
            });
        }
    }

    async checkWhisperAvailable() {
        try {
            const resp = await fetch('/api/stt/status');
            if (resp.ok) {
                const data = await resp.json();
                this.whisperAvailable = data.available;
                console.log('Whisper STT available:', this.whisperAvailable);
            }
        } catch (e) {
            console.log('Whisper STT not available');
            this.whisperAvailable = false;
        }
    }

    setupWebSpeech() {
        this.recognition = new this.SpeechRecognition();
        this.recognition.continuous = true;
        this.recognition.interimResults = true;
        this.recognition.lang = 'en-US';

        this.recognition.onstart = () => {
            this.isListening = true;
            this.userStopped = false;
            this.updateUI();
        };

        this.recognition.onend = () => {
            this.isListening = false;
            this.updateUI();

            if (this.finalTranscript.trim() && this.userStopped) {
                this.autoSend();
            }
        };

        this.recognition.onresult = (event) => {
            this.interimTranscript = '';
            let hasNewFinal = false;

            for (let i = event.resultIndex; i < event.results.length; i++) {
                const transcript = event.results[i][0].transcript;
                if (event.results[i].isFinal) {
                    this.finalTranscript += transcript;
                    hasNewFinal = true;
                } else {
                    this.interimTranscript += transcript;
                }
            }

            if (this.messageInput) {
                const currentText = this.finalTranscript + this.interimTranscript;
                this.messageInput.value = currentText;
                this.messageInput.dispatchEvent(new Event('input'));
            }

            this.clearAutoSendTimer();
            if (hasNewFinal && this.finalTranscript.trim()) {
                this.startAutoSendTimer();
            }
        };

        this.recognition.onerror = (event) => {
            console.error('Web Speech error:', event.error);

            // Try Whisper fallback for any error that blocks Web Speech
            const fallbackErrors = ['service-not-allowed', 'not-allowed', 'network'];
            if (fallbackErrors.includes(event.error) && this.whisperAvailable) {
                console.log('Switching to Whisper fallback due to:', event.error);
                this.useWhisper = true;
                this.showNotification('Using local voice recognition', 'info');
                this.startWhisper();
                return;
            }

            // Show appropriate error message
            if (event.error === 'not-allowed') {
                this.showError('Microphone access denied. Check browser permissions.');
            } else if (event.error === 'service-not-allowed' || event.error === 'network') {
                this.showError('Speech service unavailable. Install faster-whisper on server.');
            } else if (event.error === 'no-speech') {
                if (this.finalTranscript.trim()) {
                    this.autoSend();
                }
            } else if (event.error === 'audio-capture') {
                this.showError('No microphone found.');
            } else if (event.error && event.error !== 'aborted') {
                this.showError(`Voice error: ${event.error}`);
            }

            this.isListening = false;
            this.updateUI();
        };
    }

    toggle() {
        if (this.isListening) {
            this.stop();
        } else {
            this.start();
        }
    }

    start() {
        this.finalTranscript = this.messageInput?.value || '';
        this.interimTranscript = '';
        this.userStopped = false;
        this.clearAutoSendTimer();

        if (this.useWhisper || !this.webSpeechSupported) {
            if (this.whisperAvailable) {
                this.startWhisper();
            } else {
                this.showError('Voice not available');
            }
        } else {
            this.startWebSpeech();
        }
    }

    startWebSpeech() {
        if (!this.recognition) return;

        try {
            console.log('Starting Web Speech...');
            this.recognition.start();
        } catch (e) {
            if (e.message?.includes('already started')) {
                console.warn('Recognition already active');
            } else {
                console.error('Failed to start Web Speech:', e);
                if (this.whisperAvailable) {
                    console.log('Falling back to Whisper');
                    this.useWhisper = true;
                    this.startWhisper();
                } else {
                    this.showError('Failed to start voice input');
                }
            }
        }
    }

    async startWhisper() {
        // Check if we're on HTTPS or localhost (required for getUserMedia)
        if (location.protocol !== 'https:' && location.hostname !== 'localhost' && location.hostname !== '127.0.0.1') {
            this.showError('Voice requires HTTPS. Use https:// URL.');
            return;
        }

        // Check permission status first (if API available)
        if (navigator.permissions) {
            try {
                const result = await navigator.permissions.query({ name: 'microphone' });
                console.log('Microphone permission status:', result.state);
                if (result.state === 'denied') {
                    this.showError('Microphone denied in browser settings. Check site permissions.');
                    return;
                }
            } catch (e) {
                console.log('Permission query not supported:', e);
            }
        }

        try {
            console.log('Requesting microphone access...');
            const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            console.log('Microphone access granted');
            this.audioChunks = [];

            // Find a supported MIME type
            const mimeTypes = ['audio/webm', 'audio/webm;codecs=opus', 'audio/ogg', 'audio/mp4', ''];
            let mimeType = '';
            for (const type of mimeTypes) {
                if (type === '' || MediaRecorder.isTypeSupported(type)) {
                    mimeType = type;
                    break;
                }
            }

            const options = mimeType ? { mimeType } : {};
            this.mediaRecorder = new MediaRecorder(stream, options);
            this.recordingMimeType = this.mediaRecorder.mimeType || 'audio/webm';

            this.mediaRecorder.ondataavailable = (e) => {
                if (e.data.size > 0) {
                    this.audioChunks.push(e.data);
                }
            };

            this.mediaRecorder.onstop = async () => {
                stream.getTracks().forEach(track => track.stop());

                if (this.audioChunks.length > 0 && this.userStopped) {
                    await this.transcribeWithWhisper();
                }
            };

            this.mediaRecorder.start(100);
            this.isListening = true;
            this.updateUI();
            console.log('Whisper recording started');

            // Auto-stop after 5 seconds max recording time
            this.whisperTimeout = setTimeout(() => {
                if (this.mediaRecorder && this.mediaRecorder.state === 'recording') {
                    console.log('Auto-stopping Whisper recording after timeout');
                    this.stop();
                }
            }, 5000);

        } catch (e) {
            console.error('Failed to start Whisper recording:', e);
            if (e.name === 'NotAllowedError' || e.name === 'PermissionDeniedError') {
                // More helpful error for Brave users
                const isBrave = navigator.brave && navigator.brave.isBrave;
                if (isBrave) {
                    this.showError('Mic blocked. Disable Brave Shields or check brave://settings/content/microphone');
                } else {
                    this.showError('Microphone blocked. Check site permissions.');
                }
            } else if (e.name === 'NotFoundError' || e.name === 'NotReadableError') {
                this.showError('No microphone found or in use');
            } else {
                this.showError(`Recording error: ${e.message || e.name}`);
            }
            this.isListening = false;
            this.updateUI();
        }
    }

    async transcribeWithWhisper() {
        if (this.audioChunks.length === 0) return;

        const mimeType = this.recordingMimeType || 'audio/webm';
        const audioBlob = new Blob(this.audioChunks, { type: mimeType });
        const ext = mimeType.includes('ogg') ? 'ogg' : mimeType.includes('mp4') ? 'm4a' : 'webm';

        if (this.messageInput) {
            this.messageInput.placeholder = 'Transcribing...';
        }

        try {
            const formData = new FormData();
            formData.append('audio', audioBlob, `recording.${ext}`);

            // Get CSRF token from cookie
            const csrfToken = document.cookie.split('; ')
                .find(row => row.startsWith('csrf_token='))?.split('=')[1];

            const resp = await fetch('/api/stt/transcribe', {
                method: 'POST',
                body: formData,
                headers: {
                    'X-CSRF-Token': csrfToken || ''
                }
            });

            if (resp.ok) {
                const data = await resp.json();
                if (data.text) {
                    this.finalTranscript = (this.finalTranscript + ' ' + data.text).trim();
                    if (this.messageInput) {
                        this.messageInput.value = this.finalTranscript;
                        this.messageInput.dispatchEvent(new Event('input'));
                    }
                    this.autoSend();
                }
            } else {
                try {
                    const error = await resp.json();
                    this.showError(error.detail || 'Transcription failed');
                } catch {
                    this.showError(`Transcription failed (${resp.status})`);
                }
            }
        } catch (e) {
            console.error('Whisper transcription error:', e);
            this.showError('Transcription failed');
        } finally {
            if (this.messageInput) {
                this.messageInput.placeholder = 'Type a message or command...';
            }
        }
    }

    stop() {
        this.userStopped = true;
        this.clearAutoSendTimer();

        // Clear Whisper auto-stop timeout
        if (this.whisperTimeout) {
            clearTimeout(this.whisperTimeout);
            this.whisperTimeout = null;
        }

        if (this.mediaRecorder && this.mediaRecorder.state === 'recording') {
            this.mediaRecorder.stop();
            this.isListening = false;
            this.updateUI();
        } else if (this.recognition) {
            try {
                this.recognition.stop();
            } catch (e) {
                console.warn('Recognition stop error:', e);
            }
        }
    }

    startAutoSendTimer() {
        this.clearAutoSendTimer();
        this.autoSendTimeout = setTimeout(() => {
            if (this.finalTranscript.trim()) {
                this.stop();
            }
        }, this.autoSendDelay);
    }

    clearAutoSendTimer() {
        if (this.autoSendTimeout) {
            clearTimeout(this.autoSendTimeout);
            this.autoSendTimeout = null;
        }
    }

    autoSend() {
        if (this.messageInput && this.messageInput.value.trim()) {
            const originalText = this.messageInput.value.trim();
            const parsed = parseVoiceCommand(originalText);

            // If null, it was a hallucination - ignore
            if (parsed === null) {
                this.showNotification('Unclear audio, try again', 'info');
                this.messageInput.value = '';
                this.finalTranscript = '';
                this.interimTranscript = '';
                return;
            }

            if (parsed !== originalText) {
                this.messageInput.value = parsed;
                this.messageInput.dispatchEvent(new Event('input'));
                console.log(`Voice command: "${originalText}" → "${parsed}"`);
            }

            const sendBtn = document.getElementById('sendBtn');
            if (sendBtn) {
                sendBtn.click();
            }
        }
        this.finalTranscript = '';
        this.interimTranscript = '';
    }

    updateUI() {
        if (!this.voiceBtn) return;

        if (this.isListening) {
            this.voiceBtn.classList.add('listening');
            this.voiceBtn.textContent = '🎙️';
            this.voiceBtn.title = this.useWhisper ? 'Recording... (click to stop or wait 5s)' : 'Listening... (click to send)';
        } else {
            this.voiceBtn.classList.remove('listening');
            this.voiceBtn.textContent = '🎤';
            this.voiceBtn.title = 'Voice input (click to speak)';
        }
    }

    showError(message) {
        console.error('STT Error:', message);
        this.showNotification(message, 'error');
    }

    showNotification(message, type = 'info') {
        if (window.showNotification) {
            window.showNotification(message, type);
        } else if (window.Toastify) {
            Toastify({
                text: message,
                duration: 3000,
                gravity: 'top',
                position: 'right',
                backgroundColor: type === 'error' ? '#dc3545' : '#17a2b8'
            }).showToast();
        } else if (this.messageInput) {
            const oldPlaceholder = this.messageInput.placeholder;
            this.messageInput.placeholder = message;
            setTimeout(() => {
                this.messageInput.placeholder = oldPlaceholder;
            }, 3000);
        }
    }

    isSupported() {
        return this.webSpeechSupported || this.whisperAvailable;
    }

    isActive() {
        return this.isListening;
    }
}

// Initialize STT controller
window.sttController = new STTController();
