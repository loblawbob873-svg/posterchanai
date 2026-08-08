"""The bundled CalDAV server: Radicale, mounted inside this app.

Nothing to install and nothing to run: `radicale` is a dependency, and its WSGI application is
mounted at /caldav by app/main.py when `caldav_enabled` is on. That means one port, one process, one
TLS certificate and one systemd unit — the calendar rides the app's, exactly as the built-in relay
and Blossom do.

The two plugins are what make it ours: `storage` keeps every calendar as encrypted Nostr events (a
working directory is only a cache), and `auth` logs in this node's accounts with a CalDAV-only app
password. See docs/CALENDAR.md.
"""
