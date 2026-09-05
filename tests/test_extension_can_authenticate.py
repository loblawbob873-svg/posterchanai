"""THE RELAY DEMANDS NIP-42 AUTH FOR THE VAULT, SO THE EXTENSION MUST BE ABLE TO GIVE IT.

MEASURED against the live relay:

    REQ kinds:[30078] authors:[<owner>] #l:[pcai-pw]
      -> CLOSED  auth-required: NIP-78 reads require AUTH and matching authors

A brand-new Firefox install synced nothing and said nothing. The cause was not pairing -- it was
re-paired twice -- and not the data: the vault is on the relay, 118 documents, every one carrying
the `l=pcai-pw` label the extension asks for. It was that AMO had signed 1.4.6 on 2026-09-02, the
gate landed on 2026-09-03, and the rebuild kept the SAME VERSION NUMBER. Firefox sees 1.4.6
installed and 1.4.6 available and never updates, so the browser is pinned to a build that cannot
answer the challenge, for ever, through any number of reinstalls.

Two rules follow, and this file holds both:

  * the extension must carry the AUTH half for as long as the relay enforces the gate;
  * a change to that half must move the version, because a signed artifact is addressed by version
    and a rebuild under the old number is invisible to every browser that already has it.
"""
from pathlib import Path
import json
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
BG = (ROOT / "extension/background.js").read_text(encoding="utf-8")
MANIFEST = json.loads((ROOT / "extension/manifest.json").read_text(encoding="utf-8"))
SERVER = (ROOT / "app/services/nostr_relay/server.py").read_text(encoding="utf-8")


class TestTheGateAndTheAnswerShipTogether(unittest.TestCase):
    def test_the_relay_still_enforces_the_gate(self):
        """If this ever stops being true the rest of the file is arguing with nobody."""
        self.assertIn("NIP-78 reads require AUTH", SERVER)

    def test_the_extension_signs_an_auth_event(self):
        self.assertIn("22242", BG, "the extension cannot answer the relay's AUTH challenge")

    def test_it_replays_the_request_only_after_the_relay_says_yes(self):
        """NIP-42 wants the positive OK before protected traffic is retried; sending the REQ straight
        after AUTH works on permissive relays and is rejected by strict ones."""
        block = BG[BG.index("if(m[0] === 'AUTH'"):]
        block = block[: block.index("else if(m[0] === 'EOSE'")]
        self.assertIn("c.authed", block)
        self.assertIn("if(c.authed)", block, "the REQ is replayed without waiting for the OK")

    def test_the_owner_bound_authors_filter_is_present(self):
        """The gate refuses a filter that does not name the authenticated owner, so a REQ without
        `authors` is closed no matter how well the AUTH went."""
        self.assertIn("authors:[cfg.pubkey]", BG.replace(" ", ""))

    def test_the_vault_request_asks_for_the_label_the_documents_carry(self):
        """Measured on the relay: all 118 of the owner's `pcai:pw` documents carry `l=pcai-pw`."""
        self.assertIn("'#l':[L_TAG", BG.replace(" ", ""))
        self.assertIn("const L_TAG = 'pcai-pw'", BG)


class TestAChangedExtensionGetsANewVersion(unittest.TestCase):
    """A SIGNED ARTIFACT IS ADDRESSED BY VERSION. Rebuilding under the old number produces a
    different file that every browser already holding it will refuse to fetch -- which is exactly how
    a fix that was written, built and published still never reached the person who needed it."""

    def test_the_version_is_past_the_build_that_could_not_authenticate(self):
        v = tuple(int(x) for x in re.findall(r"\d+", MANIFEST["version"])[:3])
        self.assertGreater(v, (1, 4, 6),
                           "1.4.6 is the version AMO signed before the AUTH code existed; shipping "
                           "this change under it leaves every installed copy pinned to the old one")

    def test_the_manifest_declares_the_alarms_permission_the_worker_needs(self):
        """Unrelated to AUTH and checked here because it is the other way sync dies silently: a
        suspended MV3 worker runs no timers, and only chrome.alarms wakes one."""
        self.assertIn("alarms", MANIFEST.get("permissions", []))


if __name__ == "__main__":
    unittest.main()
