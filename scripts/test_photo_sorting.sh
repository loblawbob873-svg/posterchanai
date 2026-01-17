#!/bin/bash
# Automated test for Photo Gallery sorting
# Usage: ./test_photo_sorting.sh <username> <password> [base_url]

set -e

USERNAME="${1:-}"
PASSWORD="${2:-}"
BASE_URL="${3:-http://localhost:8000}"

if [ -z "$USERNAME" ] || [ -z "$PASSWORD" ]; then
    echo "Usage: $0 <username> <password> [base_url]"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

echo "Running Photo Gallery sorting test..."
echo "Username: $USERNAME"
echo "Base URL: $BASE_URL"
echo ""

python3 scripts/test_photo_sorting.py "$USERNAME" "$PASSWORD" --url "$BASE_URL"

EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo ""
    echo "✓ All tests passed!"
else
    echo ""
    echo "❌ Tests failed!"
fi

exit $EXIT_CODE
