# CalDAV/CardDAV Import & Export Guide

## Overview ✅

Full import/export functionality is now available for both calendar events and contacts. This allows you to:
- **Backup** your calendar and contacts data
- **Migrate** from other CalDAV/CardDAV servers (Radicale, Nextcloud, etc.)
- **Share** calendar events with others
- **Transfer** contacts between devices

---

## Calendar (CalDAV) Import/Export

### Export Calendar Events

**Endpoint**: `GET /api/calendar/export`

**Description**: Exports all your calendar events as a single `.ics` file (iCalendar format).

**Usage**:
```bash
# Download calendar export
curl -u "username:password" \
  http://192.168.0.1:3051/api/calendar/export \
  -o my_calendar.ics
```

**Via Web UI**:
1. Open Posterchanai web interface
2. Click user menu → Settings
3. Go to Calendar tab
4. Click "Export Calendar" button
5. Save the `.ics` file

**What gets exported**:
- ✅ All calendar events (VEVENT)
- ✅ All todos (VTODO)
- ✅ All journal entries (VJOURNAL)
- ✅ Event metadata (title, description, location, dates, recurrence, etc.)

**File format**: Standard iCalendar (RFC 5545)
- Compatible with Google Calendar, Apple Calendar, Thunderbird, etc.

---

### Import Calendar Events from Radicale

**Endpoint**: `POST /api/calendar/import/radicale`

**Description**: Imports calendar events from a Radicale CalDAV server.

**Parameters**:
- `radicale_url`: URL of the Radicale server (e.g., `http://server:5232`)
- `username`: Radicale username
- `password`: Radicale password

**Usage**:
```bash
curl -X POST http://192.168.0.1:3051/api/calendar/import/radicale \
  -u "your_username:your_password" \
  -F "radicale_url=http://old-server:5232" \
  -F "username=radicale_user" \
  -F "password=radicale_pass"
```

**Response**:
```json
{
  "success": true,
  "message": "Imported 45 events from Radicale",
  "imported": 45,
  "errors": 2
}
```

**What happens**:
1. Connects to Radicale server via CalDAV protocol
2. Fetches all calendars and events
3. Saves each event as an `.ics` file in `{upload_path}/{username}/caldav/`
4. Events become immediately available via built-in CalDAV server

**Compatible with**:
- Radicale
- Nextcloud (CalDAV URL: `https://nextcloud.example.com/remote.php/dav`)
- ownCloud
- Baïkal
- Any RFC 4791 compliant CalDAV server

---

## Contacts (CardDAV) Import/Export

### Export Contacts

**Endpoint**: `GET /api/contacts/export`

**Description**: Exports all your contacts as a single `.vcf` file (vCard format).

**Usage**:
```bash
# Download contacts export
curl -u "username:password" \
  http://192.168.0.1:3051/api/contacts/export \
  -o my_contacts.vcf
```

**Via Web UI** (if implemented):
1. Open Posterchanai web interface
2. Click user menu → Settings
3. Go to Contacts tab
4. Click "Export Contacts" button
5. Save the `.vcf` file

**What gets exported**:
- ✅ All contacts (vCard 3.0/4.0 format)
- ✅ Names, phone numbers, emails
- ✅ Organizations, notes
- ✅ All vCard fields

**File format**: Standard vCard (RFC 6350)
- Compatible with iOS Contacts, Android, Thunderbird, etc.

---

### Import Contacts from vCard File

**Endpoint**: `POST /api/contacts/import`

**Description**: Imports contacts from vCard (`.vcf`) data.

**Parameters**:
- `vcf_data`: vCard file contents (can contain multiple contacts)

**Usage**:
```bash
# Import contacts from file
curl -X POST http://192.168.0.1:3051/api/contacts/import \
  -u "username:password" \
  -F "vcf_data=@my_contacts.vcf"
```

**Response**:
```json
{
  "success": true,
  "message": "Imported 120 contacts",
  "imported": 120,
  "skipped": 5,
  "errors": 0
}
```

**What happens**:
1. Parses vCard data (supports single or multiple vCards)
2. Extracts UIDs or generates new ones
3. Skips contacts that already exist (based on UID)
4. Saves each contact as a `.vcf` file in `{upload_path}/{username}/carddav/`
5. Contacts become immediately available via built-in CardDAV server

