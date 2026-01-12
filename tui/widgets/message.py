"""
Message widget for displaying chat messages.
"""
from __future__ import annotations

import re
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static, Button
from textual.containers import Vertical, Horizontal
from textual.message import Message as TextualMessage
from rich.text import Text

from tui.utils.markdown import parse_markdown, parse_cmd_links


class MessageWidget(Widget):
    """Widget displaying a single chat message."""

    class CommandClicked(TextualMessage, bubble=True):
        """Posted when a command button is clicked."""
        def __init__(self, command: str):
            self.command = command
            super().__init__()

    def __init__(
        self,
        role: str,
        content: str,
        message_id: int | None = None,
        is_streaming: bool = False,
    ):
        super().__init__()
        self.role = role
        self.content = content
        self.message_id = message_id
        self.is_streaming = is_streaming
        self._cmd_links = []

        # Set class based on role
        self.add_class(f"message-{role}")
        if is_streaming:
            self.add_class("message-streaming")

    def compose(self) -> ComposeResult:
        role_label = self.get_role_label()
        yield Vertical(
            Horizontal(
                Static(role_label, classes="message-role"),
                Button("Copy", id="copy-btn", classes="copy-btn"),
                classes="message-header"
            ),
            Vertical(id="message-content-container"),
            Horizontal(id="message-buttons"),
            classes="message-inner"
        )

    def on_mount(self):
        """Render content on mount."""
        self.update_content(self.content)

    def get_role_label(self) -> str:
        """Get display label for role."""
        labels = {
            "user": "YOU",
            "assistant": "AI",
            "system": "SYSTEM",
        }
        return labels.get(self.role, self.role.upper())

    def _is_torrent_list(self, content: str) -> bool:
        """Check if content is a torrent list with download commands."""
        return "torrents download" in content and content.count("[Download]") >= 2

    def _parse_torrent_entries(self, content: str) -> list[dict]:
        """Parse torrent list content into entries with inline buttons."""
        entries = []
        lines = content.split("\n")
        current_entry = None

        # Pattern: **1. [Title](url)** or **1. Title**
        title_pattern = re.compile(r'\*\*(\d+)\.\s*(.+?)\*\*')
        # Pattern: [Download](cmd:torrents download category num)
        download_pattern = re.compile(r'\[Download\]\(cmd:(torrents download [^)]+)\)')

        for line in lines:
            title_match = title_pattern.search(line)
            if title_match:
                # Save previous entry
                if current_entry:
                    entries.append(current_entry)
                # Start new entry
                num = title_match.group(1)
                title = title_match.group(2)
                # Clean up markdown links in title
                title = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', title)
                current_entry = {"num": num, "title": title, "info": "", "command": None}
            elif current_entry:
                # Check for download command
                dl_match = download_pattern.search(line)
                if dl_match:
                    current_entry["command"] = dl_match.group(1)
                    # Extract info (S:X L:Y | size)
                    info_match = re.search(r'\|\s*(S:\d+\s*L:\d+\s*\|\s*[\d.]+\s*[GMKT]B)', line, re.I)
                    if info_match:
                        current_entry["info"] = info_match.group(1)

        # Don't forget last entry
        if current_entry:
            entries.append(current_entry)

        return entries

    def update_content(self, content: str):
        """Update message content."""
        self.content = content
        content_container = self.query_one("#message-content-container", Vertical)
        content_container.remove_children()

        # For streaming, show raw text for speed
        if self.is_streaming:
            content_container.mount(Static(content + " _", classes="message-body"))
            return

        # Parse and extract cmd links
        self._cmd_links = parse_cmd_links(content)

        # Check if this is a torrent list - render with inline buttons
        if self._is_torrent_list(content):
            self._render_torrent_list(content, content_container)
        else:
            # Standard markdown rendering
            try:
                rendered = parse_markdown(content)
                content_container.mount(Static(rendered, classes="message-body"))
            except Exception:
                from tui.utils.markdown import strip_markdown
                content_container.mount(Static(strip_markdown(content), classes="message-body"))

            # Render action buttons at bottom
            self._render_buttons()

    def _render_torrent_list(self, content: str, container: Vertical):
        """Render torrent list with inline download buttons."""
        import logging
        logger = logging.getLogger("tui")

        # Extract header (first lines before entries)
        lines = content.split("\n")
        header_lines = []
        for line in lines:
            if re.match(r'\*\*\d+\.', line):
                break
            if line.strip():
                header_lines.append(line)

        # Render header
        if header_lines:
            header_text = "\n".join(header_lines)
            try:
                rendered_header = parse_markdown(header_text)
                container.mount(Static(rendered_header, classes="message-body"))
            except Exception:
                container.mount(Static(header_text, classes="message-body"))

        # Parse and render torrent entries
        entries = self._parse_torrent_entries(content)
        logger.info(f"Parsed {len(entries)} torrent entries")

        for entry in entries:
            # Create row with text + button
            row = Horizontal(classes="torrent-row")

            # Torrent info text
            info_text = f"{entry['num']}. {entry['title']}"
            if entry['info']:
                info_text += f" | {entry['info']}"

            row_text = Static(info_text, classes="torrent-text")

            # Download button
            if entry['command']:
                btn = Button("DL", classes="torrent-btn")
                btn.command = entry['command']
                container.mount(row)
                row.mount(row_text)
                row.mount(btn)
            else:
                container.mount(row)
                row.mount(row_text)

    def _render_buttons(self):
        """Render cmd: link buttons for essential actions."""
        import logging
        logger = logging.getLogger("tui")

        try:
            button_container = self.query_one("#message-buttons", Horizontal)
            button_container.remove_children()

            # Skip if already rendered inline (torrent list)
            if self._is_torrent_list(self.content):
                return

            logger.info(f"_render_buttons: found {len(self._cmd_links)} cmd links")

            # Filter to essential action buttons (exclude torrents download - handled inline)
            essential_prefixes = ("mail ", "cal ", "todo ", "news ", "miniflux ", "nyaa ", "music ")
            actionable = [
                (label, cmd) for label, cmd, _, _ in self._cmd_links
                if any(cmd.startswith(p) for p in essential_prefixes)
            ]

            logger.info(f"Actionable buttons: {actionable}")

            if actionable:
                buttons_to_mount = []
                for label, command in actionable[:6]:  # Max 6 buttons
                    btn = Button(label, classes="cmd-button")
                    btn.command = command
                    buttons_to_mount.append(btn)
                    logger.info(f"Creating button: {label} -> {command}")

                if buttons_to_mount:
                    button_container.mount_all(buttons_to_mount)
                    logger.info(f"Mounted {len(buttons_to_mount)} buttons")
        except Exception as e:
            logger.error(f"Button render error: {e}")

    def on_button_pressed(self, event: Button.Pressed):
        """Handle action button clicks."""
        button = event.button
        if button.id == "copy-btn":
            self._copy_to_clipboard()
            event.stop()
        elif hasattr(button, 'command') and button.command:
            # Post command to be handled by main screen
            self.post_message(self.CommandClicked(button.command))
            event.stop()

    def _copy_to_clipboard(self):
        """Copy message content to clipboard."""
        try:
            import pyperclip
            pyperclip.copy(self.content)
            self.notify("Copied to clipboard", severity="information", timeout=2)
        except ImportError:
            # Fallback to xclip/xsel on Linux
            import subprocess
            try:
                process = subprocess.Popen(['xclip', '-selection', 'clipboard'], stdin=subprocess.PIPE)
                process.communicate(self.content.encode('utf-8'))
                self.notify("Copied to clipboard", severity="information", timeout=2)
            except FileNotFoundError:
                try:
                    process = subprocess.Popen(['xsel', '--clipboard', '--input'], stdin=subprocess.PIPE)
                    process.communicate(self.content.encode('utf-8'))
                    self.notify("Copied to clipboard", severity="information", timeout=2)
                except FileNotFoundError:
                    self.notify("Install xclip or pyperclip to copy", severity="warning", timeout=3)

    def finish_streaming(self):
        """Mark streaming as complete and re-render."""
        self.is_streaming = False
        self.remove_class("message-streaming")
        self.update_content(self.content)
