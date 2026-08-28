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
    assert "webxdc-url-realtime-v1:${canonicalXdcUrl(url)}:${messageId}" in WEBXDC
    assert "PCWebxdc.deriveUrlTopic(url,messageId)" in CONCORD
    assert "scope+'|webxdc'" not in CONCORD
    # cardHtml serialises the whole launch object; the delegated click reparses it and passes it to open.
    assert 'data-xdc="${enc(JSON.stringify(app))}"' in WEBXDC
    assert "app = JSON.parse(card.dataset.xdc || 'null')" in WEBXDC
    assert "open(app);" in WEBXDC


def test_social_uploads_publish_the_canonical_topic_contract():
    app = (ROOT / "static/js/client/app.js").read_text()
    assert "m: MIME_VENDOR" in WEBXDC
    assert "'webxdc-topic':uuid, webxdc:uuid" in WEBXDC
    assert "m['webxdc-topic']" in app
    assert "webxdc-topic '+m['webxdc-topic']" in app
    assert "application/vnd.webxdc+zip" in app


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


def test_running_app_and_realtime_echo_are_scoped_like_armada():
    # Armada identifies an active app by conversation scope AND its attachment session id. A UUID
    # alone can otherwise focus the still-running iframe from another room after forwarding.
    assert "_transportKey(app && app.transport)" in WEBXDC
    assert "['concord2', t.room || '', t.channelId || t.channel || ''].join('|')" in WEBXDC
    assert "['nip29', t.relay || '', t.groupId || ''].join('|')" in WEBXDC
    # Concord and NIP-29 realtime frames are signed by the member account. The generic webxdc bus
    # uses a throwaway rt key; checking only that key feeds every chat-scoped packet back to sender.
    assert "this.accountPk = (me && me.pubkey) || ''" in WEBXDC
    assert "this.accountPk && ev.pubkey === this.accountPk" in WEBXDC


def test_ioquake_host_election_waits_for_the_armada_wire_subscription():
    assert "rtReady = rpc('webxdc.rtJoin', {})" in WEBXDC
    assert "rtReady.then(function(){ if(joined) rpc('webxdc.rtSend'" in WEBXDC
    assert "const sub=await PCConcord.webxdcSubscribe" in WEBXDC
    assert "this._rtJoinReady.then(()=>this.reply(id,null)" in WEBXDC


def test_concord_realtime_serializes_member_signing_and_keeps_newest_packet():
    """A browser signer cannot safely service ioquake's overlapping 300ms election burst.

    The chat-scoped path must enter the same bounded newest-wins pump as ordinary Webxdc instead of
    returning early with an unobserved promise for every packet.
    """
    body = WEBXDC.split("Session.prototype.rtSend = function(b64){", 1)[1].split(
        "Session.prototype.mount", 1
    )[0]
    assert "this._rtNext = b64" in body
    assert "if(this._rtBusy) return" in body
    assert "await PCConcord.webxdcPublish" in body
    assert ".webxdcPublish(this.transport,this.app.uuid,b64" not in body
    assert "room realtime send failed" in body


def test_room_realtime_listens_on_managed_and_external_relays():
    """A room relay already present in Relay._conns is intentionally skipped by subscribeFrom.

    Concord must therefore install a normal pooled subscription too; otherwise the helper returns a
    closer successfully while listening to zero sockets, which split Armada and PosterChan games.
    """
    assert "const pooled=R.subscribe(filters,{onEvent:receive})" in CONCORD
    assert "external=R.subscribeFrom(urls,filters,{onEvent:receive})" in CONCORD
    assert "R.waitForSubscription(pooled,urls)" in CONCORD
    assert "if(external.hasTargets&&external.ready)" in CONCORD
    assert "await Promise.any(gates)" in CONCORD
    assert "const close=()=>{try{R.close(pooled);}" in CONCORD
    assert "close.publish=event=>(R.publishFastTo&&R.publishFastTo(urls,event)?1:0)" in CONCORD
    assert "liveSub&&liveSub.publish?liveSub.publish(made.wrap):0" in CONCORD
    assert "relayPublishFastTo: (relays, ev) => Relay.publishFastTo(relays, ev)" in (ROOT / "static/js/client/app.js").read_text()
    assert "typeof this.rtSub==='function'?this.rtSub():Relay.close(this.rtSub)" in WEBXDC
