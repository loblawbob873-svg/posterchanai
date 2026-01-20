# PosterchanAI Sync Client

A desktop sync client for PosterchanAI that syncs local directories with remote storage. Features a cyberpunk-themed GUI with system tray integration.

## Features

- 🔄 **Bidirectional Sync**: Syncs local and remote changes automatically
- 👁️ **File Watching**: Low CPU usage file system monitoring
- 🎨 **Cyberpunk GUI**: System tray icon with modern interface
- ⚙️ **Systemd Integration**: Runs as user systemd service
- 🔔 **Conflict Detection**: Alerts user about file conflicts
- 📝 **Logging**: View sync logs from system tray
- 🚫 **Exclude Patterns**: Configure which folders/files to exclude

## Installation

Run the installer script:

```bash
cd sync_client
./install.sh
```

The installer will:
- Create virtual environment
- Install dependencies
- Set up systemd user service
- Create configuration file

## First Run Setup

On first run, a setup wizard will automatically appear prompting you for:
- **Server URL**: Your PosterchanAI server URL (e.g., `http://localhost:8000`)
- **API Key**: Your API key (get from Settings → API Keys in web UI)
- **Sync Directory**: Local directory to sync (default: `~/PosterchanAI-Sync`)

The wizard will save your configuration automatically.

## Manual Configuration

If you need to edit configuration manually, edit `~/.config/posterchanai-sync/config.json`:

```json
{
  "server_url": "http://localhost:8000",
  "api_key": "sk-your-api-key-here",
  "sync_dir": "/home/user/PosterchanAI-Sync",
  "exclude_patterns": [
    "**/.*",
    "**/__pycache__/**",
    "**/*.pyc",
    "**/node_modules/**",
    "**/.git/**"
  ],
  "poll_interval": 30,
  "conflict_resolution": "ask",
  "auto_sync": true
}
```

### Configuration Options

- `server_url`: Your PosterchanAI server URL
- `api_key`: Your API key (get from PosterchanAI web UI)
- `sync_dir`: Local directory to sync
- `exclude_patterns`: Glob patterns for files/folders to exclude
- `poll_interval`: Seconds between periodic syncs
- `conflict_resolution`: How to handle conflicts (`ask`, `local`, `remote`, `newer`)
- `auto_sync`: Automatically sync on startup

## Usage

### Start Service

```bash
systemctl --user start posterchanai-sync
```

### Enable Auto-start

```bash
systemctl --user enable posterchanai-sync
```

### View Logs

```bash
journalctl --user -u posterchanai-sync -f
```

Or use the "View Logs" option from the system tray menu.

### System Tray Menu

Right-click the system tray icon for:
- **Start Sync**: Manually trigger a sync
- **Pause/Resume Sync**: Temporarily pause syncing
- **View Logs**: Open log viewer window
- **View Conflicts**: Show unresolved file conflicts
- **Settings...**: Open setup wizard to change server URL, API key, or sync directory
- **Quit**: Stop the sync client

## Conflict Resolution

When file conflicts are detected (both local and remote changed), the client will:

1. Log the conflict
2. Show alert in system tray
3. Resolve based on `conflict_resolution` setting:
   - `ask`: User must resolve manually (shown in conflicts list)
   - `local`: Always use local version
   - `remote`: Always use remote version
   - `newer`: Use the file with newer modification time

## Troubleshooting

### Service won't start

Check logs:
```bash
journalctl --user -u posterchanai-sync
```

Common issues:
- Invalid `server_url` or `api_key` in config
- `sync_dir` doesn't exist or isn't writable
- Missing dependencies (re-run installer)

### Files not syncing

- Check API key is valid
- Verify server is accessible
- Check exclude patterns aren't too broad
- View logs for errors

### High CPU usage

- Increase `poll_interval` in config
- Add more exclude patterns
- Check for very large directories

## Uninstallation

```bash
systemctl --user stop posterchanai-sync
systemctl --user disable posterchanai-sync
rm -rf ~/.local/share/posterchanai-sync
rm -rf ~/.config/posterchanai-sync
rm ~/.local/bin/posterchanai-sync
rm ~/.config/systemd/user/posterchanai-sync.service
systemctl --user daemon-reload
```
