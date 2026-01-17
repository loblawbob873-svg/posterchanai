# CalDAV/CardDAV Server Selection

## Overview

Users can now choose between using the **built-in CalDAV/CardDAV servers** or connecting to **external servers** (like Nextcloud, Radicale, Baïkal, etc.).

## Features

### Built-in Servers (Default)
- **Local Storage**: Calendar events (`.ics`) and contacts (`.vcf`) stored in user's storage directory
- **No External Dependencies**: Works offline, no internet required
- **Full Integration**: Works seamlessly with chat commands (`cal add`, `contacts add`, etc.)
- **WebDAV/CalDAV/CardDAV Protocol**: Accessible from any standard DAV client
- **Import/Export**: Full support for importing and exporting data

### External Servers
- **Connect to Existing**: Use your existing CalDAV/CardDAV infrastructure
- **Multiple Calendars**: Connect to multiple CalDAV calendars simultaneously
- **Compatible with**: Nextcloud, ownCloud, Radicale, Baïkal, iCloud, Google Calendar (via CalDAV), etc.

## How to Choose

### Via User Settings UI

1. Open **User Settings** → **Calendar & Contacts** tab
2. Under **Calendar Server**, select:
   - **Built-in CalDAV Server** (default)
   - **External CalDAV Server(s)**
3. Under **Contacts Server**, select:
   - **Built-in CardDAV Server** (default)
   - **External CardDAV Server**
4. Click **Save All Settings**

### Configuration Behavior

#### Built-in Mode (Default)
When "Built-in CalDAV Server" is selected:
- Calendar events are automatically stored in: `{storage_path}/{username}/caldav/*.ics`
- Contacts are automatically stored in: `{storage_path}/{username}/carddav/*.vcf`
- Chat commands (`cal add`, `contacts add`) save directly to local files
- DAV servers serve these files via standard CalDAV/CardDAV protocol

**Sync URLs** (from Storage & Cloud tab):
```
CalDAV:  http://server:8081/caldav/{username}/
CardDAV: http://server:8082/carddav/{username}/
```

#### External Mode
When "External CalDAV Server(s)" is selected:
- Add one or more external CalDAV calendar URLs
- Configure username and password for each calendar
- Chat commands interact with external servers via CalDAV protocol
- No local `.ics` files are created (unless imported)

For CardDAV, configure:
- CardDAV server URL
- Username and password
- Chat commands interact with external server via CardDAV protocol

## Technical Implementation

### Backend Changes

**File**: `app/services/caldav_service.py`

#### get_user_calendars()
```python
def get_user_calendars(user_id: int, db: Session = None) -> List[Dict[str, str]]:
    # Check if user wants built-in CalDAV
    use_builtin = db.query(UserSetting).filter(
        UserSetting.user_id == user_id,
        UserSetting.key == "use_builtin_caldav"
    ).first()
    
    if use_builtin and use_builtin.value == "true":
        # Return built-in CalDAV config
        return [{
            "name": "Built-in Calendar",
            "url": f"http://{hostname}:{port}/caldav/{username}/",
            "username": username,
            "password": "",  # Uses same auth
            "builtin": True
        }]
    
    # Otherwise, return external calendars
    ...
```

#### get_user_contacts_config()
```python
def get_user_contacts_config(user_id: int, db: Session = None) -> Optional[Dict[str, str]]:
    # Check if user wants built-in CardDAV
    use_builtin = db.query(UserSetting).filter(
        UserSetting.user_id == user_id,
        UserSetting.key == "use_builtin_cardav"
    ).first()
    
    if use_builtin and use_builtin.value == "true":
        # Return built-in CardDAV config
        return {
            "url": f"http://{hostname}:{port}/carddav/{username}/",
            "username": username,
            "password": "",
            "name": "Built-in CardDAV",
            "builtin": True
        }
    
    # Otherwise, return external config
    ...
```

### Frontend Changes

**File**: `static/js/chat.js`

- Added `calendarServerType` and `contactsServerType` dropdown elements
- Toggle visibility of external vs built-in settings sections
- Save server type preference as `use_builtin_caldav` and `use_builtin_cardav`
- Load and restore server type on settings open

**File**: `templates/includes/modals/user_settings.html`

- Added dropdown selectors for calendar and contacts server type
- Conditional sections for external server configuration
- Info sections explaining built-in server usage

### Database Schema

**UserSetting table** stores per-user preferences:

| Key | Value | Description |
|-----|-------|-------------|
| `use_builtin_caldav` | `"true"` or `"false"` | Use built-in CalDAV server |
| `use_builtin_cardav` | `"true"` or `"false"` | Use built-in CardDAV server |
| `caldav_calendars` | JSON array | External calendar configs (only if external mode) |
| `carddav_config` | JSON object | External contacts config (only if external mode) |

## Use Cases