**Supports**:
- Single vCard files
- Multi-contact vCard files (multiple BEGIN:VCARD blocks)
- vCard 3.0 and 4.0 formats

---

### Import Contacts from CardDAV Server

**Endpoint**: `POST /api/contacts/import/cardav`

**Description**: Imports contacts from another CardDAV server.

**Parameters**:
- `cardav_url`: URL of the CardDAV server
- `username`: CardDAV username
- `password`: CardDAV password

**Usage**:
```bash
curl -X POST http://192.168.0.1:3051/api/contacts/import/cardav \
  -u "your_username:your_password" \
  -F "cardav_url=http://old-server:5232" \
  -F "username=cardav_user" \
  -F "password=cardav_pass"
```

**Response**:
```json
{
  "success": true,
  "message": "Imported 85 contacts from CardDAV server",
  "imported": 85,
  "skipped": 3,
  "errors": 1
}
```

**What happens**:
1. Connects to CardDAV server via CardDAV protocol
2. Fetches all address books and contacts
3. Saves each contact as a `.vcf` file in `{upload_path}/{username}/carddav/`
4. Contacts become immediately available via built-in CardDAV server

**Compatible with**:
- Radicale
- Nextcloud (CardDAV URL: `https://nextcloud.example.com/remote.php/dav`)
- ownCloud
- Baïkal
- iOS CardDAV accounts
- Any RFC 6352 compliant CardDAV server

---

## Import Examples

### Migrate from Nextcloud

**Calendar**:
```bash
curl -X POST http://192.168.0.1:3051/api/calendar/import/radicale \
  -u "verita84@poster.place:mypassword" \
  -F "radicale_url=https://nextcloud.example.com/remote.php/dav" \
  -F "username=nextcloud_user" \
  -F "password=nextcloud_pass"
```

**Contacts**:
```bash
curl -X POST http://192.168.0.1:3051/api/contacts/import/cardav \
  -u "verita84@poster.place:mypassword" \
  -F "cardav_url=https://nextcloud.example.com/remote.php/dav" \
  -F "username=nextcloud_user" \
  -F "password=nextcloud_pass"
```

### Migrate from Radicale

**Calendar**:
```bash
curl -X POST http://192.168.0.1:3051/api/calendar/import/radicale \
  -u "verita84@poster.place:mypassword" \
  -F "radicale_url=http://192.168.0.5:5232" \
  -F "username=radicale_user" \
  -F "password=radicale_pass"
```

**Contacts**:
```bash
curl -X POST http://192.168.0.1:3051/api/contacts/import/cardav \
  -u "verita84@poster.place:mypassword" \
  -F "cardav_url=http://192.168.0.5:5232" \
  -F "username=radicale_user" \
  -F "password=radicale_pass"
```

### Import from Backup Files

**Calendar**:
```bash
# If you have a .ics backup file, you can import it by:
# 1. Copy .ics files directly to your CalDAV directory:
cp backup_events.ics /raid/posterchanai/verita84@poster.place/caldav/

# OR split multi-event .ics into individual files using Python:
python3 << 'EOF'
from icalendar import Calendar
import uuid

with open('backup_calendar.ics', 'rb') as f:
    cal = Calendar.from_ical(f.read())

for component in cal.walk():
    if component.name == "VEVENT":
        event_uid = str(component.get('uid', uuid.uuid4()))
        event_cal = Calendar()
        event_cal.add('prodid', '-//Posterchanai//Calendar//EN')
        event_cal.add('version', '2.0')
        event_cal.add_component(component)
        
        with open(f'/raid/posterchanai/verita84@poster.place/caldav/{event_uid}.ics', 'wb') as out:
            out.write(event_cal.to_ical())
print("Import complete!")
EOF
```

**Contacts**:
```bash
# Import .vcf file directly via API
curl -X POST http://192.168.0.1:3051/api/contacts/import \
  -u "verita84@poster.place:mypassword" \
  -F "vcf_data=@backup_contacts.vcf"
```

---

## Data Storage Locations

