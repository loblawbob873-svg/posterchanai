# Joplin to Posterchanai Notes Migration Guide

This guide explains how to migrate your notes from Joplin to Posterchanai's notes feature.

## Prerequisites

- Joplin installed and configured
- Posterchanai running with a user account
- Python 3.11+ with required dependencies
- Terminal/command line access

## Step-by-Step Migration

### Step 1: Find Your Posterchanai User ID

You need to know your user ID in Posterchanai. Here are two ways:

**Option A: Using SQLite (if you have access)**
```bash
cd /path/to/posterchanai
sqlite3 posterchanai.db "SELECT id, username FROM users;"
```

**Option B: Check the Admin Panel**
- Go to `http://localhost:3051/admin` (if you're an admin)
- Or check the database file directly

**Option C: Create a test query**
If you know your username, you can temporarily add this to see your ID:
```bash
cd /path/to/posterchanai
source venv/bin/activate  # or venv-xpu/bin/activate
python3 -c "from app.database import SessionLocal; from app.models import User; db = SessionLocal(); user = db.query(User).filter(User.username == 'YOUR_USERNAME').first(); print(f'User ID: {user.id}' if user else 'User not found'); db.close()"
```

### Step 2: Find Your Joplin Database Location

The Joplin database location depends on your operating system:

**Linux:**
```bash
~/.config/joplin-desktop/database.sqlite
# or
~/.config/joplin/database.sqlite
```

**Windows:**
```
%APPDATA%\Joplin\database.sqlite
# Usually: C:\Users\YourUsername\AppData\Roaming\Joplin\database.sqlite
```

**macOS:**
```bash
~/Library/Application Support/Joplin/database.sqlite
```

**Quick check:**
```bash
# Linux/macOS - try to find it
find ~ -name "database.sqlite" -path "*/joplin*" 2>/dev/null

# Or check if Joplin is running and look at its process/files
```

### Step 3: Activate Your Python Virtual Environment

Navigate to your posterchanai directory and activate the virtual environment:

```bash
cd /home/verita84/posterchanai

# Activate the virtual environment (choose the one you use)
source venv/bin/activate
# OR if you use venv-xpu:
# source venv-xpu/bin/activate
```

You should see `(venv)` or similar in your terminal prompt.

### Step 4: Run the Migration Script

**Option A: Auto-detect Joplin database (recommended)**
```bash
python scripts/migrate_joplin.py --user-id 1
```
Replace `1` with your actual user ID from Step 1.

**Option B: Specify Joplin database path manually**
```bash
# Linux/macOS
python scripts/migrate_joplin.py \
  --joplin-db ~/.config/joplin-desktop/database.sqlite \
  --user-id 1

# Windows (PowerShell)
python scripts/migrate_joplin.py --joplin-db "$env:APPDATA\Joplin\database.sqlite" --user-id 1

# Windows (CMD)
python scripts/migrate_joplin.py --joplin-db "%APPDATA%\Joplin\database.sqlite" --user-id 1
```

**Option C: Test first with dry-run (recommended!)**
```bash
python scripts/migrate_joplin.py --user-id 1 --dry-run
```
This will show you what would be migrated without actually making changes.

### Step 5: Verify the Migration

After migration completes:

1. **Check the output** - The script will tell you how many folders and notes were migrated
2. **Open Posterchanai** - Go to `http://localhost:3051`
3. **Type `notes`** in the chat to open the notes modal
4. **Verify your notes** - Check that your folders and notes appear correctly

## Example Full Migration Session

Here's what a complete migration session looks like:

```bash
# 1. Navigate to posterchanai directory
cd /home/verita84/posterchanai

# 2. Activate virtual environment
source venv/bin/activate

# 3. Find your user ID (if you don't know it)
sqlite3 posterchanai.db "SELECT id, username FROM users;"
# Output: 1|admin

# 4. Test migration first (dry-run)
python scripts/migrate_joplin.py --user-id 1 --dry-run

# 5. If dry-run looks good, run actual migration
python scripts/migrate_joplin.py --user-id 1

# 6. Check output - should see something like:
# Migrating Joplin notes for user: admin (ID: 1)
# Migrating folders...
#   Created folder: Work (ID: 1)
#   Created folder: Personal (ID: 2)
# Migrating notes...
# Migration complete!
#   Folders migrated: 2
#   Notes migrated: 45
#   Notes skipped (todos): 3
```

## Troubleshooting

### "Joplin database not found"

**Solution:** Make sure:
- Joplin has been run at least once (creates the database)
- You're using the correct path
- The file exists: `ls -la ~/.config/joplin-desktop/database.sqlite` (Linux/macOS)

**Manual path:**
```bash
# Find the exact path
find ~ -name "database.sqlite" 2>/dev/null | grep -i joplin

# Then use that exact path with --joplin-db
```

### "User with ID X not found"

**Solution:** 
- Check your user ID: `sqlite3 posterchanai.db "SELECT id, username FROM users;"`
- Make sure you're using the correct ID
- Make sure Posterchanai database exists and is accessible

### "Permission denied" or "Cannot access database"

**Solution:**
```bash
# Make sure you have read access to Joplin database
chmod 644 ~/.config/joplin-desktop/database.sqlite

# Make sure you have write access to posterchanai database
chmod 644 posterchanai.db
```

### "ModuleNotFoundError: No module named 'sqlalchemy'"

**Solution:** You're not in the virtual environment or dependencies aren't installed:
```bash
# Make sure virtual environment is activated
source venv/bin/activate

# Install dependencies if needed
pip install -r requirements.txt
```

### Script runs but no notes appear

**Possible causes:**
- Joplin database is empty or corrupted
- Notes are in a different format than expected
- Check the script output for errors

**Debug:**
```bash
# Check if Joplin database has notes
sqlite3 ~/.config/joplin-desktop/database.sqlite "SELECT COUNT(*) FROM notes;"

# Check if migration actually created notes in posterchanai
sqlite3 posterchanai.db "SELECT COUNT(*) FROM notes;"
```

## What Gets Migrated

✅ **Migrated:**
- All folders/notebooks (with hierarchy preserved)
- All notes (markdown content)
- Tags (as comma-separated values)
- Creation and update timestamps

❌ **NOT Migrated:**
- Todo items (skipped by default)
- File attachments/images (only note text content)
- Note links between notes
- Joplin-specific formatting

## After Migration

1. **Access your notes:** Type `notes` in the Posterchanai chat
2. **Organize:** Create new folders or move notes around
3. **Edit:** Click any note to edit it
4. **Search:** Use the search box to find notes
5. **Tag:** Add tags to organize notes

## Need Help?

If you encounter issues:
1. Run with `--dry-run` first to see what would happen
2. Check the error messages carefully
3. Verify file paths and permissions
4. Make sure both databases are accessible
