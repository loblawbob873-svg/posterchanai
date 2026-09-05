from types import SimpleNamespace
import pytest
from websockets.datastructures import Headers
from app.services.nostr_relay.server import _posterchan_client_allowed, RelayServer

CFG={'posterchan_clients_only':True,'posterchan_origins':['https://poster.place','app://posterchan','https://localhost','capacitor://localhost']}

@pytest.mark.parametrize('headers,peer,allowed',[
    ({'Origin':'https://poster.place'},'8.8.8.8',True),
    ({'Origin':'app://posterchan'},'8.8.8.8',True),
    ({'Origin':'https://localhost'},'8.8.8.8',True),
    ({'Origin':'capacitor://localhost'},'8.8.8.8',True),
    ({'User-Agent':'PosterChan/1.0'},'8.8.8.8',True),
    ({'User-Agent':'Mozilla/5.0 PosterChanAI/1.0'},'8.8.8.8',True),
    ({'User-Agent':'NotPosterChan/1.0'},'8.8.8.8',False),
    ({'Origin':'https://poster.place.attacker.test'},'8.8.8.8',False),
    ({'User-Agent':'OtherNostr/1.0'},'8.8.8.8',False),
    ({},'8.8.8.8',False),
    ({},'127.0.0.1',True),
    ({},'192.168.0.2',True),
    ({'X-Real-IP':'8.8.8.8'},'192.168.0.1',False),
    ({'X-Real-IP':'127.0.0.1'},'8.8.8.8',False),
])
def test_client_filter(headers,peer,allowed):
    assert _posterchan_client_allowed(CFG,Headers(headers),SimpleNamespace(remote_address=(peer,100))) is allowed


def test_duplicate_header_is_not_a_client_identity():
    headers=Headers([('Origin','https://poster.place'),('Origin','https://other.test')])
    assert not _posterchan_client_allowed(CFG,headers,SimpleNamespace(remote_address=('8.8.8.8',10)))


def test_default_disabled_preserves_clients():
    assert _posterchan_client_allowed({},Headers(),SimpleNamespace(remote_address=('8.8.8.8',10)))


def test_handshake_rejection_message():
    server=RelayServer.__new__(RelayServer);server.cfg=CFG
    request=SimpleNamespace(headers=Headers({'Upgrade':'websocket'}),path='/')
    response=server.process_request(SimpleNamespace(remote_address=('8.8.8.8',10)),request)
    assert response.status_code==403
    assert response.body==b'use a PosterChan client'


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
            with pytest.raises(InvalidStatus) as error:
                async with connect(f'ws://127.0.0.1:{port}',additional_headers={'X-Real-IP':'8.8.8.8'},proxy=None):
                    pass
            assert error.value.response.status_code==403
            assert error.value.response.body==b'use a PosterChan client'
            async with connect(f'ws://127.0.0.1:{port}',origin='https://poster.place',additional_headers={'X-Real-IP':'8.8.8.8'},proxy=None) as ws:
                await ws.send(json.dumps(['REQ','web',{'kinds':[1]}]))
                assert json.loads(await ws.recv())[0]=='EVENT'
    asyncio.run(go())
