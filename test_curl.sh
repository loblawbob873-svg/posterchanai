#!/bin/bash
# Properly encoded curl command for WebDAV audio file

# Using Python to properly URL encode the path
ENCODED_URL=$(python3 -c "
import urllib.parse
base = 'http://localhost:8000/webdav/verita84@poster.place/Music/(1994) Dookie/Green Day -01- Burnout.mp3'
parts = urllib.parse.urlparse(base)
encoded_path = urllib.parse.quote(parts.path, safe='')
print(urllib.parse.urlunparse((parts.scheme, parts.netloc, encoded_path, parts.params, parts.query, parts.fragment)))
")

echo "Testing: $ENCODED_URL"
echo ""
curl -v --range 0-1023 "$ENCODED_URL"
