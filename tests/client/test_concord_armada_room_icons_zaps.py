"""Armada interoperability guards for Concord channel chrome and private zaps."""
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
CONCORD = (ROOT / "static/js/client/concord.js").read_text(encoding="utf-8")
READER = (ROOT / "static/js/client/cord-reader.js").read_text(encoding="utf-8")
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/concord.css").read_text(encoding="utf-8")


def test_channel_icons_follow_cord_metadata_instead_of_inventing_a_custom_field():
    """CORD-03 specifies name/private, while Armada uses # plus a private lock."""
    rows = CONCORD.split("function channelRowsHtml", 1)[1].split("function channelSectionsHtml", 1)[0]
    assert 'cc-channel-kind' in rows and '>#</span>' in rows
    assert "c.private?'" in rows and 'cc-channel-lock' in rows
    assert 'aria-label="Private channel"' in rows
    assert '.cc-channel-name{' in CSS and '.cc-channel-lock{' in CSS


def test_private_zap_invoice_is_not_a_public_nip57_request():
    helper = APP.split("async function payPrivateConcordZap", 1)[1].split("function invoiceModal", 1)[0]
    assert "+'amount='+amountMsats" in helper
    assert "nostr=" not in helper
    assert "comment=" not in helper
    assert "sendPayment(bolt11)" in helper
    assert "Nwc.payInvoice(bolt11)" in helper
    assert "/^[0-9a-f]{64}$/.test(preimage)" in helper
    assert "payPrivateConcordZap," in APP.split("window.__PC =", 1)[1]


def test_private_zap_uses_armada_sealed_rumor_shape():
    handler = CONCORD.split("$$('[data-cc-zap]')", 1)[1].split("async function webxdcCordParts", 1)[0]
    for marker in ("['e',messageId(target)]", "['p',target.pubkey]", "['k',String(target.kind||9)]",
                   "['amount',String(proof.amountMsats)]", "['bolt11',proof.bolt11]",
                   "['preimage',proof.preimage]", ",9735)"):
        assert marker in handler
    assert "publishCordNative" in handler
    assert "publishCordMessage" not in handler  # Never leak this extension onto a NIP-29 relay.


def test_only_verified_reader_tallies_reach_the_ui():
    view = READER.split("async function inspectChat", 1)[1].split("async function createChatWrap", 1)[0]
    assert "zaps: [...timeline.zaps]" in view
    assert "verifyZapRumor" in READER
    assert "claimedHashes.has" in READER
    assert "new Map(opened.zaps||[])" in CONCORD
    assert "zapSummary(p,m)" in CONCORD


def test_navigation_retains_the_avatar_nodes_to_prevent_reload_flicker():
    helper = CONCORD.split("function retainCommunityRail", 1)[1].split("/* THREADS.", 1)[0]
    assert "old.innerHTML!==fresh.innerHTML" in helper
    assert "newRail.replaceWith(oldRail)" in helper
    render = CONCORD.split("function render()", 1)[1].split("function drawMentions", 1)[0]
    assert "const oldCommunityRail=" in render
    assert "retainCommunityRail(oldCommunityRail" in render


def test_avatar_rail_navigation_runtime_keeps_the_existing_image_node():
    subprocess.run(
        ["node", str(ROOT / "tests/client/concord_avatar_rail_runtime.mjs")],
        cwd=ROOT,
        check=True,
    )
