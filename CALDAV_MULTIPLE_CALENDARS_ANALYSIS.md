# Multiple Calendar Support Analysis

## Current Situation

### Built-in CalDAV Server (Single Calendar)
**Storage Structure**:
```
{storage}/
  └── caldav/
      ├── event-uid-1.ics
      ├── event-uid-2.ics
      └── todo-uid-1.ics
```

**Characteristics**:
- ✅ Simple, flat file structure
- ✅ Fast direct file access
- ✅ Easy backup/sync
- ❌ **Only ONE calendar per user**
- ❌ Can't separate work/personal/family calendars

### External CalDAV (Multiple Calendars)
**User Settings**:
```json
{
  "caldav_calendars": [
    {"name": "Work", "url": "https://...", "username": "...", "password": "..."},
    {"name": "Personal", "url": "https://...", "username": "...", "password": "..."},
    {"name": "Family", "url": "https://...", "username": "...", "password": "..."}
  ]
}
```

**Characteristics**:
- ✅ Multiple calendars supported
- ✅ Can organize events by category
- ✅ Full CalDAV protocol support
- ❌ Requires external server
- ❌ Network latency

## The Problem

When using built-in CalDAV, users can only have **one calendar**. All events are stored in the same directory:
- Work meetings
- Personal appointments
- Family events
- Todos/tasks

This is limiting for users who want to organize events by category.

## Solution Options

### Option 1: Subdirectories (Recommended)
Create subdirectories for each calendar within the caldav folder.

**Structure**:
```
{storage}/
  └── caldav/
      ├── default/          # Default calendar
      │   ├── event-1.ics
      │   └── event-2.ics
      ├── work/             # Work calendar
      │   └── event-3.ics
      ├── personal/         # Personal calendar
      │   └── event-4.ics
      └── family/           # Family calendar
          └── event-5.ics
```

**CalDAV URLs**:
```
http://localhost:8081/caldav/username/default/
http://localhost:8081/caldav/username/work/
http://localhost:8081/caldav/username/personal/
http://localhost:8081/caldav/username/family/
```

**Pros**:
- ✅ Logical separation
- ✅ Easy to browse/backup individual calendars
- ✅ Compatible with CalDAV protocol
- ✅ Can be synced separately by calendar apps

**Cons**:
- ⚠️ Need to update CalDAV server to handle paths
- ⚠️ Need UI to manage calendars
- ⚠️ Commands need to specify which calendar

### Option 2: Calendar Metadata in Files
Store calendar name as metadata in each .ics file.

**Structure**:
```
{storage}/
  └── caldav/
      ├── event-1.ics    # X-CALENDAR-NAME:Work
      ├── event-2.ics    # X-CALENDAR-NAME:Personal
      └── event-3.ics    # X-CALENDAR-NAME:Family
```

**Pros**:
- ✅ Simpler file structure
- ✅ No path changes needed

**Cons**:
- ❌ Not standard CalDAV behavior
- ❌ Harder to filter by calendar
- ❌ Can't sync individual calendars
- ❌ Breaking change from CalDAV spec

### Option 3: Hybrid - Single Calendar for Built-in, Multiple for External
Keep built-in simple with one calendar, only support multiple calendars for external servers.

**Current Behavior**:
```javascript
// Built-in mode
if (use_builtin_caldav === 'true') {
  return [{
    "name": "Built-in Calendar",
    "url": "http://localhost:8081/caldav/username/",
    "builtin": true
  }];
}

// External mode
else {
  return [
    {"name": "Work", "url": "...", "builtin": false},
    {"name": "Personal", "url": "...", "builtin": false}
  ];
}
```

**Pros**:
- ✅ No changes needed - already works this way
- ✅ Simple for most users
- ✅ Power users can use external servers

**Cons**:
- ❌ Built-in users can't organize by calendar
- ❌ Inconsistent feature set

## Recommended Implementation: Option 1 (Subdirectories)

### Phase 1: Backend Support

#### 1. Update CalDAV Server (`caldav_server.py`)

