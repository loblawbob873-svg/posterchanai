# Quick Start: Migrate Joplin Notes

## Simple 3-Step Process

### Step 1: Open Terminal/Command Line

**Linux/macOS:**
- Press `Ctrl+Alt+T` (Linux) or open Terminal app (macOS)

**Windows:**
- Press `Win+R`, type `cmd`, press Enter
- Or search for "Command Prompt" or "PowerShell"

### Step 2: Navigate to Posterchanai and Activate Environment

```bash
cd /home/verita84/posterchanai
source venv/bin/activate
```

(You should see `(venv)` appear in your prompt)

### Step 3: Run Migration

**First, find your user ID:**
```bash
sqlite3 posterchanai.db "SELECT id, username FROM users;"
```

**Then run migration (replace `1` with your user ID):**
```bash
python scripts/migrate_joplin.py --user-id 1
```

That's it! The script will automatically find your Joplin database.

## If Auto-Detection Doesn't Work

**Find your Joplin database:**
```bash
# Linux/macOS
find ~ -name "database.sqlite" -path "*joplin*" 2>/dev/null
```

**Then specify the path:**
```bash
python scripts/migrate_joplin.py \
  --joplin-db ~/.config/joplin-desktop/database.sqlite \
  --user-id 1
```

## Test First (Recommended)

Before migrating, test what will happen:
```bash
python scripts/migrate_joplin.py --user-id 1 --dry-run
```

## After Migration

1. Open Posterchanai in your browser: `http://localhost:3051`
2. Type `notes` in the chat
3. Your migrated notes will appear!

## Common Issues

**"Command not found: python"**
- Try `python3` instead of `python`

**"No module named 'sqlalchemy'"**
- Make sure you activated the virtual environment: `source venv/bin/activate`

**"Joplin database not found"**
- Make sure Joplin has been opened at least once
- Or specify the path manually with `--joplin-db`
