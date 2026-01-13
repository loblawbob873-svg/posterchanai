"""
Help screen with command reference.
"""

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Static, Markdown
from textual.containers import Container, ScrollableContainer
from textual.binding import Binding


HELP_TEXT = """
# Posterchanai TUI Help

## Keyboard Navigation

### Global Navigation (Alt + Vim keys)
Works from anywhere, even while typing in input:

| Key | Action |
|-----|--------|
| `Alt+H` | Focus left panel (← Sidebar ← Chat ← Input) |
| `Alt+L` | Focus right panel (Sidebar → Chat → Input →) |
| `Alt+J` | Scroll down |
| `Alt+K` | Scroll up |
| `Alt+[` | Hide sidebar |
| `Alt+]` | Show sidebar |
| `Alt+O` | Open URLs in browser |

### Panel Switching
| Key | Action |
|-----|--------|
| `Tab` | Next panel (Sidebar → Chat → Input) |
| `Shift+Tab` | Previous panel |

### Within Panels (when focused, not typing)
| Key | Action |
|-----|--------|
| `j` / `↓` | Move down / scroll down |
| `k` / `↑` | Move up / scroll up |
| `Enter` | Select item |
| `g` | Scroll to top (chat view) |
| `G` | Scroll to bottom (chat view) |
| `d` / `x` | Delete conversation (sidebar) |

### Input
| Key | Action |
|-----|--------|
| `↑` / `↓` | Command history |
| `Tab` | Autocomplete (or switch panel if empty) |
| `Escape` | Stop AI generation |

### Quick Actions
| Key | Action |
|-----|--------|
| `Ctrl+N` | New conversation |
| `Ctrl+B` | Toggle sidebar |
| `Ctrl+S` | Settings |
| `Ctrl+H` | Help |
| `Ctrl+Q` | Quit |
| `Ctrl+U/D` | Page up / down |

### Music Controls
| Key | Action |
|-----|--------|
| `Alt+P` | Play/Pause |
| `Alt+F` | Next track |
| `Alt+R` | Previous track |
| `Alt+M` | Minimize player |

## Commands

Type these in the chat input to access features:

### Search & Images
- `/search <query>` or `/s <query>` - Web search
- `/image <query>` or `/img <query>` - Search images
- `/generate <prompt>` or `/gen <prompt>` - Generate AI image

### Mail
- `/mail` - Check unread mail
- `/mail unread` - Show unread messages
- `/mail inbox` - Show inbox
- `/mail sent` - Show sent mail
- `/mail compose` - Compose new email
- `/mail folders` - List all folders
- `/mail folder <account> <folder>` - Browse specific folder

### Calendar & Contacts
- `/cal` - Today's events
- `/cal today` - Today's schedule
- `/cal week` - This week's events
- `/cal month` - This month's calendar
- `/cal add <event>` - Add calendar event (supports recurring: "daily", "weekly Mon Wed Fri", "monthly")
- `/cal edit <uid> <field> <value>` - Edit event (fields: title, time, location, description)
- `/cal delete <uid>` - Delete calendar event
- `/cal get <uid>` - Get event details
- `/contacts` - Search contacts
- `/contacts add` - Add new contact

### Music
- `/music` - Shuffle and play all music
- `/music browse` - Browse library
- `/music search <query>` - Search songs
- `/music play <song>` - Play a song
- `/music mood <mood>` - Generate playlist by mood
- `/music stop` - Stop playback
- `/music next` - Next track
- `/music prev` - Previous track

### Other
- `/weather` - Current weather
- `/news` - Latest news
- `/torrent search <query>` - Search torrents
- `/reminder add <text>` - Add reminder
- `/note add <text>` - Add note
- `/help` - Show this help

## Tips

- Click on action buttons in responses to execute commands
- Use Tab to autocomplete commands starting with `/`
- Press Escape to stop a long AI response
- The music player appears when you play music
- Use PIM > Add Event to quickly add calendar events with a form
- Calendar events support recurring patterns like "weekly Mon Wed Fri"

## Server Connection

This TUI connects to a Posterchanai server. Make sure:
1. The server is running
2. You have valid credentials
3. The server URL is correct in settings

---

Press `Escape` to close this help screen.
"""


class HelpScreen(Screen):
    """Help screen with command reference."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
    ]

    def compose(self) -> ComposeResult:
        yield Container(
            Static("HELP", id="help-title"),
            ScrollableContainer(
                Markdown(HELP_TEXT, id="help-content"),
                id="help-scroll"
            ),
            Static("Press ESC or Q to close", id="help-footer"),
            id="help-container"
        )

    def action_close(self):
        """Close help screen."""
        self.app.pop_screen()
