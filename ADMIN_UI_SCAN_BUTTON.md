# Admin UI Update - Single "Scan Files" Button

## What Changed

Combined two separate buttons into one streamlined action button in the Admin UI.

### Before:
- **"Rescan All Users"** button (file index + cache invalidation)
- **"Generate All Users"** button (thumbnail generation)
- Two separate operations, confusing for users

### After:
- **"Scan Files"** button (does everything in one go!)
  - ✅ Restores EXIF timestamps from photos/videos
  - ✅ Generates thumbnails for images and videos
  - ✅ Updates file index
  - ✅ Clears caches

## Benefits

1. **Simpler UX**: Single button instead of two - clearer what to do
2. **Better Messaging**: Clear explanation of all actions that will happen
3. **EXIF Stats**: Results now show how many EXIF timestamps were restored
4. **More Efficient**: One operation does everything automatically

## Location

**Admin UI → Site Settings tab → Storage Administration**

Access at: `http://192.168.0.1:3051/admin`

## Button UI

```
┌─────────────────────────────────────────────┐
│ Scan Files                                  │
│                                             │
│ [Scan All Users]                            │
│                                             │
│ Scans all user files and performs:         │
│ • Restore EXIF timestamps                   │
│ • Generate thumbnails                       │
│ • Update file index                         │
│ • Clear caches                              │
│                                             │
│ Use this after uploading files via rsync    │
│ or if photos aren't sorting correctly.      │
└─────────────────────────────────────────────┘
```

## What Happens When You Click

1. **Confirmation dialog** appears explaining all actions
2. Progress indicator shows "⏳ Scanning files for all users..."
3. Backend processes each user's files:
   - Reads EXIF data from photos/videos
   - Restores original capture dates
   - Generates missing thumbnails
   - Updates database index
4. **Results displayed** with detailed stats:
   - Total users processed
   - Files found
   - EXIF timestamps restored
   - Per-user details (expandable)

## Example Output

```
✅ Storage rescanned for 1 user(s)

Summary:
• Total users: 1
• Successful: 1
• Failed: 0
• Total files found: 4,840
• Total directories: 45

▼ View detailed results
  verita84@poster.place: 4840 files, 45 directories 
  [EXIF: 4200/4840 restored]
```

## Deployment Status

✅ **Committed**: `ec1573a9`  
✅ **Pushed**: origin/master  
✅ **Deployed**:
- Storage server (192.168.0.85) - **ACTIVE**
- Main server (192.168.0.1) - **ACTIVE**
  - posterchanai-ipex.service ✅
  - posterchanai-xpu-image.service ✅

## Files Changed

- `templates/admin/tabs/site_settings.html`: Combined button UI
- `static/js/admin.js`: Single event handler with EXIF stats display

## Usage

After rsync'ing files to the storage server:

1. Open `http://192.168.0.1:3051/admin`
2. Click **Site Settings** tab
3. Scroll to **Storage Administration**
4. Click **"Scan Files"** button
5. Confirm the action
6. Wait 2-5 minutes for large collections
7. View results showing EXIF restoration stats
8. Refresh photo gallery to see properly sorted photos!

---

**Status**: ✅ Deployed and ready to use!  
**Next Action**: Run "Scan Files" to restore EXIF timestamps for your 4,840 media files.
