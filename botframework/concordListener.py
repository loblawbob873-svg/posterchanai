"""ANSWER WHEN MENTIONED, INSIDE AN ENCRYPTED CONCORD ROOM.

`concord.py` could already open an invite, list channels, read a channel and build an outgoing
wrap — everything except the part an operator actually wanted, which is a bot that SITS IN A ROOM
AND REPLIES. Reported twice: "we need our bot framework to work with concord rooms", and then "i
still do not see a way to make a concord bot respond when mentioned". The bridge was half a
feature, and half a feature is reported as none.

WHAT IS DIFFERENT FROM `--nostr`, and it is the whole design:

  * A room's traffic is kind-1059 GIFT WRAPS on the ROOM'S OWN RELAYS, named by its bundle. That
    is a deliberate exception to "bots publish to the local relay only" — the local relay's outbox
    federates kind-1/profiles/DMs, so a wrap handed to it would reach the room's members never. The
    exception is narrow: room traffic goes to room relays, everything else this bot does is
    unchanged.
  * The node CANNOT read any of it. The invite's `#` fragment is the room key, the bot decrypts in
    its own process, and the reply is sealed again before it goes out. So there is no server-side
    moderation hook here and no transcript anywhere — which is also why nothing in this file logs
    message text.
  * There is no `p` tag to match on. A wrap is addressed to the room, not to a person, so "was I
    mentioned" is a question about the DECRYPTED text: the bot's npub, or its display name behind
    an @. `mentions()` is deliberately strict — a bot that answers everything in a community is a
    bot that gets removed from it.

IT NEVER SPEAKS UNPROMPTED. No random replies, no greetings, no summaries. One inbound mention,
one reply, and only for messages newer than the moment it started.
"""

from __future__ import annotations

import logging
import os
import re
import time

import concord as _cc

logger = logging.getLogger(__name__)

#: Room history is pulled in one REQ per pass. A community is chattier than a mention feed, but the
#: bot only ever looks at what arrived since its last pass, so this is a ceiling and not a target.
WRAP_LIMIT = 400
#: The control stream is small (channel creations, roster grants, metadata) but it is the whole map
#: of the room, so it is read generously rather than tightly.
CONTROL_LIMIT = 1000
#: How far back a FRESH start will look. A bot restarting must not answer yesterday's mentions —
#: the room has moved on and a burst of late replies is the most visible way to be a nuisance.
COLD_START_SECONDS = 300


def _seen_path() -> str:
    return os.getenv("CONCORD_SEEN_FILE") or ".concord_seen.json"


class _Seen:
    """Message ids already answered, so a restart cannot re-answer them.

    Kept as a bounded set on disk beside the bot's other state. A miss here costs a duplicate
    reply, never a missed one — the failure direction that is merely embarrassing rather than the
    one that makes the bot look dead.
    """

    def __init__(self, path: str, cap: int = 4000) -> None:
        self.path, self.cap, self.ids = path, cap, []
        self._set: set[str] = set()
        try:
            import json
            with open(path, "r", encoding="utf-8") as fh:
                self.ids = [str(x) for x in (json.load(fh) or [])][-cap:]
            self._set = set(self.ids)
        except Exception:
            pass

    def has(self, mid: str) -> bool:
        return mid in self._set

    def add(self, mid: str) -> None:
        if mid in self._set:
            return
        self._set.add(mid)
        self.ids.append(mid)
        if len(self.ids) > self.cap:
            drop = self.ids[:-self.cap]
            self.ids = self.ids[-self.cap:]
            self._set.difference_update(drop)
        try:
            import json
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self.ids, fh)
        except Exception as e:
            logger.warning(f"[concord] could not persist seen ids: {e}")


def mentions(text: str, npub: str, pubkey_hex: str, names: list[str]) -> bool:
    """Was THIS bot addressed?

    Three ways somebody can name it, and no fourth: its npub, its hex pubkey, or `@name` for one of
    the names it answers to. A bare name with no `@` does NOT count — bots are given words like
    "chess" and "news" for names, and a community that says those words in ordinary conversation
    would be answered all day.
    """
    if not text:
        return False
    low = text.lower()
    if npub and npub.lower() in low:
        return True
    if pubkey_hex and pubkey_hex.lower() in low:
        return True
    for name in names:
        if re.search(_handle_re(name), low):
            return True
    return False


