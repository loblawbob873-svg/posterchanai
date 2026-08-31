"""A stale channel id must not turn a valid Concord membership into a write denial."""
from pathlib import Path

SRC = (Path(__file__).resolve().parents[2] / "static/js/client/concord.js").read_text(encoding="utf-8")


def _publish():
    start = SRC.index("async function publishCordNative")
    return SRC[start:SRC.index("function nip29PreviousTags", start)]


def test_native_publish_repairs_the_same_stale_id_that_reads_repair():
    body = _publish()
    assert "not writable with this membership" in body
    assert "queryEnvelopeHistory" in body
    assert "reconcileChannels(reader,bundle,controlWraps||[],room,channelName)" in body
    assert "writeChannel=fixed" in body


def test_repair_never_silently_posts_to_another_named_channel():
    body = _publish()
    assert "channelName" in body
    assert "this channel is no longer available" in body
    assert "live[0]" not in body


def test_sent_wrap_is_cached_under_the_repaired_channel_id():
    assert "envelopeCacheKey(loadKey,writeChannel.id)" in _publish()
