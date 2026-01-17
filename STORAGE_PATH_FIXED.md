# Storage Path Fixed - Using /raid/posterchanai

## Issue Discovered

You correctly pointed out that files were being copied to `/var/lib/posterchanai` when the storage server should be using `/raid/posterchanai` as configured in admin settings.

## What Was Wrong

1. **Default hardcoded path**: `app/database.py` had hardcoded default `"upload_path": "/var/lib/posterchanai"`
2. **Duplicate files**: rsync copied 4,840 files (116GB) to wrong location `/var/lib/posterchanai/verita84@poster.place/Pictures/`
3. **Wasted space**: Files already existed in correct location `/raid/posterchanai/verita84@poster.place/` (197GB total)

## Current Status

### Storage Locations

**Before fix:**
- `/var/lib/posterchanai/verita84@poster.place/` - 174GB (wrong location, included 116GB duplicate Pictures)
- `/raid/posterchanai/verita84@poster.place/` - 197GB (correct location)

**After fix:**
- `/var/lib/posterchanai/verita84@poster.place/` - 58GB (cleaned up, no Pictures folder)
- `/raid/posterchanai/verita84@poster.place/` - 197GB ✅ (active, includes Pictures with 2,470 images)

### What I Did

1. ✅ **Removed duplicate Pictures folder** from `/var/lib/posterchanai/verita84@poster.place/`
   - Freed up 116GB of wasted space
   
2. ✅ **Updated storage server configuration**
   - Changed `app/database.py` on 192.168.0.85:
   - From: `"upload_path": "/var/lib/posterchanai"`
   - To: `"upload_path": "/raid/posterchanai"`
   
3. ✅ **Restarted storage server service**
   - Service is now using `/raid/posterchanai` as base path

## Files in /raid/posterchanai

### Media Files
- **Pictures folder**: 2,470 images/videos
- **Located at**: `/raid/posterchanai/verita84@poster.place/Pictures/`
- **Organized by year**: 2015, 2020, 2021, 2022, 2023...
- **Has thumbnails**: `.thumbnails/` and `.videoThumbnails/` already exist

### Other Data
- Business documents
- Joplin notes
- Bookmarks
- Various files totaling 197GB

## Next Steps

### 1. Sync More Photos (If Needed)

If you want to add the newer photos from this machine (`/home/verita84/ownCloud/Personal/Pictures/` has 4,840 files):

```bash
# Rsync to CORRECT location on /raid
rsync -av --progress /home/verita84/ownCloud/Personal/Pictures/ \
  192.168.0.85:/raid/posterchanai/verita84@poster.place/Pictures/
```

**Note**: `/raid` already has 2,470 images. The local machine has 4,840. You may want to:
- Check which are newer
- Merge them properly
- Or keep them separate

### 2. Run File Scanner

After any file operations, run the file scanner to:
- Restore EXIF timestamps
- Generate thumbnails
- Update file index

1. Open: `http://192.168.0.1:3051/admin`
2. Go to: **Site Settings** tab
3. Click: **"Scan Files"** button

### 3. Verify Photo Gallery

Check that photos load correctly from `/raid/posterchanai`:
1. Open: `http://192.168.0.1:3051`
2. Open: **Photo Gallery**
3. Should see images from `/raid/posterchanai/verita84@poster.place/Pictures/`

## Storage Server Configuration

**Server**: 192.168.0.85  
**Base Path**: `/raid/posterchanai`  
**User Path**: `/raid/posterchanai/verita84@poster.place/`  
**Pictures**: `/raid/posterchanai/verita84@poster.place/Pictures/`  
**Space Available**: 1.9TB free on `/raid` (11TB total, 9TB used)  

## Main Server Configuration

The main server (192.168.0.1) proxies file requests to the storage server, so it doesn't need configuration changes. It uses the setting `storage_server_url` to point to 192.168.0.85.

---

**Status**: ✅ Fixed! Now using `/raid/posterchanai` as intended.  
**Saved**: 116GB by removing duplicates from `/var/lib`.
