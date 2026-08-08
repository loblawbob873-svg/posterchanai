"""Nostr-native mailbox — mirrors IMAP mail into per-user encrypted kind-30078 events.

Each message is ONE app-data document, d-tag `pcai:mail:<acct>:<folder>:<uid>`, NIP-44-encrypted to
the user's server-held storage key (the same at-rest encryption the AI chat uses). So the relay IS
the mailbox: encrypted at rest, offline, and synced across the user's devices.

Account credentials stay server-side (UserSetting; see mail_service.get_user_mail_accounts) because
the server is what speaks IMAP/SMTP — a browser can't open raw mail sockets. Attachments are AES-GCM
encrypted and uploaded to Blossom; the message doc holds only {name,type,size,url,key,iv}.

This module is the STORE (read/write the encrypted docs); mail_sync drives IMAP→store; the
/client/mail API serves the GUI from here.
"""
import re
import logging

from app.services import nostr_store, settings_store

logger = logging.getLogger(__name__)

NS_MAIL = "pcai:mail:"

# A `d` PREFIX is not something a Nostr filter can match, so every read here pulls the author's
# kind-30078 documents and filters in Python — and that keyspace is SHARED with chat messages,
# calendars, contacts and uploads under the same key. The default window (5000) is far too small for
# a real mailbox sitting alongside them, and truncation is not a slow read: `have_uids` would come
# back short, the sync would read that as "never seen this message", and it would re-download and
# re-write the whole mailbox. That write-storm is what took this feature down in June 2026.
# The relay clamps any filter limit to 5000 (nostr_relay/store.py), so this is a ceiling, not a
# promise: a read that would exceed it comes back TRUNCATED and newest-first, with nothing to say
# so. Every read here is prefix-filtered to one folder, which keeps it far below — the unified view
# is the one that used to blow through it, and it now asks per account and folder for that reason.
_SCAN_LIMIT = 5000

# THIS FEATURE IS LOCAL TO THIS NODE, DELIBERATELY. `_broadcastable` in nostr_relay/server.py returns
# False for every kind-30078 carrying a `pcai:` d-tag, so mail is not copied to the public upstream
# relays — and it must stay that way. Adding `pcai:mail:` to the broadcast set is exactly what pegged
# production: the ~22 public upstreams reject encrypted mail, the outbox queue pinned at 500, and the
# CPU sat at 100%. Portability, if it is ever wanted, targets the USER's own relays (NIP-65) — never
# the instance's upstream set. See docs/MAIL.md.


def _port() -> int:
    return settings_store.get_int("nostr_relay_port", 3052)


def _tok(s: str) -> str:
    """d-tag-safe token: ':' is our path separator, so squeeze anything non-portable to '_'.
    Email local/domain and IMAP folder names never legitimately contain ':' — defensive only."""
    return re.sub(r"[^A-Za-z0-9._@-]", "_", (s or "").strip()) or "_"


def _d(account_email: str, folder: str, uid: str) -> str:
    return f"{NS_MAIL}{_tok(account_email)}:{_tok(folder)}:{_tok(str(uid))}"


def _prefix(account_email: str | None = None, folder: str | None = None) -> str:
    p = NS_MAIL
    if account_email:
        p += _tok(account_email) + ":"
        if folder:
            p += _tok(folder) + ":"
    return p


async def store_message(seckey: bytes, account_email: str, folder: str, msg: dict) -> bool:
    """Create/replace the encrypted doc for one message. `msg` is the full message dict (see
    mail_sync._to_doc): uid, message_id, from, to, cc, subject, date, ts, body_text, body_html,
    flags{read,...}, attachments[]. Idempotent on (account, folder, uid)."""
    if not msg or not msg.get("uid"):
        return False
    msg = {**msg, "account": account_email, "folder": folder}
    return await nostr_store.put_doc(_port(), seckey, _d(account_email, folder, msg["uid"]), msg, encrypt=True)


async def get_message(seckey: bytes, account_email: str, folder: str, uid: str) -> dict | None:
    return await nostr_store.get_doc(_port(), _d(account_email, folder, uid), seckey=seckey, encrypt=True)


# How many messages a folder view loads. Every one of them is a separate NIP-44 decrypt, and the
# list only shows a subject, a sender and a date — so opening an Archive of a thousand meant a
# thousand decrypts to draw one screen. The relay returns newest-first (ORDER BY created_at DESC),
# so a cap is "the most recent N", which is what a mail client shows anyway. Search passes 0 for the
# whole mailbox, because a search that only looks at the newest page is not a search.
_PAGE = 200


async def list_messages(seckey: bytes, account_email: str | None = None, folder: str | None = None,
                        limit: int | None = None, until: int | None = None) -> list:
    """Stored messages under (account[, folder]) — newest first, capped at `limit` (0/None = all).

    account=None → the whole mailbox (used by unified search).
    """
    msgs, _ = await list_page(seckey, account_email, folder, limit=limit, until=until)
    return msgs


