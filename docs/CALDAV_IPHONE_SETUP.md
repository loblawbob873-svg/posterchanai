# CalDAV/CardDAV iPhone Setup Guide

This guide explains how to connect your iPhone (or other iOS device) to the built-in CalDAV/CardDAV servers.

## Architecture

The PosterchanAI system runs three separate DAV servers:
- **Main App**: Port 3051 (FastAPI web app)
- **CalDAV Server**: Port 8081 (Calendar/events/todos)
- **CardDAV Server**: Port 8082 (Contacts)
- **WebDAV Server**: Port 8080 (File storage)

Nginx reverse proxy handles HTTPS termination and routes requests:
- `https://ai.poster.place/` → Main app (port 3051)
- `https://ai.poster.place/caldav/` → CalDAV server (port 8081)
- `https://ai.poster.place/calendar/dav/` → CalDAV server (port 8081, legacy path)
- `https://ai.poster.place/carddav/` → CardDAV server (port 8082)

## iPhone Calendar Setup

### Option 1: Automatic Setup (Recommended)

1. **Login to your account**: Open `https://ai.poster.place` in Safari on your iPhone
2. **Download profile**: Visit `https://ai.poster.place/caldav/profile` 
3. **Install profile**: 
   - Tap "Allow" to download
   - Go to Settings → General → VPN & Device Management
   - Tap "PosterChan Calendar - [your username]"
   - Tap "Install" and enter your iPhone passcode
   - Enter your CalDAV password when prompted
4. **Done!** Your calendar will appear in the Calendar app

### Option 2: Manual Setup

1. Open **Settings** → **Calendar** → **Accounts**
2. Tap **Add Account** → **Other**
3. Tap **Add CalDAV Account**

### Step 2: Enter Server Details

- **Server**: `ai.poster.place`
- **Username**: Your username (e.g., `verita84@poster.place`)
- **Password**: Your account password
- **Description**: PosterChan AI Calendar (or any name you like)

### Step 3: Wait for Autodiscovery

The iPhone will:
1. Contact `https://ai.poster.place/.well-known/caldav`
2. Get redirected to `https://ai.poster.place/caldav/`
3. Authenticate and discover your calendar at `/caldav/[your-username]/`

**Important**: The autodiscovery may try multiple paths including `/calendar/dav/` - both paths are supported via nginx proxy.

### Step 4: Verify

- Go to **Calendar** app
- You should see "PosterChan AI Calendar" (or your chosen name)
- Any events you created via the chat commands should appear
- You can create/edit events directly on iPhone and they'll sync instantly

## iPhone Contacts Setup

### Step 1: Add CardDAV Account

1. Open **Settings** → **Contacts** → **Accounts**
2. Tap **Add Account** → **Other**
3. Tap **Add CardDAV Account**

### Step 2: Enter Server Details

- **Server**: `ai.poster.place`
- **Username**: Your username (e.g., `verita84@poster.place`)
- **Password**: Your account password
- **Description**: PosterChan AI Contacts

### Step 3: Wait for Autodiscovery

The iPhone will:
1. Contact `https://ai.poster.place/.well-known/carddav`
2. Get redirected to `https://ai.poster.place/carddav/`
3. Authenticate and discover your contacts at `/carddav/[your-username]/`

### Step 4: Verify

- Go to **Contacts** app
- Filter by "PosterChan AI Contacts" account
- Your imported contacts should appear
- Changes sync instantly in both directions

## Troubleshooting

### "Cannot Connect to Server"

**Check HTTPS Certificate**:
```bash
curl -I https://ai.poster.place/.well-known/caldav
```
Should return `301 Moved Permanently` with `Location: https://ai.poster.place/caldav/`

**Check Nginx is Running**:
```bash
ssh 192.168.0.1 "sudo systemctl status nginx"
```

**Check Main App is Running**:
```bash
ssh 192.168.0.1 "sudo netstat -tlnp | grep 3051"
```

**Check CalDAV Server is Running**:
```bash
ssh 192.168.0.1 "sudo netstat -tlnp | grep 8081"
```

**Check CardDAV Server is Running**:
```bash
ssh 192.168.0.1 "sudo netstat -tlnp | grep 8082"
```

### "Unable to Verify Account Information"

This usually means:
1. Username/password is incorrect
2. CalDAV/CardDAV server is not enabled in Admin UI
3. Server is not running

**Verify Settings in Admin UI**:
1. Go to `https://ai.poster.place/admin`
2. Click **Services** tab
3. Ensure:
   - ✅ Built-in CalDAV Server: Enabled (Port: 8081)
   - ✅ Built-in CardDAV Server: Enabled (Port: 8082)

**Check Database Settings** (if UI shows enabled but still failing):
```bash
ssh 192.168.0.1
cd /home/verita84/posterchanai
venv/bin/python3 -c "
from app.database import SessionLocal
from app.models import Setting
db = SessionLocal()
caldav = db.query(Setting).filter(Setting.key == 'caldav_enabled').first()
cardav = db.query(Setting).filter(Setting.key == 'cardav_enabled').first()
print(f'CalDAV enabled: {caldav.value if caldav else None}')
print(f'CardDAV enabled: {cardav.value if cardav else None}')
db.close()
"
```
Both should return `true` (string, not boolean).

