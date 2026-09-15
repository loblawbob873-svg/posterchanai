# Built-in Telegram client: requested work

Status: planned, after the current launcher, Remote Desktop/audio, Texts attachment,
and release-stability backlog. This is not an implemented or advertised feature.

## Required behavior

- Use a personal Telegram account inside PosterChan on web, desktop and Android.
- Log into Telegram once; signing into PosterChan with the same Nostr identity on
  another device should restore access without a separate Telegram login.
- Support messages, attachments, incoming/outgoing voice calls and video calls.
- Deliver message and incoming-call notifications when the Telegram view is closed
  or the app is in the background. Notification taps open the correct conversation
  or call; switching accounts and reading messages clear stale alerts.
- Store PosterChan settings, session recovery records and bounded caches as encrypted
  Nostr events, with encrypted blobs referenced where event-size limits require it.
- Keep personal Telegram content separate from the existing Telegram bot and AI
  services. Never store a Telegram two-factor password to automate later logins.

## Session architecture to validate

Copying one MTProto authorization key to concurrently connected devices is not a
safe implementation. Telegram documents that conflicting connections can invalidate
that key; its temporary-session exception is bounded, not general device sync.
[Telegram authorization errors](https://core.telegram.org/api/errors#406-not-acceptable)

The proposed fit for login-once access is one persistent, per-user Telegram gateway
session with multiple Nostr-authenticated PosterChan interfaces. Only one worker may
own a session across the server fleet; recovery must fence the old worker. A gateway
necessarily has access to its Telegram session and message plaintext while running.
Encrypted Nostr storage does not make that gateway blind, and Telegram cloud chats
remain on Telegram's servers. Secret Chats have separate device-bound semantics.
[TDLib](https://core.telegram.org/tdlib),
[Secret Chats](https://core.telegram.org/api/end-to-end)

A client-only alternative needs distinct authorizations for new devices. An already
signed-in device can approve a login token, but token expiry, unavailable old devices
and two-factor authentication prevent a guarantee of unattended login everywhere.
[QR login](https://core.telegram.org/api/qr-login),
[Authorization](https://core.telegram.org/api/auth)

The engine and call-media integration are still to be selected and validated; a chat
API alone does not meet the calling requirement. Project-owned Telegram `api_id` and
`api_hash` are prerequisites for real integration testing. Do not publish sample keys
or claim compatibility based only on mocked responses.
[Application registration](https://core.telegram.org/api/obtaining_api_id),
[Call protocol](https://core.telegram.org/tdlib/docs/classtd_1_1td__api_1_1call_protocol.html),
[API terms](https://core.telegram.org/api/terms)

## Release evidence required

- Two PosterChan devices use one authorized Telegram session without invalidating it.
- No account-switch callback, notification or cache read exposes another account's data.
- Missing or partial encrypted records never overwrite a recoverable session or start
  a competing worker; restart, revocation and expiry have visible recovery paths.
- Sending distinguishes accepted, failed and uncertain outcomes without automatic
  duplicate sends; deleted and expired messages leave the cache.
- Real message, attachment, voice/video call and background-notification checks on
  desktop and Android, plus browser coverage. Record platform permission limits.
