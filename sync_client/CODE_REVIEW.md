# Code Review - PosterchanAI Sync Client

## Critical Issues

### 1. Threading Safety - GUI Operations from Non-Main Thread
**Location:** `create_tray_icon()`, `show_logs()`, `show_conflicts()`
**Issue:** Tkinter operations must run in the main thread. Pystray callbacks run in separate threads.
**Fix:** Use `root.after()` to schedule GUI operations on main thread.

### 2. Race Conditions in State Management
**Location:** `sync_file()`, `save_state()`
**Issue:** Multiple threads can modify `self.state` simultaneously without locks.
**Fix:** Add threading locks around state access.

### 3. Wrong API Parameter Format
**Location:** `upload_file()` - mkdir call
**Issue:** `/api/storage/mkdir` uses Form data, not query params.
**Fix:** Use `data=` instead of `params=` for mkdir call.

### 4. File Handle Leak
**Location:** `upload_file()` line 363
**Issue:** File opened but not explicitly closed if exception occurs.
**Fix:** Use context manager or ensure cleanup.

### 5. Bare Exception Handlers
**Location:** Multiple places (lines 360, 403, 712)
**Issue:** Catching all exceptions hides bugs and makes debugging difficult.
**Fix:** Catch specific exceptions.

### 6. No Retry Logic for Network Failures
**Location:** All API calls
**Issue:** Single network failure causes sync to fail permanently.
**Fix:** Add exponential backoff retry logic.

## High Priority Issues

### 7. Hash Calculation Performance
**Location:** `calculate_file_hash()` called on every sync
**Issue:** Recalculating hash for large files is expensive.
**Fix:** Cache hashes and only recalculate if mtime/size changed.

### 8. Missing Username Validation
**Location:** `__init__()`, `get_username_from_api()`
**Issue:** If username is empty, all API calls will fail silently.
**Fix:** Validate username before proceeding.

### 9. Path Traversal Risk
**Location:** `sync_file()` - `relative_to()` can raise ValueError
**Issue:** Files outside sync_dir could cause crashes.
**Fix:** Validate path is within sync_dir before processing.

### 10. Queue Processing Error Handling
**Location:** `process_queue()` line 712
**Issue:** Bare except clause swallows all errors.
**Fix:** Log specific exceptions and handle queue.Empty properly.

### 11. Conflict Resolution Logic Flaw
**Location:** `sync_file()` lines 459-477
**Issue:** Conflict detection uses 1-second threshold which may be too strict for timezone differences.
**Fix:** Use more robust conflict detection (compare hashes).

### 12. Missing Delete Handler
**Location:** `process_queue()` line 710
**Issue:** Delete action is queued but not implemented.
**Fix:** Implement delete handler.

### 13. No Rate Limiting
**Location:** All API calls
**Issue:** Rapid file changes could overwhelm server.
**Fix:** Add rate limiting/throttling.

### 14. State File Corruption Risk
**Location:** `save_state()`
**Issue:** If process crashes during write, state file could be corrupted.
**Fix:** Write to temp file then atomic rename.

## Medium Priority Issues

### 15. Debounce Thread Leak
**Location:** `handle_change()` line 208
**Issue:** Creates new thread for each file change - could create many threads.
**Fix:** Use a single debounce worker thread with queue.

### 16. Missing Progress Feedback
**Location:** `sync_directory()`
**Issue:** No way to know sync progress for large directories.
**Fix:** Add progress callback/event.

### 17. No Config Validation
**Location:** `SyncConfig.load_config()`
**Issue:** Invalid config values not validated.
**Fix:** Add validation with helpful error messages.

### 18. Log File Rotation
**Location:** Logging setup
**Issue:** Log file grows indefinitely.
**Fix:** Add log rotation (e.g., keep last 7 days).

### 19. Missing Unit Tests
**Issue:** No test coverage.
**Fix:** Add unit tests for critical functions.

### 20. Incomplete Error Messages
**Location:** Various error handlers
**Issue:** Error messages don't provide enough context for debugging.
**Fix:** Include file paths, timestamps, and operation context.

## Low Priority / Improvements

### 21. Icon Creation
**Location:** `create_tray_icon()`
**Issue:** Icon is generated programmatically - could use actual logo file.
**Improvement:** Load from image file if available.

### 22. Config File Location
**Location:** `CONFIG_DIR`
**Issue:** Uses XDG config but doesn't check XDG_CONFIG_HOME.
**Improvement:** Use `xdg.BaseDirectory` or `pathlib` with proper XDG support.

### 23. No Sync Statistics
**Issue:** No tracking of sync metrics (files synced, errors, etc.)
**Improvement:** Add statistics tracking and display in GUI.

### 24. Missing Documentation
**Issue:** Some functions lack docstrings.
**Improvement:** Add comprehensive docstrings.

### 25. Hard-coded Values
**Location:** Various places (timeouts, debounce time, etc.)
**Issue:** Magic numbers scattered throughout code.
**Improvement:** Move to config or constants.

## Security Concerns

### 26. API Key Storage
**Issue:** API key stored in plain text JSON file.
**Recommendation:** Consider using keyring or encrypted storage.

### 27. Server URL Validation
**Issue:** No validation that server_url is safe.
**Recommendation:** Validate URL scheme (only http/https) and prevent localhost abuse.

### 28. Path Sanitization
**Issue:** Remote paths not fully sanitized before API calls.
**Recommendation:** Ensure all paths are sanitized to prevent injection.

## Recommended Fixes Priority

1. **Critical:** Fix threading issues (#1, #2)
2. **Critical:** Add proper error handling (#5, #10)
3. **High:** Implement delete handler (#12)
4. **High:** Add retry logic (#6)
5. **High:** Fix state file corruption (#14)
6. **Medium:** Add debounce optimization (#15)
7. **Medium:** Add config validation (#17)
8. **Low:** Improve documentation (#24)
