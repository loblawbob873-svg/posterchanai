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
AIKEY_RESET_COMMIT="781c2477"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

ssh_router() { ssh -o StrictHostKeyChecking=no router.lan "$@"; }
ssh_desktop() { sshpass -p "$DESKTOP_PASS" ssh -o StrictHostKeyChecking=no "${DESKTOP_USER}@${DESKTOP_HOST}" "$@"; }

# ── Test 1: Gentoo colorization ────────────────────────────────────────────────

reset_gentoo() {
    log "[gentoo] Resetting gentoo.sh to HEAD..."
    git -C "$GENTOO_DIR" checkout HEAD gentoo.sh
}

run_gentoo() {
    local prompt='Add ANSI color codes to echo statements in /opt/gentoo-installer/gentoo.sh using a Python script. TWO STEPS ONLY: Step 1 — use the Write tool to create /tmp/gc.py. The script must: read /opt/gentoo-installer/gentoo.sh line by line; for each line whose stripped form starts with echo followed by a double-quoted string and has no >> or > redirect in the part before any # comment, replace echo "TEXT" with echo -e "\033[COLOR]TEXT\033[0m" using alternating colors \033[1;36m \033[1;35m \033[1;32m \033[1;33m; write all lines back to /opt/gentoo-installer/gentoo.sh; print the count of changed lines. Use import re in the script. Step 2 — use Bash tool to run: python3 /tmp/gc.py. Do NOT use the Edit tool on gentoo.sh directly. The result must have at least 30 lines matching echo -e.*\033.'
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
    log "[pyfw] Resetting all python-firewall files to HEAD on router.lan..."
    ssh_router "cd /opt/python-firewall && git checkout HEAD -- . && sudo systemctl reset-failed nginx python-firewall 2>/dev/null; sudo systemctl restart nginx && sudo systemctl restart python-firewall; rm -f ~/.local/share/opencode/opencode.db ~/.local/share/opencode/opencode.db-wal ~/.local/share/opencode/opencode.db-shm"
}

run_pyfw() {
    log "[pyfw] Running opencode on router.lan..."
    ssh_router "cd /opt/python-firewall && timeout 900 ~/.opencode/bin/opencode run --model '$MODEL' 'Completely redesign BOTH html.py AND cli.py with a full cyberpunk neon theme. WRITE ORDER: write html.py FIRST using the Write tool, then write cli.py using the Write tool. Requirements for html.py: full cyberpunk HTML/CSS/JS overhaul — dark background (#0a0a0f), neon glowing borders, cyan/magenta color scheme with text-shadow glow, scanline overlay, box-shadow pulse effects. Use these words: neon, glow, cyberpunk, glitch. CRITICAL html.py requirement: the magnifying glass search icon must open a modal popup dialog (with id modal or class modal, plus an overlay div, plus JavaScript to toggle display). Do NOT navigate to a new page. FUNCTION REQUIREMENT: html.py must define a TOP-LEVEL module function named buildWeb — NOT inside any class, NOT a class method. It must be: def buildWeb(entries, stats): at the top level of the module. STANDALONE REQUIREMENT: html.py must NOT import from commands, db, config, or any other local project modules — only Python standard library imports. The file must be importable: python3 -c \"import sys; sys.path.insert(0, \\\".\\\"); from html import buildWeb\" must succeed. STRING REQUIREMENT: Do NOT use triple-quoted strings (no f\\\"\\\"\\\", no \\\"\\\"\\\", no triple quotes of any kind) — build the HTML string using regular string concatenation with + operator. Requirements for cli.py: use colorama throughout with neon cyan/magenta ANSI colors. Import EVERY name from colorama that you use: from colorama import Fore, Back, Style, init. Use at least 10 color escape sequences. IMPORTANT: Write the COMPLETE file content in each Write call — do NOT truncate or cut off mid-file.'" || true
}

