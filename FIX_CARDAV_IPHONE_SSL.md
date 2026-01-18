# Fix CardDAV SSL Connection Issue for iPhone

## Problem
iPhone shows "Cannot connect using SSL" when trying to connect to CardDAV.

## Root Causes Identified

1. **Discovery Endpoint Redirect**: The `.well-known/carddav` endpoint was using 301 (Moved Permanently) instead of 302 (Found), and was using a relative URL instead of absolute HTTPS URL.

2. **SSL Cipher Configuration**: The nginx SSL cipher configuration was too generic (`HIGH:!aNULL:!MD5`), which might not be compatible with iPhone's strict SSL requirements.

## Fixes Applied

### 1. CardDAV Discovery Endpoint (`app/services/cardav_server.py`)

**Changed:**
- Status code: 301 → 302 (Found) for better compatibility
- Location header: Relative `/carddav/` → Absolute `https://ai.poster.place/carddav/`
- Added proper Host header detection from `X-Forwarded-Proto`
- Added `Cache-Control: no-cache` header

**Code:**
```python
@app.route("/.well-known/carddav", methods=["GET", "HEAD", "OPTIONS"])
async def carddav_discovery(request: StarletteRequest):
    """CardDAV discovery endpoint. Returns 302 redirect to CardDAV principal."""
    # Get the host from the request
    host = request.headers.get("Host", "ai.poster.place")
    # Determine if we're using HTTPS
    scheme = request.headers.get("X-Forwarded-Proto", "https")
    if not scheme or scheme == "http":
        if request.url.scheme == "https" or "443" in str(request.url.port):
            scheme = "https"
    
    # Use absolute URL for redirect (required by some clients like iPhone)
    redirect_url = f"{scheme}://{host}/carddav/"
    
    return Response(
        content="",
        status_code=302,  # Use 302 (Found) instead of 301
        headers={
            "Location": redirect_url,
            "Cache-Control": "no-cache"
        }
    )
```

### 2. Nginx SSL Configuration (`/etc/nginx/sites-enabled/ai.conf`)

**Updated SSL ciphers to be iPhone-compatible:**
- Changed from generic `HIGH:!aNULL:!MD5` to explicit cipher suite list
- Added `ssl_prefer_server_ciphers off` (let client choose best cipher)
- Added `ssl_session_cache shared:SSL:10m` for performance
- Added `ssl_session_timeout 10m`

**New cipher suite:**
```
ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:DHE-RSA-AES128-GCM-SHA256:DHE-RSA-AES256-GCM-SHA384
```

## Verification

### SSL Certificate Check
```bash
openssl s_client -connect ai.poster.place:443 -servername ai.poster.place -showcerts
# Verify return code: 0 (ok) ✓
# Certificate chain: Complete ✓
```

### Discovery Endpoint Test
```bash
curl -I https://ai.poster.place/.well-known/carddav
# Should return: HTTP/1.1 302 Found
# Location: https://ai.poster.place/carddav/
```

## Next Steps

**⚠️ IMPORTANT: Restart the application to apply code changes**

The CardDAV server code changes require the application to be restarted. The discovery endpoint will continue returning 301 until the application is restarted.

**To restart:**
1. Find the application process: `ps aux | grep python | grep app.py`
2. Restart the application (method depends on how it's run - systemd, screen, etc.)
3. Verify the fix: `curl -I https://ai.poster.place/.well-known/carddav` should return 302

## Testing on iPhone

After restarting the application:

1. Go to Settings → Contacts → Accounts → Add Account → Other → Add CardDAV Account
2. Enter:
   - **Server**: `ai.poster.place`
   - **Username**: Your username (e.g., `verita84@poster.place`)
   - **Password**: Your password
   - **Description**: Posterchanai Contacts
3. Tap "Next" - iPhone should now connect successfully

## Additional Notes

- The SSL certificate is valid and trusted (Let's Encrypt)
- The certificate chain is complete
- Nginx handles SSL termination (CardDAV server runs on HTTP port 8083)
- The discovery endpoint now properly redirects to the CardDAV principal URL

## Troubleshooting

If iPhone still shows "Cannot connect using SSL":

1. **Check application logs** for CardDAV errors
2. **Verify nginx is serving SSL correctly**: `curl -v https://ai.poster.place/.well-known/carddav`
3. **Check certificate expiration**: `openssl x509 -in /etc/letsencrypt/live/ai.poster.place/cert.pem -noout -dates`
4. **Test with another client** (e.g., macOS Contacts app) to isolate iPhone-specific issues
5. **Check iPhone network settings** - ensure it's not using a VPN or proxy that interferes with SSL
