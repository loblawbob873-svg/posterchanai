"""A git NIP-98 token signed for ONE repo does not authorize another whose id is a suffix of it
(security review #8).

`verify_nip98` bound a token to a repo by checking that the signed `u` path CONTAINED `<id>.git/<route>`,
with no boundary: an owner's token for `myrepo.git/delete` also passed for `repo.git/delete` (and for
`epo.git`, `o.git`, ...), within the replay window. The id must now be a whole path segment.
"""
import base64
import json

from app.services import git_auth
from app.services.nostr.event import build_event

SK = (0xA11CE).to_bytes(32, "big")


def _hdr(u, method="POST"):
    ev = build_event(SK, 27235, "", tags=[["u", u], ["method", method]])
    return "Nostr " + base64.b64encode(json.dumps(ev).encode()).decode(), ev["pubkey"]


def test_a_token_for_myrepo_does_not_delete_repo():
    h, pk = _hdr("https://poster.place/git/npub1owner/myrepo.git/delete")
    assert git_auth.verify_nip98(h, "POST", "myrepo.git/delete", {pk}) == pk
    assert git_auth.verify_nip98(h, "POST", "repo.git/delete", {pk}) is None
    assert git_auth.verify_nip98(h, "POST", "o.git/delete", {pk}) is None


def test_the_read_needle_is_a_whole_segment_too():
    h, pk = _hdr("https://poster.place/git/npub1owner/myrepo.git/info/refs", "GET")
    assert git_auth.verify_nip98(h, "GET", "myrepo.git", {pk}) == pk
    assert git_auth.verify_nip98(h, "GET", "repo.git", {pk}) is None
    h2, _ = _hdr("https://poster.place/git/npub1owner/myrepo.gitx/info/refs", "GET")
    assert git_auth.verify_nip98(h2, "GET", "myrepo.git", {pk}) is None
