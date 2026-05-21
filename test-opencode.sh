#!/bin/bash
# Master opencode test runner
# Tests: gentoo-colorize | python-firewall-cyberpunk | aikey-android-merge
# Each test must pass 3 consecutive times before moving to the next.
# After all pass, sync.sh is run to deploy.

OPENCODE="$HOME/.opencode/bin/opencode"
MODEL="poster/Qwen3.5-9B-Claude-Code-Q4_K_M.gguf"
PASS_THRESHOLD=3
MAX_ATTEMPTS=10

GENTOO_DIR="/opt/gentoo-installer"
DESKTOP_USER="verita84"
DESKTOP_HOST="192.168.0.102"
DESKTOP_PASS="123456"
# Pre-v1.5.1-merge commit on aikey-android branch 1.0
AIKEY_RESET_COMMIT="f45622f65c65ad79a6e1fcb07bac3b92e3a3770c"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

ssh_router() { ssh -o StrictHostKeyChecking=no router.lan "$@"; }
ssh_desktop() { sshpass -p "$DESKTOP_PASS" ssh -o StrictHostKeyChecking=no "${DESKTOP_USER}@${DESKTOP_HOST}" "$@"; }

# ── Test 1: Gentoo colorization ────────────────────────────────────────────────

reset_gentoo() {
    log "[gentoo] Resetting gentoo.sh to HEAD..."
    git -C "$GENTOO_DIR" checkout HEAD gentoo.sh
}

run_gentoo() {
    local prompt='Add vibrant cyberpunk ANSI colors to the display echo statements in /opt/gentoo-installer/gentoo.sh. IMPORTANT: Only colorize echoes that print to the terminal — those that do NOT have >> or > file redirections after them. There are approximately 50 display echo lines (section headers, status messages, progress lines) — colorize as many as possible. Use at least 3 DIFFERENT color codes. Be thorough — do not stop after just a few lines.'
    log "[gentoo] Running opencode..."
    (cd "$GENTOO_DIR" && timeout 300 "$OPENCODE" run --model "$MODEL" "$prompt") || true
}

verify_gentoo() {
    local f="$GENTOO_DIR/gentoo.sh"
    log "[gentoo] Verifying..."

    if ! bash -n "$f" 2>/dev/null; then
        log "[gentoo] FAIL: bash syntax errors"; bash -n "$f" 2>&1 | head -5; return 1
    fi

    local broken; broken=$(grep -E '^\s*\\033' "$f" 2>/dev/null | wc -l)
    if [ "$broken" -gt 0 ]; then
        log "[gentoo] FAIL: $broken lines have ANSI codes before echo"; return 1
    fi

    local valid; valid=$(grep 'echo -e.*\\033' "$f" 2>/dev/null | wc -l)
    if [ "$valid" -lt 20 ]; then
        log "[gentoo] FAIL: only $valid colorized lines (need ≥20)"; return 1
    fi

    local redirect_colored; redirect_colored=$(grep -E 'echo -e.*\\033.*[>|]' "$f" 2>/dev/null | wc -l)
    if [ "$redirect_colored" -gt 0 ]; then
        log "[gentoo] FAIL: $redirect_colored colorized echoes redirect to files"; return 1
    fi

    local colors; colors=$(grep -oE '\\033\[[0-9;]+m' "$f" 2>/dev/null | sort -u | wc -l)
    if [ "$colors" -lt 3 ]; then
        log "[gentoo] FAIL: only $colors distinct colors (need ≥3)"; return 1
    fi

    log "[gentoo] PASS: $valid colorized lines, $colors distinct colors"
}

# ── Test 2: Python-firewall cyberpunk theme ────────────────────────────────────

reset_pyfw() {
    log "[pyfw] Resetting cli.py and html.py to HEAD on router.lan..."
    ssh_router "cd /opt/python-firewall && git checkout HEAD cli.py html.py && rm -f ~/.local/share/opencode/opencode.db"
}

