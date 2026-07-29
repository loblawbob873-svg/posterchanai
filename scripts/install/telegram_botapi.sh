#!/bin/bash
# Installer step: optionally set up a local Telegram Bot API server so the bot
# can handle files up to ~2 GB (the cloud Bot API caps downloads at 20 MB).
# Delegates the build/install to scripts/setup-telegram-local-api.sh.

setup_telegram_botapi() {
    # Runs only if option 5 was chosen during the install-type selection.
    [ "${INSTALL_TELEGRAM_BOTAPI:-0}" = "1" ] || return 0

    print_step "Setting up local Telegram Bot API server (option 5)"
    echo ""
    echo "  Credentials come from the web UI: enter your API ID / API Hash in"
    echo "  Admin -> Telegram (and Save) if you haven't already."
    echo "  This step reads them from the database — it won't ask you here."
    echo ""

    # The script reads api_id/api_hash/bot_token from the database, builds &
    # starts the server, and writes the settings so the web UI just works.
    if bash "$SCRIPT_DIR/scripts/setup-telegram-local-api.sh"; then
        print_success "Local Telegram Bot API server is set up."
    else
        print_warning "Local Bot API setup didn't complete. Most likely the API ID/Hash"
        print_warning "aren't saved yet — set them in Admin -> Telegram, then run:"
        echo "    ./scripts/setup-telegram-local-api.sh"
    fi
}
