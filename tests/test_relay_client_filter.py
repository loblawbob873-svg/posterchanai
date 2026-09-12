from types import SimpleNamespace
import pytest
from websockets.datastructures import Headers
from app.services.nostr_relay.server import _posterchan_client_allowed, RelayServer

CFG={'posterchan_clients_only':True,'posterchan_origins':['https://poster.place','app://posterchan','https://localhost','capacitor://localhost']}

# THE GATE HAS THREE ANSWERS NOW, NOT TWO.
#
# True   = a PosterChan client (or the LAN): full service, exactly as before.
# 'signer' = ANOTHER client, admitted onto the socket and CONFINED to NIP-46 (24133) and inbound
#          DMs (4/13/1059). It was False, i.e. a 403 at the handshake, and that refused two things
#          this node cannot do without: the Amber QR login (`nostrconnect://` names OUR relay and
#          the signer dials it, as a native app with no Origin and its own UA), and DM DELIVERY —
#          a sender on Damus or Amethyst could not reach an inbox this relay is listed for, so the
#          message was lost with neither end told.
# False  = nothing at all. Nothing reaches this today; the value is kept because the switch must
#          still be able to refuse outright if a future rule needs it.
#
# What a confined socket may actually DO is the other half of the rule, and it is asserted in
# tests/test_a_remote_signer_can_still_reach_the_relay.py — general reads and writes are refused
# there, which is what keeps this from being "allow everyone".
FULL, CONFINED = True, 'signer'


@pytest.mark.parametrize('headers,peer,allowed',[
    ({'Origin':'https://poster.place'},'8.8.8.8',FULL),
    ({'Origin':'app://posterchan'},'8.8.8.8',FULL),
    ({'Origin':'https://localhost'},'8.8.8.8',FULL),
    ({'Origin':'capacitor://localhost'},'8.8.8.8',FULL),
    ({'User-Agent':'PosterChan/1.0'},'8.8.8.8',FULL),
    ({'User-Agent':'Mozilla/5.0 PosterChanAI/1.0'},'8.8.8.8',FULL),
    ({'User-Agent':'NotPosterChan/1.0'},'8.8.8.8',CONFINED),
    ({'Origin':'https://poster.place.attacker.test'},'8.8.8.8',CONFINED),
    ({'User-Agent':'OtherNostr/1.0'},'8.8.8.8',CONFINED),
    ({},'8.8.8.8',CONFINED),
    ({},'127.0.0.1',FULL),
    ({},'192.168.0.2',FULL),
    ({'X-Real-IP':'8.8.8.8'},'192.168.0.1',CONFINED),
    ({'X-Real-IP':'127.0.0.1'},'8.8.8.8',CONFINED),
])
def test_client_filter(headers,peer,allowed):
    assert _posterchan_client_allowed(CFG,Headers(headers),SimpleNamespace(remote_address=(peer,100))) is allowed


def test_a_lookalike_origin_never_gets_full_service():
    """The security half: a near-miss origin or a forged X-Real-IP must not be treated as one of
    ours. Being CONFINED is not being trusted — it buys NIP-46 and a DM drop, nothing more."""
    for headers, peer in (({'Origin':'https://poster.place.attacker.test'},'8.8.8.8'),
                          ({'X-Real-IP':'127.0.0.1'},'8.8.8.8'),
                          ({'User-Agent':'OtherNostr/1.0'},'8.8.8.8')):
        got = _posterchan_client_allowed(CFG,Headers(headers),SimpleNamespace(remote_address=(peer,100)))
        assert got is not True, f"{headers} was granted FULL service"


def test_duplicate_header_is_not_a_client_identity():
    headers=Headers([('Origin','https://poster.place'),('Origin','https://other.test')])
    assert _posterchan_client_allowed(CFG,headers,SimpleNamespace(remote_address=('8.8.8.8',10))) is not True


