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

    def _is_bt_list(self, content: str) -> bool:
        """Check if content is a bt list with action buttons."""
        return "**Torrents:**" in content and ("cmd:bt " in content or "cmd:torrents " in content)

    def _is_mail_list(self, content: str) -> bool:
        """Check if content is a mail list with action buttons."""
        return ("◈ INBOX ◈" in content or "◈ UNREAD ◈" in content or "◈ SEARCH" in content or ("◈" in content and "(cmd:mail " in content)) and content.count("[Read]") >= 1

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
        elif self._is_bt_list(content):
            self._render_bt_list(content, content_container)
        elif self._is_mail_list(content):
            self._render_mail_list(content, content_container)
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

    def _render_bt_list(self, content: str, container: Vertical):
        """Render bt list with inline action buttons (pause/resume/delete)."""
        import logging
        logger = logging.getLogger("tui")

        # Parse bt list entries
        lines = content.split("\n")
        current_entry = None
        entries = []

        # Pattern: 1. ⬇️ **Torrent Name**
        title_pattern = re.compile(r'^(\d+)\.\s*([^\s]+)\s*\*\*(.+?)\*\*')
        # Pattern: [▶ Start](cmd:bt start 1) or [▶ Resume](cmd:bt resume 1) or [⏸ Pause](cmd:bt pause 1)
        pause_resume_pattern = re.compile(r'\[(▶ (?:Start|Resume)|⏸ Pause)\]\(cmd:(bt (?:start|resume|pause) \d+)\)')
        # Pattern: [🗑 Delete](cmd:bt rm 1)
        delete_pattern = re.compile(r'\[🗑 Delete\]\(cmd:(bt rm \d+)\)')
        # Progress line: [██████████] 100.0% | 1.5 GB
        progress_pattern = re.compile(r'\[([█░]+)\]\s*([\d.]+)%\s*\|\s*(.+)')
        # Stats line: ↓1.2 KB/s ↑0.5 KB/s | 5S/10P
        stats_pattern = re.compile(r'↓([\d.]+\s*[KMG]?B?/s|-)\s*↑([\d.]+\s*[KMG]?B?/s|-)\s*\|\s*(\d+)S/(\d+)P')

        for line in lines:
            title_match = title_pattern.match(line.strip())
            if title_match:
                if current_entry:
                    entries.append(current_entry)
                num = title_match.group(1)
                icon = title_match.group(2)
                name = title_match.group(3)
                current_entry = {
                    "num": num,
                    "icon": icon,
                    "name": name,
                    "progress_bar": "",
                    "progress_pct": "",
                    "size": "",
                    "down": "-",
                    "up": "-",
                    "seeds": "0",
                    "peers": "0",
                    "toggle_cmd": None,
                    "toggle_label": None,
                    "delete_cmd": None,
                }
            elif current_entry:
                # Check for progress
                progress_match = progress_pattern.search(line)
                if progress_match:
                    current_entry["progress_bar"] = progress_match.group(1)
                    current_entry["progress_pct"] = progress_match.group(2)
                    current_entry["size"] = progress_match.group(3).strip()

                # Check for stats (↓ ↑ | S/P)
                stats_match = stats_pattern.search(line)
                if stats_match:
                    current_entry["down"] = stats_match.group(1)
                    current_entry["up"] = stats_match.group(2)
                    current_entry["seeds"] = stats_match.group(3)
                    current_entry["peers"] = stats_match.group(4)

                # Check for pause/resume button
                pr_match = pause_resume_pattern.search(line)
                if pr_match:
                    current_entry["toggle_label"] = pr_match.group(1)
                    current_entry["toggle_cmd"] = pr_match.group(2)

                # Check for delete button
                del_match = delete_pattern.search(line)
                if del_match:
                    current_entry["delete_cmd"] = del_match.group(1)

        if current_entry:
            entries.append(current_entry)

        logger.info(f"Parsed {len(entries)} bt entries")

        # Render header
        container.mount(Static("**Torrents:**", classes="message-body"))

        # Render each entry in a compact format
        for entry in entries:
            row = Horizontal(classes="torrent-row")

            # Truncate name to fit
            name = entry['name']
            if len(name) > 40:
                name = name[:37] + "..."

            # Compact 2-line format:
            # 1. ⬇️ Torrent Name [████░░] 50%
            #    ↓1.2KB/s ↑0KB/s 5S/10P 1.5GB
            line1 = f"{entry['num']}. {entry['icon']} {name}"
            if entry['progress_bar']:
                line1 += f" [{entry['progress_bar'][:10]}] {entry['progress_pct']}%"

            line2 = f"   ↓{entry['down']} ↑{entry['up']} {entry['seeds']}S/{entry['peers']}P"
            if entry['size']:
                line2 += f" {entry['size']}"

            info = f"{line1}\n{line2}"

            row_text = Static(info, classes="torrent-text")
            container.mount(row)
            row.mount(row_text)

            # Add action buttons with simple ASCII labels
            if entry['toggle_cmd']:
                # Show > (play) or || (pause) based on current state
                is_start = "Start" in (entry['toggle_label'] or "") or "Resume" in (entry['toggle_label'] or "")
                label = ">" if is_start else "||"
                toggle_btn = Button(label, classes="torrent-btn", name=entry['toggle_cmd'])
                toggle_btn.command = entry['toggle_cmd']
                row.mount(toggle_btn)

            if entry['delete_cmd']:
                del_btn = Button("X", classes="torrent-btn torrent-btn-danger", name=entry['delete_cmd'])
                del_btn.command = entry['delete_cmd']
                row.mount(del_btn)

    def _render_mail_list(self, content: str, container: Vertical):
        """Render mail list with inline action buttons per message."""
        import logging
        logger = logging.getLogger("tui")

        lines = content.split("\n")
        entries = []
        current_entry = None

        # Pattern: 1. **[NEW]** **sender** - subject  OR  1. **sender** - subject
        entry_pattern = re.compile(r'^(\d+)\.\s*(?:\*\*\[NEW\]\*\*\s*)?\*\*(.+?)\*\*\s*-\s*(.+)$')
        # Pattern: [Read](cmd:mail read ...) | [Reply All](cmd:mail reply ...) ...
        read_pattern = re.compile(r'\[Read\]\(cmd:(mail read [^)]+)\)')
        reply_pattern = re.compile(r'\[Reply All\]\(cmd:(mail reply [^)]+)\)')
        archive_pattern = re.compile(r'\[Archive\]\(cmd:(mail archive [^)]+)\)')
        delete_pattern = re.compile(r'\[Delete\]\(cmd:(mail delete [^)]+)\)')

        for line in lines:
            entry_match = entry_pattern.match(line.strip())
            if entry_match:
                if current_entry:
                    entries.append(current_entry)
                num = entry_match.group(1)
                sender = entry_match.group(2)
                subject = entry_match.group(3)
                is_new = "**[NEW]**" in line
                current_entry = {
                    "num": num,
                    "sender": sender,
                    "subject": subject,
                    "is_new": is_new,
                    "info": "",
                    "read_cmd": None,
                    "reply_cmd": None,
                    "archive_cmd": None,
                    "delete_cmd": None,
                }
            elif current_entry:
                # Check for date/account info line
                if "|" in line and not "[" in line:
                    current_entry["info"] = line.strip()

                # Check for action buttons
                read_match = read_pattern.search(line)
                if read_match:
                    current_entry["read_cmd"] = read_match.group(1)

                reply_match = reply_pattern.search(line)
                if reply_match:
                    current_entry["reply_cmd"] = reply_match.group(1)

                archive_match = archive_pattern.search(line)
                if archive_match:
                    current_entry["archive_cmd"] = archive_match.group(1)

                delete_match = delete_pattern.search(line)
                if delete_match:
                    current_entry["delete_cmd"] = delete_match.group(1)

        if current_entry:
            entries.append(current_entry)

        logger.info(f"Parsed {len(entries)} mail entries")

        # Render header
        header_match = re.search(r'## ◈ (.+?) ◈', content)
        if header_match:
            container.mount(Static(f"[bold cyan]{header_match.group(1)}[/bold cyan]", classes="message-body"))

        # Render each entry
        for entry in entries:
            row = Horizontal(classes="mail-row")

            # Build info text
            new_marker = "[NEW] " if entry['is_new'] else ""
            info = f"{entry['num']}. {new_marker}{entry['sender']} - {entry['subject']}"
            if entry['info']:
                info += f"\n   {entry['info']}"

            row_text = Static(info, classes="mail-text")
            container.mount(row)
            row.mount(row_text)

            # Add action buttons
            if entry['read_cmd']:
                read_btn = Button("R", classes="mail-btn")
                read_btn.command = entry['read_cmd']
                row.mount(read_btn)

            if entry['reply_cmd']:
                reply_btn = Button("↩", classes="mail-btn")
                reply_btn.command = entry['reply_cmd']
                row.mount(reply_btn)

            if entry['archive_cmd']:
                archive_btn = Button("A", classes="mail-btn")
                archive_btn.command = entry['archive_cmd']
                row.mount(archive_btn)

            if entry['delete_cmd']:
                del_btn = Button("X", classes="mail-btn mail-btn-danger")
                del_btn.command = entry['delete_cmd']
                row.mount(del_btn)

    def _render_buttons(self):
        """Render cmd: link buttons for essential actions."""
        import logging
        logger = logging.getLogger("tui")

        try:
            button_container = self.query_one("#message-buttons", Horizontal)
            button_container.remove_children()

            # Skip if already rendered inline (torrent list, bt list, or mail list)
            if self._is_torrent_list(self.content) or self._is_bt_list(self.content) or self._is_mail_list(self.content):
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
        import logging
        logger = logging.getLogger("tui")

        button = event.button
        logger.info(f"Button pressed: id={button.id}, name={button.name}, has_command={hasattr(button, 'command')}")

        if button.id == "copy-btn":
            self._copy_to_clipboard()
            event.stop()
        else:
            # Check for command in both attribute and name
            command = getattr(button, 'command', None) or button.name
            if command and command.startswith(('bt ', 'mail ', 'torrents ', 'music ', 'news ')):
                logger.info(f"Posting command: {command}")
                self.post_message(self.CommandClicked(command))
                event.stop()

    def _copy_to_clipboard(self):
        """Copy message content to clipboard."""
        import subprocess
        import os

        content = self.content.encode('utf-8')

        # Try wl-copy first (Wayland)
        if os.environ.get('WAYLAND_DISPLAY'):
            try:
                process = subprocess.Popen(['wl-copy'], stdin=subprocess.PIPE)
                process.communicate(content)
                if process.returncode == 0:
                    self.notify("Copied to clipboard", severity="information", timeout=2)
                    return
            except FileNotFoundError:
                pass

        # Try xclip (X11)
        try:
            process = subprocess.Popen(['xclip', '-selection', 'clipboard'], stdin=subprocess.PIPE)
            process.communicate(content)
            if process.returncode == 0:
                self.notify("Copied to clipboard", severity="information", timeout=2)
                return
        except FileNotFoundError:
            pass

        # Try xsel (X11)
        try:
            process = subprocess.Popen(['xsel', '--clipboard', '--input'], stdin=subprocess.PIPE)
            process.communicate(content)
            if process.returncode == 0:
                self.notify("Copied to clipboard", severity="information", timeout=2)
                return
        except FileNotFoundError:
            pass

        # Try pyperclip as last resort
        try:
            import pyperclip
            pyperclip.copy(self.content)
            self.notify("Copied to clipboard", severity="information", timeout=2)
        except (ImportError, Exception):
            self.notify("Install wl-copy (Wayland) or xclip (X11)", severity="warning", timeout=3)

    def finish_streaming(self):
        """Mark streaming as complete and re-render."""
        self.is_streaming = False
        self.remove_class("message-streaming")
        self.update_content(self.content)
