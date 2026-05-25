#!/bin/bash
# Automated test-fix loop for gentoo.sh cyberpunk task.
# Runs opencode with a simple open-ended prompt; verifies the result.
# Generic proxy improvements are applied between runs (no hardcoded task logic).

OPENCODE="$HOME/.opencode/bin/opencode"
MODEL="poster/Qwen3.5-9B-Claude-Code-Q4_K_M.gguf"
GENTOO_DIR="/opt/gentoo-installer"
PROMPT='modify /opt/gentoo-installer/gentoo.sh to have cyberpunk-style menu titles and colors throughout the script'
MAX_ATTEMPTS=5
OPENCODE_TIMEOUT=600

log() { echo "[$(date '+%H:%M:%S')] $*"; }

reset_gentoo() {
    log "[reset] Resetting gentoo.sh to HEAD..."
    git -C "$GENTOO_DIR" checkout HEAD -- gentoo.sh 2>&1
    # Clear opencode session history so each run starts fresh
    rm -f ~/.local/share/opencode/opencode.db \
          ~/.local/share/opencode/opencode.db-wal \
          ~/.local/share/opencode/opencode.db-shm 2>/dev/null
    log "[reset] Done"
}

run_gentoo() {
    log "[run] Starting opencode (timeout ${OPENCODE_TIMEOUT}s)..."
    (cd "$GENTOO_DIR" && timeout "$OPENCODE_TIMEOUT" "$OPENCODE" run --model "$MODEL" "$PROMPT") || true
    log "[run] opencode exited"
}

verify_gentoo() {
    local f="$GENTOO_DIR/gentoo.sh"
    log "[verify] Checking gentoo.sh..."

    # 1. Syntax check
    if ! bash -n "$f" 2>/dev/null; then
        log "[verify] FAIL: bash syntax errors"
        bash -n "$f" 2>&1 | head -5
        return 1
    fi

    # 2. Shebang must still be present (model must not delete script header)
    if ! head -3 "$f" | grep -qP '^#!'; then
        log "[verify] FAIL: shebang line missing — model deleted script header"
        head -5 "$f"
        return 1
    fi

    # 3. gentoo.sh must actually be modified
    local diff_lines
    diff_lines=$(git -C "$GENTOO_DIR" diff HEAD -- gentoo.sh 2>/dev/null | wc -l)
    if [ "$diff_lines" -lt 5 ]; then
        log "[verify] FAIL: gentoo.sh unchanged (diff has $diff_lines lines)"
        return 1
    fi

    # 4. At least 10 echo lines with ANSI codes AND a proper closing \033[0m reset.
    # Requires: echo -e "\033[<code>m<text>\033[0m" — rules out blank-echo→ANSI conversions
    # that lack a closing reset (the common cheat: changing bare echo to echo -e "\033[Nm[0m").
    local colored
    colored=$(grep -cP 'echo\s+-e\s+\\?"\\033\[[\d;]+m.+\\033\[0m' "$f" 2>/dev/null; true)
    colored=${colored:-0}
    if [ "$colored" -lt 10 ]; then
        log "[verify] FAIL: only $colored properly colorized echo lines with closing reset (need ≥10 with echo -e \"\\033[Nm..text..\\033[0m\")"
        log "[verify] Sample of what's there:"
        grep -P 'echo\s+-e\s+\\?"\\033\[' "$f" | head -5 || true
        return 1
    fi

    log "[verify] PASS: $colored colorized lines with proper reset, syntax OK, diff=$diff_lines lines changed"
    return 0
}

show_log_summary() {
    log "[logs] Recent proxy log summary (last 50 lines):"
    journalctl -u posterchanai-ipex.service -n 50 --no-pager 2>/dev/null \
        | grep -E "WRONG-FILE|LOOP-SC-CHECK|SHORTCIRCUIT|EXPLORATION-CAP|OAI-AGENTIC|WRITE-SUCCESS|AUTO-VERIFY" \
        | tail -20 || true
}

attempt=0
while [ "$attempt" -lt "$MAX_ATTEMPTS" ]; do
    attempt=$((attempt + 1))
    log "=== Attempt $attempt/$MAX_ATTEMPTS ==="

    reset_gentoo
    run_gentoo

    if verify_gentoo; then
        log "=== PASSED on attempt $attempt ==="
        exit 0
    fi

    log "=== FAILED on attempt $attempt ==="
    show_log_summary
    log "=== Retrying... ==="
done

log "=== FAILED after $MAX_ATTEMPTS attempts ==="
exit 1