def _handle_re(name: str) -> str:
    """`@name` on a word boundary — WITH THE SPACES IN THE NAME TREATED AS A HANDLE.

    A bot's display name is "PosterChan AI" and nobody can type a space into an @-mention, so what
    people actually write is `@PosterChan_AI`. Matched literally, that name could only ever be hit
    by `@PosterChan AI`, which no client offers and nobody types: measured in a real room, a request
    addressed to `@PosterChan_AI` was not a mention at all and the bot ignored it in silence.
    The separator is therefore any run of space/underscore/hyphen/dot, or none at all
    (`@PosterChanAI`), which is every spelling of the same handle.

    Still deliberately requires the `@`: bots are named things like "chess" and "news", and a
    community saying those words in conversation must not be answered all day.
    """
    name = (name or "").strip().lower()
    if not name:
        return r"(?!)"                       # matches nothing
    body = r"[\s_.\-]*".join(re.escape(w) for w in name.split())
    return r"(^|[^\w@])@" + body + r"(?![\w-])"


class RoomSession:
    """One joined room, kept open across passes.

    Opening an invite costs a bundle fetch, a decrypt and a control pass, so it is done once and
    reused. `refresh_controls` re-reads the control stream every pass because channels can be ADDED
    to a community while the bot is sitting in it — a bot that only knows the channels that existed
    when it started goes quietly deaf in exactly the room that is growing.
    """

    def __init__(self, room: _cc.Room, relays: list[str]) -> None:
        self.room, self.relays = room, relays
        self.channels: list[dict] = []
        self.control_pubkeys: list[str] = []

    def refresh_controls(self, query) -> list[dict]:
        # The seed pass: with no wraps the bundle can still say WHO publishes its control stream,
        # which is the only way to know what to ask a relay for.
        seed = self.room.inspect([])
        self.control_pubkeys = [p for p in (seed.get("controlPubkeys") or []) if p]
        wraps = query(self.relays, [{"kinds": [1059], "authors": self.control_pubkeys,
                                     "limit": CONTROL_LIMIT}]) if self.control_pubkeys else []
        info = self.room.inspect(wraps)
        self.channels = list(info.get("channels") or [])
        return self.channels


def open_room(room: _cc.Room, query) -> RoomSession:
    """Fetch the invite's bundle from its bootstrap relays and open it."""
    boot = room.bootstrap_relays()
    signer = room.link_signer()
    if not boot:
        raise _cc.ConcordError("this invite names no relays to fetch its bundle from")
    flt = {"kinds": [33301], "limit": 20}
    if signer:
        flt["authors"] = [signer]
    events = query(boot, [flt])
    if not events:
        # "The room is gone" and "we could not ask" read identically to an operator, and only one
        # of them is worth re-pasting the link over. Say which this is.
        raise _cc.ConcordError(
            "no community bundle came back from " + ", ".join(boot[:3])
            + " — either those relays are unreachable from this node, or the invite was revoked")
    opened = room.open(events)
    relays = [r for r in (opened.get("relays") or []) if r] or boot
    session = RoomSession(room, relays)
    session.refresh_controls(query)
    return session


def _strip_address(text: str, npub: str, names) -> str:
    """The message with the way it addressed us removed, so `geni a cat` is what the dispatcher sees.

    A room message names the bot before it asks for anything — "@PosterChan AI geni a cat" — and
    every command in `_dispatch` is matched from the START of the string (`lower.startswith(...)`).
    Left in, the address makes every command an ordinary sentence and the bot answers prose about a
    cat instead of drawing one.
    """
    out = str(text or "")
    for n in sorted([n for n in (names or []) if n], key=len, reverse=True):
        # The SAME spelling rule `mentions()` matched on, or a message it accepted as addressed to
        # us keeps the address in its body and every command reads as prose.
        body = r"[\s_.\-]*".join(re.escape(w) for w in n.strip().split())
        out = re.sub(r"@?" + body + r"(?![\w-])[:,]?", " ", out, flags=re.I)
    if npub:
        out = re.sub(r"(nostr:)?" + re.escape(npub) + r"\b", " ", out, flags=re.I)
    return re.sub(r"\s+", " ", out).strip()


