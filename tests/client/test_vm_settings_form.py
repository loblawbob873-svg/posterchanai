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


class NoNativeDialogsAnywhereInTheClient(unittest.TestCase):
    """`alert`/`confirm`/`prompt` block the renderer, and in the Electron shell and the APK's
    WebView that wedges the window with no way back. This rule has been known here for a long time
    and was enforced by nothing — which is how a bare `prompt()` survived in the VM settings, on a
    screen that ONLY exists inside the desktop shell."""

    ALLOW = {"picker.html"}

    def test_no_bare_native_dialog_in_any_client_module(self):
        bad = []
        pattern = re.compile(r"(?<![.\w$])(alert|confirm|prompt)\s*\(")
        for path in sorted(CLIENT.glob("*.js")):
            if path.name in self.ALLOW:
                continue
            src = _strip(path.read_text(encoding="utf-8"))
            for m in pattern.finditer(src):
                head = src[max(0, m.start() - 40):m.start()]
                # `uiPrompt(`, `PC().uiConfirm(`, `_confirm(` and friends all end in a word char or
                # a dot, which the lookbehind already excludes; `function confirm(` is a definition.
                if re.search(r"(function|const|let|var)\s*$", head):
                    continue
                line = src.count("\n", 0, m.start()) + 1
                bad.append("%s:%d  %s(" % (path.name, line, m.group(1)))
        self.assertEqual(bad, [], "native dialogs wedge the desktop shell:\n" + "\n".join(bad))

    def test_the_vm_disk_prompt_uses_the_client_one(self):
        self.assertIn("PC().uiPrompt('New disk size in GB'", OS_JS)


if __name__ == "__main__":
    unittest.main()
