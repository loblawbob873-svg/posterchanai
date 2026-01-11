# Posterchanai TUI

A cyberpunk-styled terminal client for Posterchanai AI chat.

## Features

- Full chat functionality with streaming responses
- All commands: mail, calendar, contacts, music, torrents, news, etc.
- Music player with ASCII spectrum visualizer
- Settings configuration
- Keyboard-driven interface

## Requirements

- Python 3.10+
- mpv (for audio playback): `apt install mpv` or `brew install mpv`

## Installation

```bash
cd tui

# Create isolated virtual environment
python -m venv .venv

# Activate it
source .venv/bin/activate  # Linux/macOS
# or
.venv\Scripts\activate     # Windows

# Install dependencies
pip install -r requirements.txt
```

## Usage

```bash
# Activate venv first
source .venv/bin/activate

# Run with default server (localhost:3051)
python -m tui

# Or specify server URL
python -m tui --server http://your-server:3051

# Show help
python -m tui --help
```

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
| `Up/Down` | Message history |
| `Enter` | Send message |
| `Escape` | Cancel/Stop generation |

## Vim Keybindings

The TUI supports vim-style navigation:

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

### Input Mode
| Key | Action |
|-----|--------|
| `i` | Focus input (insert mode) |
| `/` | Focus input with `/` prefix (command mode) |
| `Esc` | Exit input / stop generation |

## Configuration

On first run, you'll be prompted to log in. Credentials are stored securely using your system's keyring.

You can also set environment variables:

```bash
export POSTERCHANAI_SERVER=http://localhost:3051
export POSTERCHANAI_USER=myusername
```

## Theme

The TUI uses a cyberpunk color scheme:
- Cyan (#00ffff) - Primary/User messages
- Magenta (#ff00ff) - Secondary/Assistant messages
- Dark backgrounds for that terminal aesthetic
