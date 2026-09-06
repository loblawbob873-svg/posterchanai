"""Render new and already-delivered application DMs through the shipping linkifier."""
import json
import subprocess
from pathlib import Path

from tests.client.test_linkify_urls import _harness, _fn
from app.services.nostr.nostr_service import npub_of

ROOT = Path(__file__).resolve().parents[2]


def test_new_and_legacy_application_dms_have_openable_profile_mentions():
    pk = 'ab' * 32
    npub = npub_of(pk)
    harness = _harness()
    # Use the bundled real NIP-19 decoder, including checksum verification.
    start = harness.index('const NT = ')
    end = harness.index('const Store = ', start)
    harness = harness[:start] + 'const NT = () => globalThis.NostrTools;\n' + harness[end:]
    source = (ROOT / 'static/js/client/app.js').read_text()
    script = (ROOT / 'static/vendor/nostr/nostr.bundle.js').read_text() + '\n' + harness
    script += '\nconst window=globalThis;const applyEmojis=x=>x;\n' + _fn(source, '_dmBodyHtml', 'function _dmBodyHtml(m){')
    script += '\nconst inputs=' + json.dumps(['nostr:' + npub, 'nostr:' + pk]) + ';'
    script += '\nprocess.stdout.write(JSON.stringify(inputs.map(text=>_dmBodyHtml({id:"fixture",text}))));'
    result = subprocess.run(['node', '-'], input=script, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr[-1000:]
    for rendered in json.loads(result.stdout):
        assert 'class="mention"' in rendered and f'data-np="{npub}"' in rendered
        assert f'nostr:{pk}' not in rendered
