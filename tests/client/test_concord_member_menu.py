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
    assert "if(action==='profile'){if(p.openProfile)p.openProfile(target);return;}" in JS
    assert "openMemberMenu(e,target)" in JS
    assert "row.onpointerdown" in JS and "550" in JS
    assert "View profile" in JS
    assert "canBan=isOwner&&target!==viewer.pubkey" in JS
    assert "Ban from community" in JS
    assert "p.openProfile(target)" in JS
    bind = JS[JS.index("function bind(me){"):]
    menu = bind.index("const openMemberMenu=")
    assert "const viewer=p.viewer?p.viewer():{}" in bind[:menu]
    assert "isOwner=!!boundOwnerPk&&boundOwnerPk===viewer.pubkey" in bind[:menu]


def test_mobile_tap_opens_profile_but_long_press_keeps_context_actions():
    rows = JS.split("$$('[data-cc-member]')", 1)[1].split("const membersInvite", 1)[0]
    assert "memberTapAction(memberViewportIsNarrow(),longPressed)" in rows
    assert "const target=row.dataset.ccMember,narrow=" not in rows
    assert "if(action==='consume')return" in rows
    assert "if(action==='profile'){if(p.openProfile)p.openProfile(target);return;}" in rows
    assert "longPressed=true;openMemberMenu(e,target)" in rows
    assert "row.oncontextmenu" in rows
    assert "function memberTapAction(narrow,longPressed)" in JS
    assert "function memberViewportIsNarrow()" in JS


def test_delayed_ban_updates_the_original_room_not_whichever_room_is_active_later():
    handler = JS.split('const banMember=async target=>', 1)[1].split('const closeMemberMenu', 1)[0]
    assert 'roomId=roomIdentity(room)' in handler
    assert 'const latest=saved()' in handler
    assert 'latest.findIndex(item=>roomIdentity(item)===roomId)' in handler
    assert 'latest[roomIndex].banned=made.banned' in handler
    assert 'rooms[state.community]=room' not in handler


def test_member_menu_can_open_a_direct_message():
    assert 'data-cc-member-message=' in JS
    assert '>Message</button>' in JS
    assert "if(p.messageUser)p.messageUser(target)" in JS
    app = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
    assert "messageUser: (pk) =>" in app
    assert "switchView('messages'); setTimeout(()=>openDm(pk),80)" in app


def test_member_menu_is_anchored_to_the_row_and_survives_its_button_pointerdown():
    assert "anchor.getBoundingClientRect?anchor.getBoundingClientRect():null" in JS
    assert "rect?rect.right+6" in JS
    assert "if(!menu.contains(e.target))closeMemberMenu()" in JS
    assert "document.addEventListener('pointerdown',closeMemberMenu" not in JS


def test_member_rows_and_menu_have_compact_dedicated_styling():
    assert ".cc-member-menu" in CSS
    assert ".cc-member img{flex:0 0 32px" in CSS
    assert ".cc-member:hover,.cc-member:focus-visible" in CSS
