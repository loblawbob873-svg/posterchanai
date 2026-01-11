/**
 * Cyberpunk Music Player with Web Audio API Visualizer
 */
class MusicPlayer {
    constructor() {
        this.audio = null;
        this.audioContext = null;
        this.analyser = null;
        this.source = null;
        this.canvas = null;
        this.canvasCtx = null;
        this.animationId = null;

        this.queue = [];
        this.currentIndex = -1;
        this.isPlaying = false;
        this.volume = 0.8;
        this.quality = localStorage.getItem('musicQuality') || 'original'; // original, high, medium, low

        this.container = null;
        this.isInitialized = false;
    }

    init() {
        if (this.isInitialized) return;

        this.createPlayerUI();
        this.setupAudio();
        this.bindEvents();
        this.isInitialized = true;
    }

    createPlayerUI() {
        // Create player container
        this.container = document.createElement('div');
        this.container.className = 'music-player hidden';
        this.container.innerHTML = `
            <div class="music-player-header">
                <span class="drag-hint">⋮⋮ drag</span>
                <div class="header-buttons">
                    <button class="music-toggle" title="Collapse">−</button>
                    <button class="music-close" title="Close">&times;</button>
                </div>
            </div>
            <canvas class="music-visualizer"></canvas>
            <div class="music-controls">
                <div class="music-controls-row">
                    <button class="music-btn small" id="music-prev" title="Previous">⏮</button>
                    <button class="music-btn play-btn" id="music-play" title="Play">▶</button>
                    <button class="music-btn small" id="music-next" title="Next">⏭</button>
                    <div class="music-volume-container">
                        <span class="music-volume-icon">🔊</span>
                        <div class="music-volume" id="music-volume">
                            <div class="music-volume-bar"></div>
                        </div>
                    </div>
                    <button class="music-btn small" id="music-queue-btn" title="Queue">☰</button>
                    <select class="music-quality" id="music-quality" title="Stream Quality">
                        <option value="original">Original</option>
                        <option value="high">High (256k)</option>
                        <option value="medium">Medium (128k)</option>
                        <option value="low">Low (64k)</option>
                    </select>
                </div>
                <div class="music-track-info">
                    <div class="music-track-title">No track playing</div>
                    <div class="music-track-artist"></div>
                </div>
                <div class="music-progress-container">
                    <span class="music-time" id="music-current">0:00</span>
                    <div class="music-progress" id="music-progress">
                        <div class="music-progress-bar"></div>
                    </div>
                    <span class="music-time" id="music-duration">0:00</span>
                </div>
            </div>
            <div class="music-queue" id="music-queue-panel"></div>
        `;

        document.body.appendChild(this.container);

        // Setup canvas
        this.canvas = this.container.querySelector('.music-visualizer');
        this.canvasCtx = this.canvas.getContext('2d');
        this.resizeCanvas();

        // Setup dragging
        this.setupDrag();
    }

    setupDrag() {
        const header = this.container.querySelector('.music-player-header');
        let isDragging = false;
        let startX, startY, startLeft, startTop;

        header.addEventListener('mousedown', (e) => {
            if (e.target.tagName === 'BUTTON') return;
            isDragging = true;
            startX = e.clientX;
            startY = e.clientY;
            const rect = this.container.getBoundingClientRect();
            startLeft = rect.left;
            startTop = rect.top;
            this.container.style.transition = 'none';
        });

        document.addEventListener('mousemove', (e) => {
            if (!isDragging) return;
            const dx = e.clientX - startX;
            const dy = e.clientY - startY;
            this.container.style.left = (startLeft + dx) + 'px';
            this.container.style.top = (startTop + dy) + 'px';
            this.container.style.right = 'auto';
        });

        document.addEventListener('mouseup', () => {
            isDragging = false;
            this.container.style.transition = '';
        });

        // Touch support
        header.addEventListener('touchstart', (e) => {
            if (e.target.tagName === 'BUTTON') return;
            isDragging = true;
            startX = e.touches[0].clientX;
            startY = e.touches[0].clientY;
            const rect = this.container.getBoundingClientRect();
            startLeft = rect.left;
            startTop = rect.top;
        });

        document.addEventListener('touchmove', (e) => {
            if (!isDragging) return;
            const dx = e.touches[0].clientX - startX;
            const dy = e.touches[0].clientY - startY;
            this.container.style.left = (startLeft + dx) + 'px';
            this.container.style.top = (startTop + dy) + 'px';
            this.container.style.right = 'auto';
        });

        document.addEventListener('touchend', () => {
            isDragging = false;
        });
    }

