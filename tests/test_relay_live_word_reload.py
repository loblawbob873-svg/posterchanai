"""Execute shipped live reload and firehose callbacks with signed synthetic notes."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.services.nostr.event import build_event, verify_event
from app.services.nostr_relay import thread, ingest
from app.services.nostr_relay.langfilter import blocked_language, blocked_word

SOURCE = Path(thread.__file__).read_text()

def runtime():
    tree = ast.parse(SOURCE)
    firehose = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == '_firehose_event')
    reload_branch = next(n for n in ast.walk(tree) if isinstance(n, ast.If) and ast.unparse(n.test) == "cmd.get('cmd') == 'reload-blocks'")
    reload_fn = ast.AsyncFunctionDef(name='reload_blocks', args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]), body=reload_branch.body, decorator_list=[])
    cfg = {'blocked_words':set(), 'blocked_langs':set(), 'blocked_pubkeys':[], 'blocked_relays':[], 'operator':[], 'preserve':[], 'block_bridged':False, 'fetch_ancestors':False}
    fresh = {**cfg}
    store = SimpleNamespace(has_event=AsyncMock(return_value=False), add_event=AsyncMock(return_value=True), extend_preserve_pubkeys=Mock())
    gate = Mock();gate.is_member.return_value=True
    server = SimpleNamespace(subs=SimpleNamespace(fanout=Mock()), _send=Mock())
    env = {**vars(thread), 'cfg':cfg, '_bl':cfg['blocked_langs'], '_bw':cfg['blocked_words'], '_read_config':lambda:fresh, 'store':store,'gate':gate,'server':server, 'verify_event':verify_event, 'blocked_word':blocked_word,'blocked_language':blocked_language,'_FH_SEEN':set(),'_fh_mark':Mock()}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[firehose,reload_fn],type_ignores=[])),thread.__file__,'exec'),env)
    return env, fresh, store, server


def test_saved_literal_url_immediately_filters_existing_firehose_and_held_backfills():
    async def scenario():
        env,fresh,store,server=runtime()
        held_words=env['cfg']['blocked_words']
        fresh['blocked_words']={'https://theboard.world'}
        await env['reload_blocks']()
        event=build_event(bytes.fromhex('11'*32),1,'A spam link HTTPS://THEBOARD.WORLD/path?x=1',created_at=100)
        assert verify_event(event)
        await env['_firehose_event'](event)
        assert store.add_event.await_count==0, 'new blocked phrase must reach already-running firehose'
        assert server.subs.fanout.call_count==0
        assert ingest._content_blocked(event,set(),held_words), 'in-flight ancestor/sync work must see reload too'
        # Literal dots/slashes are not regex wildcards; unrelated normal content remains accepted.
        for i,text in enumerate(['https://theboardXworld','https://example.org/theboard.world','https://theboard.world'.replace('https:','http:')]):
            await env['_firehose_event'](build_event(bytes.fromhex('11'*32),1,text,created_at=101+i))
        assert store.add_event.await_count==3
        # Clearing the setting must also reach the original closures, without replacing their set.
        fresh['blocked_words']=set();await env['reload_blocks']()
        await env['_firehose_event'](event)
        assert store.add_event.await_count==4
    asyncio.run(scenario())


def test_live_reload_retains_existing_language_and_plaintext_policy():
    async def scenario():
        env,fresh,store,_=runtime();held=env['cfg']['blocked_langs']
        fresh['blocked_langs']={'ru'};await env['reload_blocks']()
        event=build_event(bytes.fromhex('12'*32),1,'Привет как дела друзья',created_at=100)
        await env['_firehose_event'](event)
        assert store.add_event.await_count==0
        assert ingest._content_blocked(event,held,set())
        # Ciphertext/list metadata is not reinterpreted as a public note by this correction.
        assert not ingest._content_blocked({**event,'kind':1059},held,{'https://theboard.world'})
    asyncio.run(scenario())
