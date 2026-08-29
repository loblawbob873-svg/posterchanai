import json
import subprocess
from pathlib import Path


HERE = Path(__file__).resolve().parent


def test_cached_auth_without_a_bearer_repeats_nostr_login():
    """The user-shaped cache cannot authorize settings/mail; only the login credential can."""
    p = subprocess.run(
        ['node', str(HERE / 'ai_session_tokenless_runtime.mjs')],
        text=True, capture_output=True, check=True,
    )
    got = json.loads(p.stdout)
    assert got == {
        'first': 'cached-without-token',
        'second': 'renewed',
        'posts': 2,
        'token': 'fresh-token',
    }
