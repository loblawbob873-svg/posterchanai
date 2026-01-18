# Fix CardDAV Nginx Authorization Header

## Problem

CardDAV requests through nginx are missing the `Authorization` header, causing 401 Unauthorized errors.

**Log shows:**
```
No Basic auth header: empty
401 Unauthorized
```

## Root Cause

Nginx needs to explicitly forward the `Authorization` header to the CardDAV server. While `proxy_pass_request_headers on` should handle this, some nginx configurations require explicit forwarding.

## Solution

Add explicit `Authorization` header forwarding to nginx configuration:

### Update `/etc/nginx/sites-available/posterchanai.conf`

Add this line to all CardDAV/CalDAV location blocks:

```nginx
proxy_set_header Authorization $http_authorization;
```

### Complete Updated Sections:

```nginx
# CardDAV discovery endpoint
location /.well-known/carddav {
    proxy_pass http://carddav/.well-known/carddav;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Authorization $http_authorization;  # ← ADD THIS
    proxy_pass_request_headers on;
}

# CardDAV server proxy
location /carddav/ {
    proxy_pass http://carddav/carddav/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Authorization $http_authorization;  # ← ADD THIS
    proxy_set_header Depth $http_depth;
    proxy_set_header Destination $http_destination;
    proxy_set_header If $http_if;
    proxy_pass_request_headers on;
    proxy_buffering off;
}

# CalDAV discovery endpoint
location /.well-known/caldav {
    proxy_pass http://caldav/.well-known/caldav;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Authorization $http_authorization;  # ← ADD THIS
    proxy_pass_request_headers on;
}

# CalDAV server proxy
location /caldav/ {
    proxy_pass http://caldav/caldav/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Authorization $http_authorization;  # ← ADD THIS
    proxy_set_header Depth $http_depth;
    proxy_set_header Destination $http_destination;
    proxy_set_header If $http_if;
    proxy_pass_request_headers on;
    proxy_buffering off;
}
```

### Also Update Upstream Port

Make sure the CardDAV upstream points to the correct port (8083, not 8082):

```nginx
upstream carddav {
    server 127.0.0.1:8083;  # ← Check this matches your CardDAV port
}
```

## Apply Changes

After updating the nginx config:

```bash
# Test configuration
sudo nginx -t

# If test passes, reload nginx
sudo systemctl reload nginx
```

## Verify

Test CardDAV connection:

```bash
curl -k -X PROPFIND https://ai.poster.place/carddav/ \
  -u "username:password" \
  -H "Depth: 0"
```

Should return `207 Multi-Status` (not `401 Unauthorized`).

## Why This Happens

Some nginx configurations or versions don't automatically forward the `Authorization` header even with `proxy_pass_request_headers on`. Explicitly setting it ensures the header is always forwarded to the backend server.
