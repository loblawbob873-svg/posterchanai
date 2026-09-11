"""A COMMUNITY COULD ONLY EVER HAVE THE ONE CHANNEL IT WAS BORN WITH.

Asked for as "we need feature to allow creating new rooms in existing communities", and pointed at
the right answer: "armada has many channels in a community so if they can do it, we should do it the
same".

Our reader already PARSES multi-channel communities — `inspectControl` walks every entity carrying a
`VSK_CHANNEL` head — so Armada writes them with a control event we can already read. What was
missing was the writer: `cord-reader.js` exposed `createBanWrap` and `createMetadataWrap` and no way
to declare a channel, so `createCommunity`'s `#general` was the only one a community could ever hold.

`createChannelWrap` is a structural clone of those two with the channel subkind. Three facts, each
read out of the shipped code rather than assumed:

  * A CHANNEL ID IS FRESH RANDOMNESS. `createCommunity` mints `generalChannelId` with the same
    helper it uses for the owner salt and the root, and the reader treats any entity with a valid
    channel head as a channel — which is exactly why a community can hold many.
  * PRIVATE CHANNELS ARE REFUSED. The first version accepted `private:true` and the channel then did
    not come back from `inspectControl` at all: a private channel needs a membership grant, which is
    a separate control write. A channel invisible to the person who made it reads as broken rather
    than unfinished, so it says so instead.
  * OWNER-ONLY, deliberately stricter than the reader, which accepts anyone holding MANAGE_CHANNELS.
    The owner always holds it through ADMIN_ALL, so refusing everyone else cannot produce an event
    another client would reject; allowing more could.

These RUN the real implementation end to end — mint, add, read back — because the only thing worth
knowing is whether a channel written here is a channel Armada's reader sees.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONCORD = (ROOT / "static/js/client/concord.js").read_text(encoding="utf-8")
READER = (ROOT / "static/js/client/cord-reader.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/concord.css").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _run(body):
    script = ("import { community } from './tests/client/cord_channel_runtime.mjs';\n"
              "const c = await community();\nconst out = {};\n" + body
              + "\nconsole.log('RESULT ' + JSON.stringify(out));")
    done = subprocess.run([NODE, "--input-type=module", "-e", script], cwd=ROOT,
                          capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stderr[-1500:]
    line = [l for l in done.stdout.splitlines() if l.startswith("RESULT ")][-1]
    return json.loads(line[len("RESULT "):])


# ───────────────────────────────── the protocol half, RUN ────────────────────────────────────────

@pytest.mark.skipif(NODE is None, reason="needs node")
def test_a_community_can_hold_many_channels():
    """THE FEATURE. A freshly minted community has only #general; after two writes the reader — the
    same one that reads Armada's communities — sees three."""
    got = _run("""
      out.before = c.channels().map(x => x.name);
      const a = await c.add({ name: 'lounge' });
      out.made = { name: a.name, kind: a.kind, idLen: a.id.length };
      out.after = c.channels().map(x => x.name);
      await c.add({ name: 'random' });
      out.three = c.channels().map(x => x.name);
    """)
    assert got["before"] == ["general"], got
    assert got["made"]["kind"] == 1059, "a channel must be published as a CORD gift wrap"
    assert got["made"]["idLen"] == 64, "a channel id is 32 bytes of hex"
    assert got["after"] == ["general", "lounge"], (
        "the new channel is not readable by the shipped reader — Armada would not see it either")
    assert got["three"] == ["general", "lounge", "random"], (
        "a second channel replaced the first instead of joining it: %r" % (got["three"],))


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_every_channel_gets_its_own_identity():
    """Ids are random, not derived from the name — so two channels never collide, and a renamed one
    keeps its history."""
    got = _run("""
      const a = await c.add({ name: 'one' }), b = await c.add({ name: 'two' });
      out.ids = [a.id, b.id];
    """)
    assert got["ids"][0] != got["ids"][1]
    assert all(re.fullmatch(r"[0-9a-f]{64}", i) for i in got["ids"]), got["ids"]


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_a_private_channel_is_refused_rather_than_made_invisible():
    """MEASURED: accepting `private:true` produced a channel that `inspectControl` did not return —
    invisible even to its author, because a private channel needs a membership grant this writer
    does not issue. Refusing out loud is the honest half of not having that yet."""
    got = _run("""
      try { await c.attempt({ name: 'staff', private: true }); out.refused = false; }
      catch (e) { out.refused = true; out.why = e.message; }
      out.channels = c.channels().map(x => x.name);
    """)
    assert got["refused"] is True, (
        "a private channel was created and will be invisible to everyone including its author")
    assert "grant" in got["why"], got["why"]
    assert got["channels"] == ["general"], "the refused channel was written anyway"


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_only_the_owner_can_write_a_channel():
    got = _run("""
      try { await c.attempt({ name: 'x' }, c.stranger()); out.stranger = 'accepted'; }
      catch (e) { out.stranger = e.message; }
      try { await c.attempt({ name: '   ' }); out.blank = 'accepted'; }
      catch (e) { out.blank = e.message; }
      out.channels = c.channels().map(x => x.name);
    """)
    assert "owner" in got["stranger"], got["stranger"]
    assert "name" in got["blank"], got["blank"]
    assert got["channels"] == ["general"], "a refused write still reached the community"


