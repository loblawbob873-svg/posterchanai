"""
News source picker screen for TUI.
"""
from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Static, Button
from textual.containers import Vertical, Horizontal, Grid
from textual.message import Message
from textual.binding import Binding


# Default news sources (matches webui)
DEFAULT_NEWS_SOURCES = [
    ("drudgereport.com", "Drudge Report"),
    ("npr.org/sections/news", "NPR"),
    ("nypost.com", "NY Post"),
    ("foxnews.com", "Fox News"),
]


class NewsPickerScreen(ModalScreen):
    """Modal screen for selecting a news source."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("j", "focus_next", "Next", show=False),
        Binding("k", "focus_prev", "Previous", show=False),
        Binding("down", "focus_next", "Next", show=False),
        Binding("up", "focus_prev", "Previous", show=False),
    ]

    CSS = """
    NewsPickerScreen {
        align: center middle;
    }

    #news-picker-container {
        width: 60;
        height: auto;
        max-height: 30;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
    }

    #news-picker-title {
        text-align: center;
        text-style: bold;
        color: $text;
        margin-bottom: 1;
    }

    #news-sources-grid {
        width: 100%;
        height: auto;
        grid-size: 2;
        grid-gutter: 1;
        margin-bottom: 1;
    }

    .news-source-btn {
        width: 100%;
        height: 3;
        background: $primary;
        color: $text;
    }

    .news-source-btn:hover {
        background: $secondary;
    }

    #news-all-btn {
        width: 100%;
        background: $accent;
        color: $text;
        margin-bottom: 1;
    }

    #news-cancel-btn {
        width: 100%;
        background: $surface;
        color: $text-muted;
    }
    """

    class NewsSourceSelected(Message):
        """Posted when a news source is selected."""
        def __init__(self, source_url: str, source_name: str):
            self.source_url = source_url
            self.source_name = source_name
            super().__init__()

    def __init__(self, sources: list[tuple[str, str]] = None):
        super().__init__()
        self.sources = sources or DEFAULT_NEWS_SOURCES

    def compose(self) -> ComposeResult:
        with Vertical(id="news-picker-container"):
            yield Static("Select News Source", id="news-picker-title")
            yield Button("All Sources (Daily News)", id="news-all-btn")
            with Grid(id="news-sources-grid"):
                for url, name in self.sources:
                    btn = Button(name, classes="news-source-btn")
                    btn.source_url = url
                    btn.source_name = name
                    yield btn
            yield Button("Cancel", id="news-cancel-btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button press."""
        if event.button.id == "news-cancel-btn":
            self.dismiss(None)
        elif event.button.id == "news-all-btn":
            # All sources - use dailynews command
            self.dismiss("dailynews")
        elif hasattr(event.button, 'source_url'):
            self.post_message(self.NewsSourceSelected(
                event.button.source_url,
                event.button.source_name
            ))
            self.dismiss(event.button.source_url)

    def on_mount(self) -> None:
        """Focus first button on mount."""
        try:
            self.query_one("#news-all-btn", Button).focus()
        except Exception:
            pass

    def action_cancel(self) -> None:
        """Cancel and close."""
        self.dismiss(None)

    def action_focus_next(self) -> None:
        """Focus next button (j/down)."""
        self.screen.focus_next()

    def action_focus_prev(self) -> None:
        """Focus previous button (k/up)."""
        self.screen.focus_previous()
