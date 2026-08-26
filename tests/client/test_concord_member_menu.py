from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
JS = (ROOT / "static/js/client/concord.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/concord.css").read_text(encoding="utf-8")


def test_member_rows_do_not_expose_destructive_buttons_permanently():
    rows = JS.split("const memberRows=", 1)[1].split("notifyMentions", 1)[0]
    assert 'data-cc-member=' in rows
    assert 'data-cc-ban=' not in rows
    assert 'class="cc-member"' in rows


def test_member_menu_supports_profile_and_owner_only_ban():
    assert "row.oncontextmenu" in JS
    assert "row.onclick=e=>{e.preventDefault();openMemberMenu(e,target);}" in JS
    assert "row.onpointerdown" in JS and "550" in JS
    assert "View profile" in JS
    assert "canBan=isOwner&&target!==viewer.pubkey" in JS
    assert "Ban from community" in JS
    assert "p.openProfile(target)" in JS


def test_member_rows_and_menu_have_compact_dedicated_styling():
    assert ".cc-member-menu" in CSS
    assert ".cc-member img{flex:0 0 32px" in CSS
    assert ".cc-member:hover,.cc-member:focus-visible" in CSS