run_pyfw() {
    log "[pyfw] Running opencode on router.lan..."
    ssh_router "cd /opt/python-firewall && timeout 300 ~/.opencode/bin/opencode run --model '$MODEL' 'modify the cli and webui to give it a cyberpunk theme'" || true
}

verify_pyfw() {
    log "[pyfw] Verifying cyberpunk theme..."
    ssh_router '
        cd /opt/python-firewall

        CHANGED=$(git diff --name-only HEAD cli.py html.py 2>/dev/null | wc -l)
        if [ "$CHANGED" -eq 0 ]; then
            echo "[pyfw] FAIL: no files changed from HEAD"; exit 1
        fi

        python3 -m py_compile cli.py 2>/dev/null || { echo "[pyfw] FAIL: cli.py syntax error"; exit 1; }
        python3 -m py_compile html.py 2>/dev/null || { echo "[pyfw] FAIL: html.py syntax error"; exit 1; }

        CLI_CYBER=$(grep -cE "\\\\033\[|\\\\x1b\[|colorama|Fore\.|Back\.|Style\.|neon|cyan|magenta|CYAN|MAGENTA|NEON" cli.py 2>/dev/null || echo 0)
        HTML_CYBER=$(grep -ciE "#[0-9a-fA-F]{3,6}|neon|glow|cyberpunk|magenta|cyan|text-shadow|box-shadow|rgba" html.py 2>/dev/null || echo 0)
        TOTAL=$((CLI_CYBER + HTML_CYBER))

        if [ "$TOTAL" -lt 2 ]; then
            echo "[pyfw] FAIL: insufficient cyberpunk elements: cli=${CLI_CYBER} html=${HTML_CYBER}"; exit 1
        fi

        echo "[pyfw] PASS: ${CHANGED} files changed, cli_cyber=${CLI_CYBER} html_cyber=${HTML_CYBER}"
    ' && return 0 || return 1
}

# ── Test 3: aikey-android merge ────────────────────────────────────────────────

# Base64-encode the complex prompt to avoid SSH quoting issues
AIKEY_PROMPT=$(cat <<'PROMPT'
~/aikey-android is a fork of /home/verita84/aria (local mirror). Merge upstream changes while preserving:
- Aikey branding (name, version in pubspec.yaml)
- AI features: lib/provider/api/ai_service_provider.dart, lib/view/page/ai_chat_page.dart, lib/view/widget/ai_chat_widget.dart, lib/provider/api/auto_update_provider.dart
- Custom theme: lib/constant/theme_props.dart (Cyberpunk Dark with defaultDarkThemeProps = cyberpunkDarkThemeProps), lib/constant/colors.dart (aikeyColor), lib/constant/builtin_misskey_colors.g.dart (Cyberpunk Dark with id: cafe0001-0000-4000-8000-cafe00000001)
- Modified lib files: about_aria_page.dart, timeline_page.dart, post_form.dart, note_footer.dart, general_settings.dart, router.dart
Steps:
1. git merge local-aria/main --no-commit --allow-unrelated-histories
2. CRITICAL: Resolve theme_props.dart conflict by keeping Aikey version (defaultDarkThemeProps = cyberpunkDarkThemeProps)
3. For all other conflicts: git diff --name-only --diff-filter=U | xargs git checkout HEAD --
4. Code Review
5. Run sync-apk.sh and fix any build errors.
PROMPT
)

reset_aikey() {
    log "[aikey] Resetting branch 1.0 to pre-v1.5.1 state on desktop..."
    ssh_desktop "
        cd ~/aikey-android
        git merge --abort 2>/dev/null || true
        git checkout 1.0 2>/dev/null || true
        git reset --hard $AIKEY_RESET_COMMIT
        git remote get-url local-aria 2>/dev/null || git remote add local-aria /home/verita84/aria
        git fetch local-aria
        rm -f ~/.local/share/opencode/opencode.db
        echo '[aikey] reset done'
    "
}

run_aikey() {
    log "[aikey] Running opencode on desktop for aikey-android merge..."
    local encoded
    encoded=$(printf '%s' "$AIKEY_PROMPT" | base64 -w0)
    ssh_desktop "
        PROMPT=\$(printf '%s' '$encoded' | base64 -d)
        cd ~/aikey-android
        timeout 900 ~/.opencode/bin/opencode run --model '$MODEL' \"\$PROMPT\"
    " || true
}

