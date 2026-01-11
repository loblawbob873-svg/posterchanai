"""
Music player widget with ASCII visualizer.
"""

import asyncio
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static, Button, ProgressBar
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual import work

from tui.audio import AudioPlayer, create_player, ASCIIVisualizer


class MusicPlayerWidget(Widget):
    """Music player with controls and visualizer."""

    is_playing = reactive(False)
    current_track = reactive(None)
    progress = reactive(0.0)
    duration = reactive(0.0)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.player: AudioPlayer | None = None
        self.visualizer = ASCIIVisualizer()
        self.playlist: list[dict] = []
        self.playlist_index = 0
        self.add_class("--hidden")  # Start hidden

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("MUSIC PLAYER", id="player-title"),
            Horizontal(
                Static("", id="track-info"),
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
                Button("||", id="btn-play", classes="player-btn player-btn-main"),
                Button(">>", id="btn-next", classes="player-btn"),
                Button("X", id="btn-stop", classes="player-btn"),
                id="player-controls"
            ),
            id="player-container"
        )

    async def on_mount(self):
        """Initialize player on mount."""
        try:
            self.player = create_player()
            self.player.on_progress = self.handle_progress
            self.player.on_track_end = self.handle_track_end
        except Exception as e:
            self.notify(f"Audio player not available: {e}", severity="warning")

    def play_track(self, track: dict):
        """Play a single track."""
        if not self.player:
            self.notify("Audio player not available", severity="error")
            return

        self.current_track = track
        self.playlist = [track]
        self.playlist_index = 0
        self._start_playback(track)

    def load_playlist(self, tracks: list[dict]):
        """Load a playlist."""
        self.playlist = tracks
        self.playlist_index = 0

    def _start_playback(self, track: dict):
        """Start playing a track."""
        if not self.player:
            return

        url = track.get("url", track.get("stream_url", ""))
        if not url:
            self.notify("No URL for track", severity="error")
            return

        self.is_playing = True
        self.progress = 0.0
        self.duration = track.get("duration", 0.0)

        # Update UI
        self._update_track_display()
        self._update_play_button()

        # Start playback
        self.player.play(url)
        self._start_visualizer()

    def _update_track_display(self):
        """Update track info display."""
        if self.current_track:
            title = self.current_track.get("title", "Unknown")
            artist = self.current_track.get("artist", "")
            info = f"{artist} - {title}" if artist else title
            # Truncate if too long
            if len(info) > 40:
                info = info[:37] + "..."
        else:
            info = "No track"

        self.query_one("#track-info", Static).update(info)

    def _update_play_button(self):
        """Update play/pause button."""
        btn = self.query_one("#btn-play", Button)
        btn.label = "||" if self.is_playing else ">"

    @work(exclusive=True)
    async def _start_visualizer(self):
        """Run visualizer animation."""
        viz_widget = self.query_one("#visualizer", Static)

        while self.is_playing:
            # Get audio levels if available
            if self.player and hasattr(self.player, 'get_levels'):
                levels = self.player.get_levels()
            else:
                # Generate fake levels for visual effect
                import random
                levels = [random.random() * 0.8 for _ in range(32)]

            # Render visualizer
            viz_text = self.visualizer.render(levels)
            viz_widget.update(viz_text)

            await asyncio.sleep(0.05)  # ~20fps

        viz_widget.update("")

    def handle_progress(self, position: float, duration: float):
        """Handle playback progress update."""
        self.progress = position
        self.duration = duration

        # Update progress bar
        if duration > 0:
            pct = (position / duration) * 100
            progress_bar = self.query_one("#progress-bar", ProgressBar)
            progress_bar.update(progress=pct)

        # Update time displays
        self.query_one("#time-current", Static).update(self._format_time(position))
        self.query_one("#time-total", Static).update(self._format_time(duration))

    def handle_track_end(self):
        """Handle track ending."""
        self.is_playing = False
        self._update_play_button()

        # Auto-play next in playlist
        if self.playlist and self.playlist_index < len(self.playlist) - 1:
            self.next_track()

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
            return

        if self.is_playing:
            self.player.pause()
            self.is_playing = False
        else:
            if self.current_track:
                self.player.resume()
                self.is_playing = True
                self._start_visualizer()

        self._update_play_button()

    def stop(self):
        """Stop playback."""
        if self.player:
            self.player.stop()

        self.is_playing = False
        self.progress = 0.0
        self._update_play_button()

        # Reset displays
        self.query_one("#time-current", Static).update("0:00")
        progress_bar = self.query_one("#progress-bar", ProgressBar)
        progress_bar.update(progress=0)

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
