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

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Ctrl+N` | New conversation |
| `Ctrl+B` | Toggle sidebar |
| `Ctrl+M` | Toggle music player |
| `Ctrl+S` | Settings |
| `Ctrl+H` | Help |
| `Ctrl+Q` | Quit |
| `Tab` | Autocomplete command |
| `Escape` | Stop AI generation |
| `Up/Down` | Command history |

## Vim Keybindings

### Navigation
| Key | Action |
|-----|--------|
| `j` | Scroll down |
| `k` | Scroll up |
| `g` | Scroll to top |
| `G` | Scroll to bottom |
| `Ctrl+D` | Page down |
| `Ctrl+U` | Page up |

### Sidebar
| Key | Action |
|-----|--------|
| `n` | Next conversation |
| `N` | Previous conversation |
| `h` | Hide sidebar |
| `l` | Show sidebar |

### Input
| Key | Action |
|-----|--------|
| `i` | Focus input (insert mode) |
| `/` | Focus input with `/` prefix (command mode) |
| `Esc` | Exit input / stop generation |

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
- `/cal add <event>` - Add calendar event
- `/contacts` - Search contacts
- `/contacts add` - Add new contact

### Music
- `/music` - Music menu
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
