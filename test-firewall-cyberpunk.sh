#!/bin/bash
# Automated test-fix loop for python-firewall cyberpunk task.
# Runs opencode on router.lan in /opt/python-firewall; verifies the result.

OPENCODE="/home/verita84/.opencode/bin/opencode"
MODEL="poster/Qwen3.5-9B-Claude-Code-Q4_K_M.gguf"
ROUTER="verita84@router.lan"
FW_DIR="/opt/python-firewall"
PROMPT='Add cyberpunk CSS to html.py and ANSI colors to cli.py. CRITICAL RULES: (A) Do NOT use colorama — use \033[ escape codes directly. (B) Do NOT rewrite cli.py from scratch — ADD color codes to the existing code only. (C) Preserve ALL existing functions exactly: buildCLI must remain defined in cli.py (firewall.py imports it). Requirements: (1) html.py must contain these EXACT strings: #00ffff #ff00ff text-shadow (use them in the CSS, e.g. color:#00ffff; border:1px solid #ff00ff; text-shadow:0 0 10px #00ffff;). (2) cli.py must contain these EXACT strings: \033[36m \033[35m \033[0m (add them to existing print/output statements, do not remove buildCLI or any other function). After editing both files run: sudo systemctl restart python-firewall'
MAX_ATTEMPTS=10
OPENCODE_TIMEOUT=600

log() { echo "[$(date '+%H:%M:%S')] $*"; }

reset_firewall() {
    log "[reset] Resetting cli.py and html.py to HEAD on router..."
    ssh "$ROUTER" "git -C '$FW_DIR' checkout HEAD -- cli.py html.py 2>&1; \
        rm -f '$FW_DIR'/opencode.json 2>/dev/null; \
        rm -f /home/verita84/.local/share/opencode/opencode.db \
              /home/verita84/.local/share/opencode/opencode.db-wal \
              /home/verita84/.local/share/opencode/opencode.db-shm 2>/dev/null; \
        echo done"
    log "[reset] Done"
}

run_firewall() {
    log "[run] Starting opencode on router (timeout ${OPENCODE_TIMEOUT}s)..."
    ssh "$ROUTER" "cd '$FW_DIR' && \
        OPENAI_BASE_URL=http://192.168.0.2:5000 \
        OPENAI_API_KEY=local \
        timeout '$OPENCODE_TIMEOUT' '$OPENCODE' run --model '$MODEL' '$PROMPT'" || true
    log "[run] opencode exited"
}

