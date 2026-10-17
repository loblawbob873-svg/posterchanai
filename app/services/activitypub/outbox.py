"""Members' own Nostr events → the fediverse. Runs in the WORKER process (app/worker.py).

Nothing is copied: every tick reads what members published to this node's relay since the saved
cursor and translates it on the way out (convert.py). A post's ActivityPub object is served from
the relay by the same id (`/ap/objects/<event id>`), so there is exactly one copy of everything.

    kind 1 / 1111  → Create(Note)   top-level posts, and replies whose parent is on the fediverse
                                    or is a member's post (a reply to a Nostr-only thread would
                                    arrive on the fediverse with no context, so it stays on Nostr)
    kind 6         → Announce       of a fediverse note or a member's post
    kind 7         → Like           ditto; sent to the author, who is the one it concerns
    kind 1068      → Create(Question)  a NIP-88 poll; its tallies go out as Update(Question)
    kind 1018      → a vote (Note with `name`) on a fediverse poll, to the poll's author
    kind 5         → Delete / Undo  for what was sent -- only the deleter's own
    kind 0         → Update(Person)
    kind 3         → Follow / Undo(Follow) for fediverse accounts (puppets) added to or removed from
                                    the contact list -- which is what brings their posts in
    kind 1984      → Flag           a NIP-56 report of a fediverse account (and its posts), to that
                                    account's own server, whose moderators are the ones who can act

Anything carrying a `proxy` tag is a mirror of somebody else's words (a note that arrived from the
fediverse, or another bridge's copy), never a member's own, so it is never sent back out.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time

from app.services import nostr_store, settings_store
from app.services.activitypub import actors, config, convert, remote, state

logger = logging.getLogger(__name__)

KINDS = [0, 1, 3, 5, 6, 7, 1018, 1068, 1111, 1984]
POST_KINDS = (1, 1111, 1068)     # what becomes an ActivityPub object (Note / Question)
_TICK_SECONDS = 30
_PAGE = 500
_RETRY_DELAYS = (60, 300, 1800)
_seen: dict = {}                 # event id → time processed (so the inclusive cursor second is not replayed)
_retries: list = []              # [(due, inbox, activity, member, attempt, key)] -- also on the relay
_stats = {"delivered": 0, "failed": 0, "last_error": "", "last_tick": 0, "queued": 0, "deleting": 0}
_scheduler = None
_SENT = "pcai:ap:sent:"          # per-Like record of who it was sent to, so an Undo reaches them


def stats() -> dict:
    return dict(_stats, queued=len(_retries))


# ------------------------------------------------------------------------------------ resolution

async def _puppet_row(pubkey: str):
    """The registry row of a puppet key. Raises relay_reader.Unavailable ("could not ask" is never "not
    a fediverse account" -- read as that, a mention or reply would silently not be delivered)."""
    from app.services import fedi_tables
    return await fedi_tables.apuppet_by_pubkey(pubkey) if pubkey else None


async def _mirror_row(event_id: str):
    from app.services import fedi_tables
    return await fedi_tables.adelivered_by_event(event_id) if event_id else None


async def _event(event_id: str, *, strict: bool = False) -> dict | None:
    from app.services.fedi_bridge_identity import query_one
    ok, ev = await query_one(settings_store._port(), {"ids": [event_id], "limit": 1})
    if not ok and strict:
        raise actors.RelayUnavailable("the relay did not answer an event read")
    return ev if ok else None


async def resolve_pubkey(pubkey: str) -> dict:
    """{"href", "name", "inbox"?} for a pubkey that has a fediverse address, else {}."""
    name = actors.handle(pubkey)
    # STRICT: a relay that did not answer RAISES here, and the delivery pass stops and retries, instead of
    # sending the post with this mention downgraded to a bare link.
    if name and (actors.is_actor(pubkey) or await actors.exposed(pubkey, strict=True)):
        href = await actors.actor_id(pubkey)
        shown = await actors.readable_handle(pubkey) or name
        if href:
            # `url` is the PROFILE address our actor document advertises (convert.person). The link in
            # the text must be exactly that: Akkoma/Pleroma recognise a mention link by comparing it to
            # the account's `url`, and our actor id (…/ap/users/…) is not it -- so every mention of one
            # of ours opened poster.place in a new tab instead of the profile inside Akkoma.
            return {"href": href, "url": f"{config.base_url()}/@{await actors.ap_handle(pubkey)}",
                    "name": f"@{shown}@{config.domain()}"}
    row = await _puppet_row(pubkey)
    # A blocked ACCOUNT (a `user@host` line, or its puppet npub on the relay blocklist) is as out of
    # reach as a blocked instance: a member's mention or reply must not deliver to it either.
    if row and row.actor_uri and not config.host_blocked(remote.host_of(row.actor_uri)) \
            and not (row.acct and config.account_blocked(row.acct)) \
            and not actors.puppet_blocked(row.actor_uri, row.acct or ""):
        canonical, _inbox = await _canonical(row.actor_uri)
        href = canonical or row.actor_uri
        return {"href": href, "url": await _profile_url(row.actor_uri) or href,
                "name": f"@{row.acct}" if row.acct else href, "remote": True}
    return {}


async def resolve_event(event_id: str) -> dict:
    """{"uri", "actor", "remote"} for an event that exists on the fediverse, else {}: a mirrored note
    (its original URI and author) or a member's own post (our object URL)."""
    base = config.base_url()
    row = await _mirror_row(event_id)
    if row and row.note_uri:
        prow = await _puppet_row(row.nostr_pubkey or "")
        actor_id = (await _canonical(prow.actor_uri))[0] or prow.actor_uri if prow else ""
        return {"uri": row.note_uri, "actor": actor_id, "remote": True}
    ev = await _event(event_id, strict=True)      # unanswered: raise, never "a Nostr-only thread"
    # Only what IS served as an object: a member's reaction, a protected post or an article has no
    # /ap/objects/ address (it answers 404), and a reply naming one arrived pointing at nothing.
    if ev and ev.get("kind") in POST_KINDS and not _is_mirror(ev) and not _protected(ev) \
            and await actors.exposed(ev.get("pubkey", ""), strict=True):
        actor = await actors.actor_id(ev["pubkey"])
        if actor:
            return {"uri": convert.object_url(base, event_id), "actor": actor, "remote": False}
    return {}


def _blocked_cached(actor_id: str) -> bool:
    """A follower blocked AFTER it followed (instance or single account) gets nothing more. Uses the
    actor cache only -- delivery must not fetch every follower on every post."""
    if config.host_blocked(remote.host_of(actor_id)):
        return True
    hit = remote._actors.get((actor_id or "").split("#")[0])
    if not hit:
        return actors.puppet_blocked(actor_id)
    from app.services.activitypub.inbox import acct_of_actor
    acct = acct_of_actor(hit[1])
    return config.account_blocked(acct) or actors.puppet_blocked(actor_id, acct)


_gone_cache = {"at": 0.0, "set": frozenset()}


async def _gone() -> frozenset:
    """Deleted fediverse accounts (state.mark_gone), read at most every five minutes."""
    now = time.monotonic()
    if now - _gone_cache["at"] > 300:
        try:
            _gone_cache.update(at=now, set=frozenset(await state.gone_actors()))
        except Exception:
            _gone_cache["at"] = now - 240           # try again in a minute; keep the last good set
    return _gone_cache["set"]


def _is_mirror(ev: dict) -> bool:
    return any(t and t[0] in ("proxy", "fedibridge") for t in ev.get("tags") or [])


def parent_of(ev: dict) -> str:
    """The event this one replies to: NIP-22's lowercase `e` for a comment, NIP-10's `reply` marker
    (else the last UNMARKED `e`) for a kind-1. A tag marked `mention` is a quote, not a parent: read
    as one, a top-level post quoting a Nostr-only note looked like a reply in a Nostr-only thread
    and was never federated at all."""
    es = [t for t in ev.get("tags") or [] if len(t) > 1 and t[0] == "e" and t[1]]
    if not es:
        return ""
    if ev.get("kind") == 1111:
        return es[-1][1]
    for marker in ("reply", "root"):
        hit = next((t for t in es if len(t) > 3 and t[3] == marker), None)
        if hit:
            return hit[1]
    # Only a real NIP-10 MARKER in the 4th place says what a tag is. Clients also put the PARENT'S
    # PUBKEY there (["e", id, relay, <pubkey>]) -- read as "marked" that made every such reply look
    # top-level, and it was federated as a new post with no thread (2026-09-24).
    plain = [t for t in es if len(t) < 4 or t[3] not in ("mention", "reply", "root")]
    return plain[-1][1] if plain else ""


def _referenced_pubkeys(ev: dict) -> list:
    from app.services.nostr import nostr_service
    out = [t[1] for t in ev.get("tags") or [] if len(t) > 1 and t[0] in ("p", "P") and t[1]]
    for m in convert._NOSTR_REF_RE.finditer(ev.get("content") or ""):
        try:
            pk = nostr_service.to_pubkey_hex(m.group(1))
        except Exception:
            pk = None
        if pk:
            out.append(pk)
    seen, uniq = set(), []
    for pk in out:
        if pk not in seen:
            seen.add(pk)
            uniq.append(pk)
    return uniq[:30]


async def _post_refs(ev: dict) -> tuple[dict, str]:
    """({bech32: fediverse address}, quoted address) for the posts an event references -- NIP-18's
    `q` tag and `nostr:note1…/nevent1…` in the text. A post with no fediverse address is left to
    the converter (it links this node's page for it)."""
    from app.services.nostr import bech32
    refs = {}
    for m in convert._NOSTR_REF_RE.finditer(ev.get("content") or ""):
        value = m.group(1)
        if value.lower().startswith(("note1", "nevent1")):
            raw = bech32.decode_any(value)
            if raw:
                refs[value] = raw.hex()
    q = next((t[1] for t in ev.get("tags") or [] if len(t) > 1 and t[0] == "q"
              and len(t[1]) == 64), "")
    links, by_id = {}, {}
    for eid in list(dict.fromkeys(list(refs.values()) + ([q] if q else [])))[:5]:
        target = await resolve_event(eid)
        if target:
            by_id[eid] = target["uri"]
    for value, eid in refs.items():
        if eid in by_id:
            links[value] = by_id[eid]
    quote = by_id.get(q) if q else next((by_id[e] for e in refs.values() if e in by_id), "")
    return links, quote or ""


async def _canonical(actor_uri: str) -> tuple[str, str]:
    """(canonical actor id, inbox) for an address from our own records -- see remote.actor(alias)."""
    try:
        doc = await remote.actor(actor_uri, alias=True)
    except remote.FetchError:
        return "", ""
    return str(doc.get("id") or "").split("#")[0], remote.inbox_of(doc)


async def _profile_url(actor_uri: str) -> str:
    """The profile page a fediverse account's own actor document names (`url`) -- what its server's
    front end, and every other, matches a mention link against. "" when it names none (the caller then
    links the actor id, which is what Pleroma and Akkoma use as their `url` anyway)."""
    try:
        doc = await remote.actor(actor_uri, alias=True)
    except remote.FetchError:
        return ""
    u = doc.get("url")
    if isinstance(u, list):
        u = next((x for x in u if isinstance(x, str) or isinstance(x, dict)), "")
    if isinstance(u, dict):
        u = u.get("href") or ""
    u = u if isinstance(u, str) else ""
    return u if u.startswith("https://") and remote.host_of(u) == remote.host_of(actor_uri) else ""


async def _inbox_for(actor_uri: str) -> str:
    return (await _canonical(actor_uri))[1]


# ------------------------------------------------------------------------------------ translation

async def build_object(ev: dict, me: str) -> tuple[dict | None, dict]:
    """(the ActivityPub object for a member's post, {"target", "mentions"}) -- a Note, or a Question
    for a poll -- or (None, {}) when the post does not go to the fediverse. ONE builder for delivery,
    the object URL, the outbox and `featured`, so the four can never disagree about a post."""
    base = config.base_url()
    kind = ev.get("kind")
    if kind not in POST_KINDS or _is_mirror(ev) or _protected(ev):
        return None, {}
    parent = parent_of(ev)
    if kind == 1111 and not parent:
        # A comment on an article (`a`) or a web page (`i`): nothing on the fediverse it could
        # reply to, and sent anyway it arrived as a standalone post with no context.
        return None, {}
    target = {}
    if parent:
        target = await resolve_event(parent)
        if not target:
            return None, {}                   # a reply in a Nostr-only thread stays on Nostr
    mentions = {}
    for pk in _referenced_pubkeys(ev):
        who = await resolve_pubkey(pk)
        if who:
            mentions[pk] = who
    links, quote = await _post_refs(ev)
    kw = dict(base=base, actor=me, followers=f"{me}/followers", mentions=mentions,
              in_reply_to=target.get("uri", ""), reply_to_actor=target.get("actor", ""), links=links, quote=quote)
    if kind == 1068:
        tally = await _tally_of(ev["id"])
        obj = convert.question_from_event(ev, counts=tally.get("counts") or {}, voters=int(tally.get("voters") or 0), **kw)
    else:
        obj = convert.note_from_event(ev, **kw)
    return obj, {"target": target, "mentions": mentions}


async def _tally_of(poll_id: str) -> dict:
    try:
        return await state.poll_tally(poll_id)
    except Exception:
        return {}


_fol_memo: dict | None = None    # member -> follower inboxes, held for ONE delivery pass (see _pass)


async def _follower_inboxes(member: str) -> list:
    if _fol_memo is not None and member in _fol_memo:
        return list(_fol_memo[member])
    gone = await _gone()
    out = [f["inbox"] for f in await state.followers(member)
           if not _blocked_cached(f.get("actor", "")) and f.get("actor") not in gone]
    if _fol_memo is not None:
        _fol_memo[member] = list(out)     # a FAILED read raised above and is never remembered
    return out


async def _copies_of(post_id: str) -> set:
    """Every server holding a copy of a MEMBER's post: its author's followers plus the inboxes it went
    to beyond them (the replied-to, the mentioned, accepted relays). A reaction to a member's post has
    no remote author to go to, and was sent to nobody -- so an emoji reaction made here never showed on
    Akkoma or anywhere else, because a server only learns of a reaction it is sent."""
    try:
        post = await _event(post_id, strict=True)
    except Exception:
        return set()
    if not post or not post.get("pubkey"):
        return set()
    return set(await _follower_inboxes(post["pubkey"])) | set((await _sent_rec(post_id)).get("inboxes") or [])


_FEDERATED_KIND_NAMES = frozenset(str(k) for k in KINDS)


def _inert_deletion(ev: dict) -> bool:
    """A kind-5 that names only kinds the fediverse never received -- above all the node's own 30078
    app documents, deleted under the operator key, which on a node whose operator has an account IS a
    member. Decided from the tags alone, so it costs no relay read: ~9,400 of these a day each paid a
    strict follower-list read (every follower document decrypted) before anything looked at the tags."""
    if ev.get("kind") != 5:
        return False
    tags = ev.get("tags") or []
    named = {t[1] for t in tags if len(t) > 1 and t[0] == "k"} | \
        {str(t[1]).split(":", 1)[0] for t in tags if len(t) > 1 and t[0] == "a"}
    return bool(named) and not named & _FEDERATED_KIND_NAMES


async def plan(ev: dict, member: str) -> list:
    """[(inbox, activity)] to send for one event. Empty when there is nothing to federate."""
    base = config.base_url()
    if not base or not actors.handle(member) or _is_mirror(ev) or _protected(ev) or _inert_deletion(ev):
        return []
    if not actors.is_actor(member) and ev.get("kind") == 3:
        return []          # a non-local user's follows would bring posts only to THIS relay, which they do not read
    me = await actors.actor_id(member)
    if not me:
        return []
    followers_url = f"{me}/followers"
    kind = ev.get("kind")
    fol = await _follower_inboxes(member)
    out: list = []

    if kind in POST_KINDS:
        note, ctx = await build_object(ev, me)
        if note is None:
            return []
        target, mentions = ctx["target"], ctx["mentions"]
        reply_actor = target.get("actor", "")
        act = convert.create(note, me)
        targets = set(fol)
        extra = set()
        for who in list(mentions.values()) + ([{"href": reply_actor, "remote": target["remote"]}] if reply_actor else []):
            if who.get("remote") and who.get("href"):
                inbox = await _inbox_for(who["href"])
                if inbox:
                    extra.add(inbox)
        # ACCEPTED relays take public top-level posts in scope (relays.carries). Recorded with the
        # other extra inboxes, so the post's Delete reaches the relays too.
        from app.services.activitypub import relays
        if relays.carries(ev, member):
            extra |= set(await relays.accepted_inboxes())
        targets |= extra
        await _remember_sent(ev["id"], extra - set(fol), kind=kind, by=member)
        out = [(i, act) for i in sorted(targets)]

    elif kind == 1018:
        out = await _vote(ev, member, me)

    elif kind in (6, 7):
        target_id = next((t[1] for t in reversed(ev.get("tags") or []) if len(t) > 1 and t[0] == "e"), "")
        target = await resolve_event(target_id) if target_id else {}
        if not target:
            return []
        author_inbox = await _inbox_for(target["actor"]) if target.get("remote") and target.get("actor") else ""
        # Recorded EVEN WITH NO AUTHOR INBOX (a boost of a member's own post): what it was and what it
        # was about is how its deletion later goes out as the right verb, naming the right object.
        if kind == 6:
            act = convert.announce(ev, base=base, actor=me, followers=followers_url, target=target["uri"],
                                   target_actor=target.get("actor", ""))
            out = [(i, act) for i in sorted(set(fol) | ({author_inbox} if author_inbox else set()))]
            await _remember_sent(ev["id"], {author_inbox} - {""}, kind=6, by=member, target=target["uri"])
        elif (ev.get("content") or "").strip() == "-":
            return []                          # a NIP-25 dislike: the fediverse has no such thing
        else:
            act = convert.like(ev, base=base, actor=me, target=target["uri"])
            inboxes = {author_inbox} - {""}
            if not target.get("remote"):
                # A member's post: every server holding a copy, AND the reactor's own followers -- the
                # addressing Pleroma/Akkoma give a reaction themselves, and the only way a server that
                # met the post through an interaction (not a follow) hears of it: the author of a post
                # reacted to may have no fediverse followers at all.
                inboxes |= await _copies_of(target_id) | set(fol)
            out = [(i, act) for i in sorted(inboxes)]
            await _remember_sent(ev["id"], inboxes, kind=7, by=member, target=target["uri"])

    elif kind == 5:
        kinds = {t[1] for t in ev.get("tags") or [] if len(t) > 1 and t[0] == "k"}
        # (A deletion naming only kinds the fediverse never received returned [] at the top --
        # `_inert_deletion` -- before the follower read.)
        for t in ev.get("tags") or []:
            if len(t) < 2 or t[0] != "e":
                continue
            gone = t[1]
            # What it was is taken from our own record of sending it first: many clients send a
            # kind-5 with no `k` tag, and read from the tags alone deleting a reaction sent a Delete
            # of a Note (and never the Undo the liked author needed).
            rec = await _sent_rec(gone)
            # ONLY THE DELETER'S OWN. The relay refuses a kind-5 for somebody else's event, but this
            # would still have sent a Delete of that event SIGNED BY THE DELETER -- and Pleroma and
            # Akkoma accept a Delete from the same domain as the object, i.e. from any member here.
            if rec.get("by") and rec["by"] != member:
                continue
            if not rec.get("by"):
                still = await _event(gone)
                if still and still.get("pubkey") != member:
                    continue
            was = str(rec.get("kind") or "")
            if was == "1984" or (not was and "1984" in kinds):
                continue                       # a Flag cannot be withdrawn, and is not a Note to Delete
            if not was:
                was = "7" if ("7" in kinds and "1" not in kinds) else "6" if ("6" in kinds and "1" not in kinds) else "1"
            extra = set(rec.get("inboxes") or [])
            if was == "7":
                act = convert.undo(convert.activity_url(base, gone), "Like", base=base, actor=me,
                                   target=rec.get("target") or "", deletion_id=ev["id"])
                out += [(i, act) for i in sorted(extra)]
            elif was == "6":
                act = convert.undo(convert.activity_url(base, gone), "Announce", base=base, actor=me,
                                   target=rec.get("target") or "", deletion_id=ev["id"])
                out += [(i, act) for i in sorted(set(fol) | extra)]
            elif was == "1018":
                continue                       # a poll vote cannot be withdrawn on the fediverse
            else:
                act = convert.delete_note(gone, base=base, actor=me, followers=followers_url, deletion_id=ev["id"])
                out += [(i, act) for i in sorted(set(fol) | extra)]

    elif kind == 0:
        # THE PROFILE THAT GOES OUT IS READ NOW, NOT FROM THE 5-MINUTE CACHE. Anything that read the
        # profile shortly before the edit -- a remote server fetching the actor does exactly that --
        # left the OLD one cached, and the Update built from it carried the old name and picture to
        # every follower ("make sure that profile updates federate"). And an older kind 0 that the
        # trailing window re-reads is superseded: it sends nothing.
        actors._profile_cache.pop(member, None)
        current = await actors.profile_event(member, strict=True)
        if current and current.get("id") != ev.get("id") and int(current.get("created_at") or 0) >= int(ev.get("created_at") or 0):
            return []
        doc = await actors.person(member)
        act = {"@context": convert.AS_CONTEXT, "id": convert.activity_url(base, ev["id"], "update"),
               "type": "Update", "actor": me, "to": [config.PUBLIC], "cc": [followers_url], "object": doc}
        out = [(i, act) for i in sorted(set(fol))]

    elif kind == 3:
        out = await _follows(ev, member, me)

    elif kind == 1984:
        out = await _flag(ev, member, me)
    return out


REPORT_TYPES = ("nudity", "malware", "profanity", "illegal", "spam", "impersonation", "other")


async def _flag(ev: dict, member: str, me: str) -> list:
    """A member's NIP-56 report (kind 1984) about a FEDIVERSE account → a `Flag` to that account's own
    server: its moderators are the only ones who can act on it, and on Nostr alone they never hear of it.

    One Flag per reported account, the shape Mastodon and Akkoma both read: `object` is a LIST of URI
    STRINGS -- the account first, then its reported statuses (Akkoma reduces over ap_id strings and
    takes the first local user as the account; Mastodon maps every entry and keeps its own); `content`
    is the comment, led by the NIP-56 type in brackets (`[spam] …`) so their moderators see the
    category and our own inbox reads it back (inbox._flag). The id is under OUR host: Mastodon keeps a
    report's uri only when it is on the actor's. Sent ONCE: the sent ledger records it, and a report
    already recorded plans nothing. A report of a native Nostr user or of a member sends nothing --
    there is no other server to tell."""
    base = config.base_url()
    if (await _sent_rec(ev["id"])).get("kind") == 1984:
        return []
    tags = [t for t in ev.get("tags") or [] if isinstance(t, list) and len(t) > 1 and isinstance(t[1], str)]
    rtype = next((str(t[2]).lower() for t in tags if t[0] in ("e", "p") and len(t) > 2
                  and str(t[2]).lower() in REPORT_TYPES), "other")
    accounts: dict = {}                          # canonical actor id -> [status uri, ...]
    for t in tags:
        if t[0] == "p" and len(t[1]) == 64:
            who = await resolve_pubkey(t[1])
            if who.get("remote") and who.get("href"):
                accounts.setdefault(who["href"], [])
    for t in tags:
        if t[0] == "e" and len(t[1]) == 64:
            target = await resolve_event(t[1])
            if target.get("remote") and target.get("actor") and target.get("uri"):
                accounts.setdefault(target["actor"], []).append(target["uri"])
    if not accounts:
        return []
    comment = " ".join((ev.get("content") or "").split())[:4000]
    content = f"[{rtype}] {comment}".strip()
    out, inboxes = [], set()
    for account, statuses in list(accounts.items())[:10]:
        if config.host_blocked(remote.host_of(account)) or actors.puppet_blocked(account):
            continue
        inbox_url = await _inbox_for(account)
        if not inbox_url:
            continue
        flag = {"@context": convert.AS_CONTEXT,
                "id": convert.activity_url(base, ev["id"], "flag") + (f"#{_h(account)}" if len(accounts) > 1 else ""),
                "type": "Flag", "actor": me, "to": [account], "content": content,
                "object": [account] + list(dict.fromkeys(statuses))[:20]}
        out.append((inbox_url, flag))
        inboxes.add(inbox_url)
    if out:
        await _remember_sent(ev["id"], inboxes, kind=1984, by=member, target=out[0][1]["object"][0])
    return out


async def _follows(ev: dict, member: str, me: str) -> list:
    """Follow the fediverse accounts newly in the contact list; unfollow the ones taken out."""
    # By CANONICAL actor id: a puppet made by the Pleroma bridge is keyed on a profile URL, and the
    # Follow, the Accept that answers it and the posts that follow are all by the canonical id.
    wanted = {}
    for t in ev.get("tags") or []:
        if len(t) > 1 and t[0] == "p":
            row = await _puppet_row(t[1])
            if row and row.actor_uri and not config.host_blocked(remote.host_of(row.actor_uri)):
                wanted[row.actor_uri] = True
    current = await state.following(member)
    moved = await state.moves()
    resolved = {}
    # Concurrently: a contact list of a few hundred fediverse accounts is a few hundred actor fetches,
    # and one after another that outlived every time limit it ran under.
    sem = asyncio.Semaphore(12)

    async def one(alias):
        async with sem:
            canonical, inbox = await _canonical(alias)
            # An account that MOVED is followed at its new address: the contact list still names the
            # old one's puppet, and cannot be rewritten here (the member signs it).
            new = moved.get(canonical) if canonical else ""
            if new and new != canonical:
                canonical, inbox = await _canonical(new)
            return alias, canonical, inbox
    unresolved_hosts = set()
    for alias, canonical, inbox in await asyncio.gather(*(one(a) for a in sorted(wanted))):
        if canonical and inbox:
            resolved[canonical] = inbox
        else:
            unresolved_hosts.add(remote.host_of(alias))
    # KEEP WHAT CANNOT BE RE-CHECKED RIGHT NOW. By host, not by address: a puppet from the old bridge
    # is keyed on a profile URL (/@bob) while the follow is recorded under the canonical id
    # (/users/bob), so comparing addresses never matched and a brief outage on bob's server made an
    # unrelated contact-list edit unfollow him.
    unresolved_known = {a for a in current if remote.host_of(a) in unresolved_hosts}
    out = []
    for actor_uri in sorted(set(resolved) - set(current)):
        inbox = resolved[actor_uri]
        # A NEW id every time: Pleroma and Akkoma drop an activity whose id they already hold, so a
        # follow → unfollow → follow again reused the first Follow's id and the refollow was ignored
        # (for ever "pending"). The hash of the target stays first -- _accept maps an Accept back by it.
        fid = f"{me}#follows/{_h(actor_uri)}/{int(time.time() * 1000)}"
        follow = {"@context": convert.AS_CONTEXT, "id": fid, "type": "Follow", "actor": me, "object": actor_uri}
        await state.set_following(member, actor_uri, inbox, "pending", follow_id=fid)
        out.append((inbox, follow))
    for actor_uri in sorted(set(current) - set(resolved) - unresolved_known):
        inbox = current[actor_uri].get("inbox") or await _inbox_for(actor_uri)
        await state.drop_following(member, actor_uri)
        if inbox:
            fid = current[actor_uri].get("id") or f"{me}#follows/{_h(actor_uri)}"   # older records: the old id
            out.append((inbox, {"@context": convert.AS_CONTEXT, "id": f"{fid}/undo/{int(time.time() * 1000)}",
                                "type": "Undo", "actor": me,
                                "object": {"id": fid, "type": "Follow", "actor": me, "object": actor_uri}}))
    if out:
        state.forget_followed_cache()
    return out


async def _vote(ev: dict, member: str, me: str) -> list:
    """A member's NIP-88 vote (kind 1018) on a FEDIVERSE poll → one answer Note per chosen option, to
    the poll's author (the only server that counts it). A vote on a poll that lives on Nostr is
    tallied here and reaches the fediverse as the poll's Update(Question) instead."""
    poll_id = next((t[1] for t in ev.get("tags") or [] if len(t) > 1 and t[0] == "e" and len(t[1]) == 64), "")
    row = await _mirror_row(poll_id) if poll_id else None
    if not row or not row.note_uri:
        return []
    poll = await _event(poll_id)
    if not poll or poll.get("kind") != 1068:
        return []
    labels = dict(convert.poll_options(poll))
    chosen = [t[1] for t in ev.get("tags") or [] if len(t) > 1 and t[0] == "response" and t[1] in labels]
    chosen = list(dict.fromkeys(chosen))
    if not chosen:
        return []
    if not convert.poll_multi(poll):
        chosen = chosen[:1]
    prow = await _puppet_row(row.nostr_pubkey or "")
    author, inbox = await _canonical(prow.actor_uri) if prow and prow.actor_uri else ("", "")
    if not author or not inbox or config.host_blocked(remote.host_of(author)):
        return []
    base = config.base_url()
    out = []
    for i, oid in enumerate(chosen):
        note = convert.vote_note(vote_id=f"{convert.activity_url(base, ev['id'])}/vote/{i}", actor=me,
                                 question=row.note_uri, question_actor=author, label=labels[oid])
        act = convert.create(note, me)
        act["to"], act["cc"] = [author], []
        out.append((inbox, act))
    await _remember_sent(ev["id"], inbox, kind=1018, by=member, target=row.note_uri)
    return out


BACKFILL_POSTS = 20


async def backfill(member: str, inbox: str) -> int:
    """Send a new follower's server the member's recent public posts -- the same Create activities the
    outbox serves, so the same object ids (the server stores each once, however often it is sent).
    Oldest first, so the newest lands on top of a profile that orders by arrival. Returns how many
    were accepted; never raises (a failure costs only the history, never the follow)."""
    try:
        items, _oldest, _last = await public_posts(member, limit=BACKFILL_POSTS)
        if not items:
            return 0
        keys = await state.keypair(member)
        key_id, priv = await actors.signing(member, keys)
    except Exception as e:
        logger.info("[activitypub] backfill for %s not sent: %s: %s", member[:12], type(e).__name__, e)
        return 0
    ok = 0
    for act in reversed(items):
        try:
            status = await remote.deliver(inbox, act, key_id=key_id, private_pem=priv)
        except Exception:
            status = 0
        if 200 <= status < 300:
            ok += 1
        elif status in (0, 408, 429) or status >= 500:
            break                                   # the server is struggling: stop, do not pile on
    logger.info("[activitypub] backfilled %d/%d post(s) of %s to %s", ok, len(items), member[:12],
                remote.host_of(inbox))
    return ok


async def public_posts(member: str, *, until: int = 0, after: str = "", limit: int = 20) -> tuple[list, int, str]:
    """(Create activities, oldest created_at) for a member's recent public posts, newest first -- what
    their OUTBOX collection serves. A remote server fills a profile from it: Akkoma and Mastodon read
    it when somebody opens the profile, and with it empty they showed only whatever had happened to
    be delivered to them (one post, or none). The same conversion and the same rules as delivery:
    no mirrors, no protected events, and a reply only when it answers something on the fediverse."""
    base = config.base_url()
    me = await actors.actor_id(member) if base else ""
    if not me:
        return [], 0, ""
    # Every kind DELIVERY sends (POST_KINDS, comments included): a comment that went out to the
    # fediverse and was then missing from the outbox read as deleted to anybody paging the profile.
    # build_object below applies the same which-replies rule delivery does.
    flt = {"kinds": list(POST_KINDS), "authors": [member], "limit": max(limit * 3, 30)}
    if until:
        # INCLUSIVE, with the id of the last one served as the tie-break: `until - 1` skipped every
        # post published in the same second as the last one on the previous page.
        flt["until"] = int(until)
    evs = await nostr_store._ws_query(settings_store._port(), [flt], strict=True)
    evs.sort(key=lambda e: (-int(e.get("created_at") or 0), e.get("id", "")))
    items, oldest, last_id = [], 0, ""
    for ev in evs:
        ts = int(ev.get("created_at") or 0)
        if until and ts == int(until) and after and ev.get("id", "") <= after:
            continue
        oldest, last_id = ts, ev.get("id", "")
        note, _ctx = await build_object(ev, me)
        if note is None:
            continue
        items.append(convert.create(note, me))
        if len(items) >= limit:
            break
    return items, oldest, last_id


def counts_as_public(ev: dict) -> bool:
    """Whether an event is one of the posts an outbox serves (a label's count, so replies are
    included without resolving their parents)."""
    return not _is_mirror(ev) and not _protected(ev)


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


async def _remember_sent(event_id: str, inbox, kind: int = 7, *, by: str = "", target: str = "") -> None:
    """Where an event went BEYOND the followers (the replied-to author, the mentioned, the boosted or
    liked author), what it was, WHO sent it and what it was about -- so its deletion reaches the same
    servers, as the right verb, naming the right object, even when the kind-5 carries no `k` tag and
    the relay has already dropped the original; and so only its author can delete it."""
    inboxes = [inbox] if isinstance(inbox, str) else sorted(set(inbox))
    inboxes = [i for i in inboxes if i]
    if not inboxes and kind not in (6, 7, 1018):
        return                        # a post to followers only: nothing beyond them to remember
    doc = {"inbox": inboxes[0] if inboxes else "", "inboxes": inboxes[:200], "kind": kind}
    if by:
        doc["by"] = by
    if target:
        doc["target"] = target
    try:
        await nostr_store.put_doc(settings_store._port(), settings_store._operator_seckey(None), _SENT + event_id, doc)
    except Exception:
        pass


async def _sent_rec(event_id: str) -> dict:
    try:
        doc = await nostr_store.get_doc(settings_store._port(), _SENT + event_id,
                                        seckey=settings_store._operator_seckey(None))
    except Exception:
        return {}
    if not isinstance(doc, dict):
        return {}
    inboxes = doc.get("inboxes") or ([doc["inbox"]] if doc.get("inbox") else [])
    return {"kind": doc.get("kind"), "inboxes": [i for i in inboxes if isinstance(i, str) and i],
            "by": str(doc.get("by") or ""), "target": str(doc.get("target") or "")}


# ------------------------------------------------------------------------------------ delivery

MAX_RETRIES_QUEUED = 5000


_down: dict = {}                 # host -> (consecutive failures, resting until)
_DOWN_AFTER = 5


def _host_resting(host: str) -> float:
    """Seconds a server that keeps failing is left alone for (0 when it is not)."""
    n, until = _down.get(host, (0, 0.0))
    return max(0.0, until - time.time())


def _note_result(host: str, ok: bool) -> None:
    """A per-server circuit breaker. A dead instance used to cost a 12-second timeout on EVERY post
    to EVERY one of its followers; after a run of failures it rests (15 minutes, doubling to 4 hours)
    and what was meant for it waits in the retry queue instead."""
    if ok:
        _down.pop(host, None)
        return
    n = _down.get(host, (0, 0.0))[0] + 1
    until = time.time() + min(900 * 2 ** max(0, n - _DOWN_AFTER), 4 * 3600) if n >= _DOWN_AFTER else 0.0
    _down[host] = (n, until)
    if len(_down) > 20000:
        for k in sorted(_down, key=lambda k: _down[k][1])[:4000]:
            _down.pop(k, None)


def _retry_key(inbox: str, activity: dict) -> str:
    return hashlib.sha256(f"{inbox}\n{activity.get('id', '')}".encode()).hexdigest()[:32]


async def _queue_retry(due: float, inbox: str, activity: dict, member: str, attempt: int) -> None:
    """Queue a delivery for later -- in memory AND on the relay, so a worker restart (every deploy)
    does not silently drop what a resting server is owed."""
    key = _retry_key(inbox, activity)
    if any(r[5] == key for r in _retries):
        return
    if len(_retries) >= MAX_RETRIES_QUEUED:
        old = _retries.pop(0)                    # the oldest is the least likely to still matter
        await _forget_retry(old[5])
    _retries.append((due, inbox, activity, member, attempt, key))
    try:
        await state.save_retry(key, {"due": due, "inbox": inbox, "activity": activity, "member": member,
                                     "attempt": attempt})
    except Exception:
        pass                                     # memory still has it; only a restart would lose it


async def _forget_retry(key: str) -> None:
    if key:
        try:
            await state.drop_retry(key)
        except Exception:
            pass


_retries_loaded = False
_retries_loaded_at = 0.0
# The retry documents are also written by the APP process: an inbox Accept that failed is handed
# here (inbox._accept_later), because this worker is what retries. Read once, those would wait for
# the next restart -- so the queue is re-read on this interval and new documents are merged in.
_RELOAD_RETRIES_EVERY = 300


async def _load_retries() -> None:
    """Pick up the deliveries a previous worker left queued -- and, every few minutes, the ones the
    app process queued since."""
    global _retries_loaded, _retries_loaded_at
    if _retries_loaded and time.time() - _retries_loaded_at < _RELOAD_RETRIES_EVERY:
        return
    try:
        saved = await state.load_retries()
    except Exception:
        return                                   # try again next tick; never assume "none"
    _retries_loaded = True
    _retries_loaded_at = time.time()
    have = {r[5] for r in _retries}
    for key, e in sorted(saved.items(), key=lambda kv: float(kv[1].get("due") or 0))[:MAX_RETRIES_QUEUED]:
        if key not in have and isinstance(e.get("activity"), dict):
            _retries.append((float(e.get("due") or 0), str(e["inbox"]), e["activity"], str(e.get("member") or ""),
                             int(e.get("attempt") or 1), key))


async def _send(inbox: str, activity: dict, member: str, attempt: int = 0, key: str = "") -> bool:
    """Deliver once. True when nothing more is owed (delivered, or an answer retrying cannot change);
    False when it is still owed (queued for a retry, or out of retries)."""
    host = remote.host_of(inbox)
    resting = _host_resting(host)
    if resting:
        if attempt < len(_RETRY_DELAYS):
            await _queue_retry(time.time() + max(resting, _RETRY_DELAYS[attempt]), inbox, activity, member, attempt + 1)
        _stats["failed"] += 1
        return False
    keys = await state.keypair(member)               # driven by an event on this relay: never limited
    key_id, priv = await actors.signing(member, keys)
    status = await remote.deliver(inbox, activity, key_id=key_id, private_pem=priv)
    _note_result(host, 200 <= status < 500 and status not in (408, 429))
    if 200 <= status < 300:
        _stats["delivered"] += 1
        return True
    _stats["failed"] += 1
    _stats["last_error"] = f"HTTP {status} from {remote.host_of(inbox)}"
    # 4xx other than 408/429 is an answer, not an outage: retrying it only makes us the storm.
    if (status == 0 or status >= 500 or status in (408, 429)) and attempt < len(_RETRY_DELAYS):
        await _queue_retry(time.time() + _RETRY_DELAYS[attempt], inbox, activity, member, attempt + 1)
        return False
    # Given up. A Follow that never arrived must not stay recorded as asked-for, or the member's
    # next contact-list change would think it already sent and never try again.
    if activity.get("type") == "Follow":
        try:
            await state.drop_following(member, convert.id_of(activity.get("object")))
        except Exception:
            pass
    return 200 <= status < 500 and status not in (408, 429)


# ------------------------------------------------------------------------------- the time budget
#
# 2026-10-11: delivery stalled for ~23 hours. Every tick ran out of the worker's time limit before its
# pass saved the cursor, so the next tick started from the same place, for ever, and nothing said so
# except one INFO line in the WORKER's journal. The tick is now built so that cannot happen again:
#   * the whole tick works to a BUDGET under the worker's limit, and every stage has its own deadline
#     -- the members' pass first (it is the one that must never wait), then the retry queue, then the
#     everyone pass, the large deletions, and last the relay subscriptions;
#   * no single event can eat the tick: its plan has a time limit, and its sends get a window --
#     whatever has not STARTED when the window closes goes to the persistent retry queue (it is owed,
#     not lost), so the cursor may move past it, exactly as it always could past a failed send;
#   * the reader takes the backlog oldest-first in time windows (`_windows`), so a flood of one kind
#     can never push older posts out of a capped read;
#   * the cursor is saved every _SAVE_EVERY seconds AND when a stage stops, so a cut-off keeps its work.

def _limit() -> float:
    """The worker's hard limit on one tick (`_job`'s wait_for)."""
    return _TICK_SECONDS * 8


# Cumulative deadlines, as fractions of _limit() from the tick's start. A stage that finishes early
# leaves its time to the next; one that overruns (a send already in flight is never cut off -- it is
# bounded by remote.deliver's own total deadline) eats only the stages after it.
_STAGE_END = {"members": 0.55, "retries": 0.65, "everyone": 0.72, "deletions": 0.78, "relays": 0.8}


def _event_window() -> float:
    """Seconds one event's sends may take before the rest are handed to the retry queue."""
    return _limit() / 12


def _plan_limit() -> float:
    """Seconds one event's plan may take (actor and parent lookups on slow servers)."""
    return _limit() / 4


_inflight: set = set()           # sends handed off past their event's window, still running
_INFLIGHT_MAX = 64
_slow: dict = {}                 # host -> monotonic time until which its new sends go to the retry queue
_SLOW_FOR = 120.0


def _host_slow(host: str) -> bool:
    until = _slow.get(host)
    if until and until > time.monotonic():
        return True
    _slow.pop(host, None)
    return False


async def _deliver(jobs: list, member: str, deadline: float, *, width: int = 8, requeue: bool = True,
                   detach: bool = True) -> list:
    """Send [(inbox, activity)] at most `width` at a time, and stop waiting at `deadline`:
      * a send that has not STARTED by then is not started -- with `requeue` it goes to the persistent
        retry queue, due now (owed, not lost); either way it is returned, in order;
      * a send already IN FLIGHT is never cancelled (a cut-off POST may have landed and could then only
        be repeated blind). With `detach` it is left running in the background, its retry written
        FIRST so a restart cannot lose it, and its server is marked slow: that server's next sends go
        straight to the retry queue instead of costing every following event the same wait. Without
        `detach` it is awaited (remote.deliver bounds it).
    2026-10-11: one slow inbox made every event wait for it, and every event behind it waited too."""
    if not jobs:
        return []
    sem = asyncio.Semaphore(width)
    late: list = []
    started: set = set()
    handed_off: set = set()

    async def one(idx, job):
        if _host_slow(remote.host_of(job[0])):
            late.append((idx, job))
            return
        async with sem:
            if time.monotonic() >= deadline:
                late.append((idx, job))
                return
            started.add(idx)
            try:
                ok = await _send(job[0], job[1], member)
            except Exception as e:
                # _send failed BEFORE it could queue its own retry (a key read, a signature): owed.
                _stats["last_error"] = f"send failed: {type(e).__name__}"[:200]
                await _queue_retry(time.time() + _RETRY_DELAYS[0], job[0], job[1], member, 1)
                return
            if ok and idx in handed_off:
                key = _retry_key(job[0], job[1])     # it landed after all: the safety copy is not owed
                _retries[:] = [r for r in _retries if r[5] != key]
                await _forget_retry(key)

    tasks = [asyncio.ensure_future(one(i, j)) for i, j in enumerate(jobs)]
    try:
        _, pending = await asyncio.wait(tasks, timeout=max(0.0, deadline - time.monotonic()))
    except asyncio.CancelledError:
        for i, t in enumerate(tasks):
            if i not in started:
                t.cancel()                           # a POST in flight is left to finish (see above)
        raise
    wait_for: list = []
    for t in pending:
        idx = tasks.index(t)
        if idx not in started:
            t.cancel()                               # waiting for a slot: never started, nothing sent
            late.append((idx, jobs[idx]))
        elif detach and len(_inflight) < _INFLIGHT_MAX:
            inbox, act = jobs[idx]
            await _queue_retry(time.time() + remote._TOTAL_SECONDS + 60, inbox, act, member, 1)
            handed_off.add(idx)
            _slow[remote.host_of(inbox)] = time.monotonic() + _SLOW_FOR
            _inflight.add(t)
            t.add_done_callback(_inflight.discard)
            _stats["handed_off"] = _stats.get("handed_off", 0) + 1
        else:
            wait_for.append(t)
    if wait_for:
        await asyncio.gather(*wait_for, return_exceptions=True)
    late = [j for _, j in sorted(late, key=lambda x: x[0])]
    if requeue:
        for inbox, act in late:
            await _queue_retry(time.time(), inbox, act, member, 0)
        _stats["handed_to_retry"] = _stats.get("handed_to_retry", 0) + len(late)
    return late


async def _flush_retries(limit: int = 200, deadline: float = 0.0) -> None:
    """Send what is due -- up to `limit` of them, concurrently; the rest stay queued for the next
    tick rather than being dropped. Nothing is STARTED after `deadline`: what is left goes back on the
    queue unchanged (its relay document was never removed)."""
    deadline = deadline or (time.monotonic() + _limit())
    try:
        await asyncio.wait_for(_load_retries(), timeout=max(1.0, deadline - time.monotonic()))
    except (asyncio.TimeoutError, TimeoutError):
        pass                                      # try again next tick; memory still has the queue
    now = time.time()
    due = sorted((r for r in _retries if r[0] <= now), key=lambda r: r[0])[:limit]
    for r in due:
        _retries.remove(r)
    sem = asyncio.Semaphore(8)

    async def one(r):
        async with sem:
            if time.monotonic() >= deadline:
                _retries.append(r)               # not started: still owed, document untouched
                return
            try:
                await _send(r[1], r[2], r[3], r[4])
            except Exception as e:
                _stats["last_error"] = f"retry failed: {type(e).__name__}"[:200]
                _retries.append((time.time() + _RETRY_DELAYS[0],) + tuple(r[1:]))
                return
            if not any(x[5] == r[5] for x in _retries):
                await _forget_retry(r[5])        # sent or given up (a re-queue keeps its document)
    await asyncio.gather(*(one(r) for r in due))


async def _members() -> list:
    """Every local user on the fediverse. (Those on a linked Pleroma account send only follows from
    here -- see `plan`.)"""
    return actors.all_actors()


_last_outcome = {"ok": True, "why": ""}          # what the last tick could do -- read by run_tick


async def tick() -> int:
    """One round: local users' events, then (in `everyone` mode) what other Nostr users on this relay
    addressed to the fediverse. Each pass has its own cursor. Returns events handled."""
    _last_outcome.update(ok=True, why="")
    if not config.enabled() or not config.base_url():
        return 0
    if not settings_store.is_hydrated():
        _last_outcome.update(ok=False, why="settings not loaded")
        return 0          # "is the Pleroma bridge on?" and friends cannot be answered yet -- decide nothing
    from app.services import fedi_tables
    from app.services.relay_reader import Unavailable
    start = time.monotonic()

    def until(stage: str) -> float:
        return start + _limit() * _STAGE_END[stage]
    try:
        await fedi_tables.aready()
    except Unavailable as e:
        # "Is this a puppet / a mirrored note?" cannot be answered: deliver nothing, move no cursor.
        logger.info("[activitypub] delivery waits: %s", e)
        _last_outcome.update(ok=False, why=f"waiting: {e}"[:200])
        return 0
    _stats["last_tick"] = int(time.time())
    # The MEMBERS' pass first. It used to run after the retry queue (200 sends, each up to 30s on a
    # dead server) and the relay subscriptions, so a full queue of slow inboxes could spend the tick
    # before a single fresh post was looked at.
    handled = await _pass(state.CURSOR, await _members(), KINDS, None, deadline=until("members"))
    await _flush_retries(deadline=until("retries"))
    if config.everyone() and time.monotonic() < until("everyone"):
        try:
            handled += await _pass(state.CURSOR_EVERYONE, None, _EVERYONE_KINDS, await _everyone_filter(),
                                   deadline=until("everyone"))
        except Exception as e:
            # Its own cursor, its own failure: it must never take the members' delivery down with it.
            logger.info("[activitypub] everyone pass failed: %s: %s", type(e).__name__, e)
            _stats["last_error"] = f"everyone pass: {type(e).__name__}"[:200]
            _last_outcome.update(ok=False, why=_stats["last_error"])
    if time.monotonic() < until("deletions"):
        try:
            await drain_deletions(deadline=until("deletions"))
        except Exception as e:
            logger.info("[activitypub] deletion backlog not drained: %s: %s", type(e).__name__, e)
    left = until("relays") - time.monotonic()
    if left > 2:
        try:
            from app.services.activitypub import relays
            await asyncio.wait_for(relays.reconcile(), timeout=left)
        except Exception as e:
            logger.info("[activitypub] relay subscriptions not reconciled: %s: %s", type(e).__name__, e)
    return handled


_INLINE_DELETION = 300        # deliveries a kind-5 may send inside the pass; more goes to the backlog
_DRAIN_PER_TICK = 1500        # backlog deliveries per tick (~30s at 16 at a time)
_drain_plans: dict = {}       # deletion id -> its plan, so a big one is planned once, not every tick


async def drain_deletions(budget: int = 0, deadline: float = 0.0) -> int:
    """Send the next slice of the queued large deletions, oldest first. The kind-5 is re-read and
    re-planned (the plan is deterministic: per deleted id, sorted inboxes) and resumed at the saved
    position, so a restart continues where it stopped; a re-sent Delete is harmless, a lost one is not.
    Nothing is started after `deadline`; the position only moves over what was started."""
    budget = budget or _DRAIN_PER_TICK
    deadline = deadline or (time.monotonic() + _limit())
    queue = await state.deletions()
    _stats["deleting"] = len(queue)
    sent = 0
    for eid, entry in sorted(queue.items(), key=lambda kv: (kv[1].get("at", 0), kv[0])):
        if sent >= budget or time.monotonic() >= deadline:
            break
        ev = await _event(eid, strict=True)
        if not ev or ev.get("kind") != 5 or ev.get("pubkey") != entry.get("member"):
            await state.finish_deletion(eid)          # not a deletion by the member it was queued for
            _drain_plans.pop(eid, None)
            continue
        jobs = _drain_plans.get(eid)
        if jobs is None:
            try:
                jobs = await asyncio.wait_for(plan(ev, ev["pubkey"]), timeout=max(1.0, deadline - time.monotonic()))
            except (asyncio.TimeoutError, TimeoutError):
                break                                  # planned again next tick; nothing was sent
            _drain_plans[eid] = jobs
        pos = max(0, int(entry.get("pos") or 0))
        chunk = jobs[pos:pos + budget - sent]
        late = await _deliver(chunk, ev["pubkey"], deadline, width=16, requeue=False, detach=False)
        # The position moves over the run of jobs that were all started; anything after the first one
        # that was not is sent again next time (a repeated Delete is harmless, a skipped one is not).
        skipped = {id(j) for j in late}
        started = next((i for i, j in enumerate(chunk) if id(j) in skipped), len(chunk))
        pos += started
        sent += started
        if pos >= len(jobs):
            await state.finish_deletion(eid)
            _drain_plans.pop(eid, None)
            _stats["deleting"] = max(0, _stats.get("deleting", 1) - 1)
        else:
            await state.deletion_progress(eid, dict(entry, pos=pos, total=len(jobs)))
    if len(_drain_plans) > 50:
        for k in list(_drain_plans)[:-50]:
            _drain_plans.pop(k, None)
    return sent


# ------------------------------------------------------------------------------ poll tallies

_POLL_WINDOW = 30 * 86400        # a poll older than this is left as it was last counted
_POLL_GRACE = 86400              # counted once more for a day after it closes (late votes, "closed")


def count_votes(poll: dict, votes: list) -> tuple[dict, int]:
    """({option id: votes}, voters) -- NIP-88's rule: each pubkey's LATEST response counts, only for
    options the poll has, and only its first option on a single-choice poll."""
    opts = {oid for oid, _ in convert.poll_options(poll)}
    end = convert.poll_end(poll)
    latest: dict = {}
    for v in sorted(votes, key=lambda e: (e.get("created_at", 0), e.get("id", ""))):
        if end and int(v.get("created_at") or 0) > end:
            continue
        latest[v.get("pubkey")] = v
    counts: dict = {}
    voters = 0
    multi = convert.poll_multi(poll)
    for v in latest.values():
        chosen = list(dict.fromkeys(t[1] for t in v.get("tags") or [] if len(t) > 1 and t[0] == "response" and t[1] in opts))
        if not multi:
            chosen = chosen[:1]
        if chosen:
            voters += 1
        for oid in chosen:
            counts[oid] = counts.get(oid, 0) + 1
    return counts, voters


async def poll_updates(members: list = None) -> int:
    """Send Update(Question) for each member poll whose count changed (or that just closed) -- to its
    followers and to every fediverse server that voted in it. Votes come from both sides: a Nostr
    user's kind-1018 and a fediverse answer (stored as a 1018 by inbox.py) are counted the same way."""
    if not config.enabled() or not config.base_url() or not settings_store.is_hydrated():
        return 0
    members = await _members() if members is None else members
    if not members:
        return 0
    now = int(time.time())
    polls = await nostr_store._ws_query(settings_store._port(), [{"kinds": [1068], "authors": members,
                                                                  "since": now - _POLL_WINDOW, "limit": 200}], strict=True)
    polls = [p for p in polls if not _is_mirror(p) and not _protected(p)
             and not (convert.poll_end(p) and convert.poll_end(p) < now - _POLL_GRACE)]
    if not polls:
        return 0
    votes = await nostr_store._ws_query(settings_store._port(), [{"kinds": [1018], "#e": [p["id"] for p in polls],
                                                                  "limit": 10000}], strict=True)
    by_poll: dict = {}
    for v in votes:
        pid = next((t[1] for t in v.get("tags") or [] if len(t) > 1 and t[0] == "e"), "")
        by_poll.setdefault(pid, []).append(v)
    sent = 0
    for poll in polls:
        counts, voters = count_votes(poll, by_poll.get(poll["id"], []))
        end = convert.poll_end(poll)
        closed = bool(end and end <= now)
        try:
            last = await state.poll_tally(poll["id"])
        except Exception:
            continue                                   # unreadable: decide nothing
        if last.get("counts") == counts and int(last.get("voters") or 0) == voters \
                and bool(last.get("closed")) == closed:
            continue
        if not last and not counts and not closed:
            continue                                   # nothing to say yet
        member = poll["pubkey"]
        me = await actors.actor_id(member)
        if not me:
            continue
        await state.set_poll_tally(poll["id"], {"counts": counts, "voters": voters, "closed": closed})
        note, _ctx = await build_object(poll, me)
        if note is None:
            continue
        act = {"@context": convert.AS_CONTEXT,
               "id": f"{convert.activity_url(config.base_url(), poll['id'], 'update')}/{now}",
               "type": "Update", "actor": me, "to": note.get("to"), "cc": note.get("cc"), "object": note}
        inboxes = set(await _follower_inboxes(member)) | set(await state.poll_voter_inboxes(poll["id"]))
        sem = asyncio.Semaphore(8)

        async def one(inbox):
            async with sem:
                await _send(inbox, act, member)
        await asyncio.gather(*(one(i) for i in sorted(inboxes)))
        sent += 1
    return sent


# ------------------------------------------------------------------------------ follow catch-up

_K3 = "pcai:ap:k3:"            # per member: created_at of the contact list last turned into Follows
_CATCHUP_PER_RUN = 3
_caught_up: set = set()        # members settled in this process -- newer lists are `_pass`'s job


async def catch_up_follows(limit: int = _CATCHUP_PER_RUN) -> int:
    """Follow the fediverse accounts already in each member's contact list.

    The delivery pass only ever sees contact lists published AFTER its cursor, and its first run
    sets the cursor to "now" -- so a list written before the fediverse server was switched on is
    never an event it handles, and a member who already followed three hundred fediverse accounts
    (through the Pleroma bridge, say) had a fediverse account that followed nobody. With the bridge
    then switched off, nothing of theirs arrived from anywhere, and nothing said so.

    `_follows` works from the WHOLE list against what is recorded as followed, so running it once on
    the current list is exactly the missing step, and running it again is harmless. The marker is
    the list's created_at: a member is revisited only when a newer list exists that nothing has
    handled -- the delivery pass hands every new contact list here. Its own job, outside the tick's timeout, because one
    member can be hundreds of actor fetches and a cancelled run would leave Follows recorded as
    asked-for that were never sent."""
    if not config.enabled() or not config.base_url() or not settings_store.is_hydrated():
        return 0
    done = 0
    for member in await _members():
        if done >= limit:
            break
        if member in _caught_up:
            continue
        try:
            mark = await state.cursor(_K3 + member)
            evs = await nostr_store._ws_query(settings_store._port(),
                                              [{"kinds": [3], "authors": [member], "limit": 1}], strict=True)
        except Exception:
            continue                                   # unreadable: ask again next run, decide nothing
        latest = max(evs, key=lambda e: e.get("created_at", 0)) if evs else None
        at = int(latest.get("created_at", 0)) if latest else 1
        if mark >= at:
            _caught_up.add(member)
            continue
        if latest is not None:
            done += 1
            try:
                jobs = await plan(latest, member)
            except Exception as e:
                logger.info("[activitypub] follow catch-up for %s failed: %s: %s", member[:12], type(e).__name__, e)
                continue
            sem = asyncio.Semaphore(8)

            async def one(inbox, act):
                async with sem:
                    await _send(inbox, act, member)
            await asyncio.gather(*(one(i, a) for i, a in jobs))
            if jobs:
                logger.info("[activitypub] follow catch-up: %s sent %d follow change(s)", member[:12], len(jobs))
        await state.set_cursor(at, _K3 + member)
        _caught_up.add(member)
    return done


_EVERYONE_KINDS = [0, 1, 1111, 1068, 1018, 5, 6, 7]


async def _everyone_filter():
    """What a NON-local Nostr user publishes that concerns the fediverse: anything by somebody who
    has followers there, and otherwise only what is addressed to it -- a mention of a fediverse
    account, a reply/like/boost of one of its notes, or the deletion of something that went out.
    Never a local user's (the first pass has those), never a puppet's (that is the fediverse
    talking), never a mirror, never a protected (`-`) event.

    Returns an async predicate over a whole PAGE, so "is this a mirrored note?" is one query per
    page rather than one per event (almost every reply and reaction carries an `e` tag)."""
    followed = await state.nostr_users_with_followers()     # raises on a failed read: stop the pass
    puppets = await _puppet_set()
    from app.services.activitypub import relays
    to_relays = relays.scope() == "everyone" and bool(await relays.accepted_inboxes())

    def _direct(ev: dict, mirrored: set) -> bool:
        tags = ev.get("tags") or []
        if any(len(t) > 1 and t[0] in ("p", "P") and t[1] in puppets for t in tags):
            return True
        return any(len(t) > 1 and t[0] in ("e", "E") and t[1] in mirrored for t in tags)

    async def page(evs: list) -> set:
        refs = {t[1] for ev in evs for t in ev.get("tags") or [] if len(t) > 1 and t[0] in ("e", "E")}
        mirrored = await _mirrored_among(refs)
        deletions = [ev for ev in evs if ev.get("kind") == 5]
        gone_ids = {t[1] for ev in deletions for t in ev.get("tags") or [] if len(t) > 1 and t[0] == "e"}
        gone = await _events(list(gone_ids)) if gone_ids else {}
        gone_refs = {t[1] for g in gone.values() for t in g.get("tags") or [] if len(t) > 1 and t[0] in ("e", "E")}
        mirrored |= await _mirrored_among(gone_refs)
        ok = set()
        for ev in evs:
            pk = ev.get("pubkey", "")
            if actors.is_actor(pk) or pk in puppets or _is_mirror(ev) or _protected(ev):
                continue
            if pk in followed:
                keep = True
            elif to_relays and ev.get("kind") in (1, 1068) and not parent_of(ev):
                keep = True                    # relay scope "everyone": every public top-level post
            elif ev.get("kind") == 0:
                keep = False
            elif ev.get("kind") == 5:
                # A deletion goes out when what it deletes did: a reply we sent must not outlive
                # its author's delete on the fediverse just because they have no followers there.
                # By OUR RECORD of sending it first -- the relay removes the author's own events as it
                # stores the kind-5, so by now the deleted event itself is almost never there to judge.
                keep = any(_direct(gone[t[1]], mirrored) for t in ev.get("tags") or []
                           if len(t) > 1 and t[0] == "e" and t[1] in gone)
                if not keep:
                    for t in [t for t in ev.get("tags") or [] if len(t) > 1 and t[0] == "e"][:10]:
                        rec = await _sent_rec(t[1])
                        if rec.get("kind") and rec.get("by") in ("", pk):
                            keep = True
                            break
            else:
                keep = _direct(ev, mirrored)
            if keep and await actors.exposed(pk):
                ok.add(ev["id"])
        return ok
    return page


def _protected(ev: dict) -> bool:
    """NIP-70: the author asked that this event not be republished by anybody else -- or the app marked
    it `nofederate` (its own private traffic)."""
    return any(t == ["-"] or (t and t[0] == "nofederate") for t in ev.get("tags") or [])


async def _events(ids: list) -> dict:
    from app.services import nostr_store
    evs = await nostr_store._ws_query(settings_store._port(), [{"ids": ids[:500]}], strict=True)
    return {e["id"]: e for e in evs if isinstance(e, dict) and e.get("id")}


async def _puppet_set() -> frozenset:
    from app.services.activitypub import dm
    return await dm._apuppet_pubkeys()


async def _mirrored_among(event_ids) -> set:
    """Which of these event ids are notes mirrored from the fediverse -- ONE pass over the ledger."""
    ids = list(event_ids)[:2000]
    if not ids:
        return set()
    from app.services import fedi_tables
    return await fedi_tables.amirrored_among(ids)


# THE CURSOR IS A `created_at`, AND `created_at` IS WHAT THE AUTHOR SAYS, NOT WHEN IT ARRIVED. An
# event signed offline and replayed later, one that reaches this relay late through the firehose, or
# anything behind an event from a fast clock (the relay accepts up to 15 minutes ahead) was older than
# the cursor by the time it was here -- and never federated. So every pass re-reads a trailing window
# behind the cursor, the cursor never runs ahead of the wall clock, and what was already SENT inside
# the window is remembered on the relay (`done`), so a worker restart does not send it twice.
_OVERLAP = {state.CURSOR: 6 * 3600, state.CURSOR_EVERYONE: 1800}
# …BUT NOT ON EVERY TICK. Re-reading the whole overlap every 30 seconds was most of what the RELAY did all day:
# measured on server1 2026-10-16, the everyone pass pulled 6,725 events (30 minutes of the firehose) and the
# members pass six hours of members' events (~25,000 operator kind-5s among them) every tick -- thousands of
# events decoded, encoded and shipped over loopback to find the few that were new. An ordinary tick re-reads
# only _SHORT_OVERLAP (clock skew, an event landing mid-tick); the FULL window is re-read every
# _FULL_OVERLAP_EVERY seconds, so a late arrival still federates, within that long instead of within 30 s.
_SHORT_OVERLAP = 120
_FULL_OVERLAP_EVERY = 900
_last_full: dict = {}            # cursor key -> monotonic time the last FULL re-read finished
_DONE_MAX = 1500
_done: dict = {}                 # cursor key -> {event id[:16]: created_at} sent inside the window


async def _cursor_doc(cursor_key: str) -> tuple[int, dict]:
    doc = await state._get(cursor_key)
    doc = doc if isinstance(doc, dict) else {}
    done = doc.get("done") if isinstance(doc.get("done"), dict) else {}
    return int(doc.get("since") or 0), {str(k): int(v) for k, v in done.items() if isinstance(v, (int, float))}


async def _save_cursor(cursor_key: str, since: int, done: dict) -> None:
    keep = sorted(done.items(), key=lambda kv: -kv[1])[:_DONE_MAX]
    await state._put(cursor_key, {"since": int(since), "done": dict(keep)})


_pass_report: dict = {}          # cursor key -> {"cursor", "stopped", "at"}: what the last pass did (health)


async def _pass(cursor_key: str, authors, kinds: list, qualifies, deadline: float = 0.0) -> int:
    """Read since `cursor_key` (minus the overlap window), translate, deliver, advance. `authors` None
    = every author (the everyone pass), narrowed by `qualifies`. Stops at `deadline` with the cursor
    saved at the last event it finished, so the next tick carries on from there."""
    global _fol_memo
    deadline = deadline or (time.monotonic() + _limit())
    try:
        since, saved_done = await _cursor_doc(cursor_key)
    except Exception as e:
        _stats["last_error"] = f"cursor unreadable: {type(e).__name__}"
        _pass_report[cursor_key] = {"cursor": 0, "stopped": "cursor unreadable", "at": int(time.time())}
        return 0                                   # never deliver from a guessed position
    now = int(time.time())
    if not since:
        await _save_cursor(cursor_key, now, {})    # first run: no backfill of everything ever posted
        _pass_report[cursor_key] = {"cursor": now, "stopped": "", "at": now}
        return 0
    if authors is not None and not authors:
        await _save_cursor(cursor_key, min(max(since, now - 60), now), saved_done)
        _pass_report[cursor_key] = {"cursor": min(max(since, now - 60), now), "stopped": "", "at": now}
        return 0
    overlap = _OVERLAP.get(cursor_key, 900)
    full = time.monotonic() - _last_full.get(cursor_key, float("-inf")) >= _FULL_OVERLAP_EVERY
    if not full:
        overlap = min(overlap, _SHORT_OVERLAP)
    done = _done.setdefault(cursor_key, {})
    for k, v in saved_done.items():
        done.setdefault(k, v)
    handled, newest, sent_any = 0, min(since, now), False
    saved_at = time.monotonic()
    stopped = ""                                   # why the pass ended before the present ("" = it didn't)

    def finished(ev) -> None:
        nonlocal newest, handled
        _seen[ev["id"]] = time.time()
        newest = max(newest, int(ev.get("created_at", 0)))
        handled += 1

    _fol_memo = {}
    try:
        async with contextlib.aclosing(_pass_reads(authors, since, now, overlap, kinds, full)) as batches:
            async for batch in batches:
                evs = sorted((e for e in batch if e.get("id") not in _seen and e.get("id", "")[:16] not in done),
                             key=lambda e: (e.get("created_at", 0), e["id"]))
                for at in range(0, len(evs), _PAGE):
                    chunk = evs[at:at + _PAGE]
                    if time.monotonic() >= deadline:
                        stopped = "time budget"
                        break
                    wanted = None
                    if qualifies is not None:
                        try:
                            wanted = await asyncio.wait_for(qualifies(chunk),
                                                            timeout=max(1.0, deadline - time.monotonic()))
                        except (asyncio.TimeoutError, TimeoutError):
                            stopped = "time budget"
                            break
                    for ev in chunk:
                        if time.monotonic() >= deadline:
                            stopped = "time budget"
                            break
                        if _inert_deletion(ev) or (wanted is not None and ev["id"] not in wanted):
                            finished(ev)           # nothing to send, decided without a read
                            continue
                        if ev.get("kind") == 3 and authors is not None:
                            # A member's contact list is the catch-up's job, never the tick's. Turning a
                            # list of a few hundred fediverse accounts into Follows is a few hundred actor
                            # fetches; inside the tick's time limit it timed out, the cursor never moved,
                            # and every post, reply and like queued behind it waited too ("I didn't see
                            # anything on DRC"). The catch-up has no limit and runs every minute; it sees
                            # this list as newer than its marker and handles it there.
                            _caught_up.discard(ev["pubkey"])
                            finished(ev)
                            continue
                        try:
                            # ONE EVENT MUST NOT EAT THE TICK: a reply naming thirty people on slow servers
                            # is thirty actor fetches. Out of time it counts as a failed try, like any other.
                            jobs = await asyncio.wait_for(plan(ev, ev["pubkey"]), timeout=_plan_limit())
                            if ev.get("kind") == 5 and len(jobs) > _INLINE_DELETION:
                                # "Delete all my posts": sent a slice per tick by drain_deletions. Inline,
                                # one of these outran the tick's time limit, was cancelled before being
                                # marked done, and was sent again from the top every tick -- with every
                                # post queued behind it never going out.
                                await state.queue_deletion(ev["id"], ev["pubkey"], len(jobs))
                                _drain_plans[ev["id"]] = jobs
                                jobs = []
                                done[ev["id"][:16]] = int(ev.get("created_at", 0))
                                sent_any = True
                        except Exception as e:
                            # STOP HERE rather than step over it: the usual cause is a relay read that
                            # failed (the follower list is read strictly), and moving the cursor past the
                            # event would lose it for good. One event that fails every time is skipped
                            # after three tries, and said so.
                            n = _failures[ev["id"]] = _failures.get(ev["id"], 0) + 1
                            logger.info("[activitypub] could not translate %s (try %d): %s: %s",
                                        ev.get("id", "")[:12], n, type(e).__name__, e)
                            if n < 3:
                                stopped = f"could not translate an event: {type(e).__name__}"
                                break
                            _stats["last_error"] = f"skipped {ev['id'][:12]} after 3 failures: {type(e).__name__}"
                            jobs = []
                        if jobs:
                            # The sends get a window; what has not started when it closes is OWED, in the
                            # persistent retry queue -- a dead or slow inbox costs this event its window,
                            # never the tick.
                            await _deliver(jobs, ev["pubkey"], min(deadline, time.monotonic() + _event_window()))
                            done[ev["id"][:16]] = int(ev.get("created_at", 0))
                            sent_any = True
                        _failures.pop(ev["id"], None)
                        finished(ev)
                        if time.monotonic() - saved_at >= _SAVE_EVERY:
                            # A pass that outruns the tick's time limit is cancelled where it stands, and
                            # the save at the end never happens: a long backlog was re-read from the same
                            # cursor every tick, for 22 hours, by a worker making progress the whole time.
                            await _save_cursor(cursor_key, min(newest, now), done)
                            saved_at = time.monotonic()
                    if stopped:
                        break
                if stopped:
                    break
    except asyncio.CancelledError:
        # Cut off by the worker's hard limit after all: keep what was finished, then go.
        try:
            await asyncio.shield(_save_cursor(cursor_key, min(newest, now), done))
        except BaseException:
            pass
        raise
    except Exception as e:
        # A relay read that failed part-way: what was finished before it still counts (saved below);
        # nothing after it is guessed at.
        stopped = f"relay read failed: {type(e).__name__}"
        _stats["last_error"] = stopped[:200]
        logger.info("[activitypub] delivery read stopped: %s: %s", type(e).__name__, e)
    finally:
        _fol_memo = None
    if len(_seen) > 40000:
        for k in sorted(_seen, key=_seen.get)[:20000]:
            _seen.pop(k, None)
    # Never ahead of the clock: one future-dated event must not move the window past what is still
    # to arrive. Inclusive of the newest second handled (more may share it; `_seen` stops a replay).
    newest = min(newest, now)
    floor = min(newest, now) - _OVERLAP.get(cursor_key, 900)   # the FULL window: it is re-read every _FULL_OVERLAP_EVERY
    for k in [k for k, v in done.items() if v < floor]:
        done.pop(k, None)
    saved = newest != since or sent_any
    if saved:
        await _save_cursor(cursor_key, newest, done)
    _pass_report[cursor_key] = {"cursor": newest if saved else since, "stopped": stopped, "at": int(time.time())}
    if full and not stopped:
        _last_full[cursor_key] = time.monotonic()
    if stopped:
        logger.info("[activitypub] %s pass stopped at %d (%ds behind): %s", cursor_key, newest,
                    max(0, now - newest), stopped)
    return handled


def _operator_pubkey() -> str:
    try:
        from app.services.nostr import bip340
        sk = settings_store._operator_seckey(None)
        return bip340.pubkey_from_seckey(sk).hex() if sk else ""
    except Exception:                                    # noqa: BLE001 -- unknown: read everything, as before
        return ""


async def _pass_reads(authors, since: int, now: int, overlap: int, kinds: list, full: bool):
    """The batches one pass reads. An ordinary tick reads the short overlap. The FULL re-read (every
    _FULL_OVERLAP_EVERY) exists for LATE arrivals, and in its long tail it does not ask for the operator key's
    deletions: on this node those are the DocTables deleting app documents -- ~25,000 kind-5s in six hours,
    none of which federates (`_inert_deletion`) -- and they were most of what each full re-read pulled off the
    relay. A member's own deletions are still read across the whole window, and the operator's within the
    short overlap, so nothing a person deletes is missed."""
    start = max(1, min(since, now) - overlap)
    op = _operator_pubkey() if (full and 5 in kinds and overlap > _SHORT_OVERLAP) else ""
    if not op:
        async with contextlib.aclosing(_windows(authors, start, kinds, now)) as batches:
            async for b in batches:
                yield b
        return
    recent = max(start, min(since, now) - _SHORT_OVERLAP)
    rest = [k for k in kinds if k != 5]
    people = [a for a in authors if a != op] if authors is not None else None
    segments = [(authors, start, rest, recent - 1)]
    if people is None or people:
        segments.append((people, start, [5], recent - 1))
    segments.append((authors, recent, kinds, None))
    for who, lo, ks, hi in segments:
        if hi is not None and hi < lo:
            continue
        async with contextlib.aclosing(_windows(who, lo, ks, now, until=hi)) as batches:
            async for b in batches:
                yield b


_failures: dict = {}
_MAX_PAGES = 20
_WINDOW_PAGES = 8                # pages one time window may take before it is narrowed
_SAVE_EVERY = 20                 # seconds of a pass between cursor saves
_last_read = {"complete": True}


async def _since(members, since: int, kinds: list = None, *, until: int | None = None,
                 pages: int = _MAX_PAGES) -> list:
    """Everything members published in [since, until]. The relay answers NEWEST FIRST with a limit,
    so this pages backwards with `until` until a page comes back short. When `pages` run out first the
    read is INCOMPLETE -- its oldest part is missing -- and `_last_read["complete"]` says so; the
    caller (`_windows`) then asks for a narrower window instead of delivering from a partial one."""
    out, seen_ids = [], set()
    _last_read["complete"] = True
    for _ in range(pages):
        flt = {"kinds": kinds or KINDS, "since": since, "limit": _PAGE}
        if members is not None:
            flt["authors"] = members
        if until is not None:
            flt["until"] = until
        page = await nostr_store._ws_query(settings_store._port(), [flt], strict=True)
        fresh = [e for e in page if e.get("id") not in seen_ids]
        for e in fresh:
            seen_ids.add(e["id"])
        out.extend(fresh)
        if len(page) < _PAGE or not fresh:
            return out
        until = min(int(e.get("created_at", 0)) for e in page)
    _last_read["complete"] = False
    return out


async def _windows(members, since: int, kinds: list, now: int, *, until: int | None = None):
    """The events from `since` on, OLDEST WINDOW FIRST, as batches. A capped newest-first read of a
    long backlog (2026-10-11: ~9,400 operator kind-5s a day, then 10,000 events max) handed over only
    the NEWEST part, and the cursor moved past the oldest ~16 hours of real posts, likes and a report:
    they were never sent. A window that does not fit in _WINDOW_PAGES is narrowed and asked again, so
    nothing is dropped however the backlog is made up; only a single SECOND holding more than
    _MAX_PAGES pages could be, and that is said out loud. The last window is open-ended (a clock a
    little ahead is still read), unless `until` closes it."""
    hi = now if until is None else min(until, now)
    start, span = since, max(0, hi - since)
    while start <= hi:
        end = min(hi, start + span)
        last = end >= hi and until is None
        _last_read["complete"] = True        # set by _since; a stand-in that never says must not inherit a stale "no"
        evs = await _since(members, start, kinds, until=None if last else end,
                           pages=_WINDOW_PAGES if end > start else _MAX_PAGES)
        if not _last_read["complete"] and end > start:
            span = (end - start) // 4
            continue
        if not _last_read["complete"]:
            _stats["last_error"] = f"over {_MAX_PAGES * _PAGE} events in second {start}; the oldest of them were not sent"
            logger.warning("[activitypub] %s", _stats["last_error"])
        yield evs
        start = end + 1
        span = max(span * 2, 600)


# ------------------------------------------------------------------------------ redelivery

_REDELIVER = "pcai:ap:redeliver:"


async def capped_read_boundary(cursor: int, pass_at: int) -> int:
    """Where the OLD capped reader stopped: the oldest `created_at` that a read of the members' events
    from `cursor` minus the overlap, as of `pass_at`, newest first, capped at _MAX_PAGES pages, handed
    over. Everything older than it in that window was dropped by that reader. Returns `cursor` when the
    read was not capped (nothing was dropped)."""
    members = await _members()
    evs = await _since(members, max(1, cursor - _OVERLAP[state.CURSOR]), KINDS, until=pass_at, pages=_MAX_PAGES)
    if _last_read["complete"] or not evs:
        return cursor
    return min(int(e.get("created_at", 0)) for e in evs)


async def redeliver(since: int, until: int | None = None, *, dry_run: bool = False, log=None) -> dict:
    """Send every member event in [since, until] that never went out -- the repair for a stall, run by
    hand (scripts/ap_redeliver.py), never by the tick. What counts as ALREADY SENT: an id in the
    delivery cursor's `done` set, an event with a `pcai:ap:sent:` record (written when it was planned:
    every boost, like, vote and report, and every post that went beyond the followers -- which is what
    keeps a report from being filed twice), and anything an earlier run of this same window sent (its
    own progress document, so a rerun or a crash part-way resumes rather than repeats). A plain post
    may still have gone out with no record of it (a stalled tick that got that far); a second Create of
    the same object id is ignored by Mastodon, Pleroma, Akkoma and Misskey alike.
    Returns counts only -- never content."""
    global _fol_memo
    say = log or (lambda *_: None)
    now = int(time.time())
    hi = min(until or now, now)
    members = await _members()
    _, done = await _cursor_doc(state.CURSOR)              # raises: never "nothing was sent"
    key = f"{_REDELIVER}{int(since)}:{int(hi)}"
    prog = await state._get(key) or {}
    sent_ids = set(prog.get("sent") or [])
    out = {"window": [int(since), int(hi)], "read": 0, "already_sent": 0, "recorded": 0, "nothing_to_send": 0,
           "delivered": 0, "deletions_queued": 0, "by_kind": {}, "dry_run": dry_run}

    async def save():
        if not dry_run:
            await state._put(key, {"sent": sorted(sent_ids), "at": int(time.time())})

    _fol_memo = {}
    try:
        async with contextlib.aclosing(_windows(members, int(since), KINDS, now, until=hi)) as batches:
            async for batch in batches:
                for ev in sorted(batch, key=lambda e: (e.get("created_at", 0), e["id"])):
                    created = int(ev.get("created_at", 0))
                    if created < since or created > hi:
                        continue
                    out["read"] += 1
                    if ev.get("kind") == 3 or _inert_deletion(ev) or _is_mirror(ev) or _protected(ev):
                        continue                  # the catch-up's job / nothing the fediverse ever had
                    short = ev["id"][:16]
                    if short in done or short in sent_ids:
                        out["already_sent"] += 1
                        continue
                    if (await _sent_rec(ev["id"])).get("kind"):
                        out["recorded"] += 1      # planned by a pass before, so it went out then
                        continue
                    k = str(ev.get("kind"))
                    out["by_kind"][k] = out["by_kind"].get(k, 0) + 1
                    if dry_run:
                        continue
                    jobs = await plan(ev, ev["pubkey"])
                    if not jobs:
                        out["nothing_to_send"] += 1
                    elif ev.get("kind") == 5 and len(jobs) > _INLINE_DELETION:
                        await state.queue_deletion(ev["id"], ev["pubkey"], len(jobs))
                        out["deletions_queued"] += 1
                    else:
                        await _deliver(jobs, ev["pubkey"], time.monotonic() + _event_window(), detach=False)
                        out["delivered"] += 1
                    sent_ids.add(short)
                    if len(sent_ids) % 20 == 0:
                        await save()
                        say(f"  ... {out['delivered']} delivered so far")
    finally:
        _fol_memo = None
    await save()
    return out


async def run_tick() -> dict:
    """What the worker runs every _TICK_SECONDS: one tick under the hard limit, then the health check
    (activitypub/health.py), which measures whether members' events are actually going out, writes
    `pcai:ap:stats` for Admin and the health board, and alerts the admin once on a stall and once on
    recovery. Returns the health verdict."""
    ok, why = True, ""
    try:
        await asyncio.wait_for(tick(), timeout=_limit())
        ok, why = _last_outcome["ok"], _last_outcome["why"]
    except Exception as e:
        ok, why = False, f"{type(e).__name__}: {e}"[:200]
        _stats["last_error"] = why
        logger.warning("[activitypub] delivery tick failed: %s", why)
    from app.services.activitypub import health
    try:
        return await health.after_tick(stats(), ok=ok, why=why)
    except Exception as e:
        logger.warning("[activitypub] delivery health check failed: %s: %s", type(e).__name__, e)
        return {}


def start_activitypub_delivery() -> None:
    """Start the delivery loop (idempotent). The TICK reads `activitypub_enabled`, so turning the
    feature on or off in Admin needs no restart."""
    global _scheduler
    if _scheduler is not None:
        return
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    async def _job():
        await run_tick()

    async def _catchup():
        try:
            await catch_up_follows()
        except Exception as e:
            logger.info("[activitypub] follow catch-up failed: %s: %s", type(e).__name__, e)

    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(_job, "interval", seconds=_TICK_SECONDS, id="activitypub_delivery",
                       max_instances=1, coalesce=True)
    _scheduler.add_job(_catchup, "interval", seconds=60, id="activitypub_follow_catchup",
                       max_instances=1, coalesce=True)

    # The history of every account followed BEFORE inbound backfill existed, a couple a minute.
    async def _history():
        try:
            from app.services.activitypub import backfill
            await backfill.catch_up()
        except Exception as e:
            logger.info("[activitypub] history catch-up failed: %s: %s", type(e).__name__, e)
    _scheduler.add_job(_history, "interval", seconds=60, id="activitypub_history_catchup",
                       max_instances=1, coalesce=True)

    async def _polls():
        try:
            await asyncio.wait_for(poll_updates(), timeout=240)
        except Exception as e:
            logger.info("[activitypub] poll tallies failed: %s: %s", type(e).__name__, e)
    _scheduler.add_job(_polls, "interval", seconds=300, id="activitypub_poll_tallies",
                       max_instances=1, coalesce=True)

    async def _orphans():
        try:
            if config.enabled():
                from app.services.activitypub import inbox
                await asyncio.wait_for(inbox.relink_orphans(), timeout=540)
        except Exception as e:
            logger.info("[activitypub] orphan replies failed: %s: %s", type(e).__name__, e)
    _scheduler.add_job(_orphans, "interval", seconds=600, id="activitypub_orphan_replies",
                       max_instances=1, coalesce=True)

    def _prune():
        from app.services.activitypub import ledger
        n = ledger.prune_if_due()
        if n:
            logger.info("[activitypub] pruned %d delivered-note record(s) past retention", n)
    _scheduler.add_job(_prune, "interval", hours=1, id="activitypub_ledger_prune",
                       max_instances=1, coalesce=True)
    _scheduler.start()
    logger.info("[activitypub] delivery loop started (every %ss; off until activitypub_enabled)", _TICK_SECONDS)


