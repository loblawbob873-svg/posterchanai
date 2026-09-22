"""Every guard below is broken ON PURPOSE, and the test that owns it must NOTICE.

Run: venv-unified/bin/python -m pytest tests/test_the_suite_can_actually_fail.py

WHY THIS EXISTS. A passing suite is evidence of nothing unless the tests can fail. This repo has
shipped, repeatedly, with green tests over broken code, and every single time the mechanism was the
same — the assertion was not measuring what it appeared to measure:

  * a `str.replace()` proof whose needle had different whitespace, so the "mutation" was a no-op and
    the suite dutifully reported "0 failures" about unmodified code;
  * `grep -c "^FAILED"` against output pytest does not phrase that way — always 0, always green;
  * a stylesheet check that searched for a selector as a SUBSTRING and so matched a rule gated to one
    platform, while the feature was dark everywhere else;
  * a DOM harness that memoised its nodes for ever, so a handler bound to a node the real app had
    already replaced kept working in the test — that one shipped the Concord attach bug AND then the
    Concord paste bug, green both times;
  * a revision-bump guard that asked "did the ebuild change?" when the question is "did its VERSION
    change?" — an unrelated edit inside the file satisfied it.

None of those are careless typing. They are all the same shape: a check that cannot distinguish the
working world from the broken one. The only way to find that shape is to BUILD the broken world and
see whether anything objects.

So each entry names a file, a mutation that reintroduces a real bug this repo has actually shipped,
and the test that must go red. The mutation is applied, the test is run, the file is restored in a
`finally`. If a mutation ever stops applying, that is a failure too — a proof that silently matches
nothing is the very thing this file exists to prevent.
"""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Reuse the interpreter running pytest, including in Docker and detached worktrees.
PY = sys.executable

# THE MUTATION HAPPENS IN A THROWAWAY WORKTREE, NEVER IN THE WORKING TREE.
#
# checkall.py runs the pytest suites and ~20 browser checks CONCURRENTLY, and those browsers load
# `static/js/client/app.js` from disk. Editing a shipped file here — even for the half-second a
# mutation lives — would corrupt whatever else happened to read it, and the resulting failure would
# land on an innocent check with nothing to explain it. That is the same rule docs/TESTING.md states
# for `streamserver/mediamtx.pid`: a check must never write into the working tree's live state.
#
# A worktree is the cheap way to get an honest copy: 3,113 tracked files, no history, no venv. The
# uncommitted work is carried across as a patch, so this measures the tree as it is RIGHT NOW and
# not as it was at the last commit — which for a guard being added in this very session is the
# whole point.

# (label, source file, needle, replacement, test target)
#
# Keep this list SHORT and load-bearing. Each entry costs two pytest subprocesses, and a slow suite is
# a suite people skip — which is its own way of having no tests.
def _current_active_color():
    """The frame colour as it is TODAY, not as it was when this list was written.

    This entry carried the literal `active_color = \\#1e525fff`. That is a value the product is
    expected to change — it moved to #257281 to bring a native window's frame into the same accent
    family as a PosterChan one — and when it did, the needle matched nothing, the mutation was never
    applied, and this canary failed for a reason that had nothing to do with the suite's ability to
    fail. A mutation harness that goes red whenever the code it mutates is edited teaches people to
    edit the harness, which is the opposite of what it is for.
    """
    import re as _re
    ini = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/wayfire.ini").read_text(encoding="utf-8")
    m = _re.search(r"^active_color = .+$", ini, _re.M)
    assert m, "wayfire.ini no longer sets active_color — this mutation has nothing to bite on"
    return m.group(0)