verify_firewall() {
    log "[verify] Checking results on router..."

    # 1. Python syntax check both files + runtime import check for cli.py
    if ! ssh "$ROUTER" "python3 -m py_compile '$FW_DIR/cli.py' 2>&1"; then
        log "[verify] FAIL: cli.py has syntax errors"
        return 1
    fi
    if ! ssh "$ROUTER" "python3 -m py_compile '$FW_DIR/html.py' 2>&1"; then
        log "[verify] FAIL: html.py has syntax errors"
        return 1
    fi
    # Check that buildCLI is importable (py_compile passes but missing def still breaks the service)
    if ! ssh "$ROUTER" "cd '$FW_DIR' && python3 -c 'from cli import buildCLI' 2>&1"; then
        log "[verify] FAIL: buildCLI not importable from cli.py"
        ssh "$ROUTER" "grep -n 'def buildCLI\|def build_cli' '$FW_DIR/cli.py' | head -5" || true
        return 1
    fi

    # 2. Files must actually be modified from HEAD
    local cli_diff html_diff
    cli_diff=$(ssh "$ROUTER" "git -C '$FW_DIR' diff HEAD -- cli.py | wc -l")
    html_diff=$(ssh "$ROUTER" "git -C '$FW_DIR' diff HEAD -- html.py | wc -l")
    if [ "${cli_diff:-0}" -lt 5 ]; then
        log "[verify] FAIL: cli.py unchanged (diff=$cli_diff lines)"
        return 1
    fi
    if [ "${html_diff:-0}" -lt 5 ]; then
        log "[verify] FAIL: html.py unchanged (diff=$html_diff lines)"
        return 1
    fi

    # 3. WebUI must have cyberpunk CSS: neon colors or glow effects
    local html_cyber
    html_cyber=$(ssh "$ROUTER" "grep -ciE '#00ffff|#ff00ff|#0ff|neon|glow|cyberpunk|text-shadow|scanline|0 0 [0-9]+px' '$FW_DIR/html.py' 2>/dev/null; true")
    html_cyber=${html_cyber:-0}
    if [ "$html_cyber" -lt 3 ]; then
        log "[verify] FAIL: html.py has only $html_cyber cyberpunk CSS indicators (need ≥3)"
        log "[verify] Sample html.py content:"
        ssh "$ROUTER" "grep -iE 'neon|glow|#00|#ff|cyberpunk|text-shadow|scanline' '$FW_DIR/html.py' | head -5" || true
        return 1
    fi

    # 4. CLI must have vivid ANSI colors or colorama neon usage
    local cli_color
    cli_color=$(ssh "$ROUTER" "grep -ciE 'neon|NEON|cyan|CYAN|magenta|MAGENTA|#00ffff|colorama|\\\\033\[' '$FW_DIR/cli.py' 2>/dev/null; true")
    cli_color=${cli_color:-0}
    if [ "$cli_color" -lt 3 ]; then
        log "[verify] FAIL: cli.py has only $cli_color color/neon indicators (need ≥3)"
        log "[verify] Sample cli.py content:"
        ssh "$ROUTER" "grep -iE 'neon|cyan|magenta|colorama|033\[' '$FW_DIR/cli.py' | head -5" || true
        return 1
    fi

    # 5. Service must be active after restart — wait 4s to let startup errors manifest
    sleep 4
    local svc_status
    svc_status=$(ssh "$ROUTER" "systemctl is-active python-firewall 2>/dev/null; true")
    if [ "$svc_status" != "active" ]; then
        log "[verify] FAIL: python-firewall service is not active (status: $svc_status)"
        ssh "$ROUTER" "journalctl -u python-firewall -n 20 --no-pager 2>/dev/null" | tail -10 || true
        return 1
    fi

    # 6. No fatal startup errors in last 25 log lines (recent enough to catch current restart errors,
    # but avoids stale errors from previous attempts that --since '2 minutes ago' fails to filter
    # on older systemd versions).
    local recent_errors
    recent_errors=$(ssh "$ROUTER" "journalctl -u python-firewall -n 25 --no-pager 2>/dev/null | grep -cE 'ImportError|NameError|SyntaxError|Traceback|start-limit-hit|Failed to start'; true")
    recent_errors=${recent_errors:-0}
    if [ "$recent_errors" -gt 0 ]; then
        log "[verify] FAIL: python-firewall has $recent_errors recent startup errors (ImportError/crash)"
        ssh "$ROUTER" "journalctl -u python-firewall -n 25 --no-pager 2>/dev/null | grep -E 'Error|Traceback|Failed'" || true
        return 1
    fi

    log "[verify] PASS: cli.py ($cli_color color indicators, diff=$cli_diff lines), html.py ($html_cyber cyber indicators, diff=$html_diff lines), service active, no errors"
    return 0
}

show_log_summary() {
    log "[logs] Recent proxy log (last 30 lines):"
    journalctl -u posterchanai-ipex.service -n 30 --no-pager 2>/dev/null \
        | grep -E "WRONG-FILE|LOOP-SC-CHECK|SHORTCIRCUIT|EXPLORATION-CAP|OAI-AGENTIC|WRITE-SUCCESS|AUTO-VERIFY|PROXY-AUTO-FIX" \
        | tail -15 || true
}

attempt=0
while [ "$attempt" -lt "$MAX_ATTEMPTS" ]; do
    attempt=$((attempt + 1))
    log "=== Attempt $attempt/$MAX_ATTEMPTS ==="

    reset_firewall
    run_firewall

    if verify_firewall; then
        log "=== PASSED on attempt $attempt ==="
        exit 0
    fi

    log "=== FAILED on attempt $attempt ==="
    show_log_summary
    log "=== Retrying... ==="
done

log "=== FAILED after $MAX_ATTEMPTS attempts ==="
exit 1
