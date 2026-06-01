#!/bin/bash
# Installer step: optionally set up a local Telegram Bot API server so the bot
# can handle files up to ~2 GB (the cloud Bot API caps downloads at 20 MB).
# Delegates the build/install to scripts/setup-telegram-local-api.sh.

setup_telegram_botapi() {
    print_step "Optional: local Telegram Bot API server (large-file support)"
    echo ""
    echo "  Telegram's cloud Bot API only lets bots download files up to 20 MB, so"
    echo "  'compress'/'convert'/'translate' on Telegram only work on small files."
    echo "  A local Bot API server raises that to ~2 GB. (The web UI and Matrix"
    echo "  already handle large files without this.) Compiles telegram-bot-api"
    echo "  (~10-20 min). This is independent of the install type you chose."
    echo ""

    read -p "Set up the local Telegram Bot API server now? [y/N]: " WANT_BOTAPI
    if [[ ! "$WANT_BOTAPI" =~ ^[Yy] ]]; then
        echo "  Skipping — you can run scripts/setup-telegram-local-api.sh anytime later."
        return 0
    fi

    echo ""
    echo "  Get these from https://my.telegram.org (API development tools):"
    read -p "  API ID: " BOTAPI_ID
    read -p "  API Hash: " BOTAPI_HASH

    if [ -z "$BOTAPI_ID" ] || [ -z "$BOTAPI_HASH" ]; then
        print_warning "API ID/Hash not provided — skipping local Bot API setup."
        return 0
    fi

    # Persist the credentials so the web UI and the setup script can use them.
    if [ -f "posterchanai.db" ]; then
        sqlite3 posterchanai.db "INSERT OR REPLACE INTO settings (key, value) VALUES ('telegram_api_id', '$BOTAPI_ID');" 2>/dev/null
        sqlite3 posterchanai.db "INSERT OR REPLACE INTO settings (key, value) VALUES ('telegram_api_hash', '$BOTAPI_HASH');" 2>/dev/null
    fi

    # Build, install, start, and configure (the script reads creds from the DB
    # and writes telegram_local_api/telegram_api_base so the web UI just works).
    API_ID="$BOTAPI_ID" API_HASH="$BOTAPI_HASH" bash "$SCRIPT_DIR/scripts/setup-telegram-local-api.sh" \
        && print_success "Local Telegram Bot API server is set up." \
        || print_warning "Local Bot API setup failed — see the output above. The bot still works on the cloud API (20 MB limit)."
}
