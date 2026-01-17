# CalDAV/CardDAV Integration Status

## Current Implementation

### Built-in Servers ✅
- **CalDAV Server**: `app/services/caldav_server.py` - Functional, runs on port 8081
- **CardDAV Server**: `app/services/cardav_server.py` - Functional, runs on port 8082
- **Storage Paths**:
  - CalDAV: `{upload_path}/{username}/caldav/*.ics`
  - CardDAV: `{upload_path}/{username}/carddav/*.vcf`

### Calendar/Contacts Features ⚠️
- **Calendar UI**: `templates/includes/modals/calendar.html` - ✅ Exists
- **Contacts UI**: `templates/includes/modals/contacts.html` - ✅ Exists
- **Frontend**: `static/js/app.js` - ✅ Handles modal interactions
- **Storage**: Uses **chat commands** (`cal add`, `contacts add`) via `command_service.py`

## Problem Identified

**The calendar and contacts features DO NOT save to the built-in CalDAV/CardDAV directories!**

### What Currently Happens:

1. **User adds calendar event** → Sends chat command `cal add ...`
2. **Command service** → Saves to external CalDAV server (if configured)
3. **Built-in CalDAV server** → Serves empty directory (no .ics files)

Same issue with contacts - they don't save to `/carddav/*.vcf`.

### Evidence:

```bash
# Built-in CalDAV directory is empty:
$ ls /raid/posterchanai/verita84@poster.place/caldav/
# (directory doesn't even exist)

# Command service doesn't import DAV path functions:
$ grep -n "get_user_caldav_path" app/services/command_service.py
# (no results)
```

## Impact

❌ **Built-in CalDAV/CardDAV servers serve NO data**  
❌ **Calendar clients connecting to port 8081/8082 see empty calendars**  
❌ **Calendar/contacts features only work with external servers**  

## Solution Needed

### Option 1: Integrate CalDAV/CardDAV into Command Service (Recommended)

Modify `app/services/command_service.py` to save events/contacts to BOTH:
1. External CalDAV servers (existing behavior)
2. Built-in CalDAV/CardDAV directories (new)

**Changes required**:
```python
# In cal_add_event():
from app.services.caldav_server import get_user_caldav_path

# After adding to external calendar:
caldav_path = get_user_caldav_path(user, db)
ics_file = caldav_path / f"{event_uid}.ics"
with open(ics_file, 'w') as f:
    f.write(ical_data)
```

### Option 2: Make Built-in Server the Primary Storage

Store all calendar/contacts data in the built-in directories first, then optionally sync to external servers.

**Changes required**:
- Refactor command service to always save to local CalDAV/CardDAV
- Add sync function to push changes to external servers
- Add sync scheduler to keep external servers in sync

### Option 3: API Direct Integration

Bypass chat commands entirely for calendar/contacts:
- Frontend calls `/api/calendar/events` POST directly
- Backend saves to built-in CalDAV directory
- Built-in server serves the data

## Recommendation

**Implement Option 1** - it's the least disruptive and maintains backwards compatibility:

1. ✅ Keep existing chat command interface
2. ✅ Keep existing external CalDAV support  
3. ✅ Add built-in CalDAV storage as a "local backup"
4. ✅ Built-in server immediately becomes useful

## Files to Modify

1. **`app/services/command_service.py`**:
   - Import `get_user_caldav_path`, `get_user_cardav_path`
   - In `cal_add_event()`: Save .ics file to local CalDAV directory
   - In `cal_edit_event()`: Update .ics file in local CalDAV directory
   - In `cal_delete_event()`: Delete .ics file from local CalDAV directory
   - In `contacts_add()`: Save .vcf file to local CardDAV directory
   - In `contacts_edit()`: Update .vcf file in local CardDAV directory
   - In `contacts_delete()`: Delete .vcf file from local CardDAV directory

2. **`app/services/caldav_service.py`** (maybe):
   - Add helper function to save event to local directory
   - Add helper function to read event from local directory

## Testing Checklist

After implementing:
- [ ] Add calendar event via UI
- [ ] Check if .ics file appears in `{username}/caldav/`
- [ ] Connect calendar client to `http://server:8081/caldav/{username}/`
- [ ] Verify event shows up in calendar client
- [ ] Add contact via UI
- [ ] Check if .vcf file appears in `{username}/carddav/`
- [ ] Connect CardDAV client to `http://server:8082/carddav/{username}/`
- [ ] Verify contact shows up in client

## Current Status

🔴 **Not integrated** - Built-in DAV servers exist but store no data  
🟡 **Partial functionality** - Servers run, authentication works, but serve empty collections  
⚠️ **Action required** - Integrate command service with built-in storage  

---

**Next Step**: Implement Option 1 to make the built-in CalDAV/CardDAV servers functional.