MUTATIONS = [
    (
        "deleting an account mid-publish aborts other users' scheduled posts",
        "app/services/scheduled_posts_service.py",
        "jobs = [(r.id, r.event_json) for r in due]",
        "jobs = ((r.id, r.event_json) for r in due)",
        "tests/test_a_scheduled_post_goes_out_once.py::AScheduledPostGoesOutOnce::"
        "test_account_deletion_mid_publish_skips_its_queue_and_keeps_other_users_moving",
    ),
    (
        "a frozen relay spends the permanent missing-event retry budget",
        "static/js/client/cards.js",   # the missing-event queue moved there with the cards split
        "const answered = live && !threw && evs.complete !== false;",
        "const answered = true;",
        "tests/client/test_need_event_retry.py",
    ),
    (
        "missing events are dropped instead of queued for another attempt",
        "static/js/client/cards.js",   # …and so did this one
        "map.set(id,n); _evQ.add(id); if(n>worst) worst=n;",
        "map.set(id,n); if(n>worst) worst=n;",
        "tests/client/test_need_event_retry.py",
    ),

    (
        "the focused window frame stops using the client's palette",
        "os/overlay/app-misc/posterchanos-shell/files/wayfire.ini",
        _current_active_color(),
        "active_color = \\#ff00ffff",
        "tests/test_window_frame_matches_the_client.py",
    ),
    (
        "a Blossom listing ignores the caller's limit and returns the whole 9.7 MB",
        "app/routers/blossom.py",
        "if limit is not None and limit >= 0:",
        "if False:",
        "tests/test_blossom_list_can_be_bounded.py",
    ),
    (
        "the Meme Builder binds paste to the node it was handed, which a render replaces",
        "static/js/client/meme.js",
        "const host=_mbPasteRoot;",
        "const host=root;",
        "tests/client/test_pasting_reaches_the_builder_and_the_email.py",
    ),
    (
        "the mail composer stops reading clipboard ITEMS, so a pasted screenshot is lost",
        "static/js/client/mail.js",
        "for(const it of [...(cd.items||[])]){",
        "for(const it of []){",
        "tests/client/test_pasting_reaches_the_builder_and_the_email.py",
    ),
    # ── 14 Sep: four reports in one day, each fixed and each proved catchable by hand. By hand is
    #    not a guarantee; these are the same proofs, run by the suite from now on.
    (
        "the theme's CRT sheet goes back on top of every photo, document and wallpaper",
        "static/css/client.css",
        ".scanlines{position:fixed;inset:0;pointer-events:none;z-index:-1;",
        ".scanlines{position:fixed;inset:0;pointer-events:none;z-index:9990;",
        "tests/client/test_decoration_never_crosses_a_document.py",
    ),
    (
        "a refused socket is held for its full grace again, so the relay count churns",
        "app/services/nostr_relay/server.py",
        'if self._note_refused(getattr(conn, "_pcai_ip", "") or ""):',
        'if False and self._note_refused(getattr(conn, "_pcai_ip", "") or ""):',
        "tests/test_a_refused_socket_is_not_held_open.py",
    ),
    (
        "the client filter fails OPEN on a repeated header, i.e. the switch is off for whoever repeats one",
        "app/services/nostr_relay/server.py",
        'if _one_header(hdrs, "Upgrade").lower() == "websocket":',
        'if hdrs.get("Upgrade", "").lower() == "websocket":',
        "tests/test_a_refused_socket_is_not_held_open.py",
    ),
    (
        "a video segment opens a websocket again, so one slow handshake ends playback",
        "app/services/media_center.py",
        "if hit and now - hit[1] < ACCOUNT_FRESH:",
        "if False and now - hit[1] < ACCOUNT_FRESH:",
        "tests/test_a_relay_blip_does_not_stop_playback.py",
    ),
    (
        "the reply window's own border is hidden again by the popup's show-only-the-composer rule",
        "static/css/client.css",
        ".os-popup-body.os-popup-compose > *:not(#modal-root):not(#pc-oswin-frame)",
        ".os-popup-body.os-popup-compose > *:not(#modal-root)",
        "tests/client/test_a_window_has_a_visible_border.py",
    ),
    # ── THE THREE THAT LOSE DATA. Everything above is something a person can SEE going wrong. These
    #    are the ones where the first symptom is that something is already gone, and each names a
    #    loss this repo has actually taken. They earn their seconds.
    (
        "the paid tier's prune stops limiting itself to feed kinds, so it deletes calendars",
        "app/services/nostr_relay/store.py",
        'base = f"origin = \'direct\' AND {_PRUNABLE_SQL} AND {self._not_preserved()}"',
        'base = f"origin = \'direct\' AND {self._not_preserved()}"',
        "tests/test_relay_prune.py",
    ),
    (
        "the Blossom age sweep stops exempting keep-flagged blobs, i.e. the encrypted drive",
        "app/services/blossom_service.py",
        "conds.append(and_(BlossomBlob.keep.is_(False),",
        "conds.append(and_(BlossomBlob.keep.is_(False) | BlossomBlob.keep.is_(True),",
        "tests/test_blossom_keep.py",
    ),
    (
        "a folder-sync delete stops waiting for the store to confirm it still holds the bytes",
        "static/js/client/syncexec.js",
        "if(held !== true){",
        "if(false){",
        # test_fs_bridge, NOT test_delete_and_restore_symmetry. The symmetry file stubs
        # `hasBlob: async () => true` everywhere — the store always confirms — so with the check
        # removed nothing there changes and it passes, which is what this entry measured on its
        # first run. The refusal is owned by the files that make the store say NO and I-CANNOT-SAY:
        # test_fs_bridge's `store_has="false"` / `"null"` against a real filesystem, and
        # exec_sim.js's B2/B3 scenarios. Pointing an entry at a plausible-looking file rather than
        # the one that exercises the branch is how a mutation harness reports coverage it has not got.
        "tests/client/test_fs_bridge.py",
    ),
    (
        "the XRP signer stops checking the payment belongs to the selected wallet",
        "app/services/exodus_xrp_codec.py",
        "if payment.account != wallet.address:",
        "if False:",
        "tests/test_an_xrp_payment_goes_where_you_typed.py",
    ),
]


