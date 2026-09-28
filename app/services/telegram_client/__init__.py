"""Telegram, as a client inside PosterChan — sign in ONCE, use it from every window and device.

THE SESSION LIVES ON THIS NODE (the operator's decision, 2026-09-28). One real Telegram login per
account, held here, so every web/PosterChanOS/tablet window is a view onto the same conversation,
notifications arrive even when no window is open, and nothing has to be re-authorised per device.
The price is stated plainly, exactly as for Calendar/CardDAV: this node CAN read that user's Telegram.
The session string is stored as a kind-30078 document NIP-44-encrypted to the user's server-held
storage key (`pcai:tgsession`) — the relay operator sees ciphertext and no other user can read it —
and it never leaves this process in the clear.

NOT ON PHONES: a second live Telegram client on the phone that already runs Telegram only duplicates
notifications and competes for the same account. The client view hides itself on phone-sized
screens (smallest dimension < 600 CSS px, Android's sw600dp tablet line); tablets get it.

Voice/video CALLS are a later phase: Telegram's call protocol (tgcalls) is not plain WebRTC.
"""
