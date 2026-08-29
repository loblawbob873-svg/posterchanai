"""Focused regressions for Concord's thread reply composer."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text()


def test_empty_reply_stays_in_composer_with_a_readable_error():
    send = APP[APP.index("$('#cmp-send',root).onclick=async()=>"):
               APP.index("// 📊 Poll", APP.index("$('#cmp-send',root).onclick=async()=>"))]
    assert "if(!text && !quote)" in send
    assert "Write a reply first." in send
    assert "ta.focus(); return" in send


def test_reply_parent_preview_opens_the_original_without_per_message_context():
    compose = APP[APP.index("function compose({reply="):APP.index("function _dtLocal", APP.index("function compose({reply="))]
    assert 'class="quoted cmp-parent"' in compose
    assert 'data-open="${enc(o.id)}"' in compose
    assert "closeModal(); openThread(id);" in compose


def test_nip07_boundary_counts_utf8_and_rejects_empty_payloads():
    signer = APP[APP.index("if (mode === 'nip07')"):
                 APP.index("if (mode === 'nip46')", APP.index("if (mode === 'nip07')"))]
    assert "new TextEncoder().encode(text).length" in signer
    assert "if(bytes < 1 || bytes > 65535)" in signer
    assert "window.nostr.nip44.encrypt(r, text)" in signer


def test_replies_keep_thread_participants_mentioned():
    tags = APP[APP.index("function replyTags(parent, id, pk){"):
               APP.index("function niceNip05", APP.index("function replyTags(parent, id, pk){"))]
    assert "parent.tags.filter(t=>t[0]==='p')" in tags
    assert "!seen.has(a)" in tags
