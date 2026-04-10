#!/bin/bash
# Fixed curl command - properly encode only the path components, not the leading slash

# The path should be: /webdav/verita84@poster.place/Music/(1994) Dookie/Green Day -01- Burnout.mp3
# We need to encode: @ -> %40, ( -> %28, ) -> %29, space -> %20

# Properly encoded URL (only encode the path components after the domain)
# Port 8808 based on netstat output
URL="http://localhost:8808/webdav/verita84%40poster.place/Music/%281994%29%20Dookie/Green%20Day%20-01-%20Burnout.mp3"

echo "Testing: $URL"
echo ""
echo "=== HEAD request ==="
curl -I "$URL" 2>&1 | head -15

echo ""
echo "=== GET request (first 1KB) ==="
curl --range 0-1023 -o test_sample.mp3 "$URL" 2>&1 | tail -5

echo ""
if [ -f test_sample.mp3 ]; then
    echo "File downloaded successfully!"
    ls -lh test_sample.mp3
    file test_sample.mp3
else
    echo "Download failed"
fi
