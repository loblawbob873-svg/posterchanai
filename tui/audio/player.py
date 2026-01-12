"""
Audio playback using mpv.
"""
from __future__ import annotations

import subprocess
import threading
import time
from abc import ABC, abstractmethod
from typing import Callable, Optional


class AudioPlayer(ABC):
    """Abstract audio player interface."""

    on_progress: Optional[Callable[[float, float], None]] = None
    on_track_end: Optional[Callable[[], None]] = None

    @abstractmethod
    def play(self, url: str):
        """Start playing audio from URL."""
        pass

    @abstractmethod
    def pause(self):
        """Pause playback."""
        pass

    @abstractmethod
    def resume(self):
        """Resume playback."""
        pass

    @abstractmethod
    def stop(self):
        """Stop playback."""
        pass

    @abstractmethod
    def seek(self, position: float):
        """Seek to position in seconds."""
        pass

    @abstractmethod
    def get_position(self) -> float:
        """Get current position in seconds."""
        pass

    @abstractmethod
    def get_duration(self) -> float:
        """Get track duration in seconds."""
        pass


class MPVPlayer(AudioPlayer):
    """Audio player using python-mpv library."""

    def __init__(self):
        import mpv
        self.mpv = mpv.MPV(
            video=False,
            terminal=False,
            input_default_bindings=False,
            input_vo_keyboard=False,
        )
        self._duration = 0.0
        self._position = 0.0
        self._playing = False
        self._monitor_thread: Optional[threading.Thread] = None

        # Set up event observers
        @self.mpv.property_observer('duration')
        def on_duration(name, value):
            if value:
                self._duration = value

        @self.mpv.property_observer('time-pos')
        def on_position(name, value):
            if value:
                self._position = value
                if self.on_progress and self._duration > 0:
                    self.on_progress(self._position, self._duration)

        @self.mpv.event_callback('end-file')
        def on_end(event):
            self._playing = False
            if self.on_track_end:
                self.on_track_end()

    def play(self, url: str):
        """Play audio from URL."""
        self.mpv.play(url)
        self._playing = True

    def pause(self):
        """Pause playback."""
        self.mpv.pause = True
        self._playing = False

    def resume(self):
        """Resume playback."""
        self.mpv.pause = False
        self._playing = True

    def stop(self):
        """Stop playback."""
        self.mpv.stop()
        self._playing = False
        self._position = 0.0

    def seek(self, position: float):
        """Seek to position."""
        self.mpv.seek(position, reference='absolute')

    def get_position(self) -> float:
        """Get current position."""
        return self._position

    def get_duration(self) -> float:
        """Get duration."""
        return self._duration

    def get_levels(self) -> list[float]:
        """Get audio levels for visualization (not available in basic mpv)."""
        # Return empty - visualization will use random data
        return []


class SubprocessPlayer(AudioPlayer):
    """Fallback player using mpv subprocess with robust buffering for poor connections."""

    def __init__(self):
        self._process: Optional[subprocess.Popen] = None
        self._playing = False
        self._position = 0.0
        self._duration = 0.0
        self._start_time = 0.0
        self._current_url: Optional[str] = None
        self._retry_count = 0
        self._max_retries = 3

    def play(self, url: str):
        """Play audio via subprocess with buffering for unstable connections."""
        self.stop()
        self._current_url = url
        self._retry_count = 0
        self._start_playback(url, seek_position=0.0)

    def _start_playback(self, url: str, seek_position: float = 0.0):
        """Internal method to start playback with optional seek position."""
        try:
            # Configure mpv with aggressive caching for poor mobile connections:
            # --cache=yes: Enable cache
            # --cache-secs=120: Buffer up to 2 minutes of audio
            # --demuxer-max-bytes=50M: Allow up to 50MB demuxer buffer
            # --demuxer-readahead-secs=120: Read ahead up to 2 minutes
            # --network-timeout=300: 5 minute network timeout
            # --stream-buffer-size=2M: 2MB stream buffer
            cmd = [
                'mpv',
                '--no-video',
                '--really-quiet',
                '--cache=yes',
                '--cache-secs=120',
                '--demuxer-max-bytes=50M',
                '--demuxer-readahead-secs=120',
                '--network-timeout=300',
                '--stream-buffer-size=2M',
            ]

            # Add seek position if resuming
            if seek_position > 0:
                cmd.append(f'--start={seek_position}')

            cmd.append(url)

            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            self._playing = True
            self._start_time = time.time() - seek_position  # Adjust for resume position
            self._start_progress_monitor()
        except FileNotFoundError:
            raise RuntimeError("mpv not found. Please install mpv.")

    def _start_progress_monitor(self):
        """Monitor playback progress with auto-retry on failure."""
        def monitor():
            while self._playing and self._process:
                if self._process.poll() is not None:
                    # Process ended - check if it was premature (potential network issue)
                    current_pos = time.time() - self._start_time

                    # If we have duration info and ended before 90% complete, try to retry
                    if (self._duration > 0 and
                        current_pos < self._duration * 0.9 and
                        self._retry_count < self._max_retries and
                        self._current_url):
                        # Likely a network interruption - retry from current position
                        self._retry_count += 1
                        self._playing = False

                        # Wait a moment before retrying
                        time.sleep(2)

                        # Resume from where we left off
                        self._start_playback(self._current_url, seek_position=current_pos)
                        return  # New monitor thread started by _start_playback

                    # Track actually ended or max retries reached
                    self._playing = False
                    if self.on_track_end:
                        self.on_track_end()
                    break

                self._position = time.time() - self._start_time
                if self.on_progress:
                    self.on_progress(self._position, self._duration)

                time.sleep(0.5)

        thread = threading.Thread(target=monitor, daemon=True)
        thread.start()

    def pause(self):
        """Pause playback using SIGSTOP."""
        if self._process and self._playing:
            import signal
            try:
                self._process.send_signal(signal.SIGSTOP)
                self._playing = False
            except Exception:
                pass

    def resume(self):
        """Resume playback using SIGCONT."""
        if self._process:
            import signal
            try:
                self._process.send_signal(signal.SIGCONT)
                self._playing = True
            except Exception:
                pass

    def stop(self):
        """Stop playback."""
        if self._process:
            self._process.terminate()
            self._process = None
        self._playing = False
        self._position = 0.0
        self._current_url = None
        self._retry_count = 0

    def seek(self, position: float):
        """Seek - restart playback from position (subprocess limitation)."""
        if self._current_url and position >= 0:
            self.stop()
            self._start_playback(self._current_url, seek_position=position)

    def set_duration(self, duration: float):
        """Set track duration (needed for retry logic)."""
        self._duration = duration

    def get_position(self) -> float:
        return self._position

    def get_duration(self) -> float:
        return self._duration


def create_player() -> AudioPlayer:
    """Create the best available audio player."""
    # Always use subprocess player - python-mpv has threading issues with Textual
    return SubprocessPlayer()
