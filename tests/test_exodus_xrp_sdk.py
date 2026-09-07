import ast
from pathlib import Path

import pytest

from app.services import exodus_xrp_sdk as SDK
from app.services.exodus_send_service import SendRefused


@pytest.fixture
def anyio_backend(): return 'asyncio'


@pytest.fixture(autouse=True)
def slot_directory(tmp_path,monkeypatch):
    monkeypatch.setenv('EXODUS_XRP_SLOT_DIR',str(tmp_path/'slots'))


@pytest.mark.anyio
async def test_missing_sdk_refuses_without_starting_a_process(monkeypatch):
    monkeypatch.setenv('EXODUS_XRP_PYTHON', '/does-not-exist/xrp-python')
    with pytest.raises(SendRefused, match='not installed'):
        await SDK.call({'operation':'recipient','address':'public-fixture'})


@pytest.mark.anyio
async def test_oversized_input_refused_before_process_creation(monkeypatch):
    async def forbidden(*args, **kwargs): raise AssertionError('process was started')
    monkeypatch.setattr(SDK.asyncio, 'create_subprocess_exec', forbidden)
    with pytest.raises(SendRefused, match='too large'):
        await SDK.call({'private':'a' * 5000})


@pytest.mark.anyio
@pytest.mark.parametrize('mode', ['oversize', 'timeout', 'invalid', 'exit'])
async def test_process_failures_are_bounded_and_do_not_expose_input(tmp_path, monkeypatch, mode):
    import sys
    fake = tmp_path/'sdk-python'
    script = {'oversize':"print('a'*100000)", 'timeout':'import time;time.sleep(20)',
              'invalid':"print('not json')", 'exit':'raise SystemExit(1)'}[mode]
    fake.write_text('#!' + sys.executable + '\n' + script + '\n')
    fake.chmod(0o700)
    monkeypatch.setenv('EXODUS_XRP_PYTHON',str(fake)); monkeypatch.setattr(SDK,'TIMEOUT',.2)
    with pytest.raises(SendRefused) as error:
        await SDK.call({'private':'secret-fixture'})
    assert 'secret-fixture' not in str(error.value)


@pytest.mark.anyio
async def test_private_material_only_travels_over_stdin(tmp_path, monkeypatch):
    import sys
    fake = tmp_path/'sdk-python'
    fake.write_text('#!' + sys.executable + '''
import json,sys,os
data=json.load(sys.stdin)
assert data['private']=='secret-fixture'
assert not any('secret-fixture' in arg for arg in sys.argv)
assert sys.argv[1]=='-I'
assert 'PRIVATE_PROXY_CREDENTIAL' not in os.environ
assert 'PYTHONPATH' not in os.environ
print('{"ok":true}')
''')
    fake.chmod(0o700); monkeypatch.setenv('EXODUS_XRP_PYTHON',str(fake))
    monkeypatch.setenv('PRIVATE_PROXY_CREDENTIAL','secret-proxy')
    monkeypatch.setenv('PYTHONPATH','untrusted-import-path')
    assert await SDK.call({'private':'secret-fixture'}) == {'ok':True}


def test_application_requirements_and_modules_do_not_import_xrp_sdk():
    root = Path(__file__).resolve().parents[1]
    for filename in ('requirements.txt','requirements-nostr.txt'):
        assert not any(line.startswith('xrpl-py') for line in (root/filename).read_text().splitlines())
    for filename in ('exodus_account_send.py','exodus_xrp_sdk.py'):
        parsed=ast.parse((root/'app/services'/filename).read_text())
        imports=[node.module for node in ast.walk(parsed) if isinstance(node,ast.ImportFrom)]
        assert not any(name and name.startswith('xrpl') for name in imports)


def test_two_worker_slots_bound_parallel_signing_and_are_released():
    with SDK._slot(), SDK._slot():
        with pytest.raises(SendRefused,match='busy'):
            with SDK._slot():
                raise AssertionError('third process acquired a slot')
    with SDK._slot():
        pass


@pytest.mark.anyio
async def test_cancellation_kills_child_and_releases_slot(tmp_path,monkeypatch):
    import asyncio
    import sys
    fake=tmp_path/'sdk-python'
    fake.write_text('#!'+sys.executable+'\nimport time\ntime.sleep(20)\n');fake.chmod(0o700)
    monkeypatch.setenv('EXODUS_XRP_PYTHON',str(fake))
    original=asyncio.create_subprocess_exec
    started=asyncio.Event();children=[]
    async def spawn(*args,**kwargs):
        child=await original(*args,**kwargs);children.append(child);started.set();return child
    monkeypatch.setattr(SDK.asyncio,'create_subprocess_exec',spawn)
    task=asyncio.create_task(SDK.call({'operation':'recipient'}))
    await asyncio.wait_for(started.wait(),2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task,3)
    assert children[0].returncode is not None
    with SDK._slot(),SDK._slot():
        pass
