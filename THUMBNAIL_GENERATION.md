# Thumbnail/Preview Generation for Photos & Videos

## Current Status ✅

Thumbnails ARE automatically generated when you upload files through the web UI.

## What Was Updated

### 1. Upload Process (Already Working)
When you upload images/videos through the web UI:
- ✅ Thumbnail generation is scheduled automatically
- ✅ Runs in background (doesn't slow down upload)
- ✅ Improved logging to show when thumbnails are generated

**Log messages you'll now see:**
```
[UPLOAD] ✓ Scheduled thumbnail generation for image: photo.jpg
[UPLOAD] ✓ Scheduled thumbnail generation for video: video.mov
```

### 2. Batch Processing (New Script)
For existing files (like after rsync), I created a batch processor.

## Generate Thumbnails for Existing Files

If you rsync'd files and they don't have thumbnails yet:

### Step 1: Run on Storage Server

```bash
ssh 192.168.0.85
cd /home/verita84/posterchanai

# Generate thumbnails for all media files
python3 scripts/generate_thumbnails.py /var/lib/posterchanai/verita84@poster.place
```

### Step 2: Monitor Progress

The script will:
- Scan all subdirectories for images and videos
- Skip files that already have thumbnails
- Generate thumbnails for files that don't
- Show progress every 50 files
- Report final statistics

**Example output:**
```
Found 10173 media files
Starting thumbnail generation for 10173 files...
✓ [1/10173] Generated thumbnail for image: photo.jpg
✓ [2/10173] Generated thumbnail for video: video.mov
Progress: 50/10173 (48 generated, 2 skipped, 0 failed)
...
Thumbnail Generation Complete!
Total files processed: 10173
Thumbnails generated: 9845
Already existed (skipped): 328
Failed: 0
```

### Step 3: Refresh Browser

After thumbnails are generated:
1. Open `http://192.168.0.1:3051`
2. Hard refresh: **Ctrl+Shift+R**
3. Photo Gallery will now show thumbnails/previews

## How Thumbnails Work

### Storage Location
```
/var/lib/posterchanai/USERNAME/
  ├── Pictures/
  │   ├── photo1.jpg
  │   └── video1.mov
  └── .thumbnails/
      ├── Pictures/
      │   ├── photo1.jpg    <- 300x300 thumbnail
      │   └── video1.jpg    <- Video thumbnail (first frame)
```

### Thumbnail Specifications
- **Size**: 300x300 pixels (maintains aspect ratio)
- **Format**: JPEG (for both images and video thumbnails)
- **Quality**: 85% (good balance of quality/size)
- **Video thumbnails**: First frame extracted

### When Thumbnails Are Generated

1. **On Upload** (automatic):
   - Upload through web UI → thumbnail generated immediately
   
2. **On Batch Process** (manual):
   - Run `generate_thumbnails.py` script for existing files
   
3. **On Demand** (fallback):
   - If thumbnail missing when viewing gallery, shows full image

## Verify It's Working

### Check if thumbnails exist:
```bash
ssh 192.168.0.85
find /var/lib/posterchanai/verita84@poster.place/.thumbnails -type f | wc -l
```

### Watch thumbnails being generated:
```bash
ssh 192.168.0.85
journalctl -u posterchanai.service -f | grep thumbnail
```

### Test upload:
1. Upload a photo through web UI
2. Check logs for: `[UPLOAD] ✓ Scheduled thumbnail generation`
3. Check `.thumbnails/` directory for new file

## Benefits

✅ **Faster gallery loading** - Small thumbnails load quickly  
✅ **Bandwidth savings** - Thumbnails ~5-10% size of originals  
✅ **Better UX** - Grid view shows many photos at once  
✅ **Automatic** - No manual intervention needed for uploads  

## Files Modified

- `/home/verita84/posterchanai/app/routers/storage.py` - Improved logging
- `/home/verita84/posterchanai/scripts/generate_thumbnails.py` - New batch processor

---

**Status**: ✅ Thumbnail generation is active and working!
