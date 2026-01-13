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


def escape_rich_brackets(text: str) -> str:
    """Escape square brackets to prevent Rich markup parsing errors."""
    return text.replace("[", "\\[").replace("]", "\\]")


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
        # Match both "torrents download" and "nyaa download" commands
        has_download_cmd = "torrents download" in content or "nyaa download" in content
        return has_download_cmd and content.count("[Download]") >= 1

    def _is_bt_list(self, content: str) -> bool:
        """Check if content is a bt list with action buttons."""
        # Must have torrents header AND action buttons AND numbered entries
        has_header = "**Torrents:**" in content or "Torrents:" in content
        has_buttons = "cmd:bt " in content or "cmd:torrents " in content
        # Format: **1. Name** at start of line
        has_entries = bool(re.search(r'^\*\*\d+\.\s*.+?\*\*', content, re.MULTILINE))
        return has_header and has_buttons and has_entries

    def _is_mail_list(self, content: str) -> bool:
        """Check if content is a mail list with action buttons."""
        return ("◈ INBOX ◈" in content or "◈ UNREAD ◈" in content or "◈ SEARCH" in content or ("◈" in content and "(cmd:mail " in content)) and content.count("[Read]") >= 1

    def _is_cal_list(self, content: str) -> bool:
        """Check if content is a calendar event list with action buttons."""
        import logging
        logger = logging.getLogger("tui")

        # Check for cmd:cal links (various formats)
        has_cal_buttons = (
            "cmd:cal get " in content or
            "cmd:cal delete " in content or
            "(cmd:cal get" in content or  # Without trailing space
            "(cmd:cal delete" in content
        )

        # Check for calendar indicators (time patterns or calendar emoji/header)
        has_time = bool(re.search(r'\d{1,2}:\d{2}\s*(AM|PM|am|pm)?', content))
        has_calendar_marker = "📅" in content or "⏰" in content
        has_date_header = bool(re.search(r'\*\*\[?[A-Z]{3}\]?\*\*', content))  # **[MON]** or **MON**

        # Calendar list if has cal buttons AND (time OR calendar marker OR date header)
        result = has_cal_buttons and (has_time or has_calendar_marker or has_date_header)

        logger.info(f"_is_cal_list: buttons={has_cal_buttons}, time={has_time}, marker={has_calendar_marker}, header={has_date_header}, result={result}")
        if not result and has_cal_buttons:
            logger.info(f"_is_cal_list MISS - content preview: {content[:300]}")
        return result

    def _is_todo_list(self, content: str) -> bool:
        """Check if content is a todo list with Done buttons."""
        # Todo list format: **1.** 🟢 Task name (due Jan 15) [Done](cmd:todo rm 1)
        has_todo_buttons = "cmd:todo rm" in content
        has_priority_icons = ("🟢" in content or "🟡" in content or "🔴" in content)
        has_numbered_items = bool(re.search(r'\*\*\d+\.\*\*', content))
        return has_todo_buttons and (has_priority_icons or has_numbered_items)

    def _parse_torrent_entries(self, content: str) -> list[dict]:
        """Parse torrent list content into entries with inline buttons."""
        entries = []
        lines = content.split("\n")
        current_entry = None

        # Pattern: **1. [Title](url)** or **1. Title** or 1. [Title](url) (size)
        title_pattern = re.compile(r'^\s*(?:\*\*)?(\d+)\.\s*(.+?)(?:\*\*)?$')
        # Pattern: [Download](cmd:...) - capture any download command
        download_pattern = re.compile(r'\[Download\]\(cmd:([^)]+)\)')

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
        # Wrap in try/except to fall back to plain text on errors
        try:
            if self._is_torrent_list(content):
                self._render_torrent_list(content, content_container)
                return
            elif self._is_bt_list(content):
                self._render_bt_list(content, content_container)
                return
            elif self._is_mail_list(content):
                self._render_mail_list(content, content_container)
                return
            elif self._is_cal_list(content):
                self._render_cal_list(content, content_container)
                return
            elif self._is_todo_list(content):
                self._render_todo_list(content, content_container)
                return
        except Exception as e:
            import logging
            logging.getLogger("tui").error(f"List render error: {e}")
            # Fall through to standard markdown

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

        lines = content.split("\n")

        # Track current category and entries
        current_category = None
        entries_by_category = []  # List of (category_name, entries_list)
        current_entries = []
        main_header = []
        in_header = True

        for line in lines:
            stripped = line.strip()

            # Check for category header (### CATEGORY)
            if stripped.startswith("### "):
                # Save previous category if any
                if current_category is not None:
                    entries_by_category.append((current_category, current_entries))
                    current_entries = []
                current_category = stripped[4:].strip()
                in_header = False
                continue

            # Check for main header (## ◈ TORRENTS ◈)
            if stripped.startswith("## ") and in_header:
                main_header.append(stripped)
                continue

            # Skip empty lines and "no torrents found" messages
            if not stripped or stripped.startswith("*No "):
                continue

            # Check for numbered entry
            if re.match(r'(?:\*\*)?\d+\.', stripped):
                in_header = False
                # Parse this entry
                title_match = re.match(r'^\s*(?:\*\*)?(\d+)\.\s*(.+?)(?:\*\*)?$', line)
                if title_match:
                    num = title_match.group(1)
                    title = title_match.group(2)
                    # Clean up markdown links in title
                    title = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', title)
                    current_entries.append({"num": num, "title": title, "info": "", "command": None})
            # Check for download command on subsequent line
            elif current_entries:
                dl_match = re.search(r'\[Download\]\(cmd:([^)]+)\)', line)
                if dl_match:
                    current_entries[-1]["command"] = dl_match.group(1)
                    # Extract info if present
                    info_match = re.search(r'\|\s*(S:\d+\s*L:\d+\s*\|\s*[\d.]+\s*[GMKT]iB)', line, re.I)
                    if not info_match:
                        info_match = re.search(r'\|\s*(S:\d+\s*L:\d+\s*\|\s*[\d.]+\s*[GMKT]B)', line, re.I)
                    if info_match:
                        current_entries[-1]["info"] = info_match.group(1)

        # Don't forget last category
        if current_entries:
            entries_by_category.append((current_category, current_entries))

        # Render main header
        if main_header:
            header_text = "\n".join(main_header)
            try:
                rendered = parse_markdown(header_text)
                container.mount(Static(rendered, classes="message-body"))
            except Exception:
                container.mount(Static(header_text, classes="message-body"))

        # Render each category
        for category, entries in entries_by_category:
            if category:
                # Render category header
                container.mount(Static(f"[bold cyan]{category}[/bold cyan]", classes="torrent-category"))

            for entry in entries:
                row = Horizontal(classes="torrent-row")
                title = escape_rich_brackets(entry['title'])
                info_text = f"{entry['num']}. {title}"
                if entry['info']:
                    info_text += f" | {escape_rich_brackets(entry['info'])}"

                row_text = Static(info_text, classes="torrent-text")

                if entry['command']:
                    btn = Button("DL", classes="torrent-btn")
                    btn.command = entry['command']
                    container.mount(row)
                    row.mount(row_text)
                    row.mount(btn)
                else:
                    container.mount(row)
                    row.mount(row_text)

        logger.info(f"Rendered {sum(len(e) for _, e in entries_by_category)} torrent entries in {len(entries_by_category)} categories")

    def _render_bt_list(self, content: str, container: Vertical):
        """Render bt list with inline action buttons (pause/resume/delete)."""
        import logging
        logger = logging.getLogger("tui")

        # Parse bt list entries
        lines = content.split("\n")
        current_entry = None
        entries = []

        # Pattern: **1. Torrent Name** (new format)
        title_pattern = re.compile(r'^\*\*(\d+)\.\s*(.+?)\*\*')
        # Pattern: Status: ⬇️ **DOWNLOADING** etc
        status_pattern = re.compile(r'Status:\s*([^\s]+)\s*\*\*(\w+)\*\*')
        # Pattern: [▶ Resume](cmd:torrents resume 1) or [⏸ Pause](cmd:torrents pause 1) (also bt alias)
        pause_resume_pattern = re.compile(r'\[(▶ (?:Start|Resume)|⏸ Pause)\]\(cmd:(?:bt|torrents) ((?:start|resume|pause) \d+)\)')
        # Pattern: [🗑 Remove](cmd:torrents rm 1) or [🗑 Delete](cmd:bt rm 1)
        delete_pattern = re.compile(r'\[🗑 (?:Delete|Remove)\]\(cmd:(?:bt|torrents) (rm \d+)\)')
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
                name = title_match.group(2)
                current_entry = {
                    "num": num,
                    "icon": "❓",  # Will be updated from status line
                    "name": name,
                    "status": "",
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
                # Check for status line (new format)
                status_match = status_pattern.search(line)
                if status_match:
                    current_entry["icon"] = status_match.group(1)
                    current_entry["status"] = status_match.group(2)

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
                    current_entry["toggle_cmd"] = "torrents " + pr_match.group(2)  # Ensure full command

                # Check for delete button
                del_match = delete_pattern.search(line)
                if del_match:
                    current_entry["delete_cmd"] = "torrents " + del_match.group(1)  # Ensure full command

        if current_entry:
            entries.append(current_entry)

        logger.info(f"Parsed {len(entries)} bt entries")

        # Render header
        container.mount(Static("**Torrents:**", classes="message-body"))

        # Render each entry in a compact format
        for entry in entries:
            row = Horizontal(classes="torrent-row")

            # Truncate name to fit and escape brackets
            name = entry['name']
            if len(name) > 40:
                name = name[:37] + "..."
            name = escape_rich_brackets(name)

            # Compact 2-line format:
            # 1. ⬇️ Torrent Name [████░░] 50%
            #    ↓1.2KB/s ↑0KB/s 5S/10P 1.5GB
            line1 = f"{entry['num']}. {entry['icon']} {name}"
            if entry['progress_bar']:
                # Escape progress bar brackets too
                line1 += f" \\[{entry['progress_bar'][:10]}\\] {entry['progress_pct']}%"

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

    def _render_cal_list(self, content: str, container: Vertical):
        """Render calendar event list with inline edit/delete buttons."""
        import logging
        logger = logging.getLogger("tui")

        lines = content.split("\n")
        entries = []
        current_date_header = None

        # Pattern for date headers: **[TUE]** Jan 14 or **Tuesday, January 14, 2025**
        date_header_pattern = re.compile(r'^\*\*\[?([A-Z]{3})\]?\*\*\s*(.+)$|^\*\*([A-Za-z]+,\s+.+)\*\*$')
        # Cyberpunk event: ⏰ `09:00` **Event Name** [✏️](cmd:cal get uid) [🗑️](cmd:cal delete uid)
        cyberpunk_event_pattern = re.compile(r'^\s*⏰\s*`(\d{2}:\d{2})`\s*\*\*(.+?)\*\*')
        # Regular event: - 9:00 AM - 10:00 AM: Event Name [✏️](cmd:...) [🗑️](cmd:...)
        regular_event_pattern = re.compile(r'^\s*-\s*(\d{1,2}:\d{2}\s*(?:AM|PM)?(?:\s*-\s*\d{1,2}:\d{2}\s*(?:AM|PM)?)?)\s*:\s*(.+?)(?:\s*\[✏️\]|$)')
        # Extract edit/delete commands
        edit_pattern = re.compile(r'\[✏️\]\(cmd:(cal get [^)]+)\)')
        delete_pattern = re.compile(r'\[🗑️\]\(cmd:(cal delete [^)]+)\)')
        # Location line
        location_pattern = re.compile(r'^\s*📍\s*\[(.+?)\]\(')

        current_entry = None

        for line in lines:
            # Check for date header
            date_match = date_header_pattern.match(line.strip())
            if date_match:
                if current_entry:
                    entries.append(current_entry)
                    current_entry = None
                if date_match.group(1):
                    # Cyberpunk format: [TUE] Jan 14
                    current_date_header = f"[{date_match.group(1)}] {date_match.group(2)}"
                else:
                    # Regular format
                    current_date_header = date_match.group(3)
                entries.append({"type": "header", "text": current_date_header})
                continue

            # Check for cyberpunk event
            cyber_match = cyberpunk_event_pattern.search(line)
            if cyber_match:
                if current_entry:
                    entries.append(current_entry)
                time_str = cyber_match.group(1)
                name = cyber_match.group(2)
                edit_match = edit_pattern.search(line)
                delete_match = delete_pattern.search(line)
                current_entry = {
                    "type": "event",
                    "time": time_str,
                    "name": name,
                    "location": None,
                    "edit_cmd": edit_match.group(1) if edit_match else None,
                    "delete_cmd": delete_match.group(1) if delete_match else None,
                }
                continue

            # Check for regular event
            regular_match = regular_event_pattern.search(line)
            if regular_match:
                if current_entry:
                    entries.append(current_entry)
                time_str = regular_match.group(1)
                name = regular_match.group(2).strip()
                # Clean up markdown from name
                name = re.sub(r'\s*\[✏️\].*$', '', name)
                edit_match = edit_pattern.search(line)
                delete_match = delete_pattern.search(line)
                current_entry = {
                    "type": "event",
                    "time": time_str,
                    "name": name,
                    "location": None,
                    "edit_cmd": edit_match.group(1) if edit_match else None,
                    "delete_cmd": delete_match.group(1) if delete_match else None,
                }
                continue

            # Check for location line (belongs to current entry)
            if current_entry:
                loc_match = location_pattern.search(line)
                if loc_match:
                    current_entry["location"] = loc_match.group(1)

        if current_entry:
            entries.append(current_entry)

        logger.info(f"Parsed {len([e for e in entries if e.get('type') == 'event'])} calendar entries")

        # Render header
        container.mount(Static("[bold cyan]📅 Calendar[/bold cyan]", classes="message-body"))

        # Render entries
        for entry in entries:
            if entry.get("type") == "header":
                container.mount(Static(f"[bold]{escape_rich_brackets(entry['text'])}[/bold]", classes="cal-header"))
            elif entry.get("type") == "event":
                row = Horizontal(classes="cal-row")

                # Build info text
                info = f"  {entry['time']} - {escape_rich_brackets(entry['name'])}"
                if entry.get('location'):
                    info += f"\n    📍 {escape_rich_brackets(entry['location'])}"

                row_text = Static(info, classes="cal-text")
                container.mount(row)
                row.mount(row_text)

                # Add action buttons
                if entry.get('edit_cmd'):
                    edit_btn = Button("E", classes="cal-btn")
                    edit_btn.command = entry['edit_cmd']
                    row.mount(edit_btn)

                if entry.get('delete_cmd'):
                    del_btn = Button("X", classes="cal-btn cal-btn-danger")
                    del_btn.command = entry['delete_cmd']
                    row.mount(del_btn)

    def _render_todo_list(self, content: str, container: Vertical):
        """Render todo list with inline Done buttons."""
        import logging
        logger = logging.getLogger("tui")

        lines = content.split("\n")
        entries = []

        # Pattern: **1.** 🟢 Task name (due Jan 15) [Done](cmd:todo rm 1)
        todo_pattern = re.compile(r'^\*\*(\d+)\.\*\*\s*(🟢|🟡|🔴)\s*(.+?)(?:\s*\(due ([^)]+)\))?\s*\[Done\]\(cmd:(todo rm \d+)\)')

        for line in lines:
            match = todo_pattern.search(line)
            if match:
                entries.append({
                    "num": match.group(1),
                    "priority": match.group(2),
                    "task": match.group(3).strip(),
                    "due": match.group(4),
                    "command": match.group(5),
                })

        logger.info(f"Parsed {len(entries)} todo entries")

        # Render header
        container.mount(Static("[bold cyan]📋 Todo List[/bold cyan]", classes="message-body"))

        if not entries:
            container.mount(Static("No todos found. Add one with `todo add <task>`", classes="message-body"))
            return

        # Render entries
        for entry in entries:
            row = Horizontal(classes="todo-row")

            # Build info text
            due_str = f" (due {entry['due']})" if entry.get('due') else ""
            info = f"{entry['num']}. {entry['priority']} {escape_rich_brackets(entry['task'])}{due_str}"

            row_text = Static(info, classes="todo-text")
            container.mount(row)
            row.mount(row_text)

            # Add Done button
            if entry.get('command'):
                done_btn = Button("✓", classes="todo-btn")
                done_btn.command = entry['command']
                row.mount(done_btn)

    def _render_buttons(self):
        """Render cmd: link buttons for essential actions.

        Note: This is only called when inline rendering didn't happen
        (either detection failed or inline render threw exception).
        So we don't need to re-check list type detection here.
        """
        import logging
        logger = logging.getLogger("tui")

        try:
            button_container = self.query_one("#message-buttons", Horizontal)
            button_container.remove_children()

            logger.info(f"_render_buttons: found {len(self._cmd_links)} cmd links")

            # Filter to essential action buttons
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
            if command and command.startswith(('bt ', 'mail ', 'torrents ', 'music ', 'news ', 'cal ', 'todo ')):
                logger.info(f"Posting command: {command}")
                self.post_message(self.CommandClicked(command))
                event.stop()

    def _copy_to_clipboard(self):
        """Copy message content to clipboard."""
        import subprocess
        import shutil
        import os
        from tui.utils.markdown import strip_markdown

        # Strip markdown formatting and cmd: links for clean copy
        clean_content = strip_markdown(self.content)
        content = clean_content.encode('utf-8')

        errors = []

        # Try wl-copy first (Wayland) - don't rely only on WAYLAND_DISPLAY
        wl_copy_path = shutil.which('wl-copy')
        if wl_copy_path:
            try:
                process = subprocess.Popen(
                    [wl_copy_path],
                    stdin=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env={**os.environ}  # Pass full environment including WAYLAND_DISPLAY
                )
                _, stderr = process.communicate(content)
                if process.returncode == 0:
                    self.notify("Copied to clipboard", severity="information", timeout=2)
                    return
                else:
                    errors.append(f"wl-copy failed: {stderr.decode().strip() or f'exit code {process.returncode}'}")
            except Exception as e:
                errors.append(f"wl-copy error: {e}")

        # Try xclip (X11)
        xclip_path = shutil.which('xclip')
        if xclip_path:
            try:
                process = subprocess.Popen(
                    [xclip_path, '-selection', 'clipboard'],
                    stdin=subprocess.PIPE,
                    stderr=subprocess.PIPE
                )
                _, stderr = process.communicate(content)
                if process.returncode == 0:
                    self.notify("Copied to clipboard", severity="information", timeout=2)
                    return
                else:
                    errors.append(f"xclip failed: {stderr.decode().strip() or f'exit code {process.returncode}'}")
            except Exception as e:
                errors.append(f"xclip error: {e}")

        # Try xsel (X11)
        xsel_path = shutil.which('xsel')
        if xsel_path:
            try:
                process = subprocess.Popen(
                    [xsel_path, '--clipboard', '--input'],
                    stdin=subprocess.PIPE,
                    stderr=subprocess.PIPE
                )
                _, stderr = process.communicate(content)
                if process.returncode == 0:
                    self.notify("Copied to clipboard", severity="information", timeout=2)
                    return
                else:
                    errors.append(f"xsel failed: {stderr.decode().strip() or f'exit code {process.returncode}'}")
            except Exception as e:
                errors.append(f"xsel error: {e}")

        # Try pyperclip as last resort
        try:
            import pyperclip
            pyperclip.copy(clean_content)
            self.notify("Copied to clipboard", severity="information", timeout=2)
            return
        except ImportError:
            errors.append("pyperclip not installed")
        except Exception as e:
            errors.append(f"pyperclip: {e}")

        # Show informative error
        if errors:
            logger.warning(f"Clipboard copy failed: {'; '.join(errors)}")
            self.notify(f"Copy failed: {errors[0][:50]}", severity="warning", timeout=3)
        else:
            self.notify("Install wl-copy (Wayland) or xclip (X11)", severity="warning", timeout=3)

    def finish_streaming(self):
        """Mark streaming as complete and re-render."""
        self.is_streaming = False
        self.remove_class("message-streaming")
        self.update_content(self.content)
