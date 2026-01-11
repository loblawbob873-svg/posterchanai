"""
Clickable command button widget.
"""

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Button
from textual.message import Message


class CmdButton(Widget):
    """A clickable button that executes a command."""

    class CommandTriggered(Message):
        """Posted when command button is clicked."""
        def __init__(self, command: str):
            self.command = command
            super().__init__()

    def __init__(self, label: str, command: str, **kwargs):
        super().__init__(**kwargs)
        self.label = label
        self.command = command
        self.add_class("cmd-button-container")

    def compose(self) -> ComposeResult:
        yield Button(self.label, classes="cmd-button")

    def on_button_pressed(self, event: Button.Pressed):
        """Handle button click."""
        self.post_message(self.CommandTriggered(self.command))


class CmdButtonGroup(Widget):
    """A group of command buttons displayed inline."""

    def __init__(self, buttons: list[tuple[str, str]], **kwargs):
        """
        Args:
            buttons: List of (label, command) tuples
        """
        super().__init__(**kwargs)
        self.buttons = buttons
        self.add_class("cmd-button-group")

    def compose(self) -> ComposeResult:
        for label, command in self.buttons:
            yield CmdButton(label, command)
