#!/bin/bash
set -e

OPENCODE="$HOME/.opencode/bin/opencode"
MODEL="poster/Qwen3.5-9B-Claude-Code-Q4_K_M.gguf"

echo "[TEST] Resetting ~/aria2 to v1.4.9..."
git -C ~/aria2 rebase --abort 2>/dev/null || true
git -C ~/aria2 reset --hard v1.4.9
# Reset origin URL in case a previous run corrupted it
git -C ~/aria2 remote set-url origin https://github.com/poppingmoon/aria.git
# Remove any extra remotes added by previous test runs (keep only 'origin')
git -C ~/aria2 remote | grep -v '^origin$' | while read r; do git -C ~/aria2 remote remove "$r"; done
# Remove any extra branches added by previous test runs (keep only current)
git -C ~/aria2 branch | grep -v '^\*' | xargs -r git -C ~/aria2 branch -D 2>/dev/null || true
echo "[TEST] aria2 is at: $(git -C ~/aria2 describe --tags --exact-match 2>/dev/null || git -C ~/aria2 log --oneline -1)"

echo "[TEST] aria (source) is at: $(git -C ~/aria log --oneline -1)"

PROMPT="The repo at ~/aria2 is a fork that is behind. The repo at ~/aria has the latest commits. Update ~/aria2 so it is fully up to date with ~/aria."

echo "[TEST] Starting opencode..."
timeout 120 "$OPENCODE" run --model "$MODEL" "$PROMPT" || true

echo ""
echo "[TEST] Done. Verifying result..."
echo "[TEST] aria2 HEAD: $(git -C ~/aria2 log --oneline -1)"
echo "[TEST] aria HEAD:  $(git -C ~/aria log --oneline -1)"

ARIA_HEAD=$(git -C ~/aria log --format="%H" -1)
ARIA2_HEAD=$(git -C ~/aria2 log --format="%H" -1)

if [ "$ARIA_HEAD" = "$ARIA2_HEAD" ]; then
    echo "[TEST] PASS: aria2 HEAD matches aria HEAD"
else
    echo "[TEST] FAIL: aria2 HEAD ($ARIA2_HEAD) does not match aria HEAD ($ARIA_HEAD)"
    exit 1
fi

if [ -f ~/aria2/lib/view/page/qr_page.dart ]; then
    echo "[TEST] PASS: v1.5.0 file qr_page.dart present"
else
    echo "[TEST] FAIL: v1.5.0 file qr_page.dart missing"
    exit 1
fi
