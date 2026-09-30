"""Phone -> Recents: a number that is not a contact can be added to contacts from its menu.

Asked for: "Android Phone dialer -> Recents -> Clicking on number should also give you ability to add
as contact in the menu". The row menu offered Call / Text / Copy / Open contact (known numbers only) /
Delete -- nothing for a stranger's number. RowMenu (pure Java, RUN here) decides the items; the
dialer draws them, and Add to contacts opens PosterChan's own editor prefilled (`contact-add:`), the
route Texts' Add contact takes.
"""
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import androidcompile as ac  # noqa: E402

PHONE = os.path.join(ac.JAVA, "place", "poster", "app", "phone")
JAVAC, JAVA = shutil.which("javac"), shutil.which("java")

HARNESS = r"""
import place.poster.app.phone.RowMenu;
public class MenuHarness { public static void main(String[] a) {
  System.out.println(RowMenu.actions(false, true, "+15550100"));   // a stranger in Recents
  System.out.println(RowMenu.actions(true, true, "+15550100"));    // a saved contact in Recents
  System.out.println(RowMenu.actions(true, false, "+15550100"));   // a Contacts-tab row
  System.out.println(RowMenu.actions(false, true, ""));            // a private / unknown caller
  System.out.println(RowMenu.ADD_CONTACT + " " + RowMenu.VIEW_CONTACT + " " + RowMenu.DELETE);
}}
"""


@pytest.mark.skipif(not (JAVAC and JAVA), reason="JDK not installed")
def test_a_strangers_number_offers_add_to_contacts_and_a_contact_does_not():
    tmp = tempfile.mkdtemp()
    h = os.path.join(tmp, "MenuHarness.java")
    open(h, "w").write(HARNESS)
    r = subprocess.run([JAVAC, "-d", tmp, os.path.join(PHONE, "RowMenu.java"), h], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = subprocess.run([JAVA, "-cp", tmp, "MenuHarness"], capture_output=True, text=True).stdout.splitlines()
    add, view, delete = (int(x) for x in out[4].split())
    stranger, known, contact_tab, hidden = (eval(l) for l in out[:4])
    assert add in stranger and view not in stranger and delete in stranger, stranger
    assert add not in known and view in known, "a saved contact offered to be added again (a duplicate card)"
    assert add not in contact_tab and delete not in contact_tab, contact_tab
    assert add not in hidden and stranger[:3] == [0, 1, 2] and hidden == [delete], hidden


def test_the_dialer_draws_rowmenu_and_add_opens_posterchans_editor():
    src = open(os.path.join(PHONE, "DialerActivity.java")).read()
    menu = src[src.index("private void rowMenu("):][:3000]
    assert "RowMenu.actions(" in menu, "the dialer decides its menu itself again"
    assert "RowMenu.ADD_CONTACT" in menu and "addPosterContact(r.number)" in menu
    add = src[src.index("private void addPosterContact("):][:600]
    assert '"contact-add:"' in add and "MainActivity.class" in add
    strings = open(os.path.join(ac.ROOT, "mobile/android/app/src/main/res/values/strings.xml")).read()
    assert 'name="tel_add_contact"' in strings
