# Migrate Files from OwnCloud to Posterchanai

## Overview
Use the `migrate_storage.py` script to copy files from OwnCloud to Posterchanai user storage.

## Prerequisites

1. **Access to OwnCloud data directory**
   - OwnCloud data is typically stored in: `/var/www/owncloud/data/<username>/files/`
   - Or if using Docker: Mount the data volume
   - Or export files from OwnCloud web interface

2. **Know your Posterchanai username**
   - The username you use to log into Posterchanai

## Step 1: Find Your OwnCloud Data Location

### Option A: OwnCloud Server (Standard Installation)
```bash
# OwnCloud data is usually here:
ls /var/www/owncloud/data/<username>/files/
```

### Option B: OwnCloud Docker
```bash
# Find the container
docker ps | grep owncloud

# Access the data directory
docker exec -it <owncloud-container> ls /var/www/html/data/<username>/files/
```

### Option C: Export from OwnCloud Web Interface
1. Log into OwnCloud web interface
2. Download files/folders you want to migrate
3. Extract to a local directory (e.g., `~/owncloud_export/`)

## Step 2: Activate Python Environment

```bash
cd /home/verita84/posterchanai
source venv/bin/activate
```

(You should see `(venv)` in your prompt)

## Step 3: Find Your Posterchanai Username

```bash
# Check available users
sqlite3 data/posterchanai.db "SELECT id, username FROM users;"
```

## Step 4: Run Migration

### Basic Usage (Skip Existing Files)
```bash
python scripts/migrate_storage.py <owncloud_data_path> <posterchanai_username>
```

**Example:**
```bash
# If OwnCloud data is at /var/www/owncloud/data/john/files/
python scripts/migrate_storage.py /var/www/owncloud/data/john/files/ john

# If you exported to ~/owncloud_export/
python scripts/migrate_storage.py ~/owncloud_export/ john
```

### Overwrite Existing Files
```bash
python scripts/migrate_storage.py <owncloud_data_path> <posterchanai_username> --overwrite
```

### Dry Run (See What Would Be Copied)
```bash
python scripts/migrate_storage.py <owncloud_data_path> <posterchanai_username> --dry-run --verbose
```

### Verbose Output (See Progress)
```bash
python scripts/migrate_storage.py <owncloud_data_path> <posterchanai_username> --verbose
```

## Step 5: Verify Migration

1. Open Posterchanai in browser: `http://localhost:3051`
2. Open File Manager
3. Check that your files are present

## Important Notes

### Distributed Storage Setup
If you're using distributed storage (storage server separate from main server):

- **Run the script on the STORAGE SERVER**, not the main server
- The script will warn you if it detects distributed storage
- Files copied to the wrong server won't be accessible

### File Permissions
- The script preserves file timestamps
- Make sure the posterchanai user has write access to the storage directory

### Large Migrations
- For large datasets, use `--verbose` to see progress
- The script shows progress every 100 files
- You can stop and resume (it skips existing files by default)

## Troubleshooting

### "User not found"
```bash
# List all users
sqlite3 data/posterchanai.db "SELECT id, username FROM users;"
```

### "Source directory does not exist"
- Check the path is correct
- Use absolute paths: `/full/path/to/owncloud/data`
- Make sure you have read permissions

### "Permission denied"
```bash
# Check permissions
ls -la <owncloud_data_path>

# If needed, adjust permissions (be careful!)
sudo chmod -R +r <owncloud_data_path>
```

### "Storage server not configured"
- If you see a warning about distributed storage, make sure you're running on the correct server
- Check `storage_server_url` setting in database

## Example: Complete Migration

```bash
# 1. Activate environment
cd /home/verita84/posterchanai
source venv/bin/activate

# 2. Check users
sqlite3 data/posterchanai.db "SELECT id, username FROM users;"

# 3. Dry run first (recommended)
python scripts/migrate_storage.py \
  /var/www/owncloud/data/john/files/ \
  john \
  --dry-run \
  --verbose

# 4. If dry run looks good, run actual migration
python scripts/migrate_storage.py \
  /var/www/owncloud/data/john/files/ \
  john \
  --verbose

# 5. Verify in File Manager
```

## Alternative: WebDAV Access

If you prefer, you can also:
1. Configure OwnCloud as external storage in Posterchanai admin panel
2. Access files directly via WebDAV without copying
3. Copy specific files as needed through the file manager

This avoids migration but requires OwnCloud to remain running.
