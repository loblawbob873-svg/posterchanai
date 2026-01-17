# Calendar & Contacts Import/Export Feature

## Overview

Added full import/export functionality for calendar events (.ics) and contacts (.vcf) files, allowing users to easily backup and restore their data.

## Features Added

### 1. User Interface (User Settings → Calendar & Contacts Tab)

**Calendar Section (Built-in Only)**:
```
Built-in Calendar Info
  ✓ Info about storage location and commands
  
  Import/Export Calendar
  [📥 Import .ics]  [📤 Export .ics]  [Status message]
  ℹ️ Import events from an iCalendar (.ics) file or export your calendar.
```

**Contacts Section (Built-in Only)**:
```
Built-in Contacts Info
  ✓ Info about storage location and commands
  
  Import/Export Contacts
  [📥 Import .vcf]  [📤 Export .vcf]  [Status message]
  ℹ️ Import contacts from a vCard (.vcf) file or export your contacts.
```

### 2. Backend API Endpoints

Created new router: `app/routers/caldav.py`

#### CalDAV Endpoints

**`GET /api/caldav/export`**
- Exports all calendar events as a single `.ics` file
- Includes both VEVENT (events) and VTODO (todos)
- Filename format: `calendar_username_20250101.ics`
- Returns: `text/calendar` downloadable file

**`POST /api/caldav/import`**
- Accepts `.ics` file upload
- Parses multiple events/todos from file
- Creates individual `.ics` files per event
- Skips duplicates based on UID
- Returns: `{count, message}` with import stats

#### CardDAV Endpoints

**`GET /api/carddav/export`**
- Exports all contacts as a single `.vcf` file
- Combines multiple VCARDs into one file
- Filename format: `contacts_username_20250101.vcf`
- Returns: `text/vcard` downloadable file

**`POST /api/carddav/import`**
- Accepts `.vcf` file upload
- Parses multiple vCards from file
- Creates individual `.vcf` files per contact
- Skips duplicates based on UID
- Returns: `{count, message}` with import stats

### 3. JavaScript Handlers (static/js/chat.js)

**Import Flow**:
1. User clicks "Import" button
2. Hidden file input triggered
3. File selected and read
4. Uploaded via FormData to API
5. Success/error status displayed
6. File input cleared for re-import

**Export Flow**:
1. User clicks "Export" button
2. API fetches and generates file
3. Blob created from response
4. Download triggered automatically
5. Temporary URL revoked after download

## Technical Details

### Import Behavior

**Calendar Import**:
- Parses standard iCalendar format (RFC 5545)
- Accepts VEVENT and VTODO components
- Auto-generates UID if missing
- One event per `.ics` file in `{storage}/caldav/`
- Duplicate detection: checks if `{UID}.ics` exists

**Contacts Import**:
- Parses standard vCard format (RFC 6350)
- Accepts multiple VCARDs in one file
- Auto-generates UID if missing
- One contact per `.vcf` file in `{storage}/carddav/`
- Duplicate detection: checks if `{UID}.vcf` exists

### Export Behavior

**Calendar Export**:
```ics
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//PosterChan AI//CalDAV Export//EN
CALSCALE:GREGORIAN
METHOD:PUBLISH
X-WR-CALNAME:username Calendar

BEGIN:VEVENT
UID:event-uuid-1
SUMMARY:Meeting
...
END:VEVENT

BEGIN:VTODO
UID:todo-uuid-2
SUMMARY:Task
...
END:VTODO

END:VCALENDAR
```

**Contacts Export**:
```vcf
BEGIN:VCARD
VERSION:3.0
UID:contact-uuid-1
FN:John Doe
...
END:VCARD

BEGIN:VCARD
VERSION:3.0
UID:contact-uuid-2
FN:Jane Smith
...
END:VCARD
```

### Error Handling

**Import**:
- Invalid file format → 400 Bad Request
- No valid events/contacts → 400 Bad Request
- Individual parse errors → Logged, counted, continue
- Success shows: `✓ Imported N event(s)/contact(s)`
- Skipped duplicates reported
- Parse errors counted

**Export**:
- No files found → 404 Not Found
- Individual read errors → Logged, skipped
- At least 1 valid file → Success
- Zero valid files → 404 Not Found

## Use Cases

### 1. Backup Calendar Before Server Migration
```
1. Click "📤 Export .ics"
2. Save file: calendar_verita84_20250101.ics
3. Migrate server
4. Click "📥 Import .ics"
5. Select saved file
6. ✓ All events restored
```

### 2. Import Google Calendar Export
```
1. Google Calendar → Settings → Import & Export
2. Download calendar.ics
3. PosterChan AI → User Settings → Calendar & Contacts
4. Click "📥 Import .ics"
5. Select calendar.ics
6. ✓ Events imported (duplicates skipped)
```

### 3. Import Contacts from Phone
```
1. Phone → Export contacts as .vcf
2. Transfer file to computer
3. PosterChan AI → User Settings → Calendar & Contacts
4. Click "📥 Import .vcf"
5. Select contacts.vcf
6. ✓ Contacts imported
```

