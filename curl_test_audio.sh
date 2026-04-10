#!/bin/bash
# Simple curl commands to test WebDAV audio file access

# Replace SERVER and PORT with your actual values
SERVER="localhost"  # or your server IP/hostname
PORT="8000"         # adjust if different

# Test 1: HEAD request to check if file exists and get headers
echo "=== Test 1: HEAD request (check headers) ==="
curl -I -X HEAD "http://${SERVER}:${PORT}/webdav/verita84%40poster.place/Music/%281994%29%20Dookie/Green%20Day%20-01-%20Burnout.mp3"

echo ""
echo "=== Test 2: GET request (download first 1KB) ==="
# Test 2: GET request with range to download just first 1KB
curl --range 0-1023 -o test_sample.mp3 "http://${SERVER}:${PORT}/webdav/verita84%40poster.place/Music/%281994%29%20Dookie/Green%20Day%20-01-%20Burnout.mp3"

echo ""
echo "=== Test 3: Full GET request (download entire file) ==="
# Test 3: Full GET request - WARNING: This will download ~3MB
# curl -o test_full.mp3 "http://${SERVER}:${PORT}/webdav/verita84%40poster.place/Music/%281994%29%20Dookie/Green%20Day%20-01-%20Burnout.mp3"

echo ""
echo "Sample file downloaded: test_sample.mp3"
ls -lh test_sample.mp3 2>/dev/null
