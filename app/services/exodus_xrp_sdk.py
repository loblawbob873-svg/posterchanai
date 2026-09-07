"""Bounded subprocess bridge that keeps XRPL dependencies out of the app interpreter."""
import asyncio
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path

from app.services.exodus_send_service import SendRefused

LIMIT = 4096
TIMEOUT = 10


@contextmanager
def _slot():
    folder = Path(os.environ.get('EXODUS_XRP_SLOT_DIR', 'data/exodus-xrp-slots')).resolve()
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    folder.chmod(0o700)
    fd = None
    try:
        for number in range(2):
            candidate = os.open(folder / str(number), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fd = candidate
                break
            except BlockingIOError:
                os.close(candidate)
        if fd is None:
            raise SendRefused('XRP signing is busy; try again shortly')
        yield
    finally:
        if fd is not None:
            os.close(fd)


async def call(data):
    with _slot():
        return await _call(data)


async def _call(data):
    payload = json.dumps(data, separators=(',', ':')).encode()
    if len(payload) > LIMIT:
        raise SendRefused('XRP signing request is too large')
    python = Path(os.environ.get('EXODUS_XRP_PYTHON', '/usr/local/libexec/pc-exodus/xrp-venv/bin/python'))
    if not python.is_absolute() or not python.is_file():
        raise SendRefused('XRP signing is not installed on this node')
    helper = str(Path(__file__).with_name('exodus_xrp_codec.py'))
    process = None
    try:
        process = await asyncio.create_subprocess_exec(str(python), '-I', helper,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, limit=LIMIT+1,
                    env={'PATH':os.defpath,'LANG':'C.UTF-8'})
        async def exchange():
            process.stdin.write(payload)
            await process.stdin.drain()
            process.stdin.close()
            result = bytearray()
            while len(result) <= LIMIT:
                part = await process.stdout.read(LIMIT + 1 - len(result))
                if not part:
                    break
                result.extend(part)
            if len(result) > LIMIT:
                raise ValueError('Oversized SDK response')
            await process.wait()
            if process.returncode != 0:
                raise ValueError('SDK process failed')
            return json.loads(result)
        result = await asyncio.wait_for(exchange(), TIMEOUT)
        if not isinstance(result, dict):
            raise ValueError('Invalid SDK response')
    except (OSError, ValueError, asyncio.TimeoutError) as error:
        raise SendRefused('XRP signing could not be completed; nothing was submitted') from error
    finally:
        if process is not None:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            async def reap():
                # A full StreamReader pauses its pipe transport. Waiting for process exit
                # without draining that pipe can deadlock even after SIGKILL.
                while await process.stdout.read(LIMIT):
                    pass
                await process.wait()
            try:
                await asyncio.wait_for(reap(), 2)
            except asyncio.TimeoutError:
                # Last-resort closure if an unexpected descendant retained the pipe. There
                # is no public Process.close API; close its asyncio transport without waiting.
                process._transport.close()
    if result.get('error'):
        raise SendRefused(result['error'])
    return result
