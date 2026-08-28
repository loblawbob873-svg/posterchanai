"""Wire-contract guards for Webxdc launched inside Armada chat scopes."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONCORD = (ROOT / "static/js/client/concord.js").read_text()
WEBXDC = (ROOT / "static/js/client/webxdc.js").read_text()
READER = (ROOT / "static/js/client/cord-reader.js").read_text()


def test_concord_cards_carry_protocol_and_armada_default_session():
    assert "{protocol,room:roomIdentity(room),channel:channelName||'general'" in CONCORD
    assert "`nip29|${room.relay}|${room.groupId}`" in CONCORD
    assert "`concord2|${channel.id}`" in CONCORD
    assert "scope+'|webxdc'" in CONCORD
    # cardHtml serialises the whole launch object; the delegated click reparses it and passes it to open.
    assert 'data-xdc="${enc(JSON.stringify(app))}"' in WEBXDC
    assert "app = JSON.parse(card.dataset.xdc || 'null')" in WEBXDC
    assert "open(app);" in WEBXDC


def test_nip29_webxdc_uses_group_scoped_armada_kinds():
    assert "kinds:[9450],'#h':[ctx.groupId],'#i':[uuid]" in CONCORD
    assert "kind:realtime?24450:9450" in CONCORD
    assert "tags:[['h',ctx.groupId],...tags]" in CONCORD


def test_concord_v2_webxdc_uses_3310_in_durable_and_ephemeral_wraps():
    assert "kind: 3310" in READER
    assert "wrapSeal(seal, channel.current.group, { ephemeral })" in READER
    assert "sealRumor(rumor, 20013, channel.current.group" in READER
    assert "['rt','1']" in CONCORD
    assert "kind=realtime?21059:1059" in CONCORD
    assert "checkChannelBinding(ev, channel.idHex, stream.epoch)" in READER


def test_scoped_sessions_do_not_fall_through_to_global_nip_dc_bus():
    assert "PCConcord.webxdcQuery(this.transport,this.app.uuid)" in WEBXDC
    assert "PCConcord.webxdcPublish(this.transport,this.app.uuid,content,u,false)" in WEBXDC
    assert "PCConcord.webxdcSubscribe(this.transport,this.app.uuid,true,receiveRt)" in WEBXDC
    # Ordinary timeline/post apps retain Ditto NIP-DC interoperability.
    assert "const KIND_UPDATE = 4932" in WEBXDC
    assert "const KIND_REALTIME = 20932" in WEBXDC