**Path Parsing**:
```python
# Current: /caldav/username/
# New:     /caldav/username/calendar-name/

def parse_caldav_path(path: str) -> tuple[str, Optional[str]]:
    """Parse username and calendar name from path."""
    # /caldav/username/ -> (username, None)  # Root
    # /caldav/username/work/ -> (username, "work")
    # /caldav/username/work/event.ics -> (username, "work")
    match = re.match(r'/?([^/]+)(?:/([^/]+))?', path)
    if match:
        username = match.group(1)
        calendar = match.group(2) if match.group(2) and match.group(2) != username else None
        return username, calendar
    return None, None
```

**Storage Path**:
```python
def get_user_calendar_path(user: User, db: Session, calendar_name: str = "default") -> Path:
    """Get the path for a specific calendar."""
    storage = get_storage_service(db)
    user_path = storage.get_user_path(user.username)
    caldav_path = user_path / "caldav" / calendar_name
    caldav_path.mkdir(parents=True, exist_ok=True)
    return caldav_path
```

**PROPFIND Response**:
```python
async def handle_propfind(path: str, user: User, db: Session, depth: str = "0") -> Response:
    """Handle PROPFIND - list calendars or events."""
    username, calendar_name = parse_caldav_path(path)
    
    # Depth 0: List calendar itself
    # Depth 1: List calendars (if root) or events (if calendar)
    
    if calendar_name is None and depth == "1":
        # List all calendars
        caldav_root = storage.get_user_path(user.username) / "caldav"
        calendars = []
        for cal_dir in caldav_root.iterdir():
            if cal_dir.is_dir():
                calendars.append({
                    "href": f"/caldav/{username}/{cal_dir.name}/",
                    "props": {
                        "resourcetype": "calendar",
                        "displayname": cal_dir.name.replace('_', ' ').title()
                    }
                })
        return create_caldav_response(calendars)
    
    elif calendar_name and depth == "1":
        # List events in specific calendar
        cal_path = get_user_calendar_path(user, db, calendar_name)
        events = []
        for ics_file in cal_path.glob("*.ics"):
            events.append({
                "href": f"/caldav/{username}/{calendar_name}/{ics_file.name}",
                "props": {
                    "getcontenttype": "text/calendar; charset=utf-8",
                    "getetag": str(ics_file.stat().st_mtime)
                }
            })
        return create_caldav_response(events)
```

#### 2. Update Service Functions (`caldav_service.py`)

**get_user_calendars()**:
```python
def get_user_calendars(user_id: int, db: Session = None) -> List[Dict[str, str]]:
    """Get user's configured CalDAV calendars."""
    # ... existing code ...
    
    if use_builtin and use_builtin.value == "true":
        # NEW: Support multiple built-in calendars
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            caldav_port = db.query(Setting).filter(Setting.key == "caldav_port").first()
            port = caldav_port.value if caldav_port else "8081"
            
            # List all calendar subdirectories
            storage = get_storage_service(db)
            user_path = storage.get_user_path(user.username)
            caldav_root = user_path / "caldav"
            
            calendars = []
            
            # If no subdirectories exist, create/use default
            if not caldav_root.exists() or not any(caldav_root.iterdir()):
                calendars.append({
                    "name": "Default",
                    "url": f"http://localhost:{port}/caldav/{user.username}/default/",
                    "username": user.username,
                    "password": "__USE_SESSION_AUTH__",
                    "builtin": True
                })
            else:
                # List existing calendar directories
                for cal_dir in sorted(caldav_root.iterdir()):
                    if cal_dir.is_dir():
                        calendars.append({
                            "name": cal_dir.name.replace('_', ' ').title(),
                            "url": f"http://localhost:{port}/caldav/{user.username}/{cal_dir.name}/",
                            "username": user.username,
                            "password": "__USE_SESSION_AUTH__",
                            "builtin": True
                        })
            
            return calendars
```

**add_event_to_calendar()** - needs calendar_name parameter:
```python
def add_event_to_calendar(
    url: str,
    username: str,
    password: str,
    # ... existing params ...
    user_id: Optional[int] = None,
    db: Optional[Session] = None,
    calendar_name: str = "default"  # NEW
) -> bool:
    """Add event to specific calendar."""
    if password == "__USE_SESSION_AUTH__" and user_id and db:
        # Extract calendar name from URL if provided
        match = re.search(r'/caldav/[^/]+/([^/]+)/?$', url)
        if match:
            calendar_name = match.group(1)
        
        # Save to specific calendar subdirectory
        # ... use calendar_name in path ...
```

