# WebDAV/CalDAV/CardDAV Network Configuration

## Summary ✅

All three DAV servers are **properly configured for remote access** - they bind to `0.0.0.0` which listens on all network interfaces.

## Server Configuration

### WebDAV Server
- **File**: `app/services/webdav_server.py`
- **Binding**: `bind_addr=('0.0.0.0', port)` ✅
- **Default Port**: 8080
- **Protocol**: HTTP + Basic Auth
- **Purpose**: File access (browse, upload, download, delete files)

### CalDAV Server  
- **File**: `app/services/caldav_server.py`
- **Binding**: `host="0.0.0.0"` ✅
- **Default Port**: 8081
- **Protocol**: HTTP + Basic Auth + CalDAV extensions
- **Purpose**: Calendar access (sync events, todos)

### CardDAV Server
- **File**: `app/services/cardav_server.py`
- **Binding**: `host="0.0.0.0"` ✅
- **Default Port**: 8082
- **Protocol**: HTTP + Basic Auth + CardDAV extensions  
- **Purpose**: Contacts access (sync address book)

## Network Access

### From Other Machines

All servers listen on **0.0.0.0** which means:
- ✅ Accessible from LAN (192.168.x.x)
- ✅ Accessible from WAN (if port forwarded)
- ✅ Works with any WebDAV/CalDAV/CardDAV client

### Connection URLs

**From LAN (192.168.0.x)**:
```
WebDAV:  http://192.168.0.85:8080/{username}/
CalDAV:  http://192.168.0.85:8081/caldav/{username}/
CardDAV: http://192.168.0.85:8082/carddav/{username}/
```

**From Main Server (192.168.0.1)**:
```
WebDAV:  http://192.168.0.1:8080/{username}/
CalDAV:  http://192.168.0.1:8081/caldav/{username}/
CardDAV: http://192.168.0.1:8082/carddav/{username}/
```

**From Internet** (if configured with reverse proxy/domain):
```
WebDAV:  https://ai.poster.place/{username}/
CalDAV:  https://ai.poster.place/caldav/{username}/
CardDAV: https://ai.poster.place/carddav/{username}/
```

## Client Compatibility

### WebDAV Clients
- **Windows**: Map Network Drive → `http://server:8080/username/`
- **macOS**: Finder → Go → Connect to Server → `http://server:8080/username/`
- **Linux**: Nautilus/Dolphin → Network → WebDAV → `http://server:8080/username/`
- **iOS**: Files app → Connect to Server
- **Android**: Solid Explorer, Total Commander + WebDAV plugin

### CalDAV Clients
- **Thunderbird**: Add Calendar → On the Network → CalDAV
- **iOS Calendar**: Settings → Accounts → Add Account → Other → CalDAV
- **Android**: DAVx5 app (free, open-source)
- **Evolution**: Add Calendar → CalDAV

### CardDAV Clients
- **iOS Contacts**: Settings → Accounts → Add Account → Other → CardDAV
- **Android**: DAVx5 app
- **Thunderbird**: Address Book → CardDAV
- **Evolution**: Add Address Book → CardDAV

## Authentication

All servers use **HTTP Basic Authentication**:
- **Username**: Your Posterchanai username (e.g., `verita84@poster.place`)
- **Password**: Your Posterchanai password

Example (curl):
```bash
curl -u "verita84@poster.place:yourpassword" \
  http://192.168.0.85:8080/verita84@poster.place/
```

## Security Considerations

### Current Setup (HTTP)
- ⚠️ **Unencrypted** - credentials and data sent in cleartext
- ✅ **OK for LAN** - if trusted local network
- ❌ **NOT OK for WAN** - vulnerable to interception

### Recommended: Add HTTPS

**Option 1: Reverse Proxy (Nginx)**
```nginx
server {
    listen 443 ssl http2;
    server_name ai.poster.place;
    
    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;
    
    # WebDAV
    location / {
        proxy_pass http://localhost:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header Authorization $http_authorization;
        proxy_pass_header Authorization;
    }
    
    # CalDAV
    location /caldav/ {
        proxy_pass http://localhost:8081/caldav/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header Authorization $http_authorization;
        proxy_pass_header Authorization;
    }
    
    # CardDAV
    location /carddav/ {
        proxy_pass http://localhost:8082/carddav/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header Authorization $http_authorization;
        proxy_pass_header Authorization;
    }
}
```