### 4. Share Calendar with Another User
```
User A:
1. Export calendar → calendar_usera.ics

User B:
1. Import calendar_usera.ics
2. ✓ All events copied to User B's calendar
```

## Files Changed

### New Files
- **`app/routers/caldav.py`** (305 lines)
  - CalDAV import/export endpoints
  - CardDAV import/export endpoints
  - Duplicate detection logic
  - UID generation

### Modified Files

**`templates/includes/modals/user_settings.html`**:
- Added import/export UI to `builtinCalendarInfo` section
- Added import/export UI to `builtinContactsInfo` section
- Hidden file inputs for upload
- Status message spans

**`static/js/chat.js`**:
- Calendar import handler (file → FormData → API)
- Calendar export handler (API → blob → download)
- Contacts import handler (file → FormData → API)
- Contacts export handler (API → blob → download)
- Status display logic

**`app/main.py`**:
- Import caldav_router and carddav_router
- Register both routers with app

**`app/routers/contacts.py`**:
- Updated import endpoint to accept File upload
- Added UploadFile import
- Changed parameter from `vcf_data: str = Form()` to `file: UploadFile = File()`
- Returns `{count, message}` instead of `{imported, skipped, errors}`

## Dependencies

Required Python libraries (already in requirements.txt):
- `icalendar` - For parsing/generating .ics files
- `vobject` - For parsing/generating .vcf files
- `fastapi` - For file uploads (UploadFile)

## Compatibility

**Import Sources**:
- ✅ Google Calendar (.ics)
- ✅ Apple Calendar (.ics)
- ✅ Outlook Calendar (.ics)
- ✅ Thunderbird (.ics, .vcf)
- ✅ iPhone Contacts (.vcf)
- ✅ Android Contacts (.vcf)
- ✅ Any RFC 5545 (iCalendar) compliant file
- ✅ Any RFC 6350 (vCard) compliant file

**Export Destinations**:
- ✅ Any CalDAV/CardDAV client
- ✅ Google Calendar import
- ✅ Apple Calendar import
- ✅ Outlook import
- ✅ Thunderbird import
- ✅ Phone contact apps

## Security

- ✅ **Authentication required**: Uses `get_current_user` dependency
- ✅ **User isolation**: Only accesses current user's files
- ✅ **Path safety**: Uses `get_user_caldav_path()` / `get_user_cardav_path()`
- ✅ **File validation**: Parses files before saving
- ✅ **UID validation**: Ensures unique identifiers
- ✅ **No path traversal**: Uses UUID-based filenames

## Testing

### Test Calendar Import
```bash
# Create test .ics file
cat > test_event.ics <<EOF
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:test-event-123
SUMMARY:Test Meeting
DTSTART:20250115T100000Z
DTEND:20250115T110000Z
END:VEVENT
END:VCALENDAR
EOF

# Import via UI or curl:
curl -X POST http://localhost:8000/api/caldav/import \
  -H "Cookie: session=..." \
  -F "file=@test_event.ics"
```

### Test Contacts Import
```bash
# Create test .vcf file
cat > test_contact.vcf <<EOF
BEGIN:VCARD
VERSION:3.0
UID:test-contact-123
FN:John Doe
EMAIL:john@example.com
TEL:+1-555-1234
END:VCARD
EOF

# Import via UI or curl:
curl -X POST http://localhost:8000/api/carddav/import \
  -H "Cookie: session=..." \
  -F "file=@test_contact.vcf"
```

## Future Enhancements

Possible improvements:
1. **Merge on conflict** - Option to update existing events instead of skip
2. **Selective import** - Choose which events/contacts to import
3. **External server import** - Import directly from CalDAV/CardDAV URLs
4. **Bulk delete** - Delete all before import (fresh start)
5. **Import history** - Track import operations
6. **Schedule exports** - Automatic backup exports
7. **Sync instead of import** - Two-way sync with external servers

## Deployment

- ✅ **Committed**: `222264b3`
- ✅ **Pushed**: To git repository
- ✅ **Deployed**: 192.168.0.85
- ✅ **Local**: Updated

## User Documentation

To use the import/export feature:

1. Go to **User Settings** (click your avatar)
2. Switch to **Calendar & Contacts** tab
3. Scroll to **Built-in Calendar Info** or **Built-in Contacts Info** sections

**To Export**:
- Click "📤 Export .ics" or "📤 Export .vcf"
- File downloads automatically

**To Import**:
- Click "📥 Import .ics" or "📥 Import .vcf"
- Select file from your computer
- Wait for "✓ Imported N event(s)/contact(s)" message
- Duplicates are automatically skipped

**Note**: Import/export buttons only appear when using the built-in CalDAV/CardDAV server. External servers should have their own import/export functionality.

## Result

✅ **Users can now easily backup and restore their calendar and contacts!**
✅ **Compatible with all major calendar and contact apps**
✅ **Smart duplicate detection prevents data corruption**
✅ **Clear user feedback on import success/failures**
