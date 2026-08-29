from pathlib import Path


ROOT=Path(__file__).resolve().parents[2]
APP=(ROOT/'static/js/client/app.js').read_text()


def test_nip07_owner_boundary_reports_operation_and_utf8_size_without_unhandled_rejection():
    block=APP[APP.index("if (window.nostr && window.nostr.nip44)"):APP.index('return s;', APP.index("if (window.nostr && window.nostr.nip44)"))]
    assert "e.nip44={op:'decrypt',bytes}" in block
    assert "e.nip44={op:'encrypt',bytes}" in block
    assert 'new TextEncoder().encode' in block
    assert 'p.catch(()=>{})' in block
    assert 'store large data as an attachment' in block


def test_major_document_owners_do_not_pass_default_empty_ciphertext():
    # Corrupt events are skipped/reported by their owner rather than normalized to a crypto request.
    owners=['notes.js','budget.js','concord.js','sms.js']
    for name in owners:
        src=(ROOT/'static/js/client'/name).read_text()
        assert "nip44dec(ME().pubkey, ev.content||'')" not in src
    # Calendar and Mail do not own NIP-44 documents; their encrypted storage is server-side.
    assert 'nip44' not in (ROOT/'static/js/client/calendar.js').read_text().lower()
    assert "nip44dec(ME.pubkey, ev.content || '')" not in APP
    assert "nip44dec(ME.pubkey, ev.content||'')" not in APP
    assert 'DM cache key event is empty or corrupt' in APP


def test_public_app_crypto_boundary_rejects_invalid_nip44_before_every_signer_mode():
    """Submodules must be safe even with an old browser extension/provider installed."""
    export=APP[APP.index('window.__PC = {'):]
    assert "signer.nip44dec(peer, ct)" in export
    assert "signer.nip44enc(peer, text)" in export
    crypto=export[export.index('nip44dec:'):export.index('// Blossom upload', export.index('nip44dec:'))]
    assert "new TextEncoder" not in crypto
    guard=APP[APP.index('function _guardSignerNip44'):APP.index('function makeSigner', APP.index('function _guardSignerNip44'))]
    assert "new TextEncoder().encode" in guard
    assert "p.catch(()=>{})" in guard


def test_news_background_read_save_observes_async_publish_failure():
    news=(ROOT/'static/js/client/news.js').read_text()
    line=next(line for line in news.splitlines() if '_readSaveT = setTimeout' in line)
    assert 'Promise.resolve(publish(' in line
    assert '.catch(()=>{})' in line
