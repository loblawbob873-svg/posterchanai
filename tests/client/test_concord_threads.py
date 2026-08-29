"""Concord threads — the presentation half of a protocol that was already correct.

A reply has always gone out as a kind-1111 with an `e` tag: correct NIP-22, and the same wire format
Armada reads. What did not exist was any way to SEE a thread. Replies were flattened into the
channel timeline with one quoted line above them, so several conversations interleaved and you
reconstructed them by eye, and nothing said a message HAD replies — a thread was invisible unless
you happened to scroll past one.

The runtime file drives the shipped helpers; this adds the rules that live in the render path,
where a browser-free harness cannot reach.
"""
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = (ROOT / "static/js/client/concord.js").read_text(encoding="utf-8")


def test_thread_helpers_runtime():
    run = subprocess.run(["node", str(ROOT / "tests/client/concord_threads_runtime.mjs")],
                         cwd=ROOT, text=True, capture_output=True)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "concord threads runtime ok" in run.stdout


def test_a_root_with_replies_offers_a_way_into_them():
    """The affordance is the whole feature: without a count on the root, a thread cannot be found."""
    assert 'data-cc-thread=' in SRC, "no control opens a thread"
    assert "?'reply':'replies'" in SRC, "the count does not say what it is counting"


def test_the_open_thread_is_filtered_to_that_conversation():
    assert "const _t=threadView(messages,state.thread);" in SRC, (
        "opening a thread does not narrow the list, so the replies stay interleaved with the "
        "channel — which is the thing threads exist to stop"
    )


def test_there_is_a_way_back_out():
    assert "cc-thread-back" in SRC, "a thread opens with no way back to the channel"


def test_replying_inside_a_thread_replies_to_the_thread():
    """Otherwise a reply typed inside a thread lands in the channel and starts a second one."""
    handler = SRC.split("$$('[data-cc-thread]')", 1)[1].split("});", 1)[0]
    assert "replyTarget=" in handler, (
        "opening a thread does not point the composer at it, so replying from inside a thread "
        "posts to the channel instead"
    )


def test_a_thread_does_not_survive_a_room_or_channel_change():
    """A thread belongs to one channel. Left set across a move, the filter matches nothing and the
    new channel looks empty — which reads as lost messages, not as a stale filter."""
    for where in ("state.thread=null;state.community=index",
                  "state.channel='general';state.thread=null;",
                  "state.channel=channel; state.thread=null;"):
        assert where in SRC, "a thread survives a transition: %r" % (where,)


def test_the_count_is_hidden_inside_a_thread():
    """Inside a thread the replies are already on screen; offering to open them again is a loop."""
    assert "_replies&&!state.thread" in SRC, (
        "the reply count is still offered while its own thread is open"
    )


# ---------------------------------------------------------------- reading a community at all

def test_an_empty_control_set_is_not_cached_as_the_answer():
    """"CHANNEL IS NOT READABLE WITH THIS MEMBERSHIP", and it was not about membership.

    A community's channels are DEFINED by its control events — Armada's own reader builds
    `channelsView` from `folded.channels`, which comes from those wraps and nothing else. With none
    of them the readable set is empty and `inspectChat` correctly refuses every id.

    `roomControls.set(loadKey, [])` made that permanent for the session: `[]` is truthy, so the
    "have I fetched these?" guard reads it as yes and the room can never recover. The error then
    repeats on every four-second live tick, which is how it was reported. One unreachable relay at
    the wrong moment cost the entire visit.
    """
    assert "if((controlWraps||[]).length) roomControls.set(loadKey,controlWraps);" in SRC, (
        "an empty control-event set is still remembered as the answer, so a community that failed "
        "to load once stays unreadable for the whole session"
    )


def test_a_community_that_could_not_be_read_says_so_instead_of_throwing_every_tick():
    """Silence plus an exception every four seconds is not a report. Say it once, and leave."""
    body = SRC.split("if((controlWraps||[]).length) roomControls.set", 1)[1][:400]
    assert "roomLoadWarning" in body, (
        "a community whose control events did not arrive fails silently apart from an exception "
        "thrown on every live tick"
    )