def test_default_disabled_preserves_clients():
    assert _posterchan_client_allowed({},Headers(),SimpleNamespace(remote_address=('8.8.8.8',10)))


def test_a_confined_client_is_admitted_and_marked():
    """It used to be handed a 403 here. That is the refusal that broke Amber and inbound DMs, so
    the handshake now proceeds and records that this socket is confined — the restriction moves to
    the per-message check instead of the door."""
    server=RelayServer.__new__(RelayServer);server.cfg=CFG
    request=SimpleNamespace(headers=Headers({'Upgrade':'websocket'}),path='/')
    conn=SimpleNamespace(remote_address=('8.8.8.8',10))
    response=server.process_request(conn,request)
    assert response is None, "a non-PosterChan client is still refused at the handshake"
    assert getattr(conn,'_pcai_signer_only',None) is True, (
        "the socket was admitted WITHOUT being marked confined — it would get full relay service")


def test_a_posterchan_client_is_not_marked_confined():
    server=RelayServer.__new__(RelayServer);server.cfg=CFG
    request=SimpleNamespace(headers=Headers({'Upgrade':'websocket','User-Agent':'PosterChan/1.0'}),path='/')
    conn=SimpleNamespace(remote_address=('8.8.8.8',10))
    assert server.process_request(conn,request) is None
    assert getattr(conn,'_pcai_signer_only',None) is False


def test_bridge_transport_can_read_and_publish_with_filter_enabled():
    import asyncio,json
    from websockets.asyncio.server import serve
    from websockets.asyncio.client import connect
    from websockets.exceptions import InvalidStatus
    from app.services.nostr_store import _ws_query,_ws_publish
    from app.services.nostr.event import build_event
    ev=build_event(b'\x41'*32,1,'bridge transport test',[])
    async def go():
        relay=RelayServer.__new__(RelayServer);relay.cfg=CFG
        async def handler(ws):
            async for raw in ws:
                msg=json.loads(raw)
                if msg[0]=='REQ':
                    await ws.send(json.dumps(['EVENT',msg[1],ev]))
                    await ws.send(json.dumps(['EOSE',msg[1]]))
                elif msg[0]=='EVENT':
                    await ws.send(json.dumps(['OK',msg[1]['id'],True,'']))
        async with serve(handler,'127.0.0.1',0,process_request=relay.process_request) as server:
            port=server.sockets[0].getsockname()[1]
            assert await _ws_publish(port,ev)
            assert (await _ws_query(port,[{'kinds':[1]}]))[0]['id']==ev['id']
            from app.services.nostr.relay import _connect
            async with _connect(f'ws://127.0.0.1:{port}',True,additional_headers={'X-Real-IP':'8.8.8.8'}) as ws:
                await ws.send(json.dumps(['REQ','server',{'kinds':[1]}]))
                assert json.loads(await ws.recv())[0]=='EVENT'
            # A plain client USED to get a 403 here, and that refusal is what stopped an inbound
            # DM from ever reaching an inbox this relay is listed for, and what broke the Amber QR
            # login. It is admitted now and confined per MESSAGE instead — which this stub handler
            # cannot show, because it answers REQ/EVENT itself rather than running the relay's
            # dispatch. The confinement itself is asserted in
            # tests/test_a_remote_signer_can_still_reach_the_relay.py.
            async with connect(f'ws://127.0.0.1:{port}',additional_headers={'X-Real-IP':'8.8.8.8'},proxy=None) as ws:
                await ws.send(json.dumps(['REQ','confined',{'kinds':[24133]}]))
                assert json.loads(await ws.recv())[0]=='EVENT'
            async with connect(f'ws://127.0.0.1:{port}',origin='https://poster.place',additional_headers={'X-Real-IP':'8.8.8.8'},proxy=None) as ws:
                await ws.send(json.dumps(['REQ','web',{'kinds':[1]}]))
                assert json.loads(await ws.recv())[0]=='EVENT'
    asyncio.run(go())
