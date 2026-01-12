"""
News source picker screen for TUI.
"""
from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Static, Button
from textual.containers import Vertical, Horizontal, Grid
from textual.message import Message


# Default news sources (matches webui)
DEFAULT_NEWS_SOURCES = [
    ("drudgereport.com", "Drudge Report"),
    ("npr.org/sections/news", "NPR"),
    ("nypost.com", "NY Post"),
    ("foxnews.com", "Fox News"),
]


class NewsPickerScreen(ModalScreen):
    """Modal screen for selecting a news source."""

    CSS = """
    NewsPickerScreen {
        align: center middle;
    }

    #news-picker-container {
        width: 50;
        height: auto;
        max-height: 20;
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
        elif hasattr(event.button, 'source_url'):
            self.post_message(self.NewsSourceSelected(
                event.button.source_url,
                event.button.source_name
            ))
            self.dismiss(event.button.source_url)

    def on_key(self, event) -> None:
        """Handle key press."""
        if event.key == "escape":
            self.dismiss(None)
