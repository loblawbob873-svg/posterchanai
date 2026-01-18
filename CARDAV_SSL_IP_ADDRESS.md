# CardDAV SSL with IP Address

## Problem

SSL certificates are issued for domain names (e.g., `ai.poster.place`), not IP addresses. When connecting via IP address (`192.168.0.1`), the SSL certificate won't match and clients will reject the connection.

## Solutions

### Option 1: Use Domain Name (Recommended)

**Always use the domain name instead of the IP address:**

- ✅ `https://ai.poster.place/carddav/`
- ❌ `https://192.168.0.1/carddav/`

**Why this works:**
- SSL certificate is valid for `ai.poster.place`
- DNS resolves `ai.poster.place` → `192.168.0.1` (works on your network)
- Client validates certificate against domain name (matches!)

**For CardDAV clients:**
- Server URL: `https://ai.poster.place/carddav/`
- Username: Your username
- Password: Your account password

### Option 2: Use HTTP for Local Network

If you must use the IP address, you can use HTTP (not HTTPS) for local connections:

**Connection URL:** `http://192.168.0.1:8082/carddav/`

**Note:** This requires:
1. CardDAV server to be accessible directly (not through nginx)
2. Port 8082 to be open on firewall
3. Client must support HTTP (some clients require HTTPS)

**Security:** HTTP is unencrypted - only use on trusted local networks!

### Option 3: Add IP to Certificate (Advanced)

You can create a certificate that includes both the domain and IP address, but this requires:
1. Self-signed certificate or custom CA
2. Client must trust the certificate
3. More complex setup

**Not recommended** - Option 1 is much simpler.

## Current Setup

Your nginx is configured for:
- Domain: `ai.poster.place`
- SSL: Port 443 with Let's Encrypt certificate
- CardDAV: Proxied from `https://ai.poster.place/carddav/` → `http://localhost:8082/carddav/`

## Testing

### Test with Domain Name (Should Work):
```bash
curl -I https://ai.poster.place/.well-known/carddav
# Should return: 301 Moved Permanently
```

### Test with IP Address (Will Fail SSL):
```bash
curl -I https://192.168.0.1/.well-known/carddav
# Will fail: SSL certificate name mismatch
```

### Test HTTP Direct (If Port Open):
```bash
curl -I http://192.168.0.1:8082/.well-known/carddav
# Should work if port 8082 is accessible
```

## Recommendation

**Always use the domain name (`ai.poster.place`) for CardDAV connections**, even on your local network. The DNS will resolve to `192.168.0.1` automatically, and the SSL certificate will be valid.

If your client doesn't support using the domain name, check:
1. DNS resolution on the client device
2. `/etc/hosts` file (add: `192.168.0.1 ai.poster.place`)
3. Local DNS server configuration