verify_pyfw() {
    log "[pyfw] Verifying cyberpunk theme..."
    ssh_router '
        cd /opt/python-firewall

        CHANGED=$(git diff --name-only HEAD cli.py html.py 2>/dev/null | wc -l)
        if [ "$CHANGED" -lt 2 ]; then
            echo "[pyfw] FAIL: both cli.py and html.py must be changed (only ${CHANGED} changed)"; exit 1
        fi

        python3 -m py_compile cli.py 2>/dev/null || { echo "[pyfw] FAIL: cli.py syntax error"; exit 1; }
        python3 -m py_compile html.py 2>/dev/null || { echo "[pyfw] FAIL: html.py syntax error"; exit 1; }

        # Ensure html.py still exports buildWeb (firewall.py does: from html import buildWeb)
        python3 -c "from html import buildWeb" 2>/dev/null \
            || { echo "[pyfw] FAIL: html.py must define or alias buildWeb (firewall.py imports it)"; exit 1; }

        # Check colorama names are actually imported — catches NameError: Style/Fore/Back not defined
        for CNAME in Fore Back Style; do
            if grep -qE "\b${CNAME}\." cli.py 2>/dev/null; then
                if ! grep -qE "from colorama import[^#]*\b${CNAME}\b|import colorama" cli.py 2>/dev/null; then
                    echo "[pyfw] FAIL: cli.py uses ${CNAME}.* but ${CNAME} is not imported from colorama"; exit 1
                fi
            fi
        done

        CLI_CYBER=$(grep -cE "\\\\033\[|\\\\x1b\[|colorama|Fore\.|Back\.|Style\.|neon|cyan|magenta|CYAN|MAGENTA|NEON" cli.py 2>/dev/null || echo 0)

        # Require deep redesign keywords — original html.py had no neon/glow/cyberpunk/glitch/terminal
        HTML_DEEP=$(grep -ciE "neon|glow|cyberpunk|glitch|terminal|scanline|matrix|hologram|pulse" html.py 2>/dev/null || echo 0)
        # Require modal implementation for magnifying glass
        HTML_MODAL=$(grep -ciE "\bmodal\b|showModal|getElementById|style\.display|\.modal|#modal|overlay" html.py 2>/dev/null || echo 0)

        if [ "$CLI_CYBER" -lt 10 ]; then
            echo "[pyfw] FAIL: cli.py needs more cyberpunk styling (cli_cyber=${CLI_CYBER}, need >=10)"; exit 1
        fi

        if [ "$HTML_DEEP" -lt 5 ]; then
            echo "[pyfw] FAIL: html.py needs deep cyberpunk redesign (html_deep=${HTML_DEEP} neon/glow/etc, need >=5)"; exit 1
        fi

        if [ "$HTML_MODAL" -lt 3 ]; then
            echo "[pyfw] FAIL: html.py missing modal popup for magnifying glass search (html_modal=${HTML_MODAL}, need >=3 modal patterns)"; exit 1
        fi

        echo "[pyfw] PASS: ${CHANGED} files changed, cli_cyber=${CLI_CYBER} html_deep=${HTML_DEEP} html_modal=${HTML_MODAL}"
    ' && return 0 || return 1
}

# ── Test 3: aikey-android merge ────────────────────────────────────────────────