verify_aikey() {
    log "[aikey] Verifying aikey-android merge result..."
    ssh_desktop '
        cd ~/aikey-android

        grep -q "defaultDarkThemeProps = cyberpunkDarkThemeProps" lib/constant/theme_props.dart 2>/dev/null \
            || { echo "[aikey] FAIL: theme_props.dart missing cyberpunk default"; exit 1; }

        grep -qiE "name:\s*(aikey|Aikey)" pubspec.yaml 2>/dev/null \
            || { echo "[aikey] FAIL: pubspec.yaml missing Aikey name"; exit 1; }

        for f in lib/provider/api/ai_service_provider.dart lib/view/page/ai_chat_page.dart lib/view/widget/ai_chat_widget.dart; do
            [ -f "$f" ] || { echo "[aikey] FAIL: missing $f"; exit 1; }
        done

        STATUS=$(git status --porcelain 2>/dev/null | head -1)
        if [ -n "$STATUS" ]; then
            echo "[aikey] FAIL: uncommitted changes or unresolved conflicts"
            git status --short | head -10
            exit 1
        fi

        [ -f build/app/outputs/flutter-apk/app-release.apk ] \
            || { echo "[aikey] FAIL: APK not built"; exit 1; }

        echo "[aikey] PASS: all checks passed"
    ' && return 0 || return 1
}

# ── 3-pass runner ─────────────────────────────────────────────────────────────

run_until_3_passes() {
    local name=$1 reset_fn=$2 run_fn=$3 verify_fn=$4
    local passes=0 attempts=0

    while [ "$passes" -lt "$PASS_THRESHOLD" ] && [ "$attempts" -lt "$MAX_ATTEMPTS" ]; do
        attempts=$((attempts + 1))
        log "[$name] Attempt $attempts (passes: $passes/$PASS_THRESHOLD)"

        if ! $reset_fn; then
            log "[$name] Reset failed — retrying"
            continue
        fi

        $run_fn

        if $verify_fn; then
            passes=$((passes + 1))
            log "[$name] PASS $passes/$PASS_THRESHOLD"
        else
            passes=0
            log "[$name] FAIL — resetting and retrying"
        fi
    done

    if [ "$passes" -ge "$PASS_THRESHOLD" ]; then
        log "[$name] SUCCESS: passed $PASS_THRESHOLD consecutive times"
        return 0
    else
        log "[$name] FAILURE after $MAX_ATTEMPTS attempts"
        return 1
    fi
}

# ── Final single-pass validation ───────────────────────────────────────────────

run_single_pass() {
    local name=$1 reset_fn=$2 run_fn=$3 verify_fn=$4
    log "[$name] Final validation run..."
    $reset_fn || { log "[$name] Final reset failed"; return 1; }
    $run_fn
    if $verify_fn; then
        log "[$name] Final PASS"
    else
        log "[$name] Final FAIL"
        return 1
    fi
}

# ── Main ───────────────────────────────────────────────────────────────────────

log "=== opencode test suite — each test must pass $PASS_THRESHOLD consecutive times ==="

run_until_3_passes "gentoo-colorize"     reset_gentoo run_gentoo verify_gentoo
run_until_3_passes "python-firewall"     reset_pyfw   run_pyfw      verify_pyfw
run_until_3_passes "aikey-android-merge" reset_aikey  run_aikey     verify_aikey

log "=== All 3-pass requirements met. Running final single-pass validation... ==="
run_single_pass "gentoo-colorize"     reset_gentoo run_gentoo verify_gentoo
run_single_pass "python-firewall"     reset_pyfw   run_pyfw      verify_pyfw
run_single_pass "aikey-android-merge" reset_aikey  run_aikey     verify_aikey

log "=== Final validation complete. Running sync.sh to deploy... ==="
bash "$(dirname "$0")/sync.sh"
log "=== Done ==="