    setupAudio() {
        this.audio = new Audio();
        // Same-origin request, no crossOrigin needed - cookies sent automatically
        this.audio.volume = this.volume;

        // Audio events
        this.audio.addEventListener('ended', () => this.next());
        this.audio.addEventListener('timeupdate', () => this.updateProgress());
        this.audio.addEventListener('loadedmetadata', () => this.updateDuration());
        this.audio.addEventListener('play', () => this.onPlay());
        this.audio.addEventListener('pause', () => this.onPause());
        this.audio.addEventListener('error', (e) => this.onError(e));
    }

    setupAudioContext() {
        if (this.audioContext) {
            // Resume if suspended (browser autoplay policy)
            if (this.audioContext.state === 'suspended') {
                this.audioContext.resume();
            }
            return;
        }

        try {
            this.audioContext = new (window.AudioContext || window.webkitAudioContext)();
            this.analyser = this.audioContext.createAnalyser();
            this.analyser.fftSize = 256;

            this.source = this.audioContext.createMediaElementSource(this.audio);
            this.source.connect(this.analyser);
            this.analyser.connect(this.audioContext.destination);

            // Resume immediately if suspended
            if (this.audioContext.state === 'suspended') {
                this.audioContext.resume();
            }
        } catch (e) {
            console.warn('Web Audio API not supported:', e);
        }
    }

    bindEvents() {
        // Play/Pause
        this.container.querySelector('#music-play').addEventListener('click', () => {
            this.togglePlay();
        });

        // Previous/Next
        this.container.querySelector('#music-prev').addEventListener('click', () => this.prev());
        this.container.querySelector('#music-next').addEventListener('click', () => this.next());

        // Progress bar - use currentTarget to always get the container element
        const progressEl = this.container.querySelector('#music-progress');
        progressEl.addEventListener('click', (e) => {
            e.stopPropagation();
            e.preventDefault();
            const rect = progressEl.getBoundingClientRect();
            const clickX = e.clientX - rect.left;
            const percent = Math.max(0, Math.min(1, clickX / rect.width));
            const duration = this.audio?.duration;
            console.log('Seek click - duration:', duration, 'percent:', percent, 'isFinite:', Number.isFinite(duration));
            // Check duration is a valid finite number
            if (this.audio && duration && Number.isFinite(duration) && duration > 0) {
                const newTime = percent * duration;
                console.log('Seeking to:', newTime);
                if (Number.isFinite(newTime)) {
                    this.audio.currentTime = newTime;
                }
            } else {
                console.log('Cannot seek - duration invalid');
            }
        });

        // Volume - use currentTarget to always get the container element
        this.container.querySelector('#music-volume').addEventListener('click', (e) => {
            const rect = e.currentTarget.getBoundingClientRect();
            const percent = (e.clientX - rect.left) / rect.width;
            this.setVolume(percent);
        });

        // Volume icon (mute toggle)
        this.container.querySelector('.music-volume-icon').addEventListener('click', () => {
            this.audio.muted = !this.audio.muted;
            this.updateVolumeIcon();
        });

        // Queue toggle
        this.container.querySelector('#music-queue-btn').addEventListener('click', () => {
            const panel = this.container.querySelector('#music-queue-panel');
            panel.classList.toggle('visible');
        });

        // Quality selector
        const qualitySelect = this.container.querySelector('#music-quality');
        qualitySelect.value = this.quality;
        qualitySelect.addEventListener('change', (e) => {
            this.quality = e.target.value;
            localStorage.setItem('musicQuality', this.quality);
            // If currently playing, reload with new quality
            if (this.isPlaying && this.currentIndex >= 0) {
                const currentTime = this.audio.currentTime;
                this.reloadCurrentTrack();
            }
        });

        // Collapse toggle
        this.container.querySelector('.music-toggle').addEventListener('click', () => {
            this.container.classList.toggle('collapsed');
            const btn = this.container.querySelector('.music-toggle');
            btn.textContent = this.container.classList.contains('collapsed') ? '+' : '−';
        });

        // Close button
        this.container.querySelector('.music-close').addEventListener('click', () => {
            this.stop();
            this.hide();
        });

        // Resize canvas on window resize
        window.addEventListener('resize', () => this.resizeCanvas());
    }

    resizeCanvas() {
        if (!this.canvas) return;
        this.canvas.width = this.canvas.offsetWidth * window.devicePixelRatio;
        this.canvas.height = this.canvas.offsetHeight * window.devicePixelRatio;
    }

