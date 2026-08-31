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
OS_JS = (CLIENT / "os.js").read_text(encoding="utf-8")


def _strip(js: str) -> str:
    """Comments removed, so a rule is never satisfied or broken by prose about it."""
    js = re.sub(r"/\*.*?\*/", " ", js, flags=re.S)
    return re.sub(r"(?<![:\w])//[^\n]*", " ", js)


class SaveIsTheLastButtonInTheForm(unittest.TestCase):
    def _form(self) -> str:
        start = OS_JS.index("const editHardware=async(name)=>{")
        return OS_JS[start:OS_JS.index("$('[data-vm-new]',w.slot)", start)]

    def _markup(self) -> str:
        """Just the innerHTML the settings form is built from."""
        form = self._form()
        # NOT the first `vmui-top`: the unreadable-hardware branch above opens with the same markup.
        start = form.index("Turn the machine off before changing hardware")
        start = form.rindex("<div class=\"vmui-top\">", 0, start)
        return form[start:form.index("`;", start)]

    def test_save_is_the_last_button_in_the_form(self):
        markup = self._markup()
        buttons = re.findall(r"data-vme-(\w+)", markup)
        buttons = [b for b in buttons if b not in ("state", "media")]
        self.assertIn("save", buttons)
        self.assertEqual(buttons[-1], "save",
                         "Save is not the last control in the form; it is followed by " +
                         ", ".join(buttons[buttons.index("save") + 1:]))

    def test_save_is_not_buried_inside_one_section(self):
        """It writes fields from more than one section, so it must not sit inside any of them."""
        markup = self._markup()
        pos = markup.index("data-vme-save")
        last_section_close = markup.rfind("</section>")
        self.assertGreater(pos, last_section_close,
                           "Save sits inside a <section>, which is what made it look like it "
                           "belonged to Performance while it also writes the boot order")

    def test_what_save_writes_is_read_in_one_place(self):
        """Read inline in the click handler, the set of saved fields and the button's position could
        drift apart — which is how this bug happened."""
        form = _strip(self._form())
        self.assertIn("const fields=()=>", form)
        self.assertIn("pcVM.update(name,fields())", form)
        for key in ("ramMiB", "cpus", "autostart", "bootOrder"):
            self.assertIn(key, form[form.index("const fields=()=>"):form.index("const clean=")], key)

    def test_an_unsaved_edit_is_never_discarded_in_silence(self):
        form = _strip(self._form())
        self.assertIn("const dirty=()=>", form)
        self.assertIn("uiConfirm(", form)
        # Both ways out of the form ask.
        self.assertIn("$('[data-vme-close]',box).onclick=leave", form)
        self.assertIn("back.onclick=leave", form)

    def test_save_cannot_be_double_submitted(self):
        """`update` redefines the libvirt domain; two in flight is an error a person reads as
        "saving is broken"."""
        form = _strip(self._form())
        handler = form[form.index("saveBtn.onclick=async()"):]
        self.assertIn("saveBtn.disabled=true", handler)
        self.assertIn("Saving", handler)


class TheVmDiskPromptUsesTheClientDialog(unittest.TestCase):
    """The app-wide audit moved to tests/test_no_native_dialogs_anywhere.py, which covers every
    served script AND every template — this one only ever scanned `static/js/client/*.js`, which is
    exactly why 67 native dialogs in the admin panel went unnoticed until one of them split the
    desktop in half. What stays here is the VM-specific half."""

    def test_the_vm_disk_prompt_uses_the_client_one(self):
        self.assertIn("PC().uiPrompt('New disk size in GB'", OS_JS)


if __name__ == "__main__":
    unittest.main()