def _is_command(body: str) -> bool:
    """Does this ask for a FEATURE, or is it just talking to us?

    Deliberately a whitelist of the dispatcher's own openers. Anything else — which is almost
    everything anyone says to a bot — goes to the ordinary generator, so adding commands here can
    never turn ordinary conversation into a tool call.
    """
    low = str(body or "").strip().lower()
    if not low:
        return False
    if low in ("help", "/help", "commands", "?"):
        return True
    if low in ("screenshot", "shot", "ss", "ytdl"):
        return True
    if low.startswith(("screenshot ", "shot ", "ss ", "ytdl ", "search ", "images ", "news ")):
        return True
    if "geni" in low or "/narrate" in low:
        return True
    return any(low == c or low.startswith(c + " ") for c in _media_commands())


def _media_commands():
    """Read from `bot_commands` at CALL time, never copied into a literal here — a second hand-typed
    list of ~84 effect names is exactly how the Telegram copies drifted (see CLAUDE.md)."""
    try:
        from bot_commands import MEDIA_COMMANDS
        return MEDIA_COMMANDS
    except Exception:
        return ()


class Wire:
    """The relay I/O this listener needs, in one injectable object.

    It exists so the listener can be RUN in a test against a real minted community instead of
    being reasoned about. Everything above this line is pure decision-making on decrypted content;
    everything below it is sockets — and the half that was wrong three times while this was being
    written (what `say` returns, whether `at` is seconds or milliseconds, where `streamPubkeys`
    live) is precisely the half a test with no relay can never check.
    """

    def __init__(self, query, publish, identity, generate, dispatch=None):
        self.query, self.publish = query, publish
        self.identity, self.generate = identity, generate
        # The shared command dispatcher (nostrListener._dispatch bound to this transport). None in
        # a test that only cares about plain replies.
        self.dispatch = dispatch


def live_wire() -> Wire:
    """The real thing: this node's relay stack and this bot's own generator."""
    import nostr as _mk                      # heavy import, kept off module import (see main.py)
    from app.services.nostr import nostr_service as _svc

    def query(relays, filters):
        try:
            return _mk._run(_svc.relay.query(relays, filters)) or []
        except Exception as e:
            logger.warning(f"[concord] relay query failed: {e}")
            return []

    def publish(relays, event):
        # ROOM RELAYS, NOT THE LOCAL ONE. Bots publish to ws://127.0.0.1:3052 everywhere else here
        # and let the relay's outbox federate — but its outbox broadcasts kind-1/profiles/DMs, so a
        # room's gift wrap handed to it would reach that room's members never. The exception is
        # this one call, and only for traffic addressed to a room.
        return _mk._run(_svc.relay.publish(relays, event))

    def identity():
        own = _mk.get_own_account() or {}
        names = [n for n in (os.getenv("NOSTR_PROFILE_NAME"), os.getenv("BOT_NAME"),
                             own.get("name"), own.get("display_name")) if n]
        return str(own.get("npub") or ""), str(own.get("pubkey") or ""), names

    def generate(text, msg):
        """The SAME generator its Nostr mentions use — a bot with one personality on the timeline
        and another in a room is two bots wearing one name.

        That is `ai.generate_reply`, which is what `nostrListener` calls for every mention. This
        used to import a `generate_message` from `bot_commands` that HAS NEVER EXISTED in this
        package, and the failure was perfectly quiet: the import raised, the except logged at
        WARNING — to a logger the bot never configures a handler for — `generate` returned "", and
        `process_mentions` read that as "nothing to say" and moved on. So the bot joined the room,
        read it, matched the mention, and then said nothing, every time, with the operator's only
        evidence being silence in a chat room.

        `ping=False` because a room reply is a reply, not a greeting. The sender's name is passed
        as the previous turn's speaker so the model answers a person rather than a wall of text.
        """
        try:
            from ai import generate_reply, is_ai_configured
        except Exception as e:
            logger.warning(f"[concord] no generator available: {e}")
            return ""
        if not is_ai_configured():
            # A bot with no model configured cannot answer, and saying so once is the difference
            # between "misconfigured" and "broken" for whoever is reading the log.
            logger.warning("[concord] no AI is configured for this bot — it cannot answer mentions")
            return ""
        who = str(msg.get("by") or "").strip()
        try:
            return (generate_reply(text, previous_content=(f"{who} says:" if who else None),
                                   ping=False) or "").strip()
        except Exception as e:
            logger.warning(f"[concord] generate_reply failed: {type(e).__name__}: {e}")
            return ""

    def dispatch(body, send, sender_key):
        """nostrListener's dispatcher, wearing the room transport. `media_ok=False`: a room's
        inbound attachments are encrypted per room, so the file/effect commands say so rather than
        silently doing nothing."""
        import nostrListener as _nl
        try:
            _nl._dispatch(None, body, None, None, reply=send, sender_key=sender_key,
                          media_ok=False)
        except Exception as e:
            logger.warning(f"[concord] command {body.split(' ')[0]!r} failed: "
                           f"{type(e).__name__}: {e}")

    return Wire(query, publish, identity, generate, dispatch)


