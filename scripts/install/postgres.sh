#!/usr/bin/env bash
# PostgreSQL provisioning — Postgres is the ONE database (app + built-in Nostr relay). Creates the
# `posterchan` role and `posterchan_relay` database (idempotent). The app/relay connect via localhost
# `trust` by default (no password); set DATABASE_URL / NOSTR_RELAY_PG_DSN in data/secrets.env for
# password auth or a remote server. Bare-metal only — Docker uses the `postgres` compose service.

setup_postgres() {
    print_step "Setting up PostgreSQL (the app + Nostr relay database)"

    if ! command -v psql >/dev/null 2>&1; then
        print_warning "PostgreSQL client not found. Install + start PostgreSQL, then re-run this installer."
        echo "    Debian/Ubuntu: sudo apt install postgresql && sudo systemctl enable --now postgresql"
        echo "    Fedora:        sudo dnf install postgresql-server && sudo postgresql-setup --initdb && sudo systemctl enable --now postgresql"
        echo "    Arch:          sudo pacman -S postgresql (then initdb + start)"
        echo "    Gentoo:        emerge dev-db/postgresql (then emerge --config + start)"
        return 0
    fi

    # Find a superuser psql invocation that works on this box.
    local PSQL=""
    if psql -U postgres -d postgres -c '\q' >/dev/null 2>&1; then
        PSQL="psql -U postgres -d postgres"
    elif sudo -n -u postgres psql -d postgres -c '\q' >/dev/null 2>&1; then
        PSQL="sudo -u postgres psql -d postgres"
    elif sudo -u postgres psql -d postgres -c '\q' >/dev/null 2>&1; then
        PSQL="sudo -u postgres psql -d postgres"
    else
        print_warning "Couldn't connect to PostgreSQL as a superuser. Create them manually:"
        echo "    sudo -u postgres psql -c \"CREATE ROLE posterchan LOGIN;\""
        echo "    sudo -u postgres psql -c \"CREATE DATABASE posterchan_relay OWNER posterchan;\""
        return 0
    fi

    # glibc/ICU collation bump (e.g. after an OS upgrade) blocks CREATE DATABASE from the template —
    # refresh the recorded version. Harmless no-op when already current.
    $PSQL -c "ALTER DATABASE template1 REFRESH COLLATION VERSION;" >/dev/null 2>&1 || true
    $PSQL -c "ALTER DATABASE postgres  REFRESH COLLATION VERSION;" >/dev/null 2>&1 || true

    # Role + database (idempotent).
    if ! $PSQL -tAc "SELECT 1 FROM pg_roles WHERE rolname='posterchan'" | grep -q 1; then
        $PSQL -c "CREATE ROLE posterchan LOGIN;" >/dev/null 2>&1 \
            && echo "    created role 'posterchan'" || print_warning "could not create role 'posterchan'"
    fi
    if ! $PSQL -tAc "SELECT 1 FROM pg_database WHERE datname='posterchan_relay'" | grep -q 1; then
        $PSQL -c "CREATE DATABASE posterchan_relay OWNER posterchan;" >/dev/null 2>&1 \
            && echo "    created database 'posterchan_relay'" || print_warning "could not create database 'posterchan_relay'"
    fi

    if psql -h 127.0.0.1 -U posterchan -d posterchan_relay -c '\q' >/dev/null 2>&1; then
        print_success "PostgreSQL ready (db 'posterchan_relay', role 'posterchan', passwordless localhost trust)."
    else
        print_warning "PostgreSQL set up, but 'posterchan' can't connect passwordless — your pg_hba.conf may require a password."
        echo "    If so, set a password and put the DSN in data/secrets.env, e.g.:"
        echo "      export DATABASE_URL=postgresql+psycopg2://posterchan:PASS@127.0.0.1:5432/posterchan_relay"
        echo "      export NOSTR_RELAY_PG_DSN='host=127.0.0.1 dbname=posterchan_relay user=posterchan password=PASS'"
    fi
}
