# Photo Gallery Sorting Test

## Location
The test script is located at: `/home/verita84/posterchanai/scripts/test_photo_sorting.py`

## How to Run

### From the project directory:
```bash
cd /home/verita84/posterchanai
python3 scripts/test_photo_sorting.py <username> <password> [options]
```

### Or use the shell script:
```bash
cd /home/verita84/posterchanai
./scripts/test_photo_sorting.sh <username> <password> [base_url]
```

## Examples

### Test via API (server must be running):
```bash
# Default (localhost:8000)
python3 scripts/test_photo_sorting.py verita84@poster.place your_password

# Custom server URL
python3 scripts/test_photo_sorting.py verita84@poster.place your_password --url http://your-server:port
```

### Test file timestamps directly (no server needed):
```bash
python3 scripts/test_photo_sorting.py verita84@poster.place your_password --storage-path /raid/posterchanai
```

## What It Tests

1. **Backend API Sorting** - Verifies `/api/files/all-images` returns images newest-first
2. **File Timestamps** - Checks actual file system timestamps

## Output

The script will show:
- First 20 images with their timestamps and dates
- Any sorting errors (images out of order)
- Summary of test results

## Exit Codes

- `0` - All tests passed
- `1` - Some tests failed
- `2` - No tests could be run (server down and path not found)
