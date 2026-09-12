"""«Set up on this device» must either open a chooser or say why it cannot.

Reported twice, most recently: "FOlder sync still not working after latest update on desktop
'Set up on this device' nothing happens". It was not a handler that failed — it was a button
rendered `disabled`, which fires no handler and explains nothing, and an "Add a folder…" button
that was not rendered at all. Both were gated on `FS()`, whose job is to answer *who may WRITE
this device's folders* by walking `window.opener` up to the primary surface. An Electron window
created by the MAIN PROCESS has no `window.opener`, so the walk has nothing to climb and returns
null — on the one surface the desktop actually opens Folder Sync in.

No opener is not evidence of a second writer; it is evidence we could not ask. `FS_PICK()` falls
back to this document's own `pcFs` for PICKING only, so the one-writer rule that `FS()` enforces
for sweeping is untouched — which is the half these tests are really guarding.
"""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SYNC = (ROOT / "static/js/client/sync.js").read_text(encoding="utf-8")


def _code(js):
    """The CODE, with comments removed — a rule about what a handler calls must not be satisfied or
    broken by prose. (This test first failed on a comment that merely *named* the old call.)"""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(?m)//.*$", "", js)


def _fn(src, head):
    """The text of one function/arrow, from its opening line to the matching close."""
    i = src.index(head)
    depth, j = 0, i
    while True:
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
        j += 1


class TheButtonIsNeverDead(unittest.TestCase):

    def test_set_up_on_this_device_is_not_rendered_disabled(self):
        """A disabled button IS "nothing happens": no handler runs and nothing is said."""
        row = _code(SYNC[SYNC.index("sync-attach"):SYNC.index("Set up on this device")])
        self.assertNotIn("disabled", row)

    def test_add_a_folder_is_always_rendered(self):
        """Gated on the bridge, it vanished — a folder list with no way to add to it."""
        self.assertIn('<button class="btn btn-neon" id="sync-add">', SYNC)
        self.assertNotIn("""${fs ? '<button class="btn btn-neon" id="sync-add">""", SYNC)

    def test_both_pickers_ask_the_pick_resolver_and_explain_a_refusal(self):
        for head in ("feed.querySelectorAll('.sync-attach').forEach",
                     "if(add) add.onclick = async () =>"):
            body = _code(_fn(SYNC, head))
            self.assertIn("FS_PICK()", body, head)
            self.assertIn("FS_WHY()", body, head)
            self.assertNotIn("FS().pick()", body, head)

    def test_a_cancelled_chooser_stays_silent(self):
        """`pc:fs:pick` answers null on cancel and THROWS when refused, so falsy is always a
        cancel. Toasting there would fire on every dismissed dialog."""
        body = _code(_fn(SYNC, "feed.querySelectorAll('.sync-attach').forEach"))
        self.assertIn("if(!picked) return;", body)
        self.assertNotIn("no folder was chosen", body)


