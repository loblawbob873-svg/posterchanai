"""app.js must not reference `PC` — the name exists in every OTHER client module and not in that one.

Run: venv-unified/bin/python -m unittest tests.client.test_app_globals

sync.js, notes.js, calendar.js and the rest all open with `const PC = window.__PC || {}`, so `PC.foo`
is idiomatic across this codebase. app.js is where that object is BUILT — it assigns straight to
`window.__PC` and never binds `PC` — so the identical line is a ReferenceError there, and one that
only fires when the code path runs.

It shipped: `PC.syncBlobs.CHUNK` inside the chunked-upload helper, which is reached only by a file
over 64 MB. Every large upload failed with `upload: PC is not defined` while everything else worked,
so nothing about the sweep looked wrong until someone read the failure list.

There is no linter over this file — it is a 25k-line IIFE — and pyflakes has no JavaScript
equivalent here, so this is the cheapest thing that catches the whole class.
"""

import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(REPO, "static", "js", "client", "app.js")


def _strip_comments_and_strings(src: str) -> str:
    """Crude but adequate: block comments, line comments, then quoted runs.

    Deliberately not a JS parser. A false NEGATIVE here (a real `PC.` hidden inside something this
    mangles) is possible; a false POSITIVE is what would make the test annoying, and stripping is
    what prevents those.
    """
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    src = re.sub(r"(?m)^\s*//.*$", " ", src)
    src = re.sub(r"(?m)\s//[^\n]*$", " ", src)
    src = re.sub(r"'(?:\\.|[^'\\\n])*'", "''", src)
    src = re.sub(r'"(?:\\.|[^"\\\n])*"', '""', src)
    src = re.sub(r"`(?:\\.|[^`\\])*`", "``", src, flags=re.S)
    return src


class TestAppJsHasNoPCBinding(unittest.TestCase):
    def test_app_js_never_uses_PC_dot(self):
        code = _strip_comments_and_strings(open(APP, encoding="utf-8").read())
        # `PC.` not preceded by a word char, a dot or an underscore — so window.__PC, _PC and PCSync
        # are all left alone; only a bare `PC.something` is a finding.
        hits = [m.start() for m in re.finditer(r"(?<![\w.$_])PC\.", code)]
        if hits:
            lines = sorted({code[:h].count("\n") + 1 for h in hits})
            self.fail(
                "app.js references a bare `PC.` at line(s) %s. That name is bound in the other client "
                "modules but NOT here — app.js builds the object and assigns it to window.__PC — so "
                "this is a ReferenceError the moment the line runs. Use a module-level value instead."
                % lines)

    def test_the_check_can_actually_see_one(self):
        """A stripper this crude could silently mangle the file into nothing, and then the test above
        passes for ever without looking at anything."""
        planted = _strip_comments_and_strings("function f(){ return PC.syncBlobs.CHUNK; }")
        self.assertTrue(re.search(r"(?<![\w.$_])PC\.", planted),
                        "the comment/string stripper is eating real code")

    def test_window_dunder_pc_is_not_a_finding(self):
        for ok in ["window.__PC = {", "window.__PC.syncBlobs.get(sha)", "_PC.foo", "PCSync.paint()"]:
            self.assertIsNone(re.search(r"(?<![\w.$_])PC\.", _strip_comments_and_strings(ok)), ok)


class TestNoDuplicateFunctionDeclarations(unittest.TestCase):
    """Two `function foo(){}` in one scope is not an error — the second silently replaces the first.

    app.js is a 25k-line IIFE with no linter over it, so the whole file is one scope and a name
    reused anywhere in it wins from wherever it was declared last. `_fmtBytes` was declared twice:
    the survivor stopped at MB, so anything genuinely large rendered as "4096.0 MB" and a GB-sized
    budget could not be displayed at all. Nothing failed; it just quietly produced worse output than
    the function someone thought they were calling.
    """

    def test_no_top_level_function_is_declared_twice(self):
        src = _strip_comments_and_strings(open(APP, encoding="utf-8").read())
        # Module-level declarations in this file are indented exactly two spaces.
        names = re.findall(r"(?m)^  (?:async )?function ([A-Za-z_$][\w$]*)\s*\(", src)
        dupes = sorted({n for n in names if names.count(n) > 1})
        self.assertFalse(
            dupes,
            "declared more than once at the top level of app.js: %s. The later declaration wins "
            "silently, so every caller gets whichever one happens to be last in the file." % dupes)

    def test_the_check_can_see_a_duplicate(self):
        planted = _strip_comments_and_strings(
            "  function dup(a){ return 1; }\n  function other(){}\n  function dup(a){ return 2; }\n")
        names = re.findall(r"(?m)^  (?:async )?function ([A-Za-z_$][\w$]*)\s*\(", planted)
        self.assertEqual(sorted({n for n in names if names.count(n) > 1}), ["dup"])


