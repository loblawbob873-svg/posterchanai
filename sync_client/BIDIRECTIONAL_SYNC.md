# Bidirectional Sync Implementation

## Overview
The WebDAV client now supports full bidirectional synchronization with automatic change detection, delete handling, move/rename detection, and conflict resolution.

## Implemented Features

### 1. ✅ Automatic Local Change Detection (Polling-Based)
**Location**: `WebDAVSync.detect_local_changes()`

**How it works**:
- Polls local filesystem during each sync cycle
- Compares local file mtimes with cached sync state mtimes
- Detects new files (not in sync state)
- Detects modified files (mtime or size changed)
- Automatically marks files as "dirty" for upload

**Usage**:
- Called automatically during `sync_bidirectional()`
- No manual intervention required
- Files are automatically uploaded on next sync

### 2. ✅ Delete Detection (Both Directions)
**Location**: `WebDAVSync.detect_deletions()`

**Remote Deletions**:
- Compares current remote file list with sync state
- Detects files that existed in last sync but not in current remote
- Automatically deletes corresponding local files
- Removes from sync state

**Local Deletions**:
- Compares sync state with current local filesystem
- Detects files that exist remotely but were deleted locally
- Automatically deletes remote files via WebDAV DELETE
- Removes from sync state

**Usage**:
- Called automatically during `sync_bidirectional()`
- Handles deletions in both directions seamlessly

### 3. ✅ Move/Rename Detection
**Location**: `WebDAVSync.detect_moves()`

**How it works**:
- Uses content hashing (MD5) to identify moved files
- Compares file hashes from sync state with current files
- Detects when a file appears in a new location with same content
- Uses WebDAV MOVE command to handle rename on server
- Updates sync state and moves local file

**Usage**:
- Called automatically during `sync_bidirectional()`
- Detects moves before deletions (moves might look like delete+create)
- Handles both file and directory moves

### 4. ✅ Two-Way Sync with Conflict Resolution
**Location**: `WebDAVSync.sync_bidirectional()`, `WebDAVSync.resolve_conflict()`

**Conflict Detection**:
- Detects when both local and remote files were modified
- Compares mtimes to determine if conflict exists
- Only conflicts if both sides modified since last sync

**Conflict Resolution Strategies**:
- `last_write_wins` (default): Use file with newer mtime
- `local_wins`: Always use local version
- `remote_wins`: Always use remote version
- `manual`: Create `.conflict` file and use remote version

**Configuration**:
```json
{
  "conflict_resolution": "last_write_wins"
}
```

**Sync Flow**:
1. Detect local changes
2. Get current remote file list
3. Detect moves/renames
4. Detect deletions (both directions)
5. Handle remote deletions (delete local)
6. Handle local deletions (delete remote)
7. Sync from remote (with conflict detection)
8. Sync local changes to remote (with conflict resolution)

## Sync State Tracking

**Location**: `CacheManager` sync state methods

**What's tracked**:
- Remote path
- Local path
- Modification time (mtime)
- File size
- Content hash (MD5)

**Storage**:
- Stored in `~/.config/posterchanai-sync/cache/sync_state.json`
- Persisted across restarts
- Updated after each successful sync operation

## Usage

### Automatic Operation
The bidirectional sync runs automatically every 10 seconds (configurable via `sync_interval`).

### Manual Sync
```python
from webdav_mount import WebDAVMount

mount = WebDAVMount()
mount.mount()  # Initializes sync
# sync_bidirectional() is called automatically in monitor loop
```

### Configuration
```json
{
  "sync_interval": 10,
  "conflict_resolution": "last_write_wins",
  "cache_max_size_mb": 204800
}
```

## Performance

- **Local change detection**: O(n) where n = number of local files
- **Delete detection**: O(m) where m = number of files in sync state
- **Move detection**: O(k) where k = number of new files (hash computation)
- **Overall**: Efficient polling-based approach, no file system watching overhead

## Limitations

1. **Polling-based**: Not real-time (10s interval by default)
2. **Move detection**: Requires content hashing (computes MD5 for new files)
3. **Conflict resolution**: Basic strategies, no 3-way merge
4. **Large directories**: May be slow for very large directory trees

## Future Enhancements

- File system watching (inotify) for real-time change detection
- More sophisticated conflict resolution (3-way merge)
- Batch operations for better performance
- Incremental sync (only changed files)
