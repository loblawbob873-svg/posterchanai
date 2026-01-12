"""
Language picker screen for TUI translate commands.
"""
from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Static, Button
from textual.containers import Vertical, Grid


# Common languages for translation
LANGUAGES = [
    "English",
    "Spanish",
    "French",
    "German",
    "Italian",
    "Portuguese",
    "Russian",
    "Chinese",
    "Japanese",
    "Korean",
    "Arabic",
    "Hindi",
]


class LanguagePickerScreen(ModalScreen):
    """Modal screen for selecting a translation language."""

    CSS = """
    LanguagePickerScreen {
        align: center middle;
    }

    #language-picker-container {
        width: 50;
        height: auto;
        max-height: 20;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
    }

    #language-picker-title {
        text-align: center;
        text-style: bold;
        color: $text;
        margin-bottom: 1;
    }

    #languages-grid {
        width: 100%;
        height: auto;
        grid-size: 3;
        grid-gutter: 1;
        margin-bottom: 1;
    }

    .language-btn {
        width: 100%;
        height: 3;
        background: $primary;
        color: $text;
    }

    .language-btn:hover {
        background: $secondary;
    }

    #language-cancel-btn {
        width: 100%;
        background: $surface;
        color: $text-muted;
    }
    """

    def __init__(self, pending_command: str = ""):
        """
        Args:
            pending_command: The translate command to append language to
                             e.g. "mail translate work 123"
        """
        super().__init__()
        self.pending_command = pending_command

    def compose(self) -> ComposeResult:
        with Vertical(id="language-picker-container"):
            yield Static("Translate to:", id="language-picker-title")
            with Grid(id="languages-grid"):
                for lang in LANGUAGES:
                    btn = Button(lang, classes="language-btn")
                    btn.language = lang
                    yield btn
            yield Button("Cancel", id="language-cancel-btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button press."""
        if event.button.id == "language-cancel-btn":
            self.dismiss(None)
        elif hasattr(event.button, 'language'):
            # Return the full command with language appended
            full_command = f"{self.pending_command} {event.button.language}"
            self.dismiss(full_command)

    def on_key(self, event) -> None:
        """Handle key press."""
        if event.key == "escape":
            self.dismiss(None)
