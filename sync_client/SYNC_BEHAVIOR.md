# WebDAV Client Sync Behavior

## Current Implementation Status

### ✅ **Updates (Remote → Local)**
- **Status**: ✅ **WORKING**
- **How it works**:
  - Compares local file mtime with remote file mtime
  - Downloads if remote is newer or file doesn't exist locally
  - Updates local mtime to match remote
  - Uses cache to avoid unnecessary downloads
- **Location**: `sync_from_remote()` lines 893-931

### ⚠️ **Updates (Local → Remote)**
- **Status**: ⚠️ **PARTIALLY IMPLEMENTED**
- **How it works**:
  - `sync_to_remote()` method exists to upload files
  - `sync_pending_changes()` syncs files marked as "dirty" in cache
  - Cache has `mark_modified()` to mark files as needing sync
- **Missing**:
  - ❌ **No automatic detection of local file changes**
  - ❌ **No file system watching (inotify/fswatch)**
  - ❌ **No polling to detect local modifications**
  - Files must be manually marked as dirty in cache
- **Location**: `sync_to_remote()` line 989, `sync_pending_changes()` line 1008

### ❌ **Deletes**
- **Status**: ❌ **NOT IMPLEMENTED**
- **What exists**:
  - `delete()` method in WebDAVClient (can delete remote files)
- **Missing**:
  - ❌ **No detection of local file deletions**
  - ❌ **No detection of remote file deletions** (orphaned local files)
  - ❌ **No automatic cleanup of deleted files**
- **Location**: `WebDAVClient.delete()` line 420

### ❌ **Moves/Renames**
- **Status**: ❌ **NOT IMPLEMENTED**
- **Missing**:
  - ❌ **No detection of file moves/renames**
  - ❌ **No handling of directory moves**
  - ❌ **No conflict resolution for moves**

### ⚠️ **Conflict Resolution**
- **Status**: ⚠️ **BASIC (mtime-based)**
- **Current behavior**:
  - If remote is newer → downloads (overwrites local)
  - If local is newer → does nothing (local changes not detected)
- **Missing**:
  - ❌ **No conflict detection when both sides modified**
  - ❌ **No conflict resolution strategies** (last-write-wins, manual, etc.)
  - ❌ **No backup of conflicting files**

## Sync Strategy

### Current: **One-Way Sync (Remote → Local)**
- Primary direction: Remote → Local
- Syncs every 30 seconds (configurable)
- Polling-based (no file system events)
- Network-aware (handles disconnects, resume from hibernation)

### Monitoring Loop
- Runs every 10 seconds
- Checks network connectivity every 60 seconds
- Syncs from remote every 30 seconds (if online)
- Calls `sync_pending_changes()` before `sync_from_remote()`
- Detects system suspend/resume

## What Needs to be Added

### 1. **Local Change Detection**
```python
# Option A: File system watching (inotify on Linux)
import inotify.adapters

# Option B: Polling-based detection
def detect_local_changes(self):
    # Compare local file mtimes with cached mtimes
    # Mark files as dirty if modified
```

### 2. **Delete Detection**
```python
def detect_deletions(self):
    # Remote deletions: Compare remote file list with local files
    # Local deletions: Track files that existed but are now gone
    # Delete orphaned files/directories
```

### 3. **Move/Rename Detection**
```python
def detect_moves(self):
    # Compare file hashes/content
    # Detect when file appears in new location with same content
    # Handle as move rather than delete+create
```

### 4. **Two-Way Sync**
```python
def sync_bidirectional(self):
    # 1. Detect local changes
    # 2. Detect remote changes
    # 3. Resolve conflicts
    # 4. Sync local → remote
    # 5. Sync remote → local
```

## Recommendations

### For Production Use:
1. **Add file system watching** (inotify) for real-time local change detection
2. **Implement delete detection** for both directions
3. **Add conflict resolution** with configurable strategies
4. **Implement move/rename detection** using content hashing
5. **Add sync state tracking** to avoid redundant operations

### Quick Wins:
1. **Polling-based local change detection** (simpler than inotify)
2. **Remote delete detection** (compare file lists)
3. **Basic conflict resolution** (last-write-wins or manual)

### Current Limitations:
- **One-way sync**: Primarily remote → local
- **No real-time updates**: 30-second polling interval
- **Manual upload required**: Local changes must be manually marked
- **No delete handling**: Files deleted remotely or locally are not cleaned up
- **No move detection**: Moves appear as delete+create