    // Public API

    play(track) {
        if (!this.isInitialized) this.init();

        // Setup audio context on first user interaction
        this.setupAudioContext();

        if (track) {
            // Add to queue if not already there
            const existingIndex = this.queue.findIndex(t => t.path === track.path);
            if (existingIndex >= 0) {
                this.currentIndex = existingIndex;
            } else {
                this.queue.push(track);
                this.currentIndex = this.queue.length - 1;
            }
        }

        if (this.currentIndex < 0 || this.currentIndex >= this.queue.length) {
            return;
        }

        const currentTrack = this.queue[this.currentIndex];
        this.audio.src = this.getStreamUrl(currentTrack);
        this.audio.play();

        // Update UI
        this.container.querySelector('.music-track-title').textContent = currentTrack.title || currentTrack.filename || 'Unknown';
        this.container.querySelector('.music-track-artist').textContent = currentTrack.artist || '';

        this.show();
        this.updateQueueUI();
    }

    getStreamUrl(track) {
        let url = track.streamUrl || `/api/music/stream?path=${encodeURIComponent(track.path)}`;
        // Add quality parameter if not original
        if (this.quality && this.quality !== 'original') {
            url += (url.includes('?') ? '&' : '?') + `quality=${this.quality}`;
        }
        return url;
    }

    reloadCurrentTrack() {
        if (this.currentIndex < 0 || this.currentIndex >= this.queue.length) return;
        const currentTrack = this.queue[this.currentIndex];
        this.audio.src = this.getStreamUrl(currentTrack);
        this.audio.play();
    }

    pause() {
        this.audio.pause();
    }

    togglePlay() {
        if (this.isPlaying) {
            this.pause();
        } else if (this.currentIndex >= 0) {
            this.audio.play();
        }
    }

    stop() {
        this.audio.pause();
        this.audio.currentTime = 0;
        this.isPlaying = false;
        this.stopVisualizer();
    }

    next() {
        if (this.queue.length === 0) return;
        this.currentIndex = (this.currentIndex + 1) % this.queue.length;
        this.play();
    }

    prev() {
        if (this.queue.length === 0) return;
        // If more than 3 seconds in, restart current track
        if (this.audio.currentTime > 3) {
            this.audio.currentTime = 0;
            return;
        }
        this.currentIndex = (this.currentIndex - 1 + this.queue.length) % this.queue.length;
        this.play();
    }

    addToQueue(track) {
        if (!this.isInitialized) this.init();
        this.queue.push(track);
        this.updateQueueUI();
        this.show();
    }

    clearQueue() {
        if (!this.isInitialized) this.init();
        this.queue = [];
        this.currentIndex = -1;
        this.updateQueueUI();
    }

    removeFromQueue(index) {
        if (index < 0 || index >= this.queue.length) return;

        this.queue.splice(index, 1);

        if (index < this.currentIndex) {
            this.currentIndex--;
        } else if (index === this.currentIndex) {
            // Currently playing track was removed
            if (this.queue.length > 0) {
                this.currentIndex = Math.min(this.currentIndex, this.queue.length - 1);
                this.play();
            } else {
                this.stop();
            }
        }

        this.updateQueueUI();
    }

    setVolume(percent) {
        this.volume = Math.max(0, Math.min(1, percent));
        this.audio.volume = this.volume;
        this.container.querySelector('.music-volume-bar').style.width = `${this.volume * 100}%`;
        this.updateVolumeIcon();
    }

    show() {
        this.container.classList.remove('hidden');
    }

    hide() {
        this.container.classList.add('hidden');
    }

    // Internal methods

    onPlay() {
        this.isPlaying = true;
        this.container.classList.add('playing');
        this.container.querySelector('#music-play').innerHTML = '&#10074;&#10074;';
        this.startVisualizer();
    }

    onPause() {
        this.isPlaying = false;
        this.container.classList.remove('playing');
        this.container.querySelector('#music-play').innerHTML = '&#9654;';
        this.stopVisualizer();
    }

    onError(e) {
        const audio = this.audio;
        const errorCode = audio.error ? audio.error.code : 'unknown';
        const errorMsg = audio.error ? audio.error.message : 'unknown';
        console.error('Audio error:', {
            code: errorCode,
            message: errorMsg,
            src: audio.src,
            networkState: audio.networkState,
            readyState: audio.readyState
        });
        this.container.querySelector('.music-track-title').textContent = `Error loading track (${errorCode})`;
    }

