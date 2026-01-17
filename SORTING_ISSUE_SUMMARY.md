# Photo Gallery Sorting Issue - Summary

## The Problem

Your images are NOT sorted by original capture date because **all files have the same timestamp** from when you rsync'd them.

## What I Found

### API Response (Newest First - Sorting IS Working!):
```
1. avatar.jpeg: 2026-01-17 14:24:13  <- NEWEST (you just uploaded this?)
2. walelt-2.png: 2026-01-17 01:55:55  <- All rsync'd at same time
3. wallet-1.png: 2026-01-17 01:55:55  <- Same timestamp
4. FFCA79F2...mov: 2026-01-17 01:55:55  <- Same timestamp
5. FF6CD1A6...mov: 2026-01-17 01:55:55  <- Same timestamp
```

**The sorting IS correct** - newest file (avatar.jpeg) is first!

The problem is 10,000+ of your files all have timestamp `01:55:55` because that's when you rsync'd them.

## The Solution

You need to **restore original timestamps from EXIF metadata**. But there's a problem:

### The Symlink Issue
- Symlink exists: `/var/lib/posterchanai/verita84@poster.place/Pictures` →  `/home/verita84/ownCloud/Personal`
- BUT the target directory doesn't exist on the storage server!
- The API is somehow still returning images (possibly cached or different mount)

## Where Are Your Images Actually Stored?

I need to know:
1. Did you rsync images TO the storage server (192.168.0.85)?
2. Or did you rsync them somewhere else and create a symlink?
3. Where is the actual `/home/verita84/ownCloud/Personal/Pictures` directory?

## Next Steps

Please tell me:
1. **Where did you rsync your images TO?** (full path)
2. **Which server has the actual files?** (192.168.0.1 or 192.168.0.85 or another?)
3. **What command did you use to rsync?**

Once I know where the files actually are, I can:
1. Fix the symlink (if needed)
2. Run the EXIF timestamp restoration script on the correct location
3. Your newest photos will then appear first!

## Temporary Workaround

The sorting IS working by file modification time. Your newest file (avatar.jpeg from today at 14:24) IS showing first. The older files just all have the same timestamp from the rsync.

If you upload NEW images through the web UI, they will appear at the top with correct timestamps.
