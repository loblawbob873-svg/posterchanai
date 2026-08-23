from pathlib import Path

SRC=(Path(__file__).parents[1]/'mobile/android/app/src/main/java/place/poster/app/phone/DialerActivity.java').read_text()


def test_contact_row_opens_platform_contact_and_call_icon_still_calls():
    assert 'card.setOnClickListener' in SRC
    assert 'startActivity(ContactList.view(r.contactId))' in SRC
    assert 'call.setOnClickListener' in SRC
    assert 'placeNumber(r.number)' in SRC


def test_long_press_keeps_full_action_menu():
    assert 'card.setOnLongClickListener' in SRC
    assert 'rowMenu(r); return true' in SRC

