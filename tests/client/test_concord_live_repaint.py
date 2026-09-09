"""A LIVE MESSAGE MUST APPEAR WHILE SOMEBODY IS TYPING.

Reported as "new concord room messages are only appearing after I send or switch rooms".
backgroundRender() refuses to paint while focus is inside the composer — correctly, because
render() rebuilds the workspace and a replaced textarea loses focus, which on Android closes the
soft keyboard mid-sentence. Refusing the WHOLE paint made the live stream invisible for as long as
the cursor sat in the message box, which is the ordinary state of somebody in a chat.

Driven under node against a stub document rather than asserted as source text: what was broken is a
paint that did not happen, and a deferral looks like nothing at all in the source.
"""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_a_message_arriving_mid_sentence_is_drawn_and_the_composer_is_not_replaced():
    result = subprocess.run(['node', str(ROOT / 'tests/client/concord_live_repaint_runtime.mjs')],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
