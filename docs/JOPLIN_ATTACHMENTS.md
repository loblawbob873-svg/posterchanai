# Joplin Notes Migration with All File Types

The migration script now fully supports migrating **all file types** from Joplin notes, including images, documents, videos, audio, archives, code files, and more!

## How It Works

### 1. **Resource Detection**
- The script scans Joplin's `resources` table to find all attachments (all file types)
- It looks for resource references in note content using the pattern: `](:/resource_id)`
- Joplin stores resources in a `resources/` directory next to the database
- The script automatically detects file types from MIME types and file extensions

### 2. **File Migration**
- For each resource found in a note:
  - Locates the resource file in Joplin's resources directory (tries multiple paths/extensions)
  - Preserves original filename and extension
  - Copies it to Posterchanai's storage: `{upload_path}/{username}/notes/{note_id}/{filename}`
  - Updates the note content to reference the new file location
  - Handles all file types: images, documents, videos, audio, archives, code, etc.

### 3. **Storage Structure**
Attachments are stored in:
```
{upload_path}/{username}/notes/{note_id}/
├── image_20240101_120000.png
├── document_20240101_120001.pdf
├── video_20240101_120002.mp4
├── audio_20240101_120003.mp3
├── archive_20240101_120004.zip
└── ...
```

All file types are preserved with their original extensions and names.

Default `upload_path`: `/var/lib/posterchanai`

### 4. **Content Updates**
Joplin references like:
```markdown
![Image](:/abc123def456...)
```

Are converted to:
```markdown
![Image](/api/notes/files/username/note_id/filename.png)
```

## Running the Migration

The migration script automatically handles attachments:

```bash
cd /home/verita84/posterchanai
source venv/bin/activate
python scripts/migrate_joplin.py --user-id 1
```

The script will:
1. ✅ Find Joplin's resources directory automatically
2. ✅ Extract all resource references from notes
3. ✅ Copy files to Posterchanai storage
4. ✅ Update note content with new file paths
5. ✅ Store attachment metadata in the database

## What Gets Migrated

✅ **Migrated (All File Types):**
- **Images**: PNG, JPG, JPEG, GIF, WebP, SVG, BMP, TIFF, ICO
- **Documents**: PDF, DOC, DOCX, XLS, XLSX, PPT, PPTX, ODT, ODS, ODP
- **Text Files**: TXT, MD, HTML, CSS, JS, JSON, XML
- **Archives**: ZIP, RAR, TAR, GZ, 7Z
- **Audio**: MP3, WAV, OGG, M4A, WebM
- **Video**: MP4, MPEG, MOV, AVI, WebM, MKV
- **Code**: PY, Java, C, C++, C#, SH, and more
- **Other**: Any file type referenced in Joplin notes
- Note content with updated file references

❌ **NOT Migrated:**
- Resources not referenced in any note
- Corrupted or missing resource files
- Resources that can't be found in the filesystem (script tries multiple paths)

**Note**: The script attempts to find files with or without extensions, and handles various MIME types automatically.

## Troubleshooting

### "Resources directory not found"

**Solution:** The script looks for resources in:
- `{joplin_db_path}/../resources/`
- `{joplin_db_path}/../../resources/`

If your Joplin installation uses a different structure, you may need to:
1. Check where Joplin stores resources
2. Create a symlink, or
3. Manually copy resources after migration

### "Resource file not found"

**Possible causes:**
- Resource was deleted from Joplin but still referenced in note
- Resource is stored in a different location
- File extension doesn't match

**Solution:** The note will still be migrated, but the attachment reference will remain as the original Joplin reference (which won't work). You can manually fix these after migration.

### "ERROR migrating attachment"

**Possible causes:**
- Permission issues
- Disk space full
- Invalid file data

**Solution:** Check the error message and ensure:
- You have write permissions to the upload directory
- There's enough disk space
- The Joplin resource file is not corrupted

## Viewing Attachments

After migration:

1. **Open notes**: Type `notes` in Posterchanai chat
2. **View attachments**: Click on a note with attachments
3. **Images**: Display as thumbnails in the note editor
4. **Other files**: Show with appropriate icons:
   - 📄 PDFs and documents
   - 🎬 Videos
   - 🎵 Audio files
   - 📝 Office documents
   - 📦 Archives
   - 💻 Code files
   - 📎 Other file types
5. **All files**: Click to download or view

Attachments are displayed in the note editor with:
- Image previews (thumbnails)
- PDF icons with download links
- File names and links

## Manual Fixes

If some attachments didn't migrate correctly:

1. **Check the migration log** - Look for "WARNING" or "ERROR" messages
2. **Find missing resources** - Check Joplin's resources directory
3. **Manually copy files** - Copy to `{upload_path}/{username}/notes/{note_id}/`
4. **Update note content** - Edit the note to fix file references

## Storage Location

To find where your attachments are stored:

```bash
# Check upload path setting
sqlite3 posterchanai.db "SELECT value FROM settings WHERE key = 'upload_path';"

# List attachments for a user
ls -la /var/lib/posterchanai/username/notes/
```

## Notes

- **File sizes**: Large attachments may take time to migrate
- **Disk space**: Ensure you have enough space for all attachments
- **Backup**: Consider backing up Joplin resources before migration
- **Performance**: Migration may be slow with many large attachments
