# Fix Photo Gallery Sorting After Rsync

## Problem
You rsync'd files to posterchanai, which **overwrote the original file timestamps** with the rsync time. Now all files show the same date (when you copied them), instead of when the photos/videos were actually taken.

## Solution
Restore original timestamps from EXIF metadata embedded in the files.

## Step 1: Install exiftool (if not already installed)

**On storage server (192.168.0.85):**
```bash
ssh 192.168.0.85
sudo pacman -S perl-image-exiftool  # For Arch/Gentoo
# OR
sudo apt install libimage-exiftool-perl  # For Debian/Ubuntu
```

## Step 2: Run the Timestamp Restoration Script

**I've created a script for you:**
```bash
# Copy script to storage server
rsync -av /home/verita84/posterchanai/scripts/restore_timestamps.sh 192.168.0.85:/home/verita84/posterchanai/scripts/

# Run it on storage server
ssh 192.168.0.85
cd /home/verita84/posterchanai
./scripts/restore_timestamps.sh
```

**What it does:**
- Reads EXIF DateTimeOriginal/CreationDate from each photo/video
- Sets the file modification time to match the original capture date
- Processes all images (JPG, PNG, HEIC) and videos (MOV, MP4)
- Shows progress as it works
- Reports statistics when done

**Expected output:**
```
Processing images...
  Processed 100 files...
  Processed 200 files...
Processing videos...
  Processed 300 files...

Timestamp Restoration Complete!
Total files processed: 10173
Successfully updated: 9845
Failed/No EXIF data: 328
```

## Step 3: Restart posterchanai

```bash
ssh 192.168.0.85 "sudo systemctl restart posterchanai.service"
```

## Step 4: Refresh Browser

Open `http://192.168.0.1:3051` and press **Ctrl+Shift+R** (hard refresh)

## Result

Your photos and videos will now be sorted by **original capture date** instead of rsync date!
- Newest photos (by actual capture date) at the top
- Proper chronological order
- Videos sorted by recording date

## Alternative: Preserve Timestamps During Rsync

**For future syncs, use the `-t` or `--times` flag:**
```bash
rsync -avt --times source/ destination/  # Preserves modification times
```

This prevents the problem from happening again.

## Technical Details

### EXIF Tags Used:
- **Images**: `DateTimeOriginal` (when photo was taken)
- **Videos**: `CreationDate`, `CreateDate`, or `DateTimeOriginal` (when recorded)

### Files Affected:
- All files in `/var/lib/posterchanai/verita84@poster.place/Pictures/`
- Includes all subdirectories recursively

### What if a file has no EXIF data?
Files without EXIF data keep their current timestamp (rsync time). This typically affects:
- Screenshots
- Downloaded images
- Edited/processed photos that lost metadata

---

**Ready to fix your sorting?** Just run the script and restart! 🎉