class TestSyncedPrefsRoundTrip(unittest.TestCase):
    """Every pref WRITTEN to the pcai:client-prefs doc must also be READ back from it.

    `saveClientPrefsNostr({k: v})` and `restoreClientPrefsNostr()` are two lists a hundred lines
    apart, and a key added to one and not the other fails in the quietest possible way: the value is
    published, so the sync looks done and the relay really does hold it, but no device ever adopts
    it. Nothing errors and nothing is lost — the setting just silently does not travel, which is
    indistinguishable from the sync being broken generally.

    The storage budgets are the reason this exists: localStorage is what a reinstall takes with it,
    so a budget that publishes but never restores would be lost by exactly the event it was meant to
    survive.
    """

    def test_every_saved_pref_is_also_restored(self):
        src = open(APP, encoding="utf-8").read()
        saved = set(re.findall(r"saveClientPrefsNostr\(\s*{\s*([^}]*)}", src))
        keys = set()
        for blob in saved:
            keys |= set(re.findall(r"([A-Za-z_$][\w$]*)\s*:", blob))
        self.assertTrue(keys, "found no saveClientPrefsNostr calls — has the helper been renamed?")
        body = src[src.index("async function restoreClientPrefsNostr"):]
        body = body[:body.index("\n  }")]
        missing = sorted(k for k in keys if ("pr.%s" % k) not in body)
        self.assertFalse(
            missing,
            "published to pcai:client-prefs but never read back in restoreClientPrefsNostr: %s. "
            "The value reaches the relay, so the sync looks like it worked, but no device adopts it."
            % missing)

    def test_the_budgets_are_in_that_set(self):
        """Named, because they are the ones a reinstall was losing."""
        src = open(APP, encoding="utf-8").read()
        for key in ("musicOfflineGB", "mediaCacheGB"):
            with self.subTest(key=key):
                self.assertIn("saveClientPrefsNostr({ %s" % key, src)
                self.assertIn("pr.%s" % key, src)


class TestUploadsAreFiledSafely(unittest.TestCase):
    """Where an upload is filed in the drive index.

    `Music` is not an ordinary folder: FilesIdx.isEncFolder hardcodes it as ENCRYPTED, and the music
    library reads everything in it with the drive master key. So a plain, unencrypted blob filed
    under Music is listed as a track that cannot be decrypted — a broken row in the library, which is
    worse than leaving the file unfiled. Anything that really belongs in the library has to go
    through uploadMusicTrack (compress to opus, encrypt, then index), never through a folder label.
    """

    def test_no_plain_upload_is_filed_under_music(self):
        src = _strip_comments_and_strings(open(APP, encoding="utf-8").read())
        # strings are blanked by the stripper, so match the surviving structure of the call
        raw = open(APP, encoding="utf-8").read()
        bad = [m.start() for m in re.finditer(r"uploadBlob\([^;]{0,200}?folder:\s*'Music'", raw)]
        if bad:
            lines = sorted({raw[:b].count("\n") + 1 for b in bad})
            self.fail("uploadBlob is filing a plain blob under Music at line(s) %s — that folder is "
                      "encrypted by definition, so the result is a track the player cannot read. Use "
                      "uploadMusicTrack." % lines)
        self.assertIn("uploadMusicTrack(", src, "the library path has gone — check this guard still applies")

    def test_the_check_can_see_one(self):
        planted = "const u = await uploadBlob(f, {folder:'Music'});"
        self.assertTrue(re.search(r"uploadBlob\([^;]{0,200}?folder:\s*'Music'", planted))

    def test_both_action_rows_keep_files_the_same_way(self):
        """A result arrives either as an /api/files/ artifact or as a base64 payload, so there are two
        button rows for one set of actions — and every time one row got something the other did not,
        it was a bug. This one: `ytdl`'s MP3 is an artifact and a generated song is a payload, so a
        track ended up in the music library or in a folder depending on which command made it.

        Both save paths must go through _keepBytes, which is the single answer to where a kept file
        goes."""
        src = open(APP, encoding="utf-8").read()
        for fn in ("async function saveFileToBlossom", "async function saveEffectToBlossom"):
            i = src.index(fn)
            body = src[i:i + 2000]
            with self.subTest(fn=fn):
                self.assertIn("_keepBytes(", body,
                              "%s must route through _keepBytes, or the two rows disagree about "
                              "where a saved file belongs" % fn)

    def test_audio_reaches_the_library_not_a_folder(self):
        src = open(APP, encoding="utf-8").read()
        self.assertIn("uploadMusicTrack(file)", src,
                      "_keepBytes must hand audio to the library path; a folder called Music is "
                      "encrypted by definition and would list it as an unplayable track")
        self.assertIn('data-kind="${enc(kind||\'\')}"', src,
                      "the artifact row's Save button must carry the kind — that row is rendered from "
                      "persisted markdown and has nothing else to tell a song from a screenshot")

    def test_uploads_can_be_filed_at_all(self):
        """The composer used to upload straight past the index, so every picture posted from it was
        in the drive as an unnamed sha256."""
        src = open(APP, encoding="utf-8").read()
        self.assertIn("if(opts && opts.folder) _fileUnder(", src)
        self.assertIn("folder:'Posts'", src)


