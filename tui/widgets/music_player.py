"""
Music player widget with ASCII visualizer.
"""
from __future__ import annotations

import asyncio
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static, Button, ProgressBar
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual import work


class MusicPlayerWidget(Widget):
    """Music player with controls and visualizer."""

    is_playing = reactive(False)
    current_track = reactive(None)
    progress = reactive(0.0)
    duration = reactive(0.0)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.player = None
        self.visualizer = None
        self.playlist: list[dict] = []
        self.playlist_index = 0
        self.add_class("--hidden")  # Start hidden

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("MUSIC PLAYER", id="player-title"),
            Horizontal(
                Static("No track loaded", id="track-info"),
                id="player-info"
            ),
            Static("", id="visualizer"),
            Horizontal(
                Static("0:00", id="time-current"),
                ProgressBar(total=100, show_eta=False, id="progress-bar"),
                Static("0:00", id="time-total"),
                id="progress-row"
            ),
            Horizontal(
                Button("<<", id="btn-prev", classes="player-btn"),
                Button(">", id="btn-play", classes="player-btn player-btn-main"),
                Button(">>", id="btn-next", classes="player-btn"),
                Button("X", id="btn-stop", classes="player-btn"),
                id="player-controls"
            ),
            id="player-container"
        )

    def _ensure_player(self) -> bool:
        """Ensure player is initialized. Returns True if ready."""
        if self.player is not None:
            return True

        try:
            from tui.audio import create_player, ASCIIVisualizer
            self.visualizer = ASCIIVisualizer()
            self.player = create_player()
            if self.player:
                self.player.on_progress = self._on_progress_callback
                self.player.on_track_end = self._on_track_end_callback
            return self.player is not None
        except Exception as e:
            self.notify(f"Audio player failed: {e}", severity="error")
            return False

    def _on_progress_callback(self, position: float, duration: float):
        """Thread-safe progress callback."""
        try:
            self.app.call_from_thread(self._update_progress, position, duration)
        except Exception:
            pass

    def _on_track_end_callback(self):
        """Thread-safe track end callback."""
        try:
            self.app.call_from_thread(self._handle_track_end)
        except Exception:
            pass

    def _update_progress(self, position: float, duration: float):
        """Update progress on main thread."""
        self.progress = position
        self.duration = duration

        try:
            if duration > 0:
                pct = (position / duration) * 100
                progress_bar = self.query_one("#progress-bar", ProgressBar)
                progress_bar.update(progress=pct)

            self.query_one("#time-current", Static).update(self._format_time(position))
            self.query_one("#time-total", Static).update(self._format_time(duration))
        except Exception:
            pass

    def _handle_track_end(self):
        """Handle track end on main thread."""
        self.is_playing = False
        self._update_play_button()

        # Auto-play next in playlist
        if self.playlist and self.playlist_index < len(self.playlist) - 1:
            self.next_track()

    def play_track(self, track: dict):
        """Play a single track."""
        if not track:
            self.notify("No track to play", severity="warning")
            return

        self.current_track = track
        self.playlist = [track]
        self.playlist_index = 0

        if not self._ensure_player():
            self.notify("Could not initialize audio player", severity="error")
            return

        self._start_playback(track)

    def load_playlist(self, tracks: list[dict]):
        """Load a playlist."""
        self.playlist = tracks
        self.playlist_index = 0

    def _start_playback(self, track: dict):
        """Start playing a track."""
        if not self.player:
            self.notify("Audio player not initialized", severity="error")
            return

        url = track.get("url", track.get("stream_url", track.get("streamUrl", "")))
        if not url:
            self.notify("No stream URL for track", severity="error")
            return

        self.is_playing = True
        self.progress = 0.0
        self.duration = track.get("duration", 0.0)

        # Update UI
        self._update_track_display()
        self._update_play_button()

        # Start playback - subprocess player doesn't block
        try:
            self.player.play(url)
            title = track.get("title", "Unknown")
            self.notify(f"Now playing: {title}", severity="information")
            self._run_visualizer()
        except Exception as e:
            self.is_playing = False
            self.notify(f"Playback failed: {e}", severity="error")

    def _update_track_display(self):
        """Update track info display."""
        if self.current_track:
            title = self.current_track.get("title", "Unknown")
            artist = self.current_track.get("artist", "")
            info = f"{artist} - {title}" if artist else title
            if len(info) > 40:
                info = info[:37] + "..."
        else:
            info = "No track"

        try:
            self.query_one("#track-info", Static).update(info)
        except Exception:
            pass

    def _update_play_button(self):
        """Update play/pause button."""
        try:
            btn = self.query_one("#btn-play", Button)
            btn.label = "||" if self.is_playing else ">"
        except Exception:
            pass

    @work(exclusive=True, group="visualizer")
    async def _run_visualizer(self):
        """Run visualizer animation."""
        if not self.visualizer:
            return

        try:
            viz_widget = self.query_one("#visualizer", Static)
        except Exception:
            return

        import random
        while self.is_playing:
            try:
                levels = [random.random() * 0.8 for _ in range(32)]
                viz_text = self.visualizer.render(levels)
                viz_widget.update(viz_text)
            except Exception:
                break

            await asyncio.sleep(0.1)

        try:
            viz_widget.update("")
        except Exception:
            pass

    def _format_time(self, seconds: float) -> str:
        """Format seconds as M:SS."""
        mins = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{mins}:{secs:02d}"

    def on_button_pressed(self, event: Button.Pressed):
        """Handle control button presses."""
        btn_id = event.button.id

        if btn_id == "btn-play":
            self.toggle_playback()
        elif btn_id == "btn-stop":
            self.stop()
        elif btn_id == "btn-prev":
            self.prev_track()
        elif btn_id == "btn-next":
            self.next_track()

    def toggle_playback(self):
        """Toggle play/pause."""
        if not self.player:
            self.notify("Type 'music' in chat to load tracks first", severity="warning")
            return

        if not self.current_track:
            self.notify("No track loaded. Type 'music' in chat.", severity="warning")
            return

        if self.is_playing:
            self.player.pause()
            self.is_playing = False
        else:
            self.player.resume()
            self.is_playing = True
            self._run_visualizer()

        self._update_play_button()

    def stop(self):
        """Stop playback."""
        if self.player:
            try:
                self.player.stop()
            except Exception:
                pass

        self.is_playing = False
        self.progress = 0.0
        self._update_play_button()

        try:
            self.query_one("#time-current", Static).update("0:00")
            progress_bar = self.query_one("#progress-bar", ProgressBar)
            progress_bar.update(progress=0)
        except Exception:
            pass

        self.add_class("--hidden")

    def next_track(self):
        """Play next track in playlist."""
        if not self.playlist:
            return

        if self.playlist_index < len(self.playlist) - 1:
            self.playlist_index += 1
            track = self.playlist[self.playlist_index]
            self.current_track = track
            self._start_playback(track)

    def prev_track(self):
        """Play previous track in playlist."""
        if not self.playlist:
            return

        if self.playlist_index > 0:
            self.playlist_index -= 1
            track = self.playlist[self.playlist_index]
            self.current_track = track
            self._start_playback(track)
