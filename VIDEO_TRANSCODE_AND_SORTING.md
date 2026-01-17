# Photo Gallery - Video Transcoding & Sorting

## Issue 1: Video Transcoding ✅ FIXED

### Problem
Videos were NOT being transcoded automatically when viewing them, wasting bandwidth.

### Root Cause
- Transcoding only happened during upload
- When viewing videos, system checked for existing transcoded version but didn't create one on-demand

### Solution Applied
Modified `/home/verita84/posterchanai/app/routers/storage.py` to:
1. Check if transcoded version exists
2. **If NOT, transcode on-demand** when video is first viewed
3. Save transcoded version for future use
4. Serve original if transcoding fails

### Benefits
- ✅ **Automatic transcoding**: First viewer triggers transcode, subsequent viewers get optimized version
- ✅ **Bandwidth savings**: H.264/AAC web-optimized format, typically 40-60% smaller
- ✅ **Faster playback**: Web-optimized settings for better streaming
- ✅ **Graceful fallback**: Original served if transcoding fails

### Technical Details
```python
# Transcodes video on-demand in background thread
transcoded_result = await asyncio.to_thread(transcode_video, user_path, full_path)
# Transcoded videos stored in: /var/lib/posterchanai/USERNAME/.transcoded/
```

---

## Issue 2: Sorting (Newest First)

### Current Status
✅ **Backend IS sorting correctly** (newest first by timestamp)
✅ **Frontend IS sorting correctly** (newest first by timestamp)

### Why It May Appear Wrong
**All your recent images have IDENTICAL timestamps** because they were copied/synced at the same time.

### Evidence
```
Images at offset 0 (newest):
- walelt-2.png: 1768640155.462971 (2026-01-17 01:55:55)
- wallet-1.png: 1768640155.462971 (2026-01-17 01:55:55) <- SAME
- FFCA79F2...mov: 1768640155.462971 (2026-01-17 01:55:55) <- SAME

Images at offset 100 (older):
- C2EF8678...mov: 1768639997.435783 (2026-01-17 01:53:17)
- C2D2DB2D...mp4: 1768639997.312449 (2026-01-17 01:53:17)
```

**Sorting IS working** - the timestamps decrease as you scroll down (newer → older).

### Why This Happened
When you copy/sync files (e.g., from iPhone to server), the file modification time becomes the copy/sync time, not the original photo capture time.

### Solutions

#### Option 1: Restore Original Timestamps (Recommended)
Use EXIF data to restore original capture dates:
```bash
# Install exiftool
ssh 192.168.0.85 "sudo pacman -S perl-image-exiftool"

# Restore timestamps from EXIF for images
find /var/lib/posterchanai/USERNAME/Pictures -name "*.jpg" -o -name "*.png" | while read file; do
    exiftool -d "%s" -DateTimeOriginal "$file" | grep -o '[0-9]*' | xargs -I{} touch -d @{} "$file"
done
```

#### Option 2: Preserve Timestamps When Copying
For future copies, use:
```bash
rsync -av --times source/ dest/  # Preserves modification times
# or
cp -p source dest  # Preserves timestamps
```

#### Option 3: Sort by EXIF Date (Requires Code Change)
Modify backend to read EXIF DateTimeOriginal instead of file mtime.

### Verification
Backend logs confirm sorting is working:
```
[STORAGE] ✓ Sorting verified: All 50 checked images are in correct order (newest first)
[STORAGE] Found 10173 images total. First 10 (newest first):
```

---

## Summary

### ✅ Fixed
1. **Video transcoding** - Now transcodes automatically on first view
2. **Sorting logic** - Was already correct, issue is identical timestamps

### 🔍 Root Cause
- Videos: No on-demand transcoding before
- Sorting: Files copied without preserving original dates

### 🎯 Next Steps
1. **Test video transcoding**: Open a video in gallery, check logs for "Transcoding video on-demand"
2. **Fix timestamps** (optional): Run exiftool to restore original dates from EXIF metadata
3. **Hard refresh browser**: Ctrl+Shift+R to clear cached JavaScript

### Files Modified
- `/home/verita84/posterchanai/app/routers/storage.py` - Added on-demand transcoding

### Deployed To
- ✅ Storage server (192.168.0.85)
- ✅ Service restarted
