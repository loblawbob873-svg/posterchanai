# PosterchanAI WebDAV Sync Client

A pure Python daemon that syncs your PosterchanAI storage to a local directory using WebDAV. Features bidirectional sync with automatic change detection, delete handling, and conflict resolution.

## Features

- 🔄 **Bidirectional Sync**: Automatic two-way synchronization between local and remote
- 📝 **Local Change Detection**: Automatically detects and uploads locally modified files
- 🗑️ **Delete Detection**: Detects and syncs deletions in both directions
- 📦 **Move/Rename Detection**: Detects moved/renamed files using content hashing
- ⚔️ **Conflict Resolution**: Configurable conflict resolution strategies
- 💾 **Offline Caching**: Cache files locally for offline access (200GB default)
- 🌐 **Network Detection**: Checks network connectivity before attempting sync
- 💤 **Suspend/Resume Handling**: Automatically detects and handles system hibernation
- 📊 **Health Monitoring**: Periodically checks sync health and fixes issues automatically
- ⏱️ **Exponential Backoff**: Smart retry intervals to avoid overwhelming the server
- ⚙️ **Systemd Integration**: Runs as user systemd service
- 🔐 **Secure**: Uses your PosterchanAI account credentials
- 🚀 **Fast**: Optimized sync with minimal overhead

## Prerequisites

**No OS packages required!** This is a pure Python implementation.

**Required:**
- Python 3.8 or later
- Internet connection for initial setup

**Optional:**
- systemd (for service management)

All Python dependencies are installed automatically by the installer:
- `requests` - for WebDAV communication
- That's it! No FUSE, no system packages needed.

## Installation

Run the installer script:

```bash
cd sync_client
./install.sh
```

The installer will:
- Create virtual environment
- Install Python dependencies
- Set up systemd user service
- Create configuration directory

## First Run Setup

On first run, a setup wizard will automatically appear prompting you for:
- **Server URL**: Your PosterchanAI server URL (e.g., `https://ai.poster.place`)
- **Username**: Your PosterchanAI username (usually your email address)
- **Password**: Your PosterchanAI account password
- **Mount Point**: Local directory where files will be synced (default: `~/PosterchanAI-Mount`)

The wizard will save your configuration automatically. You can re-run the wizard anytime with:
```bash
posterchanai-webdav-mount --setup
```

## Manual Configuration

If you need to edit configuration manually, edit `~/.config/posterchanai-sync/config.json`:

```json
{
  "server_url": "https://ai.poster.place",
  "username": "your-email@example.com",
  "password": "your-account-password",
  "mount_point": "/home/user/PosterchanAI-Mount",
  "sync_interval": 10,
  "cache_max_size_mb": 204800,
  "cache_max_age_days": 30,
  "conflict_resolution": "last_write_wins",
  "offline_mode": false
}
```

### Configuration Options

- `server_url`: Your PosterchanAI server URL (required)
- `username`: Your PosterchanAI username/email (required)
- `password`: Your PosterchanAI account password (required)
- `mount_point`: Local directory where files will be synced (default: `~/PosterchanAI-Mount`)
- `sync_interval`: Sync interval in seconds (default: `10`)
- `cache_max_size_mb`: Maximum cache size in MB (default: `204800` = 200GB)
- `cache_max_age_days`: Maximum age of cached files in days (default: `30`)
- `conflict_resolution`: Conflict resolution strategy (default: `last_write_wins`)
  - `last_write_wins`: Use file with newer modification time
  - `local_wins`: Always use local version
  - `remote_wins`: Always use remote version
  - `manual`: Create `.conflict` file and use remote version
- `offline_mode`: If true, only use cache, don't try to sync (default: `false`)

## Usage

### Start Service

```bash
systemctl --user start posterchanai-sync
```

### Enable Auto-start

```bash
systemctl --user enable posterchanai-sync
```

### Check Sync Status

```bash
posterchanai-webdav-mount --status
```

This will show:
- Whether the sync daemon is running
- Last sync time
- Number of files synced
- Cache status

### View Logs

```bash
journalctl --user -u posterchanai-sync -f
```

### Run Setup Wizard

```bash
posterchanai-webdav-mount --setup
```

## How It Works

The daemon uses pure Python to sync your WebDAV storage to a local directory. It performs bidirectional synchronization:

1. **Remote → Local**: Downloads new/updated files from server
2. **Local → Remote**: Uploads locally modified files to server
3. **Delete Detection**: Removes files deleted on either side
4. **Move/Rename Detection**: Detects moved/renamed files using content hashing
5. **Conflict Resolution**: Handles conflicts when both sides were modified

Files appear in the local directory and are automatically synced with the server. You can:

- Access files normally via the mount point
- Edit files directly (changes sync automatically)
- Use any file manager or application
- No need to manually sync - it's fully automatic!

### Offline Caching

The daemon includes comprehensive offline caching:

1. **Automatic Caching**: Files are automatically cached when accessed
2. **Offline Access**: Cached files are available even when offline
3. **Change Tracking**: Local changes are tracked and synced when online
4. **Smart Sync**: Pending changes are automatically synced when network reconnects
5. **Cache Management**: Old files are automatically cleaned up based on age and size limits

### Intelligent Monitoring

