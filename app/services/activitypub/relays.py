"""ActivityPub RELAYS: how a server's public posts reach instances where nobody follows its authors.

The fediverse has no global feed. A server delivers a post to its author's followers, and nowhere else,
so a Nostr user on this node with no fediverse followers is REACHABLE (their profile and outbox serve
their posts) but never DELIVERED. A relay (relay.fedi.buzz, relay.intahnet.co.uk, any `activityrelay` or
Pleroma relay) is the fediverse's answer: a server subscribes, sends it its public posts, and the relay
re-shares each one to every instance subscribed to it.

This speaks the MASTODON convention, which every relay implements:

  * the admin lists a relay's INBOX (`https://relay.example/inbox`) under Admin → Social;
  * the INSTANCE actor (/ap/actor) sends it `Follow {object: as:Public}`; the relay answers `Accept`
    (or `Reject`) to our shared inbox, and only then is anything sent to it;
  * what goes to a subscribed relay is what Mastodon sends: PUBLIC, TOP-LEVEL posts (and polls) --
    not replies, not boosts -- each signed by its own author, plus the Delete when one is deleted;
  * a relay removed from the list is sent `Undo(Follow)`.

WHOSE posts is `activitypub_relay_scope`: `local` (the default) is the accounts this node gave a name --
the usual "my instance's posts reach the fediverse"; `everyone` is every Nostr account the relay serves
in `everyone` mode (hundreds of posts an hour here; relay operators do block firehose-sized instances).

ONE DOCUMENT PER RELAY (`pcai:ap:relay:<hash>`, operator-signed, like every other AP state doc), so a
worker restart knows what it asked for, and a relay that never answers is asked again -- once an hour,
not on every tick.
"""
from __future__ import annotations

import hashlib
import logging
import time

from app.services import settings_store
from app.services.activitypub import config, convert, state

logger = logging.getLogger(__name__)

_PREFIX = "pcai:ap:relay:"
_RETRY_PENDING = 3600          # a Follow the relay never answered is sent again after this long
_RETRY_REJECTED = 86400        # a relay that refused may approve later (allowlist relays): ask daily
MAX_RELAYS = 20


def _h(url: str) -> str:
    return hashlib.sha256((url or "").encode()).hexdigest()[:24]


def configured() -> list:
    """The relay inboxes an admin listed: https only, unique, in order, at most MAX_RELAYS. A line may
    carry a `# comment`. Anything else is ignored rather than guessed at -- a relay URL is somewhere
    this node will post every public status it has."""
    out = []
    raw = settings_store.get("activitypub_relays", "") or ""
    for line in raw.replace(",", "\n").splitlines():
        u = line.split("#")[0].strip()
        if not u.startswith("https://") or " " in u or len(u) > 300:
            continue
        from app.services.activitypub import remote
        h = remote.host_of(u)
        if not h or "." not in h or config.is_own_host(h) or config.host_blocked(h):
            continue
        if u not in out:
            out.append(u)
    return out[:MAX_RELAYS]


def scope() -> str:
    v = str(settings_store.get("activitypub_relay_scope", "") or "").strip().lower()
    return "everyone" if v == "everyone" else "local"


def follow_id(inbox: str) -> str:
    """Our Follow's id for a relay: the instance actor's, and recognisable as a RELAY follow by its
    shape alone -- the Accept may come back naming only this id."""
    import os
    # Unique even within one millisecond: a relay (like any server) drops an activity id it has seen.
    return f"{config.base_url()}/ap/actor#relay-follow/{_h(inbox)}/{int(time.time() * 1000)}-{os.urandom(4).hex()}"


def is_relay_follow(follow_id_: str) -> bool:
    base = config.base_url()
    return bool(base) and str(follow_id_ or "").startswith(f"{base}/ap/actor#relay-follow/")


async def subscriptions(*, strict: bool = True) -> dict:
    """{inbox: {"state", "follow_id", "at", ...}} for every relay this node has asked, live or not."""
    from app.services import nostr_store
    docs = await nostr_store.list_docs(settings_store._port(), _PREFIX, seckey=settings_store._operator_seckey(None),
                                       strict=strict, limit=1000)
    return {d["inbox"]: d for d in docs.values() if isinstance(d, dict) and d.get("inbox")}


