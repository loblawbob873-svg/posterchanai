"""
Audio playback using mpv.
"""

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
    """Fallback player using mpv subprocess."""

    def __init__(self):
        self._process: Optional[subprocess.Popen] = None
        self._playing = False
        self._position = 0.0
        self._duration = 0.0
        self._start_time = 0.0

    def play(self, url: str):
        """Play audio via subprocess."""
        self.stop()

        try:
            self._process = subprocess.Popen(
                ['mpv', '--no-video', '--really-quiet', url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            self._playing = True
            self._start_time = time.time()
            self._start_progress_monitor()
        except FileNotFoundError:
            raise RuntimeError("mpv not found. Please install mpv.")

    def _start_progress_monitor(self):
        """Monitor playback progress."""
        def monitor():
            while self._playing and self._process:
                if self._process.poll() is not None:
                    # Process ended
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
        """Pause - not supported in subprocess mode."""
        pass

    def resume(self):
        """Resume - not supported in subprocess mode."""
        pass

    def stop(self):
        """Stop playback."""
        if self._process:
            self._process.terminate()
            self._process = None
        self._playing = False
        self._position = 0.0

    def seek(self, position: float):
        """Seek - not supported in subprocess mode."""
        pass

    def get_position(self) -> float:
        return self._position

    def get_duration(self) -> float:
        return self._duration


def create_player() -> AudioPlayer:
    """Create the best available audio player."""
    try:
        return MPVPlayer()
    except ImportError:
        # python-mpv not installed, try subprocess
        return SubprocessPlayer()
    except Exception:
        return SubprocessPlayer()
