from pathlib import Path


ROOT=Path(__file__).resolve().parents[2]
SMS=(ROOT/'static/js/client/sms.js').read_text()


def test_live_tombstone_evicts_attachment_bytes_before_marking_message_gone():
    absorb=SMS[SMS.index('async function absorb(evs)'):SMS.index('function rebuild()')]
    assert "if(!ev.content){ forgetMessageParts(have); S.msgs.set" in absorb


def test_explicit_delete_evicts_provider_media_cache():
    remove=SMS[SMS.index('async function remove(docs)'):SMS.index('async function askForRead')]
    # The message is taken before the tombstone goes out (the relay's echo can replace it with a bare
    # `gone` marker meanwhile), and its media evicted only once a relay ACCEPTED the delete -- a refused
    # delete keeps the message, so it must keep its pictures too (tests/client/test_texts_gaps_full_app.py).
    held=remove.index('const held = S.msgs.get(d)')
    publish=remove.index("PC.publish(KIND, '', [['d', d]",held)
    accepted=remove.index("if(!(r && r.ok)){ kept++; continue; }",publish)
    evict=remove.index('forgetMessageParts(held)',accepted)
    assert held < publish < accepted < evict


def test_transient_attachment_failure_is_not_cached_for_the_session():
    part=SMS[SMS.index('async function partData(p)'):SMS.index('function attHtml')]
    assert 'ATT_FAIL_RETRY_MS' in part
    assert 'Date.now()-Number(remembered._at||0)' in part
    assert 'ATT.delete(id)' in part


def test_old_apk_cannot_silently_claim_historical_mms_is_complete():
    assert '&& S.mmsAudited' in SMS
    assert 'Update PosterChan on this phone to copy older picture and video messages' in SMS
    # The VERSION, not a version. Pinned literally, this stopped checking anything the moment the
    # latch was bumped to v10 — which is precisely when a re-audit matters most, because every v9
    # phone had finished with an archive that held no attachments at all.
    import re as _re
    assert _re.search(r"HWM_BLOSSOM = \(\) => HWM\(\) \+ '_blossom_v\d+'", SMS), \
        "the completion latch moved and this assertion stopped checking anything"