def _imeta(url: str, mime: str, name: str) -> list:
    """A PUBLIC room attachment, in the shape `concord.js:publicAttachments` actually parses.

    Fields are "key value" strings inside one `imeta` tag, and the parser REFUSES anything carrying
    an `encryption-algorithm` or a non-https url — so a bot's picture must be plain and hosted, not
    Armada-encrypted. (Room attachments a PERSON posts are encrypted per room; a bot cannot mint
    that without the room's file key, which is why its media goes out public and its text does not.)
    """
    return ["imeta", f"url {url}", f"m {mime}", f"name {name}"]


def _room_reply(room, cid: str, publish, relays):
    """The Concord transport for `nostrListener._dispatch`.

    Same answers as on the timeline, delivered into the room: any bytes the command produced are
    uploaded to this bot's configured media host and named in an `imeta` tag, then the whole thing
    goes out as ONE message — a picture and its caption arriving as two messages reads as a bot
    talking to itself.
    """
    def send(text: str = "", image_bytes=None, video_bytes=None, audio_bytes=None, **_ignored):
        import nostr as _mk
        from app.services.nostr import media as _media
        from nostr import _SECKEY, _MEDIA_CFG      # the bot's own identity + media host

        # THE SHAPES ARE NORMALISED BY `nostr._to_media_list`, NEVER BY A SECOND COPY HERE.
        # A command hands back bytes, a LIST of bytes, or a list of (bytes, mime) depending which
        # command it was — measured: a first version of this iterated `image_bytes` directly, which
        # walks a bare `bytes` one INTEGER at a time, and every geni reply died as
        # "object supporting the buffer API required" after the picture had already been generated.
        items = _mk._to_media_list(image_bytes, video_bytes, audio_bytes)

        tags = []
        for i, (data, mime) in enumerate(items):
            if not data:
                continue
            try:
                info = _mk._run(_media.upload(_MEDIA_CFG, _SECKEY, data, mime))
            except Exception as e:
                logger.warning(f"[concord] media upload failed: {type(e).__name__}: {e}")
                continue
            url = info.get("url")
            if not url:
                continue
            ext = (mime.split("/")[-1] or "bin").split(";")[0]
            tags.append(_imeta(url, info.get("mime") or mime, f"reply{i}.{ext}"))

        if items and not tags:
            # THE PICTURE WAS MADE AND THEN LOST, AND THE CAPTION MUST NOT CLAIM OTHERWISE.
            # `geni`'s caption is "Here is your image. Hope you like it." — sent alone after every
            # upload failed, that is the bot stating something untrue about work it really did do,
            # and the reader has no way to tell it from a client that failed to render.
            logger.warning("[concord] %d file(s) were made but none could be uploaded", len(items))
            text = ((text + "\n\n") if text else "") + (
                "⚠️ I made that but couldn't upload it just now — ask me again in a moment.")
        if not text and not tags:
            # Nothing to say AND nothing to show. Saying so is better than a silent no-op, which is
            # the failure shape this whole listener has already been bitten by once.
            logger.warning("[concord] a command produced neither text nor media — nothing sent")
            return
        made = room.say(cid, text or "", tags=tags or None)
        wrap = made.get("wrap")
        if not wrap:
            logger.warning("[concord] the bridge built no wrap to publish")
            return
        publish(relays, wrap)
    return send


