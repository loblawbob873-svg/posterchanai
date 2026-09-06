"""EVERY TEMPLATE TAG IN THE SHELL MUST BE RESOLVABLE BY THE DESKTOP BUNDLER.

The registration feature added one line to templates/client.html:

    <div class="auth-foot{% if not registration_enabled|default(true) %} hidden{% endif %}">

and every desktop build died on it — linux, mac and windows, five retries each:

    build-www: unrendered template tag in client.html: {% if not registration_enabled|default(true) %}

`desktop/build-www.sh` renders the shell LOCALLY (a bundle has no Jinja) and hard-fails on any tag
it does not know, deliberately: the alternative is shipping a literal `{% %}` into the app. The
mobile build never sees it — the APK FETCHES an already-rendered page — so the Android job sailed
through, green, while no desktop app was published at all. That asymmetry is the whole trap, and
build-www.sh's own comment already records it happening once before.

It also cannot be fixed by rendering alone. A bundle is built once and pointed at an instance
afterwards, so ANY value baked in at build time is either wrong or permanent — which for this one
means hiding "Create a new identity" for ever, on the single screen a person with no account has.
So the tag resolves to the OPEN branch and the instance's real answer arrives at runtime from
/client/config, exactly as `nostr_only` already does.

These RUN the shipped bundler against the shipped template. A test that grepped for the tag would
have to be updated for every new one; this one asks the property — can the bundle be built — so the
next tag is covered before anybody thinks about it.
"""
from __future__ import annotations

import re
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLIENT_HTML = ROOT / "templates/client.html"
BUILD_WWW = ROOT / "desktop/build-www.sh"
APP_JS = ROOT / "static/js/client/app.js"
CLIENT_PY = ROOT / "app/routers/client.py"

pytestmark = pytest.mark.skipif(not shutil.which("bash"), reason="bash unavailable")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """Run the real desktop bundler. This is the check that failed CI."""
    done = subprocess.run(["bash", str(BUILD_WWW)], cwd=BUILD_WWW.parent,
                          capture_output=True, text=True, timeout=600)
    return done


def test_the_desktop_bundle_builds_at_all(built):
    """THE FAILING BUILD, reproduced. Every desktop platform runs this same script."""
    assert built.returncode == 0, (
        "desktop/build-www.sh fails — no desktop app can be published:\n"
        + (built.stdout + built.stderr)[-1500:])


def test_no_template_tag_survives_into_the_bundle(built):
    """The guard's own reason for existing: a literal {% %} shipped into the app would be visible
    text in somebody's window."""
    index = BUILD_WWW.parent / "www/index.html"
    assert index.is_file(), "the bundler produced no index.html"
    html = index.read_text(encoding="utf-8")
    left = re.search(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", html, flags=re.S)
    assert not left, f"an unrendered template tag reached the bundle: {left.group(0)[:120]!r}"


def test_the_bundle_leaves_signup_reachable(built):
    """Resolved to the OPEN branch on purpose. A bundle has not chosen an instance when it is
    built, and baking `hidden` would remove the only control a person with no account can use —
    permanently, since a bundle is not rebuilt when it is pointed somewhere else."""
    html = (BUILD_WWW.parent / "www/index.html").read_text(encoding="utf-8")
    foot = re.search(r'class="auth-foot([^"]*)"', html)
    assert foot, "the signup row is missing from the bundle entirely"
    assert "hidden" not in foot.group(1), (
        "the bundle bakes signup CLOSED — every install of this build hides 'Create a new identity' "
        "regardless of which instance it is pointed at")


def test_the_instance_still_gets_to_answer_at_runtime():
    """The other half. Resolving the tag is only safe because the real answer is published and
    applied — otherwise this 'fix' would silently turn the feature off for every bundled app."""
    assert '"registration_enabled": registration_service.enabled()' in CLIENT_PY.read_text(
        encoding="utf-8").split("async def client_config")[1], (
        "/client/config does not publish registration_enabled, so a bundle can never learn that "
        "signup is closed on the instance it is pointed at")
    assert runtime_signup_hidden(enabled=False, solo=False)
    assert not runtime_signup_hidden(enabled=True, solo=False)


def runtime_signup_hidden(enabled, solo):
    if not shutil.which('node'):
        pytest.skip('Node unavailable')
    app = APP_JS.read_text(encoding='utf-8')
    helper = app[app.index('  function _registrationOpen('):app.index('  function applyInstanceGating(')]
    gating = app.split('function applyInstanceGating(){')[1]
    start = gating.index('    try{')
    end = gating.index('    }catch(_){ }', start) + len('    }catch(_){ }')
    code = f"const CFG={{registration_enabled:{json.dumps(enabled)}}},_standalone=()=>{json.dumps(solo)};let hidden=null;const document={{querySelector:()=>({{classList:{{toggle:(name,on)=>hidden=on}}}})}};" + helper + gating[start:end] + 'console.log(JSON.stringify(hidden));'
    result = subprocess.run(['node', '-e', code], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_a_standalone_bundle_keeps_signup():
    assert not runtime_signup_hidden(enabled=False, solo=True)


def test_the_mobile_bundle_is_unaffected_because_it_fetches_a_rendered_page():
    """Stated so the asymmetry is not rediscovered: the APK was GREEN through this whole failure,
    which is exactly what made it look like the desktop build was at fault on its own."""
    mobile = (ROOT / "mobile/build-www.sh").read_text(encoding="utf-8")
    assert "unrendered template tag" not in mobile, (
        "mobile/build-www.sh now renders the shell locally too — it needs every substitution the "
        "desktop bundler has, and this test's reasoning no longer holds")
