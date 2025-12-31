// Mascot Controller
class MascotController {
    constructor() {
        this.image = document.getElementById('mascotImage');
        this.speechBubble = document.getElementById('speechBubble');
        this.particles = document.getElementById('particles');

        // Map moods to their image indices
        this.moodImages = {
            'neutral': { front: '00', left: '01', right: '02', up: '03', down: '04' },
            'happy': { front: '05', left: '06', right: '07', up: '08', down: '09' },
            'excited': { front: '10', left: '11', right: '12', up: '13', down: '14' },
            'blink': { front: '15', left: '16', right: '17', up: '18', down: '19' },
            'wink': { front: '20', left: '21', right: '22', up: '23', down: '24' },
            'thinking': { front: '25', left: '26', right: '27', up: '28', down: '29' },
            'surprised': { front: '30', left: '31', right: '32', up: '33', down: '34' },
            'shy': { front: '35', left: '36', right: '37', up: '38', down: '39' },
            'determined': { front: '40', left: '41', right: '42', up: '43', down: '44' },
            'sleepy': { front: '45', left: '46', right: '47', up: '48', down: '49' }
        };
        this.poses = ['front', 'left', 'right', 'up', 'down'];
        this.currentMood = 'neutral';
        this.currentPoseIndex = 0;
        this.animationInterval = null;
        this.speechTimeout = null;

        this.init();
    }

    init() {
        // Click interaction
        this.image.addEventListener('click', () => this.onMascotClick());

        // Start idle animation
        this.startIdleAnimation();
    }

    getImagePath(mood, pose) {
        const moodData = this.moodImages[mood] || this.moodImages['neutral'];
        const index = moodData[pose] || moodData['front'];
        return `/static/mascot/mascot-${mood}-${pose}-${index}.png`;
    }

    setMood(mood) {
        if (!this.moodImages[mood]) mood = 'neutral';
        this.currentMood = mood;
        this.updateImage();
    }

    updateImage() {
        const pose = this.poses[this.currentPoseIndex % this.poses.length];
        this.image.src = this.getImagePath(this.currentMood, pose);
        // Fallback to neutral-front if image doesn't exist
        this.image.onerror = () => {
            this.image.src = '/static/mascot/mascot-neutral-front-00.png';
        };
    }

    startIdleAnimation() {
        // Cycle through poses every 3 seconds
        this.animationInterval = setInterval(() => {
            this.currentPoseIndex = (this.currentPoseIndex + 1) % this.poses.length;
            this.updateImage();
        }, 3000);
    }

    stopIdleAnimation() {
        if (this.animationInterval) {
            clearInterval(this.animationInterval);
            this.animationInterval = null;
        }
    }

    onMascotClick() {
        // Show a random reaction
        const reactions = ['Hehe~', 'Hi there!', 'Teehee!', "What's up?", 'Need help?'];
        const reaction = reactions[Math.floor(Math.random() * reactions.length)];
        this.showSpeech(reaction, 2000);

        // Wiggle animation
        this.image.classList.add('happy');
        setTimeout(() => this.image.classList.remove('happy'), 500);

        // Spawn particles
        this.spawnParticles(['💖', '✨', '💬'], 5);
    }

    showSpeech(text, duration = 3000) {
        if (this.speechTimeout) clearTimeout(this.speechTimeout);

        this.speechBubble.textContent = text;
        this.speechBubble.classList.add('visible');

        this.speechTimeout = setTimeout(() => {
            this.speechBubble.classList.remove('visible');
        }, duration);
    }

    spawnParticles(emojis, count = 3) {
        for (let i = 0; i < count; i++) {
            setTimeout(() => {
                const particle = document.createElement('span');
                particle.className = 'particle';
                particle.textContent = emojis[Math.floor(Math.random() * emojis.length)];
                particle.style.left = `${40 + Math.random() * 20}%`;
                particle.style.top = `${60 + Math.random() * 20}%`;
                this.particles.appendChild(particle);

                // Remove after animation
                setTimeout(() => particle.remove(), 2000);
            }, i * 100);
        }
    }

    // Called when AI responds
    onResponse(isPositive = true) {
        if (isPositive) {
            this.setMood('happy');
            this.showSpeech('Here you go!', 2000);
            this.spawnParticles(['💖', '✨'], 3);
        } else {
            this.setMood('thinking');
            this.showSpeech('Hmm...', 2000);
        }

        // Reset to neutral after a while
        setTimeout(() => this.setMood('neutral'), 5000);
    }

    // Called when user sends message
    onUserMessage() {
        this.setMood('excited');
        this.showSpeech('Let me think...', 2000);
        this.spawnParticles(['💭', '🤔'], 2);
    }

    // Called when generating image
    onGeneratingImage() {
        this.setMood('determined');
        this.showSpeech('Creating art...', 3000);
        this.spawnParticles(['🎨', '✨'], 3);
    }

    // Called on error
    onError() {
        this.setMood('surprised');
        this.showSpeech('Oops!', 2000);
        setTimeout(() => this.setMood('neutral'), 3000);
    }
}

// Initialize mascot controller
window.mascotController = new MascotController();