async def _save(inbox: str, doc: dict) -> None:
    await state._put(_PREFIX + _h(inbox), dict(doc, inbox=inbox, at=int(time.time())))


_accepted_cache = {"at": 0.0, "list": []}


async def accepted_inboxes(max_age: float = 60.0) -> list:
    """The relay inboxes that ACCEPTED us and are still listed -- the only ones anything is sent to.
    A failed read keeps the last good answer: "could not ask" is not "no relays", and it is not
    "every relay" either."""
    now = time.monotonic()
    if _accepted_cache["at"] and now - _accepted_cache["at"] < max_age:
        return _accepted_cache["list"]
    listed = set(configured())
    try:
        subs = await subscriptions()
    except Exception:
        return _accepted_cache["list"]
    out = sorted(i for i, d in subs.items() if d.get("state") == "accepted" and i in listed)
    _accepted_cache.update(at=now, list=out)
    return out


def forget_cache() -> None:
    _accepted_cache["at"] = 0.0
    _actors_cache["at"] = 0.0


def relay_hosts_cached() -> set:
    """Hosts of the relays listed in SETTINGS (synchronous). Used only to decide who may FOLLOW the
    instance actor -- never to drop traffic: a Pleroma relay lives on its instance's own host."""
    from app.services.activitypub import remote
    return {remote.host_of(u) for u in configured()}


_actors_cache = {"at": 0.0, "set": frozenset()}


async def relay_actors(max_age: float = 60.0) -> frozenset:
    """The ACTOR ids of the listed relays that answered us -- what the inbox recognises relayed traffic
    by. By actor, not by host: a Pleroma or Akkoma relay is `https://<instance>/relay`, on the host of
    an instance whose people members follow, and dropping by host silenced that whole instance --
    posts, deletions, blocks and DMs. A failed read keeps the last good answer."""
    now = time.monotonic()
    if _actors_cache["at"] and now - _actors_cache["at"] < max_age:
        return _actors_cache["set"]
    try:
        subs = await subscriptions()
    except Exception:
        return _actors_cache["set"]
    listed = set(configured())
    out = frozenset(str(d.get("actor") or "").split("#")[0] for i, d in subs.items()
                    if i in listed and d.get("actor") and d.get("state") in ("accepted", "pending", "rejected"))
    _actors_cache.update(at=now, set=out)
    return out


def carries(ev: dict, member: str) -> bool:
    """Whether this event is one a relay takes: a public TOP-LEVEL post or poll (never a reply, a boost,
    a mirror or a protected event -- the caller has already refused those), by an account in scope."""
    from app.services.activitypub import actors, outbox
    if ev.get("kind") not in (1, 1068) or outbox.parent_of(ev):
        return False
    return actors.is_actor(member) or scope() == "everyone"


async def reconcile() -> int:
    """Make the subscriptions match the list: Follow what is listed and not (recently) asked, Undo what
    was accepted or asked and is no longer listed. Returns how many activities were sent. Runs on the
    delivery tick; a relay document that cannot be read decides nothing."""
    from app.services.activitypub import remote
    base = config.base_url()
    if not base:
        return 0
    try:
        subs = await subscriptions()
    except Exception:
        return 0
    listed = configured()
    me = f"{base}/ap/actor"
    keys = await state.keypair("instance")
    key_id, priv = f"{me}#main-key", keys["priv"]
    sent = 0
    now = int(time.time())
    for inbox in listed:
        cur = subs.get(inbox) or {}
        st = cur.get("state")
        if st == "accepted":
            continue
        if st == "pending" and now - int(cur.get("at") or 0) < _RETRY_PENDING:
            continue
        if st == "rejected" and now - int(cur.get("at") or 0) < _RETRY_REJECTED:
            continue
        # RE-READ before writing: the app process may have recorded the relay's Accept a moment ago,
        # and writing "pending" over it would send one more Follow and stop delivery until answered.
        try:
            again = (await subscriptions()).get(inbox) or {}
        except Exception:
            continue
        if again.get("state") == "accepted":
            continue
        fid = follow_id(inbox)
        follow = {"@context": convert.AS_CONTEXT, "id": fid, "type": "Follow", "actor": me, "object": config.PUBLIC}
        # The last few Follow ids are ALL honoured: a relay may answer an earlier one late, or answer a
        # repeated Follow with nothing because it already considers us subscribed.
        recent = ([fid] + [x for x in (again.get("follow_ids") or []) if isinstance(x, str)])[:5]
        await _save(inbox, {"state": "pending", "follow_id": fid, "follow_ids": recent,
                            "actor": again.get("actor") or ""})
        status = await remote.deliver(inbox, follow, key_id=key_id, private_pem=priv)
        logger.info("[activitypub] relay %s: Follow sent (HTTP %s)", remote.host_of(inbox), status)
        sent += 1
    for inbox, cur in subs.items():
        if inbox in listed or cur.get("state") not in ("accepted", "pending", "rejected"):
            continue
        if cur.get("state") == "rejected":
            # Never subscribed: nothing to undo. Marked removed so listing it again asks afresh.
            await _save(inbox, {"state": "removed", "follow_id": cur.get("follow_id") or ""})
            continue
        fid = cur.get("follow_id") or ""
        undo = {"@context": convert.AS_CONTEXT, "id": f"{fid or me}/undo/{now}", "type": "Undo", "actor": me,
                "object": {"id": fid, "type": "Follow", "actor": me, "object": config.PUBLIC}}
        await _save(inbox, {"state": "removed", "follow_id": fid})
        status = await remote.deliver(inbox, undo, key_id=key_id, private_pem=priv)
        logger.info("[activitypub] relay %s: unsubscribed (HTTP %s)", remote.host_of(inbox), status)
        sent += 1
    if sent:
        forget_cache()
    return sent


