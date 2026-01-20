# PosterchanAI WebDAV Mount Client

A simple daemon that mounts your PosterchanAI storage as a local filesystem using WebDAV. Much simpler than the old sync client - just mount and use your files normally!

## Features

- 🗂️ **Filesystem Mount**: Access your remote storage as a local directory
- 💾 **Offline Caching**: Cache files locally for offline access
- 🔄 **Intelligent Auto-Reconnect**: Automatically remounts with smart retry logic
- 🌐 **Network Detection**: Checks network connectivity before attempting remount
- 💤 **Suspend/Resume Handling**: Automatically detects and handles system hibernation
- 📊 **Health Monitoring**: Periodically checks mount health and fixes issues automatically
- ⏱️ **Exponential Backoff**: Smart retry intervals to avoid overwhelming the server
- 🔄 **Automatic Sync**: Syncs pending changes when network reconnects
- ⚙️ **Systemd Integration**: Runs as user systemd service
- 🔐 **Secure**: Uses your PosterchanAI account credentials
- 📝 **Simple**: No complex sync logic, just mount and use

## Prerequisites

**No OS packages required!** This is a pure Python implementation.

All dependencies (Python packages) are installed automatically by the installer:
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
- **Server URL**: Your PosterchanAI server URL (e.g., `http://localhost:8000`)
- **API Key**: Your API key (get from Settings → API Keys in web UI)
- **WebDAV URL**: Your WebDAV server URL (e.g., `http://localhost:8080/username`)
- **Account Password**: Your PosterchanAI account password (for WebDAV authentication)
- **Mount Point**: Local directory where storage will be mounted (default: `~/PosterchanAI-Mount`)

The wizard will save your configuration automatically.

## Manual Configuration

If you need to edit configuration manually, edit `~/.config/posterchanai-sync/config.json`:

```json
{
  "server_url": "http://localhost:8000",
  "api_key": "sk-your-api-key-here",
  "username": "your-username",
  "webdav_url": "http://localhost:8080/your-username",
  "password": "your-account-password",
  "mount_point": "/home/user/PosterchanAI-Mount"
}
```

### Configuration Options

- `server_url`: Your PosterchanAI server URL
- `api_key`: Your API key (get from PosterchanAI web UI)
- `username`: Your username (auto-detected from server)
- `webdav_url`: Your WebDAV server URL (usually `{server_url}:8080/{username}`)
- `password`: Your PosterchanAI account password (for WebDAV authentication)
- `mount_point`: Local directory where storage will be mounted
- `enable_cache`: Enable offline caching (default: `true`)
- `cache_max_size_mb`: Maximum cache size in MB (default: `1000`)
- `cache_max_age_days`: Maximum age of cached files in days (default: `30`)

## Usage

### Start Service

```bash
systemctl --user start posterchanai-sync
```

### Enable Auto-start

```bash
systemctl --user enable posterchanai-sync
```

### Check Mount Status

```bash
posterchanai-webdav-mount --status
```

### Manually Mount

```bash
posterchanai-webdav-mount --mount
```

### Manually Unmount

```bash
posterchanai-webdav-mount --unmount
```

### View Logs

```bash
journalctl --user -u posterchanai-sync -f
```

### Run Setup Wizard

```bash
posterchanai-webdav-mount --setup
```

## How It Works

The daemon uses pure Python to sync your WebDAV storage to a local directory. Files appear in the local directory and are automatically synced with the server. You can:

- Access files normally via the mount point
- Edit files directly (changes sync automatically)
- Use any file manager or application
- No need to manually sync - it's a real filesystem mount!

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

If you see import errors, ensure Python packages are installed:
```bash
pip install requests
```

That's all you need! No system packages required.

If mounting fails with permission errors, you may need to:

1. Add yourself to the `fuse` group:
   ```bash
   sudo usermod -aG fuse $USER
   ```
   (Log out and back in for this to take effect)

2. Ensure `/dev/fuse` has correct permissions (usually handled by udev)

### Service won't start

Check logs:
```bash
journalctl --user -u posterchanai-sync
```

Common issues:
- Invalid `webdav_url` or `password` in config
- `mount_point` doesn't exist or isn't writable
- Missing Python dependencies (re-run installer: `./install.sh`)

### Mount point not accessible

- Check if mount succeeded: `posterchanai-webdav-mount --status`
- Check if WebDAV server is running on your PosterchanAI server
- Verify WebDAV URL is correct
- Check logs for errors

### High CPU usage

This shouldn't happen with WebDAV mount - it's much more efficient than the old sync client. If you see high CPU:

- Check if mount is stuck in a reconnect loop (check logs)
- Verify WebDAV server is accessible
- Check network connectivity
- The intelligent monitor uses exponential backoff to prevent rapid retries

### Mount not reconnecting after network disconnect

The daemon should automatically detect network disconnects and reconnect. If it's not working:

- Check logs: `journalctl --user -u posterchanai-sync -f`
- Verify network detection is working (should see "Network not available" messages)
- Try manually unmounting and remounting: `posterchanai-webdav-mount --unmount && posterchanai-webdav-mount --mount`

### Mount not working after hibernation

The daemon should automatically detect resume from hibernation. If it's not working:

- Check logs for "Suspend/resume detected" messages
- The daemon checks for time jumps > 5 minutes to detect suspend/resume
- Try manually checking status: `posterchanai-webdav-mount --status`
- If not mounted, it should automatically remount within 60 seconds

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

## Differences from Old Sync Client

The old sync client was a complex bidirectional sync system with:
- File watching
- Conflict detection
- State tracking
- Complex sync logic

The new WebDAV mount client is much simpler:
- Pure Python implementation (no external tools like davfs2)
- Just mounts remote storage as a filesystem
- No sync needed - it's a real mount!
- Automatic reconnection on failure
- Much lower resource usage
- Simpler codebase
- Intelligent network and suspend/resume handling

If you need bidirectional sync, you can use tools like `rsync` or `rclone` on the mounted directory.