The daemon includes intelligent monitoring that:

1. **Network Detection**: Before attempting to remount, it checks:
   - Network interfaces are up
   - DNS resolution works
   - WebDAV server is reachable
   - Only attempts remount when network is available

2. **Suspend/Resume Detection**: Automatically detects when your system:
   - Resumes from hibernation/suspend
   - Immediately checks mount status and remounts if needed

3. **Health Checks**: Periodically verifies the mount is actually working:
   - Tests if mount point is accessible
   - Detects stale mounts
   - Automatically fixes issues by unmounting and remounting

4. **Exponential Backoff**: When remount fails:
   - Starts with 5 second retry interval
   - Doubles on each failure (5s, 10s, 20s, 40s, 80s, 160s, 300s max)
   - Resets to 5 seconds on success
   - Prevents overwhelming the server with rapid retries

5. **Automatic Sync**: When network reconnects:
   - Detects pending changes in cache
   - Automatically syncs all modified files
   - Logs sync progress and results

## Troubleshooting

### Python dependencies

If you see import errors, re-run the installer:
```bash
cd sync_client
./install.sh
```

The installer will automatically install all required Python packages.

### Installation issues

If the installer fails:
1. Ensure Python 3.8+ is installed: `python3 --version`
2. Check you have write permissions to `~/.local` and `~/.config`
3. Run installer with verbose output: `bash -x install.sh`

### Service won't start

Check logs:
```bash
journalctl --user -u posterchanai-sync
```

Common issues:
- Invalid `server_url`, `username`, or `password` in config
- `mount_point` doesn't exist or isn't writable
- Missing Python dependencies (re-run installer: `./install.sh`)
- Network connectivity issues (check server URL is reachable)

### Sync not working

- Check sync status: `posterchanai-webdav-mount --status`
- Check if WebDAV server is running on your PosterchanAI server
- Verify server URL and credentials are correct
- Check logs for errors: `journalctl --user -u posterchanai-sync -f`
- Ensure network connectivity to server

### High CPU usage

This shouldn't happen - the sync client is optimized for efficiency. If you see high CPU:

- Check if sync is stuck in a loop (check logs)
- Verify server is accessible
- Check network connectivity
- Reduce sync interval in config if needed (default: 10 seconds)
- The sync uses exponential backoff to prevent rapid retries

### Sync not resuming after network disconnect

The daemon should automatically detect network disconnects and resume sync. If it's not working:

- Check logs: `journalctl --user -u posterchanai-sync -f`
- Verify network detection is working (should see "Network not available" messages)
- Restart the service: `systemctl --user restart posterchanai-sync`

### Sync not working after hibernation

The daemon should automatically detect resume from hibernation. If it's not working:

- Check logs for "Suspend/resume detected" messages
- The daemon checks for time jumps > 5 minutes to detect suspend/resume
- Try manually checking status: `posterchanai-webdav-mount --status`
- Restart the service if needed: `systemctl --user restart posterchanai-sync`

### Offline caching

The cache is stored in `~/.config/posterchanai-sync/cache/`. You can:

- **View cache size**: `du -sh ~/.config/posterchanai-sync/cache`
- **Clear cache**: `rm -rf ~/.config/posterchanai-sync/cache/*`
- **Check pending changes**: Look for "dirty" files in cache metadata

Cache is automatically cleaned up based on:
- Maximum size (default: 1000MB)
- Maximum age (default: 30 days)
- Files with pending changes are never removed

### Files not syncing after going online

If files you modified offline aren't syncing:

- Check logs for sync attempts: `journalctl --user -u posterchanai-sync | grep -i sync`
- Verify network is actually connected
- Check if files are marked as "dirty" in cache
- Try manually triggering a sync by restarting the service

## Uninstallation

```bash
systemctl --user stop posterchanai-sync
systemctl --user disable posterchanai-sync
posterchanai-webdav-mount --unmount
rm -rf ~/.local/share/posterchanai-sync
rm -rf ~/.config/posterchanai-sync
rm ~/.local/bin/posterchanai-webdav-mount
rm ~/.config/systemd/user/posterchanai-sync.service
systemctl --user daemon-reload
```

## Sync Features

The sync client includes comprehensive bidirectional sync:

### Automatic Local Change Detection
- Polls local filesystem and compares modification times
- Automatically marks modified files for upload
- No manual intervention required

### Delete Detection
- Detects remote deletions (removes local files)
- Detects local deletions (removes remote files)
- Handles deletions in both directions seamlessly

### Move/Rename Detection
- Uses content hashing to detect moved files
- Handles moves via WebDAV MOVE command
- Detects moves before deletions (moves might look like delete+create)

### Conflict Resolution
- Detects conflicts when both local and remote files were modified
- Configurable resolution strategies:
  - `last_write_wins`: Use file with newer modification time (default)
  - `local_wins`: Always use local version
  - `remote_wins`: Always use remote version
  - `manual`: Create `.conflict` file and use remote version

### Performance Optimizations
- Uses mtime for fast change detection (no file reads)
- Only calculates MD5 when actually syncing files
- Efficient polling-based sync (10 second interval)
- Minimal resource usage

See `BIDIRECTIONAL_SYNC.md` for detailed documentation.
