"""Admin → Save sends only the fields that CHANGED since the page loaded.

It sent every field -- the values as they were when the page opened -- so anything changed elsewhere
in between was silently put back. A block made from the client was undone by the next Admin Save,
twice in one evening, and the blocked fediverse account kept posting. Runs the shipped payload
builder from admin.js under node against a stand-in form."""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _run(fields, loaded):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    src = (ROOT / "static/js/admin.js").read_text()
    start = src.index("function _norm(v)")
    end = src.index("if (typeof module !== 'undefined')", start)
    script = src[start:end] + f"""
const fields={json.dumps(fields)}.map(f=>({{...f, getAttribute:()=>f.attr||null}}));
const form={{querySelectorAll:()=>fields}};
console.log(JSON.stringify(buildSettingsPayload(form, new Map(Object.entries({json.dumps(loaded)})))));
"""
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_an_untouched_list_is_not_sent_back():
    loaded = {"nostr_relay_blocked_pubkeys": "npub1a\nnpub1b", "site_name": "PosterChan", "flag": "true"}
    fields = [{"name": "nostr_relay_blocked_pubkeys", "type": "textarea", "value": "npub1a\r\nnpub1b"},
              {"name": "site_name", "type": "text", "value": "PosterChan"},
              {"name": "flag", "type": "checkbox", "checked": True}]
    assert _run(fields, loaded) == {}, "an unchanged form overwrote settings changed elsewhere"


def test_what_changed_is_sent():
    loaded = {"site_name": "PosterChan", "flag": "true", "n": "5"}
    fields = [{"name": "site_name", "type": "text", "value": "Poster Place"},
              {"name": "flag", "type": "checkbox", "checked": False},
              {"name": "n", "type": "number", "value": "5"}]
    assert _run(fields, loaded) == {"site_name": "Poster Place", "flag": "false"}


def test_a_field_the_server_never_reported_is_sent_when_it_has_a_value():
    fields = [{"name": "new_key", "type": "text", "value": "x"},
              {"name": "empty_key", "type": "text", "value": ""},
              {"name": "num_default", "type": "number", "value": "60", "attr": "60"},
              {"name": "num_typed", "type": "number", "value": "90", "attr": "60"}]
    assert _run(fields, {}) == {"new_key": "x", "num_typed": "90"}