def process_mentions(state: dict | None = None, wire: Wire | None = None) -> int:
    """One pass: read every readable channel, answer what named us. Returns replies sent."""
    w = wire or (state or {}).get("wire") or live_wire()
    query = w.query

    st = state if state is not None else _STATE
    room = st.get("room")
    if room is None:
        room = _cc.from_env()
        if room is None:
            return 0
        st["room"] = room
    session = st.get("session")
    if session is None:
        session = open_room(room, query)
        st["session"] = session
        # A fresh session answers only what arrives from now on (minus a small grace), never the
        # backlog it just decrypted.
        st.setdefault("floor_ms", int(time.time() * 1000) - COLD_START_SECONDS * 1000)
        logger.info(f"[concord] joined {room!r} — {len(session.channels)} channel(s)")

    seen: _Seen = st.setdefault("seen", _Seen(_seen_path()))
    npub, pk_hex, names = w.identity()

    sent = 0
    for channel in session.refresh_controls(query):
        cid = str(channel.get("id") or "")
        streams = [p for p in (channel.get("streamPubkeys") or []) if p]
        if not cid or not streams:
            continue
        # A channel's traffic is published by its STREAM groups, which is what the web client asks
        # for too. There is no `#h` tag to filter on — the channel binding is inside the seal, so
        # the authors are the only server-side selector a room's privacy allows.
        wraps = query(session.relays, [{"kinds": [1059], "authors": streams, "limit": WRAP_LIMIT}])
        if not wraps:
            continue
        try:
            read = session.room.read(cid, wraps)
        except _cc.ConcordError as e:
            logger.warning(f"[concord] could not read #{channel.get('name')}: {e}")
            continue
        for msg in (read.get("messages") or []):
            mid = str(msg.get("id") or "")
            # `at` is MILLISECONDS (foldTimeline's `ms`), not a unix second. Comparing it against a
            # seconds floor makes every message look like the distant future, i.e. the bot answers
            # the entire backlog on its first pass — in a community, from a stranger's bot.
            at_ms = int(msg.get("at") or 0)
            text = str(msg.get("text") or "")
            author = str(msg.get("pubkey") or "")
            if not mid or seen.has(mid) or at_ms < int(st.get("floor_ms") or 0):
                continue
            if author and pk_hex and author.lower() == pk_hex.lower():
                continue                      # never answer ourselves
            if not mentions(text, npub, pk_hex, names):
                continue
            try:
                # GENERATE FIRST, THEN MARK. The mark exists so a crash mid-PUBLISH cannot make the
                # next pass answer twice — but marking before the generator ran meant a generator
                # that produced nothing (no model configured, a bad import, a timeout) consumed the
                # mention for ever. The bot then stayed silent about that question even once the
                # generator was fixed, which is how a two-line import bug looked like a dead bot.
                # Nothing has been said yet at this point, so there is nothing to double-send.
                # EVERY COMMAND THE TIMELINE BOT HAS, BEHIND THE MENTION GATE.
                # `_dispatch` is nostrListener's — one implementation, two transports — so `geni`,
                # `search`, `images`, `news`, `screenshot`, `ytdl`, `/narrate` and `help` answer
                # here exactly as they do on Nostr. It is only ever reached for a message that
                # NAMED this bot (the `mentions()` guard above), which is the rule that keeps a bot
                # welcome in somebody else's room.
                body = _strip_address(text, npub, names)
                if w.dispatch is not None and _is_command(body):
                    w.dispatch(body, _room_reply(session.room, cid, w.publish, session.relays),
                               author)
                    seen.add(mid)
                    sent += 1
                    continue
                reply = w.generate(text, msg)
                if not reply:
                    continue                  # deliberately NOT marked — ask us again and we'll try
                seen.add(mid)                 # committed: from here a failure must not re-answer
                made = session.room.say(cid, reply)
                wrap = made.get("wrap")
                if not wrap:
                    logger.warning("[concord] the bridge built no wrap to publish")
                    continue
                w.publish(session.relays, wrap)
                sent += 1
            except Exception as e:
                logger.warning(f"[concord] reply failed in #{channel.get('name')}: {e}")
    return sent


_STATE: dict = {}
