# EXIF Timestamp Restoration - Automatic Implementation

## Overview

The file scanner now **automatically restores original file timestamps from EXIF metadata** when scanning user files. This ensures that photos and videos are properly sorted by their original capture date, not the date they were uploaded or synced.

## How It Works

### 1. During File Scan (Admin UI)

When you click **"Scan Files"** in the Admin UI:

1. The scanner walks through all user files
2. For each image/video file, it reads the EXIF metadata:
   - **Images**: `DateTimeOriginal`, `CreateDate`, `DateCreated`
   - **Videos**: `CreationDate`, `CreateDate`, `DateTimeOriginal`, `MediaCreateDate`
3. If found, the file's modification timestamp is updated to match the original capture date
4. Files are then indexed with the correct dates

**Result**: Your newest photos (by capture date) will show first in the Photo Gallery!

### 2. During File Upload

When a user uploads a new image or video:

1. File is saved to storage
2. EXIF timestamp is **automatically restored** from metadata
3. Thumbnail is generated
4. File is indexed with correct date

**Result**: New uploads are automatically sorted correctly without manual intervention.

## Supported File Types

### Images
- `.jpg`, `.jpeg`, `.png`, `.gif`, `.bmp`, `.tiff`, `.tif`, `.heic`, `.heif`, `.webp`

### Videos
- `.mp4`, `.mov`, `.avi`, `.mkv`, `.m4v`, `.3gp`, `.wmv`, `.flv`, `.webm`, `.mpg`, `.mpeg`

## EXIF Tags Used

The system tries multiple EXIF tags in order until it finds a valid date:

| File Type | Tags (in priority order) |
|-----------|-------------------------|
| Images    | DateTimeOriginal → CreateDate → DateCreated |
| Videos    | CreationDate → CreateDate → DateTimeOriginal → MediaCreateDate |

## Requirements

- **exiftool** must be installed on the system
- Check with: `which exiftool` and `exiftool -ver`
- Install on Gentoo: `emerge media-libs/exiftool`
- Install on Debian/Ubuntu: `apt install libimage-exiftool-perl`

## How to Trigger EXIF Restoration

### Method 1: Admin UI File Scanner (Recommended)

1. Open: `http://192.168.0.1:3051/admin`
2. Navigate to: **Services** tab
3. Click: **"Scan Files"** button
4. Wait for scan to complete

The scanner will automatically:
- Restore EXIF timestamps for all media files
- Update file index
- Clear caches

You'll see output like:
```
[EXIF] Starting batch timestamp restoration in: /var/lib/posterchanai/verita84@poster.place
[EXIF] Restored timestamp for IMG_1234.JPG: 2024-12-25 10:30:45
[EXIF] Batch complete: 4226 processed, 3890 restored, 336 skipped, 0 errors
```

### Method 2: Automatic on Upload

No action needed! Every new image/video uploaded automatically has its EXIF timestamp restored.

## What Changed

### New Files

- **`app/utils/exif_utils.py`**: EXIF utility functions
  - `restore_exif_timestamp()`: Restore timestamp for single file
  - `batch_restore_timestamps()`: Batch process entire directory
  - EXIF date parsing for various formats

### Modified Files

- **`app/routers/admin.py`**: File scanner now calls `batch_restore_timestamps()`
- **`app/routers/storage.py`**: Upload handler calls `restore_exif_timestamp()`

## Logging

The system logs EXIF operations:

- **INFO**: Successful timestamp restorations
- **DEBUG**: Files without EXIF data or with matching timestamps
- **ERROR**: Failed operations

Example logs:
```
[EXIF] Restored timestamp for photo.jpg: 2024-01-15 14:23:45
[EXIF] Batch complete: 100 processed, 87 restored, 13 skipped, 0 errors
[UPLOAD] ✓ Restored EXIF timestamp for: IMG_5678.JPG
```

## Performance

- **Speed**: ~10-50 files/second (depends on file sizes and disk I/O)
- **Non-blocking**: Runs in thread pool, doesn't block API requests
- **Efficient**: Only updates timestamps that differ from EXIF data

For 4,226 files: Expected completion in 1-3 minutes.

## Benefits

✅ **Accurate Sorting**: Files sorted by actual capture date, not upload date  
✅ **Automatic**: No manual scripts or commands needed  
✅ **Works with rsync**: Fixes timestamp issues from file transfers  
✅ **Upload Integration**: New uploads get correct timestamps automatically  
✅ **Safe**: Only updates files with valid EXIF metadata  
✅ **Fast**: Uses optimized batch processing  

## Troubleshooting

### Q: Photos still not sorted correctly?

1. Clear browser cache: **Ctrl+Shift+R**
2. Run file scanner again in Admin UI
3. Check logs: `journalctl -u posterchanai.service -f`

### Q: EXIF restoration not working?

1. Check if exiftool is installed: `which exiftool`
2. Check file permissions: Files must be readable/writable
3. Check logs for EXIF warnings/errors

### Q: Some files still have wrong dates?

Not all files have EXIF metadata:
- Downloaded images (often stripped)
- Screenshots (no camera metadata)
- Edited files (metadata removed)

These files keep their filesystem modification time.

## Migration from Old System

The old standalone script (`scripts/restore_timestamps.sh`) is **no longer needed**. The functionality is now built into:

1. Admin UI file scanner
2. File upload handler

Simply run the file scanner once to restore all existing timestamps, then all future uploads will be handled automatically.

---

**Summary**: Your photo gallery will now automatically show newest photos first, based on when they were actually taken, not when they were uploaded or synced! 📸