# ───────────────────────────────── the wiring half ───────────────────────────────────────────────

def test_the_writer_is_the_shape_the_other_control_writers_are():
    """It has to emit what Armada emits, so it is deliberately the same seven lines as the two
    shipped control writers with a different subkind — not a second idea about the wire format."""
    fn = READER.split("async function createChannelWrap(", 1)[1].split("\n  }", 1)[0]
    for needed in ("control(bundle, controlWraps)", "TAG_SUBKIND, VSK_CHANNEL", "TAG_ENTITY",
                   "TAG_EVERSION", "buildRumor(", "sealRumor(", "wrapSeal(seal, group)"):
        assert needed in fn, "createChannelWrap no longer uses %s" % needed
    assert "randomBytes(32)" in fn, "a channel id is no longer fresh randomness"
    assert "createChannelWrap: () => createChannelWrap" in READER, "it is not exported"


def test_only_the_owner_is_offered_the_control():
    """A button that always fails is worse than no button. A local sandbox has no control plane and
    a NIP-29 group is not a CORD community, so neither gets one either."""
    fn = CONCORD.split("function canAddChannel(p,room){", 1)[1].split("\n  }", 1)[0]
    assert "room.local" in fn and "nip29" in fn
    assert "createChannelWrap" in fn, "the control is offered where the writer does not exist"
    assert "viewer.pubkey.toLowerCase()===owner.toLowerCase()" in fn.replace(" ", "")


def test_the_control_exists_and_is_gated_in_the_markup():
    assert 'id="cc-add-channel"' in CONCORD
    assert "canAddChannel(p,room)?" in CONCORD, "the + is drawn for everybody"
    assert ".cc-add-channel{" in CSS, "the control has no stylesheet"


def test_a_failed_write_never_edits_the_saved_room():
    """`reconcileChannels` refuses to rewrite a room from a control read because a PARTIAL read has
    deleted whole channel lists before. The same care applies here: the channel is appended only
    after the relays accept it, and the room is found again by durable identity because signing can
    outlast the active-room index."""
    h = CONCORD.split("const addChannel=$('#cc-add-channel');", 1)[1].split("\n    };", 1)[0]
    assert h.index("relayPublishRoom") < h.index("save(latest)"), (
        "the channel is saved before the relays accept it")
    assert "roomIdentity(item)===roomId" in h, (
        "the room is written back by index, so a channel can land in the wrong community")
    assert "already has #" in h, "creating a duplicate channel name is not refused"