async def list_page(seckey: bytes, account_email: str | None = None, folder: str | None = None,
                    limit: int | None = None, until: int | None = None) -> tuple:
    """One page of messages plus the cursor for the NEXT one: (messages, next_until).

    Paging is by the relay's `until` (event created_at, newest first), not by the message date — a
    mailbox mirrored in one sync holds thousands of messages whose dates span years but whose STORED
    order is the order they arrived. The cursor is opaque to the caller and only ever handed back;
    each page is sorted by date for display, and the caller accumulates.

    TWO THINGS THIS GETS RIGHT, both of which silently lost mail when it did not:

    * The cursor is `min(created_at)`, INCLUSIVE, not `min - 1`. Documents written by one sync share
      a second — at ~9 writes/second, many do — and the relay's LIMIT cuts mid-second, so stepping
      the cursor past that second drops every sibling that did not fit on the page. The overlap this
      creates is the price, and the caller de-duplicates by uid (the client already does).
    * "Is there more?" is decided by how many EVENTS the relay returned, not by how many decoded.
      One undecryptable document made the page look short, which read as end-of-list and hid every
      older message in the folder behind it.
    """
    want = _SCAN_LIMIT if limit == 0 else int(limit or _PAGE)
    docs = await nostr_store.list_docs(_port(), _prefix(account_email, folder), seckey=seckey,
                                       encrypt=True, limit=want, until=until, with_meta=True)
    raw = len(docs)
    pairs = [(v, ts) for (v, ts) in docs.values() if isinstance(v, dict)]
    msgs = [v for v, _ in pairs]
    msgs.sort(key=lambda m: m.get("ts", 0), reverse=True)
    nxt = None
    if raw >= want and docs:
        oldest = min(ts for (_v, ts) in docs.values())
        # Only advance if the page actually spans more than one second; otherwise an entire page of
        # ties would hand back the same cursor forever. In that case step past it and accept that a
        # second holding more than `want` documents cannot be paged through — vanishingly rare, and
        # far better than an infinite "Load older".
        newest = max(ts for (_v, ts) in docs.values())
        nxt = oldest if oldest != newest else oldest - 1
    return msgs, nxt


async def set_flags(seckey: bytes, account_email: str, folder: str, uid: str, **flags) -> bool:
    """Merge flags (e.g. read=True) into the stored doc. No-op if the message isn't stored."""
    msg = await get_message(seckey, account_email, folder, uid)
    if not msg:
        return False
    f = dict(msg.get("flags") or {})
    f.update(flags)
    msg["flags"] = f
    return await store_message(seckey, account_email, folder, msg)


async def delete_message(seckey: bytes, account_email: str, folder: str, uid: str) -> bool:
    """Remove the message doc from the mailbox (NIP-09 kind-5). The IMAP-side delete is mail_service's
    job; this drops it from the Nostr mailbox so the GUI reflects it."""
    return await nostr_store.delete_doc(_port(), seckey, _d(account_email, folder, uid))


async def have_uids(seckey: bytes, account_email: str, folder: str) -> set:
    """UIDs already mirrored for (account, folder) — so sync only stores genuinely new mail. The UID is
    the last d-tag segment, so this reads d-tags WITHOUT decrypting any content (sync ran a NIP-44
    decrypt of the whole folder here on every pass, pegging the event loop — see the CPU-spike fix)."""
    prefix = _prefix(account_email, folder)
    # PAGED, because the relay clamps any limit to 5000 and this set is the sync's dedup. A folder
    # with more than 5000 mirrored messages returned only the newest 5000, so every pass saw the
    # remainder as "never seen", re-fetched them over IMAP and re-wrote them — forever. That is the
    # write-storm the module header describes, arriving by a different road, and nothing logs when a
    # read hits the cap. Walking the cursor to exhaustion is the only honest way to answer
    # "what do I already have?".
    out, until, guard = set(), None, 0
    while guard < 200:                       # 200 pages x 5000 = a million documents; a real bound
        guard += 1
        dtags, nxt, seen = await nostr_store.list_dtags(_port(), prefix, seckey=seckey,
                                                        limit=_SCAN_LIMIT, until=until,
                                                        with_meta=True)
        out |= {d[len(prefix):] for d in dtags if len(d) > len(prefix)}
        if not nxt or not seen:
            break
        until = nxt if until != nxt else nxt - 1     # never hand back the same cursor
    else:
        logger.warning("[mail] have_uids for %s/%s stopped at the page guard — dedup may be partial",
                       account_email, folder)
    return out


def search(messages: list, query: str) -> list:
    """Substring search (case-insensitive) over subject/from/to/body of already-loaded messages.
    Mailbox search is local because the mail already lives in the relay — no IMAP round-trip."""
    q = (query or "").strip().lower()
    if not q:
        return messages
    out = []
    for m in messages:
        # include preview (always inline, even when the large body was offloaded to Blossom)
        hay = " ".join(str(m.get(k, "")) for k in ("subject", "from", "from_email", "to", "cc", "body_text", "preview", "folder")).lower()
        if q in hay:
            out.append(m)
    return out
