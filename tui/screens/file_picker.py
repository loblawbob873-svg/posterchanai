"""
File picker screen for selecting files (e.g., mail attachments).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Callable

from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Static, Button, DirectoryTree, Input, Label
from textual.containers import Container, Horizontal, Vertical
from textual.binding import Binding


class FilteredDirectoryTree(DirectoryTree):
    """DirectoryTree that shows all files."""

    def filter_paths(self, paths):
        """Show all files and directories."""
        return paths


class FilePickerScreen(ModalScreen[Optional[str]]):
    """Modal screen for picking a file."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "select", "Select"),
    ]

    def __init__(
        self,
        start_path: str = None,
        title: str = "Select File",
        callback: Optional[Callable[[str], None]] = None,
    ):
        super().__init__()
        self.start_path = start_path or str(Path.home())
        self.title_text = title
        self.callback = callback
        self.selected_file: Optional[str] = None

    def compose(self) -> ComposeResult:
        with Container(id="file-picker-container"):
            yield Static(self.title_text, id="file-picker-title")
            yield FilteredDirectoryTree(self.start_path, id="file-tree")
            with Horizontal(id="selected-path"):
                yield Label("Selected: ")
                yield Input(placeholder="No file selected", id="path-input")
            with Horizontal(id="file-picker-buttons"):
                yield Button("Select", id="select-btn", variant="primary")
                yield Button("Cancel", id="cancel-btn")

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected):
        """Handle file selection in tree."""
        self.selected_file = str(event.path)
        path_input = self.query_one("#path-input", Input)
        path_input.value = self.selected_file

    def on_button_pressed(self, event: Button.Pressed):
        """Handle button presses."""
        if event.button.id == "select-btn":
            self.action_select()
        elif event.button.id == "cancel-btn":
            self.action_cancel()

    def action_select(self):
        """Confirm selection and close."""
        # Get path from input (user might have typed it)
        path_input = self.query_one("#path-input", Input)
        path = path_input.value.strip()

        if path and os.path.isfile(path):
            if self.callback:
                self.callback(path)
            self.dismiss(path)
        elif path:
            self.notify(f"Not a valid file: {path}", severity="error")
        else:
            self.notify("No file selected", severity="warning")

    def action_cancel(self):
        """Cancel and close."""
        self.dismiss(None)