class TheWriterRuleIsUntouched(unittest.TestCase):

    def test_the_sweeper_still_resolves_through_the_opener_walk(self):
        """`FS()` is the one-writer rule. The fallback must live in FS_PICK, never in FS."""
        fs = _code(_fn(SYNC, "const FS = () => {"))
        self.assertIn("window.opener", fs)
        self.assertIn("FS_OPENER_HOPS", fs)
        self.assertNotIn("FS_PICK", fs)

    def test_the_two_resolvers_answer_two_different_questions(self):
        """TWO RESOLVERS, AND COLLAPSING THEM BREAKS THE RULE THE FIRST ONE IS FOR.

        The original half-fix gave the two pick buttons a fallback and left `reconcileTrash`,
        `restoreTrash`, the sweep, `verifyFolder` and `conflictCleanup` on the strict resolver, so a
        device picked a folder and immediately said "this device has no filesystem access". The
        correction to THAT was to delete FS_PICK and put the fallback in `FS()` — which made every
        surface claiming `backgroundOwner:false` answer with itself, i.e. a second writer on one
        device, which is the single thing this resolver exists to prevent.

        Neither question is the other. Writing needs the PRIMARY; picking needs A BRIDGE."""
        self.assertNotIn("const FS_PICK = FS;", SYNC,
                         "the picker was collapsed into the writer — a fallback in FS() mints a "
                         "second writer on every secondary surface")
        pick = _fn(SYNC, "  const FS_PICK = ")
        self.assertIn("FS()", pick, "the picker must still prefer the primary when there is one")
        self.assertIn("window.pcFs", pick, "the picker has no fallback, so the button stays dead")

    def test_the_paths_that_write_still_demand_the_primary(self):
        """The classification is the rule. A read or a button may use the picker; anything that
        touches the folder must not, or two windows sweep the same tree."""
        for fn_start in ("  async function syncFolder(",
                         "  async function conflictCleanup(",
                         "  async function verifyFolder("):
            if fn_start not in SYNC:
                continue
            body = _code(_fn(SYNC, fn_start))
            with self.subTest(fn=fn_start.strip()):
                self.assertNotIn("FS_PICK()", body,
                                 f"{fn_start.strip()} writes to the folder and must resolve the "
                                 "PRIMARY surface, not merely a bridge")

    def test_every_filesystem_consumer_goes_through_the_one_resolver(self):
        """No call site may reach for `window.pcFs` directly and re-invent the rule."""
        body = _code(SYNC)
        # `FS_WHY` is the one legitimate reader: it distinguishes "this build has no bridge at all"
        # from "this window cannot reach one", which is the difference between two messages. It
        # decides nothing about access.
        why = _code(_fn(SYNC, "  const FS_WHY = () => {"))
        direct = [ln.strip() for ln in body.splitlines()
                  if "window.pcFs" in ln and "const FS" not in ln
                  and "return window.pcFs" not in ln and ln.strip() not in why]
        self.assertEqual(direct, [], "a caller bypasses FS(): " + "; ".join(direct[:3]))

    def test_the_fallback_lives_in_FS_itself(self):
        fs = _code(_fn(SYNC, "const FS = () => {"))
        self.assertIn("return window.pcFs || null;", fs,
                      "FS() still answers null on a window with no opener")


class TheResolversRun(unittest.TestCase):
    """Execute the real resolvers under node against the window shapes that matter."""

    def _run(self, setup):
        src = SYNC[:SYNC.index("const FS_WHY")]
        src = src[src.index("const FS_OPENER_HOPS"):]
        script = (
            "global.window=global;" + setup + "\n" + src +
            "\nconsole.log(JSON.stringify({fs:!!FS(),pick:!!FS_PICK()}));"
        )
        done = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
        assert done.returncode == 0, done.stdout + done.stderr
        import json
        return json.loads(done.stdout)

    def test_the_primary_surface_resolves_both_ways(self):
        out = self._run("window.pcShell={backgroundOwner:true};window.pcFs={pick(){}};")
        self.assertTrue(out["fs"])
        self.assertTrue(out["pick"])

    def test_a_window_with_no_opener_can_still_pick_but_must_not_claim_to_be_the_writer(self):
        """THE BUG AND THE OVERCORRECTION, WHICH ARE BOTH IN THIS ONE CASE.

        A main-process window has no `window.opener`, so the walk has nothing to climb. That left
        the "Set up on this device…" button disabled and every trash path refusing with "this device
        has no filesystem access" while the folder synced perfectly from another surface.

        The fix for that was to let `FS()` fall back to this document's own bridge — which quietly
        broke the rule the resolver exists for. A surface declaring `backgroundOwner:false` was then
        answering with ITSELF, so two windows on one device could both sweep and both publish.
        `test_folder_sync_finds_the_primary_surface.py` catches it with a chain of secondaries.

        Both are satisfied by keeping the two questions apart: WRITING still demands a primary, and
        PICKING — a person pressing a button in the window they are looking at, which writes
        nothing — is happy with any bridge."""
        out = self._run("window.pcShell={backgroundOwner:false};window.pcFs={pick(){},listTrash(){}};"
                        "window.opener=null;")
        self.assertFalse(out["fs"], "a surface that says it is not the owner must not become a "
                                    "second writer just because it cannot see the first")
        self.assertTrue(out["pick"], "picking a folder needs a bridge, not authority")

    def test_a_window_whose_opener_is_the_primary_uses_the_primary(self):
        out = self._run("window.pcShell={backgroundOwner:false};window.pcFs={pick(){}};"
                        "window.opener={pcShell:{backgroundOwner:true},pcFs:{pick(){}},closed:false};")
        self.assertTrue(out["fs"])
        self.assertTrue(out["pick"])

    def test_no_bridge_anywhere_resolves_to_nothing(self):
        out = self._run("window.pcShell={backgroundOwner:false};window.opener=null;")
        self.assertFalse(out["fs"])
        self.assertFalse(out["pick"])


if __name__ == "__main__":
    unittest.main()
