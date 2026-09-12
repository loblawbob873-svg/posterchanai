"""The bug SHAPES from 2026-09-11, checked across the codebase instead of one test per bug.

Eleven reports in a day. Every one was an instance of five shapes, and the per-feature tests missed
them all because each test was written around the edit that had just been made rather than the rule
the code is supposed to keep:

  1. THE SAME RULE, ENFORCED IN ONE COPY OF N. The relay list was written unconditionally in TWO
     save paths; `--concord` was forced in THREE places; the folder-sync bridge fallback went into
     ONE of seven call sites. Fixing the copy in front of you looks complete and is not.
  2. HAPPY PATH ONLY. `concord_invite` accepted an `nsec1…` private key because the only input ever
     tested was a valid invite.
  3. COLD STATE ONLY. The payment-rail lookup was skipped whenever the profile was already cached —
     the common case, and the one no test was ever in.
  4. ONE REALM ONLY. The start menu's rescan repaired the desktop's cache for free when both ran in
     one module instance, so the test passed with the fix removed.
  5. A REFUSAL NOBODY CAN SEE. A `disabled` button, a silent `return`, an `except` that logs to a
     logger with no handler. "Nothing happens" is not a diagnosis anybody can act on.

These are deliberately CHEAP, SOURCE-LEVEL scans. They cannot prove a feature works — that is what
the runtime tests beside them are for. What they can do is fail when a rule exists in one place and
not in its siblings, which is the failure that keeps reaching the operator.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def code(js):
    """Source with comments stripped — a rule must not be satisfied by prose. (A test of mine
    already failed once on a comment that merely NAMED the call it was banning.)"""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(?m)//.*$", "", js)


class ShapeOne_TheSameRuleInEveryCopy(unittest.TestCase):
    """A guard added to one site and not its siblings."""

    def test_every_relay_list_write_is_gated_on_the_switch(self):
        app = code(read("static/js/client/app.js"))
        for m in re.finditer(r"ClientSettings\.set\('relays',\s*([^)]*)\)", app):
            line = app[app.rfind("\n", 0, m.start()) + 1:app.index("\n", m.end())]
            val = m.group(1).strip()
            self.assertTrue(val in ("[]", "merged") or "if(on)" in line
                            or "relaysEnabled', true" in line,
                            "relay list written with the switch possibly off: " + line.strip()[:110])

    def test_concord_mode_is_never_forced_behind_the_operator(self):
        self.assertNotIn('modes = list(modes) + ["--concord"]',
                         read("app/services/bot_manager_service.py"))
        builds = code(read("static/js/admin-bots.js"))
        builds = builds[builds.index("function _buildModes("):]
        self.assertNotIn("modes.add('--concord')", builds[:builds.index("\n}")])

    def test_no_caller_re_invents_the_filesystem_bridge_rule(self):
        """THE SHAPE IS "ONE RULE, NOT ONE FUNCTION", AND I GOT THAT WRONG HERE.

        This used to demand `const FS_PICK = FS;` — one resolver, on the theory that two can
        disagree. They can, but these two are answering DIFFERENT QUESTIONS, and collapsing them
        broke the more important one: with a fallback inside `FS()`, every surface reporting
        `backgroundOwner:false` answered with itself, so two windows on one device could both sweep
        and both publish. `tests/client/test_folder_sync_finds_the_primary_surface.py` catches that.

        Writing needs the PRIMARY (`FS`); picking a folder needs A BRIDGE (`FS_PICK`) and writes
        nothing. The shape this class is really about is that no CALLER invents a third answer."""
        sync = code(read("static/js/client/sync.js"))
        # `assertIn("const FS_PICK = ")` would match `const FS_PICK = FS;` as happily as the real
        # thing, which is the vacuous-assertion shape this whole file exists to catch. Demand the
        # two properties that actually distinguish them.
        self.assertNotIn("const FS_PICK = FS;", sync,
                         "the picker was collapsed into the writer again")
        pick = next(l for l in sync.splitlines() if l.strip().startswith("const FS_PICK"))
        self.assertIn("FS()", pick, "the picker must still prefer the primary surface")
        self.assertIn("window.pcFs", pick, "the picker has no fallback, so the button stays dead")
        # The LAST thing FS() does when the walk finds no primary must be to refuse. Reading the
        # final `return` is the honest way to ask that — an `endswith` on the slice is defeated by
        # the closing brace and passes on correct code, which is the same vacuous shape again.
        fs = sync[sync.index("const FS = () => {"):sync.index("const FS_PICK")]
        last_return = fs.rstrip()[fs.rstrip().rindex("return "):].split("\n")[0].strip()
        self.assertEqual(last_return, "return null;",
                         "FS() ends by falling back to the local bridge — that mints a second "
                         f"writer on every surface reporting backgroundOwner:false (got {last_return!r})")
        stray = [l.strip() for l in sync.splitlines()
                 if "window.pcFs" in l and "const FS" not in l and "return window.pcFs" not in l
                 and "this app build has no filesystem" not in l]
        self.assertEqual(stray, [], "a caller re-invents the bridge rule: " + "; ".join(stray[:2]))


class ShapeTwo_TheUnhappyPath(unittest.TestCase):
    """Input nobody tried."""

    def test_a_credential_field_is_validated_server_side(self):
        src = read("app/routers/bots.py")
        self.assertIn("def _vet_config(", src)
        self.assertEqual(src.count("_vet_config("), 3, "a bot-config write path is unvetted")
        self.assertIn("nsec1", src, "the check does not name the key it refuses")


class ShapeFive_ARefusalNobodyCanSee(unittest.TestCase):
    """"Nothing happens" is the report this produces every time."""

    def test_a_folder_sync_control_is_only_disabled_when_the_bridge_really_is_missing(self):
        """AND THE RULE IS NOT "NEVER DISABLED", WHICH IS WHAT I FIRST WROTE HERE.

        The report was a button that did nothing — disabled with a working bridge one window away,
        explaining nothing and firing no handler. The over-correction was to forbid `disabled`
        outright, which contradicts
        `tests/client/test_a_popped_out_window_can_still_do_its_job.py`: on a build with NO bridge at
        all a live button is a worse lie than a dead one, because pressing it can only fail.

        So `disabled` must be CONDITIONED on the bridge, never rendered unconditionally — and the
        control has to say which of the two reasons it is (`FS_WHY`, asserted below)."""
        sync = code(read("static/js/client/sync.js"))
        row = sync[sync.index("sync-attach"):sync.index("Set up on this device")]
        if "disabled" in row:
            self.assertIn("fs ?", row, "the attach button is disabled unconditionally — it will be "
                                       "dead on a device that can sync perfectly well")
            self.assertIn("FS_WHY()", row, "a disabled control must say why it is disabled")
        self.assertIn('<button class="btn btn-neon" id="sync-add">', sync)

    def test_a_refused_bridge_says_which_of_the_two_reasons_it_is(self):
        sync = read("static/js/client/sync.js")
        self.assertIn("const FS_WHY", sync)
        self.assertIn("this app build has no filesystem access", sync)
        self.assertIn("this window cannot reach the folder bridge", sync)

    def test_the_concord_generator_reports_a_missing_model(self):
        """It returned "" for years-worth of causes; the caller read that as 'nothing to say'."""
        src = read("botframework/concordListener.py")
        self.assertIn("no AI is configured", src)
        self.assertIn("generate_reply failed", src)

    def test_an_upstream_that_refuses_our_subscription_is_logged(self):
        """Asserted on a SHORT phrase: these messages wrap across source-string concatenation
        breaks, and a longer literal tests the line wrapping rather than the message. (This exact
        mistake has now bitten three of my own tests today, so it is worth writing down.)"""
        src = read("app/services/nostr_relay/firehose.py")
        self.assertIn("refused a ", src)
        self.assertIn("subscription: %s", src)
        self.assertIn("_split_filter", src, "the oversized-filter split is gone")

    def test_a_bot_whose_room_is_switched_off_says_so(self):
        self.assertIn("join that room", read("app/services/bot_manager_service.py"))
        self.assertIn("join that room", read("static/js/admin-bots.js"))


if __name__ == "__main__":
    unittest.main()
