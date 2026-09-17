"""Virtual Machines, phase 2 client paths, run on the SHIPPED static/js/client/vms.js under node — see
tests/client/vms_phase2_runtime.mjs: the desktop LocalHost adapter against a `window.pcVM` stub built
from desktop/preload.js, session keys for remote signers, and Find hosts."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VMS = (ROOT / "static/js/client/vms.js").read_text(encoding="utf-8")


@pytest.mark.skipif(not shutil.which("node"), reason="node is required")
def test_vms_phase2_runtime():
    r = subprocess.run(["node", str(ROOT / "tests/client/vms_phase2_runtime.mjs")],
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ALL OK" in r.stdout


def test_the_settings_form_puts_save_last_in_a_sticky_footer():
    """The os.js lesson (project_vm_settings_form): Save is the LAST control of the form, outside every
    section, in a footer of its own — and starts disabled until something changed."""
    start = VMS.index("function settingsScreen(){")
    body = VMS[start:VMS.index("function isosScreen(){", start)]
    form = body[body.index('<form id="vms-settings"'):body.index("</form>")]
    controls = re.findall(r"<(?:button|input|select)\b[^>]*>", form)
    assert 'data-act="settings-save"' in controls[-1], controls[-3:]
    foot = form[form.rindex('<div class="vms-formfoot">'):]
    assert "</section>" not in foot and 'data-act="settings-save"' in foot
    assert re.search(r'data-act="settings-save" disabled', foot), "Save must start disabled (nothing to save yet)"
    assert ".vms-formfoot{position:sticky" in VMS


def test_no_native_dialogs_in_the_vm_client():
    for name in ("vms.js", "vmrpc.js", "vmconsole.js"):
        src = (ROOT / "static/js/client" / name).read_text(encoding="utf-8")
        code = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
        assert not re.search(r"(?<![\w.])(?:window\.)?(?:confirm|prompt|alert)\s*\(", code), name