class TestParkedTimelineKeepsLivePosts(unittest.TestCase):
    """Desktop mode parks an unfocused window: its DOM leaves #feed and VIEW goes to whichever window
    took focus. Every painter keys on VIEW, so the timeline is alive and on screen in another window
    while VIEW says 'profile'.

    flushLive drained its buffer with a splice and THEN returned on that VIEW check, so the posts
    were not deferred, they were destroyed — and nothing backfills, because markEosed only draws on
    the first EOSE. Refocusing showed exactly what it showed before: "not showing new posts when
    other window is focused".
    """

    def test_flush_live_does_not_drop_a_parked_window_s_posts(self):
        src = open(APP, encoding="utf-8").read()
        i = src.index("function flushLive()")
        body = src[i:i + 1600]
        self.assertIn("_tlParked()", body,
                      "flushLive must keep live posts when the timeline is parked in an unfocused "
                      "desktop-mode window; the splice above has already emptied the buffer, so an "
                      "early return here deletes them")
        self.assertIn("_livePending.push(ev)", body)

    def test_they_are_buffered_in_the_first_place(self):
        """The subscription's own handler gates on VIEW too, so without this there is nothing for
        flushLive to keep."""
        src = open(APP, encoding="utf-8").read()
        self.assertIn("(VIEW===view || _tlParked())", src)

    def test_parked_means_alive_but_not_current(self):
        src = open(APP, encoding="utf-8").read()
        i = src.index("function _tlParked()")
        body = src[i:i + 300]
        for needle in ("window.PCOS", "tl-notes"):
            self.assertIn(needle, body,
                          "parked is 'the timeline DOM exists while VIEW is elsewhere, in desktop "
                          "mode' — any looser test would buffer for a timeline that is simply gone")


class TestSyncedFolderThumbnails(unittest.TestCase):
    """A preview in a synced folder means fetching and DECRYPTING a whole blob on this device.

    There is no server-side thumbnail and there cannot be one — the server cannot read the picture.
    That is affordable for what is on screen and ruinous for a folder of thousands, so four limits
    hold it up and none of them is decorative: lazy (only what was scrolled to), bounded (a fast
    scroll must not open hundreds of parallel decrypts), size-capped, and LRU-revoked (object URLs
    leak the whole picture until the tab closes otherwise).
    """

    def test_previews_are_lazy_bounded_capped_and_revoked(self):
        src = open(APP, encoding="utf-8").read()
        for needle, why in (
            ("IntersectionObserver", "previews must be lazy — a folder can hold thousands"),
            ("_THUMB_PAR", "a fast scroll would otherwise start hundreds of parallel decrypts"),
            ("_THUMB_MAX", "a full-size photo IS the whole file; past a point it is not worth it"),
            ("revokeObjectURL", "object URLs leak every picture drawn until the tab is closed"),
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, src, why)

    def test_a_late_decrypt_does_not_paint_a_dead_card(self):
        """The grid is rebuilt on every navigation, so a decrypt landing after the user moved on
        must not write into a card that has left the page."""
        src = open(APP, encoding="utf-8").read()
        i = src.index("function _bindThumbs")
        self.assertIn("isConnected", src[i:i + 900])

    def test_a_synced_folder_has_no_delete_button(self):
        """Deleting from a synced folder is not a drive operation — it has to become a tombstone in
        the manifest and then a deletion on every other device, which is the sweep's job and is
        guarded by three snapshots and a collapse check. A button here writing the manifest directly
        would be a second, unguarded way to delete someone's files off every machine they own."""
        src = open(APP, encoding="utf-8").read()
        i = src.index("async function _renderSyncedRoot")
        body = src[i:i + 6000]
        self.assertNotIn("delsync", body)
        self.assertIn("keepsync", body)


if __name__ == "__main__":
    unittest.main()
