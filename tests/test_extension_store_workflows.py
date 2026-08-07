"""The store-publishing steps of .github/workflows/extension.yml, RUN rather than read.

A workflow is the one kind of code whose bugs are only ever discovered in production: the first
real execution is the test, it happens after a version bump, and a failure there is a release that
did not go out. Every mistake found while writing these two steps would have surfaced that way —
`secrets` not being available to a step's `if:`, a depth-1 clone making HEAD~1 unresolvable, an
`OK false` reason never being surfaced.

So this extracts the actual `run:` scripts out of the YAML and executes them with `curl` stubbed,
the same way tests/test_logs_scheduler.py runs the real probe script with `sudo` stubbed. A parser
test cannot catch a heredoc that got re-indented by YAML, a jq/python one-liner that mis-reads the
API response, or an error path that exits 0 — and those are the failures that matter here.

No network, no credentials: the stub answers as Google's API would.
"""

import json
import os
import stat
import subprocess

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows", "extension.yml")


def _step(name):
    wf = yaml.safe_load(open(WF, encoding="utf-8"))
    for s in wf["jobs"]["build"]["steps"]:
        if s.get("name") == name:
            return s
    raise AssertionError(f"no step named {name!r}")


def _run(script, tmp_path, curl_script, env=None):
    """Run a workflow step's shell with `curl` stubbed by `curl_script`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(curl_script)
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)

    sh = tmp_path / "step.sh"
    sh.write_text(script)
    e = dict(os.environ)
    e["PATH"] = f"{bin_dir}:{e['PATH']}"
    e.update(env or {})
    return subprocess.run(["bash", str(sh)], capture_output=True, text=True, env=e,
                          cwd=str(tmp_path), timeout=60)


CWS_ENV = {"CWS_CLIENT_ID": "id", "CWS_CLIENT_SECRET": "sec",
           "CWS_REFRESH_TOKEN": "rt", "CWS_ITEM_ID": "item123"}


def _curl_stub(token='{"access_token":"tok"}', upload=None, publish=None):
    """A curl that answers by which API it was called against, like Google's does."""
    upload = upload or json.dumps({"uploadState": "SUCCESS"})
    publish = publish or json.dumps({"status": ["OK"]})
    return f"""#!/bin/bash
for a in "$@"; do
  case "$a" in
    *oauth2.googleapis.com/token*) printf '%s' {json.dumps(token)}; exit 0;;
    *upload/chromewebstore*)       printf '%s' {json.dumps(upload)}; exit 0;;
    */publish*)                    printf '%s' {json.dumps(publish)}; exit 0;;
  esac
done
printf '%s' '{{}}'
"""


@pytest.fixture
def cws(tmp_path):
    script = _step("Publish to the Chrome Web Store")["run"]
    # The step uploads a real path; make it exist so `curl -T` has something to name.
    d = tmp_path / "extension" / "dist"
    d.mkdir(parents=True)
    (d / "posterchan-passwords-chrome.zip").write_bytes(b"PK\x03\x04zip")
    return script


def test_a_normal_publish_succeeds(cws, tmp_path):
    r = _run(cws, tmp_path, _curl_stub(), CWS_ENV)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "::notice" in r.stdout


def test_pending_review_is_a_success_not_a_failure(cws, tmp_path):
    """The Web Store reviews before going live, so ITEM_PENDING_REVIEW is the EXPECTED outcome of a
    successful submission. Treating it as failure would make every correct release look broken."""
    r = _run(cws, tmp_path, _curl_stub(publish='{"status":["ITEM_PENDING_REVIEW"]}'), CWS_ENV)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ITEM_PENDING_REVIEW" in r.stdout


def test_a_duplicate_version_fails_loudly_with_the_stores_own_reason(cws, tmp_path):
    """The failure this whole gate exists to prevent. It must not pass green, and the REASON has to
    reach the log — `uploadState: FAILURE` alone sends you looking in the wrong place."""
    bad = json.dumps({"uploadState": "FAILURE",
                      "itemError": [{"error_detail": "Version already exists in the store"}]})
    r = _run(cws, tmp_path, _curl_stub(upload=bad), CWS_ENV)
    out = r.stdout + r.stderr
    assert r.returncode != 0, "a rejected upload exited 0"
    # The ANNOTATION, not merely the text. An earlier version of this test asserted the reason
    # appeared anywhere in the output and passed even with the itemError handling deleted — because
    # the raw response is echoed one line earlier, so the string was there either way. What matters
    # is that it reaches the GitHub UI as an error rather than sitting inside a JSON blob nobody
    # expands.
    assert "::error title=Chrome Web Store upload::Version already exists" in out, (
        f"the store's own reason is not surfaced as an annotation:\n{out}")


def test_an_expired_refresh_token_names_the_seven_day_trap(cws, tmp_path):
    """Google expires refresh tokens after 7 days while the OAuth consent screen is in "Testing".
    The workflow then starts failing about a week after setup, and the cause is not in the workflow
    — so the error has to say where to look."""
    r = _run(cws, tmp_path, _curl_stub(token='{"error":"invalid_grant"}'), CWS_ENV)
    assert r.returncode != 0
    assert "In production" in r.stdout + r.stderr, "the error does not name the real cause"


def test_a_failed_publish_call_is_not_reported_as_success(cws, tmp_path):
    r = _run(cws, tmp_path, _curl_stub(publish='{"status":["ITEM_NOT_UPDATABLE"]}'), CWS_ENV)
    assert r.returncode != 0, "a refused publish exited 0"


def test_both_store_steps_are_gated_on_a_version_change_and_on_their_secrets():
    """Neither store accepts a version it already has, and both reject it only AFTER the upload.
    Ungated, every ordinary commit to extension/ would attempt a release and fail.

    The secret check must be on `env`, not `secrets`: the secrets context is not available to a
    step's `if:` at all, so `secrets.X != ''` there is silently always true — and a fork's PR would
    fail red on credentials it is not allowed to have.
    """
    for name, key in (("Submit to addons.mozilla.org", "AMO_JWT_ISSUER"),
                      ("Publish to the Chrome Web Store", "CWS_CLIENT_ID")):
        cond = _step(name)["if"]
        assert "steps.bump.outputs.changed == 'yes'" in cond, f"{name} is not gated on a version bump"
        assert f"env.{key} != ''" in cond, f"{name} must test the secret via env, not the secrets context"
        assert "secrets." not in cond, f"{name}: the secrets context does not work in a step if:"


def test_the_version_gate_can_see_the_previous_commit():
    """actions/checkout defaults to a depth-1 clone, where the commit before the push does not
    exist — so the comparison silently reads "changed" every time and submits on every push, which
    is the duplicate-version error the gate exists to prevent."""
    wf = yaml.safe_load(open(WF, encoding="utf-8"))
    checkout = wf["jobs"]["build"]["steps"][0]
    assert str(checkout.get("with", {}).get("fetch-depth")) == "0", "shallow clone: HEAD~1 is unresolvable"

    gate = _step("Did the manifest version change?")["run"]
    assert "github.event.before" in gate, (
        "comparing against HEAD~1 skips the release when a push carries several commits and the "
        "bump was the first of them")