### Use Case 1: Local-Only User
**Scenario**: User wants everything stored locally with no external dependencies.

**Configuration**:
- Calendar Server: Built-in CalDAV Server
- Contacts Server: Built-in CardDAV Server

**Benefits**:
- Works offline
- No external accounts needed
- Data fully under user control
- Fast and reliable

### Use Case 2: Nextcloud User
**Scenario**: User already uses Nextcloud for calendar and contacts.

**Configuration**:
- Calendar Server: External CalDAV Server(s)
  - URL: `https://nextcloud.example.com/remote.php/dav/calendars/username/`
- Contacts Server: External CardDAV Server
  - URL: `https://nextcloud.example.com/remote.php/dav/addressbooks/username/contacts/`

**Benefits**:
- Sync with existing Nextcloud data
- Use Posterchanai as an interface to Nextcloud
- Keep all data centralized

### Use Case 3: Hybrid User
**Scenario**: User wants local calendar but syncs contacts from external server.

**Configuration**:
- Calendar Server: Built-in CalDAV Server
- Contacts Server: External CardDAV Server (Nextcloud)

**Benefits**:
- Calendar data local and fast
- Contacts synced across multiple devices via Nextcloud
- Best of both worlds

## Migration

### From External to Built-in

1. **Export existing data**:
   ```bash
   curl -u "user:pass" http://external-server/caldav/user/ > calendar.ics
   curl -u "user:pass" http://external-server/carddav/user/ > contacts.vcf
   ```

2. **Change to built-in mode** in User Settings

3. **Import data**:
   - Use "Import from Radicale" button for calendars
   - Use `/api/contacts/import` for contacts (see CALDAV_CARDAV_IMPORT_EXPORT.md)

### From Built-in to External

1. **Export from built-in**:
   - Calendar: Use "Export Calendar" button in User Settings
   - Contacts: Use `/api/contacts/export` endpoint

2. **Import to external server** using its native tools

3. **Change to external mode** in User Settings

4. **Configure external server URLs**

## Chat Commands

Chat commands work transparently with both built-in and external servers:

### Calendar Commands
```
cal add dinner at 7pm tomorrow
cal list
cal edit <uid> location Kitchen
cal delete <uid>
```

### Contacts Commands
```
contacts add John Doe john@example.com 555-1234
contacts list
contacts search john
contacts edit <uid> phone 555-9999
contacts delete <uid>
```

The backend automatically routes these commands to the appropriate server (built-in or external) based on user settings.

## Advantages of Built-in Servers

1. **Simplicity**: No external configuration needed
2. **Performance**: Local file access is instant
3. **Privacy**: Data never leaves your server
4. **Reliability**: No dependency on external services
5. **Offline**: Works without internet connection
6. **Integration**: Direct file system access for backups/scripts
7. **Control**: Full ownership of data files

## Advantages of External Servers

1. **Existing Infrastructure**: Use what you already have
2. **Cross-Platform Sync**: Sync with mobile devices, desktop apps
3. **Centralization**: Single source of truth across multiple apps
4. **Enterprise Features**: Advanced features from Nextcloud, etc.
5. **Team Collaboration**: Share calendars with others

## Troubleshooting

### Built-in Server Not Working

**Check if servers are enabled**:
```bash
# Admin UI → Site Settings → WebDAV/CalDAV/CardDAV Server Settings
# Enable CalDAV Server: ON
# Enable CardDAV Server: ON
```

**Check if servers are running**:
```bash
ss -tlnp | grep ':8081\|:8082'
```

**Check file permissions**:
```bash
ls -la /raid/posterchanai/username/caldav/
ls -la /raid/posterchanai/username/carddav/
```

### External Server Connection Issues

**Test connection manually**:
```bash
curl -u "username:password" https://external-server/caldav/user/
```

**Check firewall**: Ensure external server is accessible

**Verify credentials**: Double-check username and password

### Commands Not Saving

**Check User Settings**: Verify correct server type is selected

**Check Logs**:
```bash
journalctl -u posterchanai.service | grep -i caldav
```

**Test directly**: Use DAV client to verify server works

## Security Notes

- Built-in servers use the same authentication as main app
- External servers use their own credentials (stored encrypted)
- Built-in mode = fewer attack surfaces (no external dependencies)
- External mode = credentials transmitted over network (use HTTPS!)

## Performance

### Built-in Servers
- **File access**: ~1-5ms
- **Search**: Instant (local filesystem)
- **Sync**: Not needed (data already local)

### External Servers
- **Network latency**: Depends on server location (10-500ms)
- **Search**: Depends on server performance
- **Sync**: Automatic via DAV protocol

---

**Status**: ✅ Fully implemented and ready to use!  
**Default**: Built-in servers (for simplicity and privacy)  
**Flexibility**: Switch anytime without data loss
