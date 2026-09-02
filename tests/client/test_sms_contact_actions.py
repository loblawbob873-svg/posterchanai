from pathlib import Path
import json
import subprocess


ROOT = Path(__file__).resolve().parents[2]
SMS = (ROOT / "static/js/client/sms.js").read_text()
CSS = (ROOT / "static/css/client.css").read_text()
CONTACT_SIM = ROOT / "tests/client/contacts_device_sim.js"


def test_open_text_thread_offers_call_and_copy_number_actions():
    thread = SMS[SMS.index("function paintThread("):SMS.index("async function composeNew(")]
    assert 'id="sms-call"' in thread
    assert "window.location.href='tel:'+encodeURIComponent(String(t.address||''))" in thread
    assert 'id="sms-copy-number"' in thread
    assert "PC.copyValue(String(t.address||''),'','number copied')" in thread


def test_unknown_text_thread_can_open_a_prefilled_contact_editor():
    thread = SMS[SMS.index("function paintThread("):SMS.index("async function composeNew(")]
    assert 'id="sms-add-contact"' in thread
    assert "savedContact = !!(window.PCContacts" in thread
    assert "${savedContact?'':" in thread
    assert "window.__PC_CONTACT_ADD_PHONE=String(t.address||'')" in thread
    contacts = (ROOT / "static/js/client/contacts.js").read_text()
    assert "addPhone(add)" in contacts
    assert "editCard(card || null, card ? '' : phone)" in contacts


def test_contacts_runtime_prefills_the_number_from_a_text_thread():
    result = subprocess.run(
        ["node", str(CONTACT_SIM), json.dumps({
            "env": "browser", "action": "add-phone", "phone": "+1 (555) 010-4477"
        })], capture_output=True, text=True, check=True,
    )
    out = json.loads(result.stdout)
    assert out["prefilledPhone"] == "+1 (555) 010-4477"


def test_contact_actions_remain_compact_without_squeezing_the_contact_name():
    assert ".sms-title{" in CSS and "min-width:0;flex:1" in CSS
    assert ".sms-contact-actions{display:flex" in CSS
    assert "@media(max-width:420px){.sms-contact-actions .btn" in CSS
