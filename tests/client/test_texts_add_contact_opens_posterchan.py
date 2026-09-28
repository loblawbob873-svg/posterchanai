"""Texts → "Add contact" opens POSTERCHAN's Contacts editor with the number, not the phone's.

Reported on Android: "in texts, if you click on Contact button at the top -> it goes to phone add
contact. Posterchan is not a storage location" / "we need the contact button to actually go to
Posterchan add contact with the info". The native Texts screen (sms/ThreadActivity) fired the
platform ACTION_INSERT editor, which only offers the accounts the phone's Contacts app stores to --
so the person was saved to Google/the SIM, outside the encrypted address book that follows their
account. It now parks `contact-add:<number>` in LaunchView and brings MainActivity forward (the same
hand-off DialerActivity uses for `contact:`), and phoneshell.js lands it on contacts.js `addPhone`.

This runs the SHIPPED phoneshell.js landing (the device half is SmsContactDeviceTest, on the
emulator) and pins the native side to that hand-off.
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHELL = ROOT / "static/js/client/phoneshell.js"
THREAD = (ROOT / "mobile/android/app/src/main/java/place/poster/app/sms/ThreadActivity.java").read_text()

HARNESS = r"""
const fs = require('fs');
const listeners = {};
global.document = { visibilityState:'visible', querySelector: s => s === '#feed' ? {} : null,
  addEventListener: (n, fn) => { (listeners[n] ||= []).push(fn); } };
const events = [];
let launch;
const home = { consumeLaunchView: async () => ({view: ''}), addListener: (n, fn) => { if(n==='launchView') launch=fn; } };
global.window = { __PC_BOOTED: true, addEventListener(){},
  __PC: { capPlugin: n => n === 'HomeScreen' ? home : null,
          switchView: v => events.push({view:v, add:window.__PC_CONTACT_ADD_PHONE||'', open:window.__PC_CONTACT_PHONE||''}),
          closeModal(){} } };
global.navigator = {language:'en'};
global.localStorage = {getItem(){return null},setItem(){},removeItem(){}};
eval(fs.readFileSync(process.argv[1], 'utf8'));
(async () => {
  const wait = () => new Promise(r => setTimeout(r, 50));
  launch && launch({view: 'contact-add:' + encodeURIComponent(process.argv[2])});
  await wait();
  window.__PC_CONTACT_ADD_PHONE = '';
  launch && launch({view: 'contact:' + encodeURIComponent(process.argv[2])});
  await wait();
  process.stdout.write(JSON.stringify({events, hooked: !!launch}));
})();
"""


def test_the_launch_route_opens_the_add_contact_editor_with_the_number():
    number = "+1 (555) 010-4477"
    r = subprocess.run(["node", "-e", HARNESS, str(SHELL), number], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["hooked"], "phoneshell.js no longer listens for launchView"
    assert out["events"][0] == {"view": "contacts", "add": number, "open": ""}, \
        f"contact-add: must open Contacts with the number parked for addPhone: {out['events']}"
    # `contact:` (open an existing card) must be unaffected by the new prefix sharing its start.
    assert out["events"][1] == {"view": "contacts", "add": "", "open": number}, out["events"]


def test_texts_hands_the_number_to_posterchan_not_the_platform_editor():
    body = THREAD[THREAD.index("private void addContact()"):]
    body = body[:body.index("\n    }\n") + 6]
    assert "ACTION_INSERT" not in body and "ContactsContract" not in body, \
        "Texts still opens the phone's contact editor"
    assert 'LaunchView.request("contact-add:" + Uri.encode(' in body
    assert "new Intent(this, MainActivity.class)" in body


def test_contacts_consumes_the_parked_number():
    contacts = (ROOT / "static/js/client/contacts.js").read_text()
    assert "window.__PC_CONTACT_ADD_PHONE" in contacts and "addPhone(add)" in contacts
