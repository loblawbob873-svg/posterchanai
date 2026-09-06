"""OUR OWN MEDIA SERVER SPEAKS BLOSSOM. THAT IS NOT A DISCOVERY QUESTION.

Reported from the browser console:

    XHR GET https://media.poster.place/.well-known/nostr/nip96.json
    CORS Missing Allow Origin ... Status code: 405

`detectProto` asks a media host whether it publishes a NIP-96 well-known, which is the right way to
detect one (by capability, not by hostname). Asked of THIS node's own Blossom server the answer can
only ever be no -- and it is a loud no: the host replies 405 with no CORS header, so the browser
reports a blocked cross-origin request, which reads like a broken media server.

The probe is kept for every other host, because that is the case it exists for. The tests RUN the
shipped function against a stubbed fetch, since the complaint is about whether the REQUEST is made
at all -- a source-text assertion cannot see that.
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run():
    out = subprocess.run(["node", str(ROOT / "tests/client/detect_proto_sim.mjs")],
                         cwd=ROOT, text=True, capture_output=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_our_own_media_host_is_never_asked():
    r = _run()
    assert r["ownMediaAsked"] == 0, "still probing the node's own Blossom server"
    assert r["ownMediaHost"] == "blossom"


def test_the_answer_does_not_depend_on_a_trailing_slash_or_a_sub_path():
    """`blossom_url` and the stored media server can disagree about both."""
    r = _run()
    assert r["ownMediaSlashAsked"] == 0 and r["ownMediaTrailingSlash"] == "blossom"
    assert r["ownMediaSubPathAsked"] == 0 and r["ownMediaSubPath"] == "blossom"


def test_the_built_in_path_form_is_also_recognised():
    r = _run()
    assert r["builtinAsked"] == 0 and r["builtin"] == "blossom"


def test_every_other_host_is_still_probed():
    """This is what the function is FOR; silencing it everywhere would be the worse bug."""
    r = _run()
    assert r["strangerAsked"] == 1
    assert r["strangerUrl"].endswith("/.well-known/nostr/nip96.json")


def test_nostr_build_keeps_its_hostname_answer():
    r = _run()
    assert r["nostrBuild"] == "nip96"


def test_a_bundle_with_no_config_does_not_throw():
    """A desktop/APK bundle with no instance has no CFG at all."""
    r = _run()
    assert r["noCfg"] == "blossom" and r["noCfgAsked"] == 1
