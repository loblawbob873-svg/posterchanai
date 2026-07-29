# Notes Feature - Complete Guide

## Overview

The Notes feature provides a full-featured note-taking system with search, folders, tags, and attachment support.

## Accessing Notes

### Via UI Button
- Click the **"📝 Notes"** button in the quick actions bar (under PIM button)
- Opens a full-screen notes browser

### Via Command
- Type `notes` in chat
- Opens the notes browser

### Via Modal
- Type `notes` command (opens modal)
- Or use the notes browser and click a note

## Commands

### Basic Commands
- `notes` - Open notes browser
- `notes search <query>` - Search notes by title/content
- `notes folder <name>` - Filter notes by folder
- `notes new` - Create new note
- `notes list` - List all notes

### Natural Language Commands (LLM-trained)
The system understands natural language queries:

- `note find memes` → `notes search memes`
- `find note about groceries` → `notes search groceries`
- `note about project` → `notes search project`
- `search notes for recipes` → `notes search recipes`
- `show my notes` → `notes`
- `open notes` → `notes`
- `notes in work folder` → `notes folder work`

### Voice Commands
- "show my notes" / "open notes" → Opens notes
- "search notes <query>" / "find note <query>" → Searches notes
- "note find <query>" → Searches notes
- "notes in <folder>" → Filters by folder

## Autocomplete

### Tab Autocomplete
When typing note commands, press **Tab** for autocomplete:

1. **Command autocomplete:**
   - Type `notes ` + Tab → Shows: `search | folder | new | list`
   - Type `notes s` + Tab → Autocompletes to `notes search `

2. **Note title autocomplete:**
   - Type `notes search ` + Tab → Shows matching note titles
   - Type `note find m` + Tab → Shows notes starting with "m"
   - Type `find note mem` + Tab → Shows notes containing "mem"

3. **Smart suggestions:**
   - As you type, matching note titles appear
   - Single match auto-completes
   - Multiple matches show in toast notification

### Example Autocomplete Flow
```
User types: "note find m"
Press Tab → Shows: "memes, meeting notes, music ideas"
User types: "note find me"
Press Tab → Auto-completes to: "note find memes"
```

## Features

### Search
- **Real-time search** as you type
- Searches both **title** and **content**
- **Tag filtering** with `tag:` prefix
- **Folder filtering** via sidebar

### Organization
- **Folders** - Organize notes into folders
- **Tags** - Tag notes for easy filtering
- **Pin** - Pin important notes to top
- **Sort** - Pinned first, then by date

### Attachments
- **All file types** supported
- Images display as thumbnails
- PDFs and documents show with icons
- Files stored securely per user

### Markdown Support
- Full markdown formatting
- Headers, lists, links, images
- Code blocks
- Inline formatting

## API Endpoints

### Notes
- `GET /api/notes` - List notes (with search/folder filters)
- `GET /api/notes/{id}` - Get single note
- `POST /api/notes` - Create note
- `PUT /api/notes/{id}` - Update note
- `DELETE /api/notes/{id}` - Delete note

### Folders
- `GET /api/notes/folders` - List folders
- `POST /api/notes/folders` - Create folder
- `PUT /api/notes/folders/{id}` - Update folder
- `DELETE /api/notes/folders/{id}` - Delete folder

### Attachments
- `GET /api/notes/files/{username}/{note_id}/{filename}` - Download attachment

## Storage

Notes are stored in SQLite database. Attachments are stored using the same user storage structure as chat files:
```
{upload_path}/{username}/notes/{note_id}/{filename}
```

Where:
- `upload_path` is the same setting used for all user storage (default: `/var/lib/posterchanai`)
- Chat files are stored at: `{upload_path}/{username}/{conversation_id}/{filename}`
- Notes attachments are stored at: `{upload_path}/{username}/notes/{note_id}/{filename}`

This uses the same `upload_path` setting configured in Admin → Storage, ensuring consistent storage management.

**⚠️ Load Balancing Configuration:**

For load-balanced setups with multiple nodes, configure storage using one of these methods:

### Option 1: Storage Server Proxying (Recommended)

Proxy file requests to a designated storage node:

1. **Storage Node**: Leave `storage_server_url` empty, set `upload_path` to local directory
2. **Client Nodes**: Set `storage_server_url` to storage node URL (e.g., `http://192.168.0.10:3051`)
3. All file requests from client nodes will be proxied to the storage node

**Benefits:** No shared filesystem required, simpler setup

### Option 2: Shared Storage

All nodes share the same storage location:

- Set `upload_path` to a shared network filesystem (NFS, CIFS/SMB, etc.)
- Mount the same shared storage on all nodes to the same path
- Each node uses the same `upload_path` pointing to shared storage

**Setup Example (NFS):**
```bash
# On all nodes, mount the same NFS share
sudo mount -t nfs nfs-server:/export/posterchanai /var/lib/posterchanai
```

**Why:** If Node A creates a note with attachments, Node B needs access to those files when serving the note.

See [Load Balancing Documentation](ADVANCED.md#load-balancing) for complete setup details.

## Migration from Joplin

See [Joplin Migration Guide](JOPLIN_MIGRATION.md) for complete instructions.

Quick migration:
```bash
python scripts/migrate_joplin.py --user-id 1
```

## Tips

1. **Quick search**: Type `note find <keyword>` for instant search
2. **Autocomplete**: Use Tab to see note title suggestions
3. **Voice**: Say "note find memes" for hands-free search
4. **Folders**: Organize by project, topic, or date
5. **Tags**: Use tags for cross-cutting categories
6. **Pin**: Pin frequently accessed notes

## Keyboard Shortcuts

- **Tab** - Autocomplete commands/note titles
- **Enter** - Send command
- **Shift+Enter** - New line in note editor
- **Escape** - Close modal/browser

## Troubleshooting

### "Error loading notes" Message

If you see "Error loading notes. Please try again.":

1. **Check browser console** (F12 → Console) for detailed error messages
2. **Check server logs** for backend errors - look for "Error creating note" or "Error fetching notes"
3. **Verify authentication** - Make sure you're logged in
4. **Check database** - Ensure the notes tables exist (they're created automatically on first startup)
5. **Network issues** - Check if the API endpoint `/api/notes` is accessible
6. **500 Internal Server Error** - If you see this, check server logs for the actual error. The API now returns JSON errors instead of HTML, so you should see the error detail in the browser console.

**Common causes:**
- Database migration not run (restart server to auto-migrate)
- Missing `attachments` column (auto-added on startup)
- Pydantic version mismatch (should use v2.5.0+)

### "New Note" Button Not Working

If clicking "New Note" doesn't open the editor:

1. **Refresh the page** - The notes manager initializes on page load
2. **Check console** - Look for JavaScript errors
3. **Try opening notes modal first** - Click a note in the list, then try "New Note"

### Notes Not Saving

If notes aren't saving:

1. **Check title** - Notes require a title (auto-save only works after title is set)
2. **Check console** - Look for API errors
3. **Check server logs** - Backend errors will be logged
4. **Verify permissions** - Ensure you have write access to the upload directory

### Attachments Not Displaying

If attachments aren't showing:

1. **Check file path** - Files are stored at `{upload_path}/{username}/notes/{note_id}/`
2. **Check permissions** - Ensure files are readable
3. **Check console** - Look for 404 errors on attachment URLs
4. **Verify migration** - If migrated from Joplin, ensure files were copied correctly
