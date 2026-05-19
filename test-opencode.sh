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

PROMPT='Add vibrant cyberpunk ANSI colors to the display echo statements in /opt/gentoo-installer/gentoo.sh. IMPORTANT: Only colorize echoes that print to the terminal — those that do NOT have >> or > file redirections after them. There are approximately 50 display echo lines (section headers, status messages, progress lines) — colorize as many as possible. Use at least 3 DIFFERENT color codes. Be thorough — do not stop after just a few lines.'

echo "[TEST] Starting opencode..."
timeout 300 "$OPENCODE" run --model "$MODEL" "$PROMPT" || true

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

# Check 2: no broken patterns (color codes before echo, not inside it)
BROKEN=$(grep -E '^\s*\\033' gentoo.sh 2>/dev/null | wc -l)
if [ "$BROKEN" -gt 0 ]; then
    echo "[TEST] FAIL: $BROKEN lines have ANSI codes BEFORE echo (broken pattern)"
    grep -nE '^\s*\\033' gentoo.sh | head -5
    exit 1
fi
echo "[TEST] PASS: no broken color patterns"

# Check 3: must have valid echo -e lines with \033 color codes (at least 20)
VALID=$(grep 'echo -e.*\\033' gentoo.sh 2>/dev/null | wc -l)
echo "[TEST] Valid colorized echo lines: $VALID"
if [ "$VALID" -ge 20 ]; then
    echo "[TEST] PASS: $VALID valid echo -e color lines found"
    grep -n 'echo -e.*\\033' gentoo.sh | head -5
else
    echo "[TEST] FAIL: only $VALID colorized lines — need at least 20 (there are ~90 display echo lines in this file)"
    grep -n '\\033' gentoo.sh | head -5
    exit 1
fi

# Check 3b: no colorized echoes that redirect to files (would break config files)
REDIRECT_COLORED=$(grep -E 'echo -e.*\\033.*[>|]' gentoo.sh 2>/dev/null | wc -l)
if [ "$REDIRECT_COLORED" -gt 0 ]; then
    echo "[TEST] FAIL: $REDIRECT_COLORED colorized echoes redirect to files (breaks config files)"
    grep -nE 'echo -e.*\\033.*[>|]' gentoo.sh | head -5
    exit 1
fi
echo "[TEST] PASS: no colorized file-writing echoes"

# Check 4: must use at least 3 distinct color codes
COLORS=$(grep -oE '\\033\[[0-9;]+m' gentoo.sh 2>/dev/null | sort -u | wc -l)
echo "[TEST] Distinct color codes: $COLORS"
if [ "$COLORS" -ge 3 ]; then
    echo "[TEST] PASS: $COLORS distinct color codes found"
    grep -oE '\\033\[[0-9;]+m' gentoo.sh | sort -u | head -6
else
    echo "[TEST] FAIL: only $COLORS distinct color code(s) — prompt requires multiple different colors"
    grep -oE '\\033\[[0-9;]+m' gentoo.sh | sort -u
    exit 1
fi