### "Unable to Update Calendars" After Initial Success

This can happen if:
1. The CalDAV server restarted/crashed
2. Network connectivity issues
3. Invalid calendar data

**Check CalDAV Server Logs**:
```bash
ssh 192.168.0.1 "sudo journalctl -u posterchanai-ipex -n 100 --no-pager | grep -i caldav"
```

**Restart Services** (if needed):
```bash
ssh 192.168.0.1 "sudo systemctl restart posterchanai-ipex"
```
Wait 10-15 seconds for the app to fully restart, then test again.

### Events Not Syncing

**Force Refresh on iPhone**:
1. Pull down in Calendar app to refresh
2. Or: Settings → Calendar → Accounts → PosterChan AI → Toggle calendar off/on

**Check Event Files Exist**:
```bash
ssh 192.168.0.1 "ls -lah /var/lib/posterchanai/users/[your-username]/caldav/"
```
You should see `.ics` files for each event/todo.

### Wrong Paths (404 Errors)

**Nginx Config Issue** - Check nginx log:
```bash
ssh 192.168.0.1 "sudo tail -f /var/log/nginx/error.log"
```

**Verify Nginx Routes**:
```bash
ssh 192.168.0.1 "sudo nginx -T | grep -A10 'location /caldav/'"
```

Should show:
```nginx
location /caldav/ {
    proxy_pass http://caldav/caldav/;
    ...
}
```

## Technical Details

### CalDAV Autodiscovery Flow

1. iPhone requests: `PROPFIND https://ai.poster.place/.well-known/caldav`
2. FastAPI returns: `301 Redirect → https://ai.poster.place/caldav/`
3. Nginx proxies: `https://ai.poster.place/caldav/` → `http://localhost:8081/caldav/`
4. CalDAV server authenticates user via Basic Auth
5. CalDAV server returns multistatus XML with calendar collection info
6. iPhone discovers: `https://ai.poster.place/caldav/[username]/`
7. iPhone performs `PROPFIND` (Depth: 1) to list events
8. CalDAV server returns all `.ics` files from user's caldav directory

### CardDAV Autodiscovery Flow

Same as CalDAV but with `/carddav/` paths and `.vcf` files.

### HTTP Methods Used

- **PROPFIND**: List resources (calendar collections, events)
- **REPORT**: Calendar queries with filters (date ranges)
- **GET**: Retrieve specific event/contact
- **PUT**: Create/update event/contact
- **DELETE**: Remove event/contact
- **MKCALENDAR**: Create calendar (handled automatically)

### File Storage

Events/Todos:
```
/var/lib/posterchanai/users/[username]/caldav/
  ├── event-uid-1.ics
  ├── event-uid-2.ics
  └── todo-uid-3.ics
```

Contacts:
```
/var/lib/posterchanai/users/[username]/carddav/
  ├── contact-uid-1.vcf
  ├── contact-uid-2.vcf
  └── contact-uid-3.vcf
```

Each event/todo/contact is stored as a separate file with its UID as the filename.

## Multiple Devices

You can add the same account to multiple devices:
- iPhone
- iPad
- Mac (System Settings → Internet Accounts → Add Other Account → CalDAV/CardDAV)
- Android (via DAVx⁵ app)
- Thunderbird (via Lightning add-on)
- Evolution (Linux)
- Windows Calendar (Windows 10/11)

All devices will sync changes instantly via the CalDAV/CardDAV protocols.

## Security Notes

- All communication is over HTTPS (TLS 1.2+)
- Authentication uses HTTP Basic Auth (Base64 encoded username:password)
- This is secure over HTTPS but avoid using the same password across services
- Consider using app-specific passwords (future feature)
- CalDAV/CardDAV servers are isolated from main app for security
- File permissions ensure users can only access their own data

## Nginx Configuration Reference

See `docs/nginx-posterchanai.conf` for the complete nginx configuration.

Key sections:
```nginx
# CalDAV autodiscovery handled by main app
location /.well-known/caldav {
    proxy_pass http://web;
    ...
}

# CalDAV server (primary path)
location /caldav/ {
    proxy_pass http://caldav/caldav/;
    ...
}

# CalDAV server (legacy path for some clients)
location /calendar/dav/ {
    proxy_pass http://caldav/caldav/;
    ...
}

# CardDAV autodiscovery handled by main app
location /.well-known/carddav {
    proxy_pass http://web;
    ...
}

# CardDAV server
location /carddav/ {
    proxy_pass http://carddav/carddav/;
    ...
}
```

## Related Documentation

- `docs/nginx-posterchanai.conf` - Complete nginx configuration
- `CALENDAR_CONTACTS_IMPORT_EXPORT.md` - Import/export functionality
- `FIX_CARDAV_NOT_ENABLED.md` - Troubleshooting "not enabled" errors
- `app/services/caldav_server.py` - CalDAV server implementation
- `app/services/cardav_server.py` - CardDAV server implementation