    updateProgress() {
        if (!this.audio.duration) return;

        const percent = (this.audio.currentTime / this.audio.duration) * 100;
        this.container.querySelector('.music-progress-bar').style.width = `${percent}%`;
        this.container.querySelector('#music-current').textContent = this.formatTime(this.audio.currentTime);
    }

    updateDuration() {
        this.container.querySelector('#music-duration').textContent = this.formatTime(this.audio.duration);
    }

    updateVolumeIcon() {
        const icon = this.container.querySelector('.music-volume-icon');
        if (this.audio.muted || this.volume === 0) {
            icon.innerHTML = '&#128263;'; // Muted
        } else if (this.volume < 0.5) {
            icon.innerHTML = '&#128265;'; // Low
        } else {
            icon.innerHTML = '&#128266;'; // High
        }
    }

    updateQueueUI() {
        if (!this.container) return;
        const panel = this.container.querySelector('#music-queue-panel');
        if (!panel) return;

        if (this.queue.length === 0) {
            panel.innerHTML = '<div style="padding: 16px; text-align: center; color: var(--text-secondary);">Queue is empty</div>';
            return;
        }

        panel.innerHTML = this.queue.map((track, i) => `
            <div class="music-queue-item ${i === this.currentIndex ? 'active' : ''}" data-index="${i}">
                <span class="track-num">${i + 1}</span>
                <span class="track-title">${track.title || track.filename}</span>
                <span class="remove-btn" data-remove="${i}">&times;</span>
            </div>
        `).join('');

        // Bind queue item events
        panel.querySelectorAll('.music-queue-item').forEach(item => {
            item.addEventListener('click', (e) => {
                if (e.target.classList.contains('remove-btn')) {
                    const idx = parseInt(e.target.dataset.remove);
                    this.removeFromQueue(idx);
                } else {
                    const idx = parseInt(item.dataset.index);
                    this.currentIndex = idx;
                    this.play();
                }
            });
        });
    }

    formatTime(seconds) {
        if (isNaN(seconds)) return '0:00';
        const mins = Math.floor(seconds / 60);
        const secs = Math.floor(seconds % 60);
        return `${mins}:${secs.toString().padStart(2, '0')}`;
    }

    // Visualizer

    startVisualizer() {
        if (!this.analyser) return;

        const bufferLength = this.analyser.frequencyBinCount;
        const dataArray = new Uint8Array(bufferLength);

        const draw = () => {
            this.animationId = requestAnimationFrame(draw);

            this.analyser.getByteFrequencyData(dataArray);

            const width = this.canvas.width;
            const height = this.canvas.height;

            // Clear with fade effect
            this.canvasCtx.fillStyle = 'rgba(10, 10, 15, 0.3)';
            this.canvasCtx.fillRect(0, 0, width, height);

            const barCount = 64;
            const barWidth = width / barCount;
            const barGap = 2;

            for (let i = 0; i < barCount; i++) {
                // Sample from data array
                const dataIndex = Math.floor(i * bufferLength / barCount);
                const value = dataArray[dataIndex];
                const barHeight = (value / 255) * height * 0.9;

                const x = i * barWidth;
                const y = height - barHeight;

                // Gradient from cyan to magenta
                const hue = 180 + (i / barCount) * 120; // 180 (cyan) to 300 (magenta)
                const gradient = this.canvasCtx.createLinearGradient(x, height, x, y);
                gradient.addColorStop(0, `hsla(${hue}, 100%, 50%, 0.8)`);
                gradient.addColorStop(1, `hsla(${hue}, 100%, 70%, 0.4)`);

                this.canvasCtx.fillStyle = gradient;
                this.canvasCtx.fillRect(x + barGap / 2, y, barWidth - barGap, barHeight);

                // Glow effect on top
                this.canvasCtx.shadowColor = `hsla(${hue}, 100%, 50%, 0.8)`;
                this.canvasCtx.shadowBlur = 10;
                this.canvasCtx.fillRect(x + barGap / 2, y, barWidth - barGap, 2);
                this.canvasCtx.shadowBlur = 0;
            }
        };

        draw();
    }

    stopVisualizer() {
        if (this.animationId) {
            cancelAnimationFrame(this.animationId);
            this.animationId = null;
        }

        // Fade out canvas
        if (this.canvasCtx) {
            this.canvasCtx.fillStyle = 'rgba(10, 10, 15, 0.95)';
            this.canvasCtx.fillRect(0, 0, this.canvas.width, this.canvas.height);
        }
    }
}

// Global instance
window.musicPlayer = new MusicPlayer();