#### 3. Update Command Service (`command_service.py`)

**Calendar Selection in Commands**:
```python
# Option A: Use first calendar by default
calendars = get_user_calendars(self.user.id, self.db)
if not calendars:
    return {"type": "text", "content": "❌ No calendars configured"}

# Use first calendar (or let user specify)
cal = calendars[0]

# Option B: Allow users to specify calendar
# cal add work:meeting tomorrow at 3pm
# cal list personal
# cal add family:birthday party saturday
```

### Phase 2: UI Support

#### User Settings - Calendar Management

Add UI to create/manage calendars:

```html
<fieldset>
    <legend>Built-in Calendars</legend>
    <p class="fieldset-info">Create multiple calendars to organize your events.</p>
    
    <div id="builtinCalendarList">
        <!-- List of calendars -->
        <div class="calendar-item">
            <input type="text" value="Default" readonly>
            <span class="calendar-count">12 events</span>
        </div>
        <div class="calendar-item">
            <input type="text" value="Work">
            <button class="btn-danger btn-sm">Delete</button>
        </div>
    </div>
    
    <div class="form-group">
        <input type="text" id="newCalendarName" placeholder="Calendar name">
        <button type="button" class="btn-secondary" id="addBuiltinCalendar">+ Add Calendar</button>
    </div>
</fieldset>
```

#### Command Enhancement

```bash
# Current
cal add meeting tomorrow at 3pm

# Enhanced
cal add meeting tomorrow at 3pm               # Uses default calendar
cal add work:meeting tomorrow at 3pm          # Adds to "work" calendar
cal list work                                 # Lists work calendar only
cal list all                                  # Lists all calendars
```

## Migration Path

### For Existing Users

When upgrading, automatically migrate existing events:

```python
def migrate_flat_to_calendar_structure(user: User, db: Session):
    """Migrate flat caldav/*.ics to caldav/default/*.ics"""
    storage = get_storage_service(db)
    user_path = storage.get_user_path(user.username)
    caldav_root = user_path / "caldav"
    
    if not caldav_root.exists():
        return
    
    # Check if already migrated (has subdirectories)
    has_subdirs = any(p.is_dir() for p in caldav_root.iterdir())
    if has_subdirs:
        return  # Already migrated
    
    # Move all .ics files to default/ subdirectory
    default_cal = caldav_root / "default"
    default_cal.mkdir(exist_ok=True)
    
    for ics_file in caldav_root.glob("*.ics"):
        ics_file.rename(default_cal / ics_file.name)
    
    logger.info(f"Migrated {user.username} calendar to subdirectory structure")
```

Run this on server startup for all users.

## Timeline

### Immediate (Current State)
- ✅ Built-in: Single calendar
- ✅ External: Multiple calendars supported

### Phase 1 (Backend) - ~4-6 hours
1. Update `caldav_server.py` for path parsing
2. Update `caldav_service.py` for calendar listing
3. Add migration script
4. Test with CalDAV clients

### Phase 2 (Commands) - ~2-3 hours
1. Update command parser for calendar selection
2. Add calendar listing commands
3. Test calendar operations

### Phase 3 (UI) - ~3-4 hours
1. Add calendar management in User Settings
2. Add calendar creation/deletion
3. Show calendar in event displays

## Alternative: Keep It Simple

If multiple calendars aren't a priority, the current implementation is perfectly functional:
- **Built-in users**: Get one calendar (sufficient for most personal use)
- **Power users**: Can configure external CalDAV with multiple calendars

This keeps the codebase simple and the built-in server lightweight.

## Recommendation

**For now: Keep current implementation** (Option 3 - Hybrid)
- Built-in server remains simple with single calendar
- Users who need multiple calendars can use external servers (Nextcloud, Radicale, etc.)
- Reduces complexity and maintenance burden

**Future enhancement: Add subdirectories** (Option 1)
- Implement when there's clear user demand
- Provides better organization without sacrificing simplicity
- Backwards compatible with migration script

---

**Current Status**: ✅ Working with single calendar per user for built-in, unlimited for external.
