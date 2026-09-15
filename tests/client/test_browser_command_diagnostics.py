"""A CDP silence remains a failure with safe, useful command context."""
import asyncio
import json
import pytest
from tests.client.test_effects_full_app import Browser


def test_silent_cdp_reports_command_without_protocol_secrets():
    class SilentSocket:
        def __init__(self):
            self.sent=[]
            self.count=0
        async def send(self, message):
            self.sent.append(json.loads(message))
        async def recv(self):
            self.count+=1
            if self.count<=12:
                return json.dumps({'method':'Network.event'+str(self.count),
                                   'params':{'url':'https://secret.invalid/?token=private'}})
            raise TimeoutError('simulated stalled CDP transport')
    async def exercise():
        ws=SilentSocket()
        with pytest.raises(AssertionError, match='CDP command timed out') as failure:
            await Browser(ws).js('document.querySelector("#private-selector").textContent === `private-value`')
        message=str(failure.value)
        assert 'Runtime.evaluate' in message and 'document.querySelector' in message
        assert 'textContent' in message and '<redacted>' in message
        assert 'private' not in message and 'secret.invalid' not in message
        assert 'Network.event12' in message and 'Network.event1,' not in message
        assert 'Network.event4\'' not in message
        assert len(ws.sent)==1, 'a timeout must not retry the browser command'
        assert isinstance(failure.value.__cause__, TimeoutError)
    asyncio.run(exercise())


def test_successful_cdp_reply_is_unchanged():
    class Socket:
        async def send(self, message):
            self.message=json.loads(message)
        async def recv(self):
            return json.dumps({'id':self.message['id'],'result':{'result':{'value':True}}})
    assert asyncio.run(Browser(Socket()).js('true')) is True
