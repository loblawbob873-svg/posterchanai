"""The VM settings form: Save last, no native dialogs, and no edit lost in silence.

"In Virtual Machines, fix the VM settings to make it better. Such as adding the Save button as the
last button in the form."

Save sat at the END OF THE FIRST SECTION — under "Performance" — while the thing it writes includes
`bootOrder`, which is a control in the section BELOW it. So changing "Start from" left the only Save
button off the top of the screen, above the field just edited, looking like it belonged to some
other settings. It is the last control in the form now, in a footer of its own.

Two more, found while moving it:

  * `prompt('New disk size in GB','40')` — a NATIVE dialog, in the one screen that exists only
    inside the Electron shell, where a native dialog blocks the renderer and wedges the window. The
    rest of this client learned that long ago and nothing was watching for a new one.
  * Nothing tracked whether the form had unsaved edits, so "Back to machines" discarded them
    without a word.
"""
import re
import unittest
from pathlib import Path

CLIENT = Path(__file__).resolve().parents[2] / "static" / "js" / "client"
# The desktop's old "Local VMs" painter in os.js is gone: a local VM's settings are the Virtual Machines
# screen's settings form (vms.js), the same form a server-hosted VM uses. The rules moved with it.
VMS_JS = (CLIENT / "vms.js").read_text(encoding="utf-8")


def _strip(js: str) -> str:
    """Comments removed, so a rule is never satisfied or broken by prose about it."""
    js = re.sub(r"/\*.*?\*/", " ", js, flags=re.S)
    return re.sub(r"(?<![:\w])//[^\n]*", " ", js)


def _fn(name: str) -> str:
    src = _strip(VMS_JS)
    start = src.index(name)
    return src[start:src.index("\n  }", start)]


class SaveIsTheLastButtonInTheForm(unittest.TestCase):
    def _markup(self) -> str:
        form = _fn("function settingsScreen(){")
        start = form.index('<form id="vms-settings"')
        return form[start:form.index("</form>", start)]

    def test_save_is_the_last_button_in_the_form(self):
        controls = re.findall(r'data-act="([\w-]+)"|name="([\w-]+)"', self._markup())
        last = [a or n for a, n in controls][-1]
        self.assertEqual(last, "settings-save", "Save is not the last control in the form")

    def test_save_is_not_buried_inside_one_section(self):
        """It writes fields from more than one section, so it must not sit inside any of them."""
        markup = self._markup()
        self.assertGreater(markup.index('data-act="settings-save"'), markup.rfind("</section>"))

    def test_what_save_writes_is_read_in_one_place(self):
        """Save and the dirty check read the same two functions, so what is saved and what counts as
        an unsaved change cannot drift apart."""
        save, dirty = _fn("async function saveSettings(){"), _fn("function settingsDirty(){")
        for body in (save, dirty):
            self.assertIn("settingsFields()", body)
            self.assertIn("settingsDelta(", body)
        fields = _fn("function settingsFields(){")
        for key in ("vcpus", "ram_mib", "autostart", "boot"):
            self.assertIn(key, fields)

    def test_an_unsaved_edit_is_never_discarded_in_silence(self):
        leave = _fn("async function leaveSettings(){")
        self.assertIn("settingsDirty()", leave)
        self.assertIn("PC.uiConfirm(", leave)
        # Both ways out of the form ask: the header Back and the footer Back are the same action.
        self.assertEqual(_fn("function settingsScreen(){").count('data-act="settings-leave"'), 2)

    def test_save_cannot_be_double_submitted(self):
        """`vm.update` redefines the domain; two in flight is an error a person reads as "saving is broken"."""
        save = _fn("async function saveSettings(){")
        self.assertIn("if(!st || st.busy) return;", save)
        self.assertLess(save.index("st.busy = true"), save.index("await call("))
        self.assertIn("S.settings.busy", _fn("function syncSettingsSave(){"))


class TheVmFormUsesNoNativeDialog(unittest.TestCase):
    def test_adding_a_disk_is_a_field_not_a_native_prompt(self):
        self.assertIn('name="add_disk_gib"', VMS_JS)
        code = _strip(VMS_JS).replace("uiPrompt(", "").replace("uiConfirm(", "")
        self.assertNotRegex(code, r"(?<![\w.])(prompt|confirm|alert)\(")


if __name__ == "__main__":
    unittest.main()
