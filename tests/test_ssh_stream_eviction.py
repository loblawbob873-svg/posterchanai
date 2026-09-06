import asyncio
from app.services import ssh_keeper, ssh_service

def test_buffer_eviction_while_streaming_does_not_replay_retained_output(monkeypatch):
    monkeypatch.setattr(ssh_service,'REPLAY_MAX',4)
    monkeypatch.setattr(ssh_keeper,'OUT_CHUNK',2)
    session=ssh_service.SshSession(user_id=1)
    session._push(b'AAAA')
    frames=[]
    async def send(writer,frame):
        frames.append(frame)
        if len(frames)==1:
            # The PTY keeps draining while a slow browser blocks the previous network write.
            session._push(b'BBBBBBBB')
    monkeypatch.setattr(ssh_keeper,'_send',send)
    asyncio.run(ssh_keeper._stream(session,None,0,asyncio.Event()))
    out=[f for f in frames if f['t']=='out']
    assert ''.join(f['d'] for f in out)=='AABBBB'
    assert [f['seq'] for f in out]==[2,10,12]
