"""WebXDC attachments in encrypted DMs are playable and share one multiplayer identity."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text()
WEBXDC = (ROOT / "static/js/client/webxdc.js").read_text()
CSS = (ROOT / "static/css/client.css").read_text()


def _between(start, end):
    return APP[APP.index(start):APP.index(end, APP.index(start))]


def test_nip17_uses_the_shared_inner_rumor_id_for_the_game_topic():
    ingest = _between("async function ingestWrap", "function myInboxRelays")
    assert "xdcMessageId:rumor.id||ev.id" in ingest
    assert "tags:(rumor.tags||[]).map(t=>t.slice())" in ingest


def test_legacy_dm_keeps_its_shared_event_identity_and_tags():
    ingest = _between("function ingestDM(ev)", "async function decryptMsg")
    assert "xdcMessageId:ev.id" in ingest
    assert "tags:(ev.tags||[]).map(t=>t.slice())" in ingest


def test_dm_body_renders_the_standard_passive_webxdc_card():
    body = _between("function _dmBodyHtml(m)", "function _scheduleDmRefresh")
    assert "PCWebxdc.appOf" in body
    assert "id:m.xdcMessageId||m.id" in body
    assert "PCWebxdc.cardHtml(app)" in body
    assert "text=text.replace(app.url,'').trim()" in body
    assert "return body+files+xdcCard" in body
    # Play remains delegated once by webxdc.js; repainting/decrypting a DM must not bind it again.
    assert "document.addEventListener('click'" in WEBXDC
    assert "closest('.xdc-card')" in WEBXDC


def test_both_dm_composers_offer_mini_apps_inside_attach_menu():
    assert APP.count("['webxdc','🎮 Multiplayer mini app']") == 2
    assert "PCWebxdc.attach(inp)" in APP
    assert "PCWebxdc.attach(body)" in APP
    # Do not consume another permanent mobile composer column for this action.
    assert 'id="dm-webxdc"' not in APP


def test_dm_card_uses_attachment_width_and_a_small_screen_action_row():
    assert ".bubble:has(.xdc-card){width:min(520px,92%);max-width:92%}" in CSS
    assert ".bubble .xdc-card{box-sizing:border-box;width:100%;max-width:100%;min-width:0" in CSS
    assert "white-space:normal;overflow:hidden" in CSS
    phone = CSS[CSS.index("@media(max-width:520px){", CSS.index(".bubble:has(.xdc-card)")):]
    phone = phone[:phone.index("}\n/* ---- Games")]
    assert "grid-template-columns:38px minmax(0,1fr) auto" in phone
    assert ".xdc-reset{grid-column:2;grid-row:2" in phone
    assert ".xdc-play{grid-column:3;grid-row:2" in phone