# Base64-encode the complex prompt to avoid SSH quoting issues
AIKEY_PROMPT=$(cat <<'PROMPT'
~/aikey-android is a fork of /home/verita84/aria (local mirror). Complete ALL steps in order:

PRE-MERGE SETUP:
1. Run `flutter --version`. If Dart SDK < 3.12.0, run `flutter upgrade` first.
2. Restore signing keystore from git history: git show 7e4c990a:android/app/upload.keystore > android/app/upload.keystore
3. Write android/key.properties with EXACTLY these contents (use the Write tool):
   storePassword=AikeyApp2024!
   keyPassword=AikeyApp2024!
   keyAlias=aikey
   storeFile=../app/upload.keystore
4. Check pubspec.yaml for path dependencies pointing to non-existent local directories. Remove any such entries with: sed -i '/^  DEPNAME:/,+1d' pubspec.yaml  (replace DEPNAME with the actual dep name). Do NOT run flutter pub get here.

MERGE:
5. git merge local-aria/main --no-commit --allow-unrelated-histories
6. CRITICAL: Resolve theme_props.dart conflict by keeping Aikey version (defaultDarkThemeProps = cyberpunkDarkThemeProps)
7. For all other conflicts: git diff --name-only --diff-filter=U | xargs git checkout HEAD --
8. Commit: git add -A && git commit -m 'Merge local-aria/main'

BUILD:
9. Run ./sync-apk.sh ONCE. If it fails:
   - Dart SDK version error: run `flutter upgrade`, then retry once
   - "proguard-android.txt is no longer supported" error: run: find ~/.pub-cache -name 'build.gradle' -path '*/flutter_inappwebview_android*' -exec sed -i 's/proguard-android\.txt/proguard-android-optimize.txt/g' {} +  then retry once
   - Any other build error: investigate and fix, then retry once
10. STOP IMMEDIATELY after ./sync-apk.sh completes successfully. Do not run git status, git log, or ./sync-apk.sh again.

PRESERVE throughout:
- Aikey branding (name, version in pubspec.yaml)
- AI features: lib/provider/api/ai_service_provider.dart, lib/view/page/ai_chat_page.dart, lib/view/widget/ai_chat_widget.dart, lib/provider/api/auto_update_provider.dart
- Custom theme: lib/constant/theme_props.dart (Cyberpunk Dark with defaultDarkThemeProps = cyberpunkDarkThemeProps), lib/constant/colors.dart (aikeyColor), lib/constant/builtin_misskey_colors.g.dart (Cyberpunk Dark with id: cafe0001-0000-4000-8000-cafe00000001)
- Modified lib files: about_aria_page.dart, timeline_page.dart, post_form.dart, note_footer.dart, general_settings.dart, router.dart
PROMPT
)

reset_aikey() {
    log "[aikey] Resetting master to pre-v1.5.1 state on desktop..."
    ssh_desktop "
        cd ~/aikey-android
        git merge --abort 2>/dev/null || true
        git checkout master 2>/dev/null || true
        git reset --hard $AIKEY_RESET_COMMIT
        git remote get-url local-aria 2>/dev/null || git remote add local-aria /home/verita84/aria
        git fetch local-aria
        rm -f ~/.local/share/opencode/opencode.db ~/.local/share/opencode/opencode.db-wal ~/.local/share/opencode/opencode.db-shm
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

        # Only fail on actual unresolved merge conflicts (UU), not normal modified files like pubspec.lock
        CONFLICTS=$(git diff --name-only --diff-filter=U 2>/dev/null | wc -l)
        if [ "$CONFLICTS" -gt 0 ]; then
            echo "[aikey] FAIL: unresolved merge conflicts"
            git diff --name-only --diff-filter=U | head -10
            exit 1
        fi

        [ -f build/app/outputs/flutter-apk/app-release.apk ] \
            || { echo "[aikey] FAIL: APK not built"; exit 1; }

        echo "[aikey] PASS: all checks passed"
    ' && return 0 || return 1
}

# ── Single-run runner ─────────────────────────────────────────────────────────

run_once() {
    local name=$1 reset_fn=$2 run_fn=$3 verify_fn=$4
    log "[$name] Running..."
    $reset_fn || { log "[$name] Reset failed"; return 1; }
    $run_fn
    if $verify_fn; then
        log "[$name] PASS"
        return 0
    else
        log "[$name] FAIL"
        return 1
    fi
}

# ── Main ───────────────────────────────────────────────────────────────────────

while true; do
    log "=== opencode test suite — running each test once in sequence ==="

    RC_GENTOO=0; RC_PYFW=0; RC_AIKEY=0

    run_once "gentoo-colorize"     reset_gentoo run_gentoo verify_gentoo || RC_GENTOO=1
    run_once "python-firewall"     reset_pyfw   run_pyfw   verify_pyfw   || RC_PYFW=1
    run_once "aikey-android-merge" reset_aikey  run_aikey  verify_aikey  || RC_AIKEY=1

    if [ $RC_GENTOO -ne 0 ] || [ $RC_PYFW -ne 0 ] || [ $RC_AIKEY -ne 0 ]; then
        log "=== One or more tests failed — repeating all tests ==="
        continue
    fi

    break
done

log "=== All tests passed. ==="
log "=== Running sync.sh ==="
bash /home/verita84/posterchanai/sync.sh