**Option 2: Caddy (Automatic HTTPS)**
```
ai.poster.place {
    reverse_proxy /caldav/* localhost:8081
    reverse_proxy /carddav/* localhost:8082
    reverse_proxy /* localhost:8080
}
```

## Firewall Configuration

### Allow Ports on Storage Server

```bash
# WebDAV
sudo firewall-cmd --permanent --add-port=8080/tcp

# CalDAV
sudo firewall-cmd --permanent --add-port=8081/tcp

# CardDAV
sudo firewall-cmd --permanent --add-port=8082/tcp

# Reload
sudo firewall-cmd --reload
```

Or with iptables:
```bash
sudo iptables -A INPUT -p tcp --dport 8080 -j ACCEPT  # WebDAV
sudo iptables -A INPUT -p tcp --dport 8081 -j ACCEPT  # CalDAV
sudo iptables -A INPUT -p tcp --dport 8082 -j ACCEPT  # CardDAV
```

## Enabling the Servers

The servers are **not enabled by default**. To enable:

### Via Admin UI (Recommended)
1. Open `http://192.168.0.1:3051/admin`
2. Go to **Site Settings** tab
3. Scroll to **WebDAV/CalDAV/CardDAV Server Settings**
4. Enable the servers you want:
   - ☑ Enable WebDAV Server
   - ☑ Enable CalDAV Server
   - ☑ Enable CardDAV Server
5. Configure ports if needed
6. Save settings
7. Restart service

### Via Database
```bash
sqlite3 db.sqlite "UPDATE settings SET value='true' WHERE key='webdav_enabled';"
sqlite3 db.sqlite "UPDATE settings SET value='true' WHERE key='caldav_enabled';"
sqlite3 db.sqlite "UPDATE settings SET value='true' WHERE key='cardav_enabled';"
```

## Testing from Remote Machine

### Test WebDAV
```bash
# List files
curl -u "username:password" http://192.168.0.85:8080/username/

# Upload file
curl -u "username:password" -T test.txt \
  http://192.168.0.85:8080/username/test.txt

# Download file
curl -u "username:password" -O \
  http://192.168.0.85:8080/username/test.txt
```

### Test CalDAV
```bash
# PROPFIND request
curl -u "username:password" -X PROPFIND \
  -H "Depth: 1" \
  http://192.168.0.85:8081/caldav/username/
```

### Test CardDAV
```bash
# PROPFIND request
curl -u "username:password" -X PROPFIND \
  -H "Depth: 1" \
  http://192.168.0.85:8082/carddav/username/
```

## Troubleshooting

### Can't Connect from Other Machine

1. **Check if server is running**:
   ```bash
   ss -tlnp | grep ':8080\|:8081\|:8082'
   ```

2. **Check firewall**:
   ```bash
   sudo firewall-cmd --list-ports
   ```

3. **Test from server itself**:
   ```bash
   curl http://localhost:8080/username/
   ```

4. **Check logs**:
   ```bash
   journalctl -u posterchanai.service | grep -i webdav
   ```

### Authentication Fails

- Verify username format (e.g., `verita84@poster.place`)
- Check password is correct
- Ensure user exists in database

### Permission Denied

- Check file permissions on storage directory
- Verify user has storage quota available

## Performance Notes

- **WebDAV**: Direct file access, good performance
- **CalDAV/CardDAV**: Lightweight protocol, very fast for calendar/contacts
- **Concurrent Users**: Servers use thread pools (10 threads for WebDAV, uvicorn workers for DAV)
- **Network Speed**: Limited by your LAN/WAN bandwidth

## Storage Paths

Files are served from the configured upload path:
- **Storage Server**: `/raid/posterchanai/{username}/`
- **WebDAV Root**: `/raid/posterchanai/`
- **CalDAV Data**: `/raid/posterchanai/{username}/caldav/*.ics`
- **CardDAV Data**: `/raid/posterchanai/{username}/carddav/*.vcf`

---

**Status**: ✅ All DAV servers are properly configured for remote access!  
**Ready to use**: Just enable them in Admin UI and configure firewall if needed.
