from pathlib import Path

ROOT=Path(__file__).parents[1]
SRC=(ROOT/'mobile/android/app/src/main/java/place/poster/app/phone/DialerActivity.java').read_text()
LAYOUT=(Path(__file__).parents[1]/'mobile/android/app/src/main/res/layout/tel_recent_row.xml').read_text()
CONTACTS=(ROOT/'static/js/client/contacts.js').read_text()
PLUGIN=(ROOT/'mobile/android/app/src/main/java/place/poster/app/sms/SmsPlugin.java').read_text()
ROUTES=(ROOT/'mobile/android/app/src/main/java/place/poster/app/sms/SmsRoutes.java').read_text()


def test_contact_row_opens_posterchan_contact_and_call_icon_still_calls():
    assert 'card.setOnClickListener' in SRC
    assert 'openPosterContact(r)' in SRC
    assert 'LaunchView.request("contact:" + Uri.encode(r.number.trim())' in SRC
    assert 'new Intent(this, MainActivity.class)' in SRC
    assert 'startActivity(ContactList.view(r.contactId))' not in SRC
    assert 'call.setOnClickListener' in SRC
    assert 'placeNumber(r.number)' in SRC


def test_long_press_keeps_full_action_menu():
    assert 'card.setOnLongClickListener' in SRC
    assert 'rowMenu(r); return true' in SRC


def test_contact_row_has_direct_text_and_call_actions():
    assert '@+id/pc_rc_text' in LAYOUT
    assert '@string/tel_text_number' in LAYOUT
    assert '@+id/pc_rc_call' in LAYOUT
    assert 'text.setOnClickListener' in SRC
    assert 'openTextNumber(r.number)' in SRC


def test_text_stays_in_posterchan_and_normalizes_the_recipient():
    assert 'SmsRoutes.conversation(this, raw)' in SRC
    assert 'String number = SmsKeys.normalize(raw)' in ROUTES
    assert 'new Intent(ctx, ThreadActivity.class)' in ROUTES
    assert '.putExtra(ThreadActivity.EXTRA_ADDRESS, number)' in ROUTES
    assert '.addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP)' in ROUTES
    assert 'FLAG_ACTIVITY_NEW_TASK' not in ROUTES
    assert 'Intent.ACTION_SENDTO' not in SRC
    assert 'if (open == null)' in SRC


def test_launcher_contacts_card_has_call_and_native_text_actions():
    assert 'id="cc-call"' in CONTACTS
    assert 'id="cc-text"' in CONTACTS
    assert "capPlugin('Sms', 'openConversation')" in CONTACTS
    assert 'PC.switchView(\'texts\')' in CONTACTS
    assert 'public void openConversation(PluginCall call)' in PLUGIN
    assert 'SmsRoutes.conversation(getActivity()' in PLUGIN