def _git(*args, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd or ROOT), capture_output=True, text=True)


def _make_worktree(into: Path) -> str:
    """A detached checkout of HEAD with this session's uncommitted changes applied. '' on success."""
    made = _git("worktree", "add", "--detach", str(into), "HEAD")
    if made.returncode != 0:
        return f"could not create a worktree: {made.stderr.strip()[:200]}"
    diff = _git("diff", "--binary", "HEAD")  # include changed binary assets too
    if diff.returncode != 0:
        return f"could not read the working diff: {diff.stderr.strip()[:200]}"
    if diff.stdout.strip():
        applied = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"],
                                 cwd=str(into), input=diff.stdout, capture_output=True, text=True)
        if applied.returncode != 0:
            return f"could not apply the working diff: {applied.stderr.strip()[:300]}"
    return ""


def _run(target: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, "-B", "-m", "pytest", target, "-q", "-x", "-p", "no:cacheprovider"],
        cwd=str(cwd), capture_output=True, text=True,
        timeout=300,
    )


class TheSuiteCanFail(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = Path(tempfile.mkdtemp(prefix="pc-mutate-"))
        cls.tree = cls._tmp / "wt"
        cls.why = _make_worktree(cls.tree)

    @classmethod
    def tearDownClass(cls):
        _git("worktree", "remove", "--force", str(cls.tree))
        shutil.rmtree(cls._tmp, ignore_errors=True)
        _git("worktree", "prune")

    def setUp(self):
        # A skip that says WHY. Never a silent pass — "could not run" is not "passed".
        if self.why:
            self.skipTest(self.why)

    def test_every_guard_is_noticed_when_it_is_broken(self):
        for label, rel, needle, replacement, target in MUTATIONS:
            with self.subTest(guard=label):
                path = self.tree / rel
                self.assertTrue(path.exists(), f"{rel} is missing from the worktree")
                original = path.read_text()

                # A mutation that does not apply proves nothing and must be reported as loudly as a
                # test that does not fail — this is the exact shape that produced a false "0 failures".
                self.assertIn(needle, original,
                              f"the mutation for {rel} no longer applies; this proof is vacuous")
                self.assertEqual(original.count(needle), 1,
                                 f"{needle!r} is ambiguous in {rel}; the mutation would be imprecise")

                # THE CONTROL RUN, and it is not ceremony. Everything below reads an exit code, and
                # a test that cannot RUN in this worktree produces a non-zero one for reasons that
                # have nothing to do with the mutation — so without this, an entry whose target had
                # been renamed, or which imports something the worktree lacks, would report that the
                # suite catches a bug it has never once seen. "Could not run" is not "noticed".
                clean = _run(target, self.tree)
                self.assertEqual(
                    clean.returncode, 0,
                    f"{target} does not pass on the UNMUTATED worktree (pytest exit {clean.returncode}). "
                    f"Nothing can be concluded about {label!r} until it does — exit 5 means it "
                    f"collected no tests at all, 4 a usage error, 2/3 that it broke while running.\n"
                    + (clean.stdout + clean.stderr)[-8000:])

                try:
                    path.write_text(original.replace(needle, replacement, 1))
                    self.assertNotEqual(path.read_text(), original, "the mutation did not land")
                    # Exit CODE, never scraped output: reading pytest's prose for the word FAILED is
                    # how a green run was reported over a suite that never matched anything. And the
                    # code has to be exactly 1 — "a test failed". Accepting any non-zero was this
                    # file's own version of the bug it exists to find: pytest answers 5 for "no tests
                    # collected", 4 for a usage error and 2/3 when it breaks, and every one of those
                    # would have been read here as proof that the guard works.
                    broke = _run(target, self.tree)
                    self.assertEqual(
                        broke.returncode, 1,
                        f"{target} did not FAIL with this broken: {label}.\n"
                        + (f"That test does not measure what it claims to."
                           if broke.returncode == 0 else
                           f"pytest exited {broke.returncode}, which is not 'a test failed' — it is 'the run "
                           f"itself did not work', and that proves nothing either way.")
                        + "\n" + (broke.stdout + broke.stderr)[-8000:])
                finally:
                    path.write_text(original)

    def test_the_working_tree_was_never_touched(self):
        """The mutations happen in a copy. If one ever escapes, every later check is poisoned."""
        for _, rel, needle, _, _ in MUTATIONS:
            live = ROOT / rel
            self.assertIn(needle, live.read_text(),
                          f"{rel} in the WORKING TREE was mutated — the isolation failed")


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