### CalDAV (Calendar)
```
{upload_path}/{username}/caldav/*.ics
```
Example: `/raid/posterchanai/verita84@poster.place/caldav/event-uuid.ics`

Each event is stored as a separate `.ics` file named by its UID.

### CardDAV (Contacts)
```
{upload_path}/{username}/carddav/*.vcf
```
Example: `/raid/posterchanai/verita84@poster.place/carddav/contact-uuid.vcf`

Each contact is stored as a separate `.vcf` file named by its UID.

---

## Backup Best Practices

### Automated Backup Script

Create a backup script to regularly export your data:

```bash
#!/bin/bash
# /home/user/backup-posterchanai.sh

BACKUP_DIR="/backups/posterchanai"
DATE=$(date +%Y%m%d)
USERNAME="verita84@poster.place"
PASSWORD="yourpassword"
SERVER="http://192.168.0.1:3051"

mkdir -p "$BACKUP_DIR"

# Backup calendar
curl -u "$USERNAME:$PASSWORD" \
  "$SERVER/api/calendar/export" \
  -o "$BACKUP_DIR/calendar_$DATE.ics"

# Backup contacts
curl -u "$USERNAME:$PASSWORD" \
  "$SERVER/api/contacts/export" \
  -o "$BACKUP_DIR/contacts_$DATE.vcf"

# Compress
tar -czf "$BACKUP_DIR/posterchanai_backup_$DATE.tar.gz" \
  "$BACKUP_DIR/calendar_$DATE.ics" \
  "$BACKUP_DIR/contacts_$DATE.vcf"

# Remove files older than 30 days
find "$BACKUP_DIR" -name "*.ics" -mtime +30 -delete
find "$BACKUP_DIR" -name "*.vcf" -mtime +30 -delete
find "$BACKUP_DIR" -name "*.tar.gz" -mtime +90 -delete

echo "Backup completed: posterchanai_backup_$DATE.tar.gz"
```

Add to crontab for daily backups:
```bash
crontab -e
# Add this line for daily backup at 2 AM:
0 2 * * * /home/user/backup-posterchanai.sh
```

### Direct File Backup

You can also backup directly from the filesystem:

```bash
# Backup CalDAV data
tar -czf calendar_backup_$(date +%Y%m%d).tar.gz \
  /raid/posterchanai/*/caldav/

# Backup CardDAV data
tar -czf contacts_backup_$(date +%Y%m%d).tar.gz \
  /raid/posterchanai/*/carddav/
```

---

## Troubleshooting

### Import Fails with "caldav library not installed"

Install the required library:
```bash
pip install caldav
```

### Export Returns Empty File

- Check that events/contacts exist in the CalDAV/CardDAV directories
- Verify file permissions: `ls -la /raid/posterchanai/username/caldav/`
- Check logs: `journalctl -u posterchanai.service | grep -i export`

### Import Skips All Contacts/Events

This happens when UIDs already exist. To force re-import:
1. Backup existing data
2. Delete the CalDAV/CardDAV directory
3. Re-import

```bash
# Backup first
cp -r /raid/posterchanai/username/caldav /tmp/caldav_backup

# Clear and re-import
rm -rf /raid/posterchanai/username/caldav/*
# Now run import command
```

### Invalid vCard/iCalendar Error

- Ensure the file is valid UTF-8 encoded
- Check for special characters or malformed data
- Try validating with online tools:
  - vCard validator: https://vcardvalidator.com/
  - iCalendar validator: https://icalendar.org/validator.html

---

## API Endpoints Summary

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/calendar/export` | GET | Export all calendar events to .ics |
| `/api/calendar/import/radicale` | POST | Import events from Radicale/CalDAV server |
| `/api/contacts/export` | GET | Export all contacts to .vcf |
| `/api/contacts/import` | POST | Import contacts from .vcf data |
| `/api/contacts/import/cardav` | POST | Import contacts from CardDAV server |

---

## Security Notes

- All import/export endpoints require authentication (HTTP Basic Auth)
- User can only access their own data
- Imported data is validated before storage
- UIDs prevent duplicate imports
- All data stored in user-specific directories with proper permissions

---

**Status**: ✅ Full import/export functionality implemented!  
**Ready to use**: Export for backups, import from other servers, or migrate your data.