async def answer(kind: str, obj, signer: str) -> str:
    """A relay's Accept/Reject of OUR Follow. Believed only from the relay's own host, for a follow we
    recorded, naming the id we sent -- an unsolicited Accept must not make this node post every public
    status it has to somebody's inbox."""
    from app.services.activitypub import remote
    fid = convert.id_of(obj)
    try:
        subs = await subscriptions()
    except Exception:
        return "ignored: relay records unreadable"
    for inbox, cur in subs.items():
        ids = [cur.get("follow_id")] + [x for x in (cur.get("follow_ids") or []) if isinstance(x, str)]
        if not fid or fid not in ids:
            continue
        if remote.host_of(signer) != remote.host_of(inbox):
            return "ignored: answered by somebody other than the relay"
        if inbox not in configured():
            return "ignored: that relay is no longer listed"
        if cur.get("state") in ("accepted", "rejected") and cur.get("state") == ("accepted" if kind == "Accept" else "rejected"):
            return f"relay already {cur.get('state')}"
        await _save(inbox, {"state": "accepted" if kind == "Accept" else "rejected", "follow_id": fid,
                            "follow_ids": ids[:5], "actor": signer})
        forget_cache()
        logger.info("[activitypub] relay %s %sed our subscription", remote.host_of(inbox), kind.lower())
        return f"relay {kind.lower()}ed"
    return "ignored: not a relay follow we sent"


async def follow_back(activity: dict, signer: str) -> str:
    """A LitePub relay (Pleroma's, and activityrelay in that mode) FOLLOWS the instance that subscribed,
    and waits for an Accept before it will take that instance's posts. Only a relay that is LISTED here
    is answered: the instance actor has no followers otherwise, and a stranger's Follow of it grants
    nothing."""
    from app.services.activitypub import remote
    if convert.id_of(activity.get("object")) != f"{config.base_url()}/ap/actor":
        return "ignored: not a follow of the instance actor"
    if remote.host_of(signer) not in relay_hosts_cached():
        return "ignored: the instance actor is followed only by relays it subscribed to"
    who = await remote.actor(signer)
    inbox = remote.inbox_of(who)
    if not inbox or remote.host_of(inbox) != remote.host_of(signer):
        return "ignored: relay has no inbox on its own host"
    me = f"{config.base_url()}/ap/actor"
    keys = await state.keypair("instance")
    accept = {"@context": convert.AS_CONTEXT, "id": f"{me}#accepts/{int(time.time() * 1000)}", "type": "Accept",
              "actor": me, "object": {k: activity[k] for k in ("id", "type", "actor", "object") if k in activity}}
    status = await remote.deliver(inbox, accept, key_id=f"{me}#main-key", private_pem=keys["priv"])
    return f"relay follow accepted (HTTP {status})"
