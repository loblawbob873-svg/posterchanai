"""
Language picker screen for TUI translate commands.
"""
from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Static, Button, Input
from textual.containers import Vertical


# Common languages for autocomplete suggestions
LANGUAGES = [
    "English", "Spanish", "French", "German", "Italian", "Portuguese",
    "Russian", "Chinese", "Japanese", "Korean", "Arabic", "Hindi",
    "Dutch", "Polish", "Vietnamese", "Thai", "Turkish", "Greek",
    "Swedish", "Norwegian", "Danish", "Finnish", "Czech", "Hungarian",
    "Romanian", "Ukrainian", "Indonesian", "Malay", "Tagalog", "Hebrew",
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

    #language-input {
        width: 100%;
        margin-bottom: 1;
    }

    #language-suggestions {
        width: 100%;
        height: auto;
        max-height: 8;
        margin-bottom: 1;
        color: $text-muted;
    }

    #language-buttons {
        width: 100%;
        height: auto;
        layout: horizontal;
    }

    #language-ok-btn {
        width: 50%;
        background: $primary;
    }

    #language-cancel-btn {
        width: 50%;
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
            yield Input(placeholder="Type language (e.g. Spanish, Japanese)...", id="language-input")
            yield Static("", id="language-suggestions")
            with Vertical(id="language-buttons"):
                yield Button("OK", id="language-ok-btn", variant="primary")
                yield Button("Cancel", id="language-cancel-btn")

    def on_mount(self) -> None:
        """Focus the input on mount."""
        self.query_one("#language-input", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Update suggestions as user types."""
        query = event.value.strip().lower()
        suggestions_widget = self.query_one("#language-suggestions", Static)

        if not query:
            suggestions_widget.update("")
            return

        # Find matching languages
        matches = [lang for lang in LANGUAGES if lang.lower().startswith(query)][:5]
        if matches:
            suggestions_widget.update("Suggestions: " + ", ".join(matches))
        else:
            suggestions_widget.update("")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle Enter key in input."""
        self._submit_language()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button press."""
        if event.button.id == "language-cancel-btn":
            self.dismiss(None)
        elif event.button.id == "language-ok-btn":
            self._submit_language()

    def _submit_language(self) -> None:
        """Submit the selected language."""
        lang_input = self.query_one("#language-input", Input)
        language = lang_input.value.strip()

        if not language:
            language = "English"  # Default

        # Capitalize first letter
        language = language.capitalize()

        # Return the full command with language appended
        full_command = f"{self.pending_command} {language}"
        self.dismiss(full_command)

    def on_key(self, event) -> None:
        """Handle key press."""
        if event.key == "escape":
            self.dismiss(None)
