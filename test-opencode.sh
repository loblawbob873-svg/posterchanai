#!/bin/bash
set -e

GENTOO_DIR="/opt/gentoo-installer"
OPENCODE="$HOME/.opencode/bin/opencode"
MODEL="poster/Qwen3.5-9B-Claude-Code-Q4_K_M.gguf"

echo "[TEST] Resetting gentoo.sh to HEAD..."
cd "$GENTOO_DIR"
git checkout HEAD gentoo.sh
echo "[TEST] gentoo.sh reset."

echo "[TEST] Current colorization state:"
grep -c '\\033' gentoo.sh 2>/dev/null | xargs echo "  Colorized lines:" || echo "  Colorized lines: 0"

PROMPT='Add vibrant cyberpunk ANSI colors to the display echo statements in /opt/gentoo-installer/gentoo.sh. Use multiple different colors.'

echo "[TEST] Starting opencode..."
timeout 120 "$OPENCODE" run --model "$MODEL" "$PROMPT" || true

echo ""
echo "[TEST] Done. Checking gentoo.sh..."

# Check 1: bash syntax must pass
if bash -n gentoo.sh 2>/dev/null; then
    echo "[TEST] PASS: bash syntax check OK"
else
    echo "[TEST] FAIL: gentoo.sh has bash syntax errors after colorization"
    bash -n gentoo.sh 2>&1 | head -10
    exit 1
fi

# Check 2: must have valid echo -e lines with \033 color codes
VALID=$(grep -c 'echo -e.*\\033' gentoo.sh 2>/dev/null || echo 0)
echo "[TEST] Valid colorized echo lines: $VALID"
if [ "$VALID" -gt 0 ]; then
    echo "[TEST] PASS: $VALID valid echo -e color lines found"
    grep -n 'echo -e.*\\033' gentoo.sh | head -5
else
    echo "[TEST] FAIL: no valid 'echo -e' color lines found (file may have broken escapes)"
    grep -n '\\033' gentoo.sh | head -5
    exit 1
fi
