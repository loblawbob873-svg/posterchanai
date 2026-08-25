"""A document in Files opens in PosterChan Code, and saving puts it back.

Run: venv-unified/bin/python -m pytest tests/client/test_files_open_in_code.py

Files → Blossom holds content-addressed BLOBS, not paths. PosterChan Code opens files by workspace
path (`/api/code/file?path=…`), so there was no way to get a document from the drive into the editor
at all — Office had one (the 📝 button) and Code had none.

The round trip is deliberately split: the editor holds the text and knows nothing about Blossom;
app.js owns the drive index, the encryption and the folder, so it owns the save. Content addressing
means an edit is a NEW blob — the index is re-pointed and the old blob is left recoverable, exactly
as the office save does.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(ROOT, "static", "js", "client", "app.js")
CODE = os.path.join(ROOT, "static", "js", "client", "code.js")


def _read(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def _decomment(js):
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", js)


def _fn(src, head):
    i = src.index(head)
    j = src.index("{", i)
    depth, k = 0, j
    while k < len(src):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
        k += 1
    raise AssertionError(f"{head} never closes")


class FilesOpenInCode(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _read(APP)
        cls.code = _read(CODE)

    # ---- the way in -------------------------------------------------------------------------
    def test_there_is_one_open_door_and_it_is_bound(self):
        """ONE door per file, and it is the FILE. Two small icon buttons crowded a card that is
        meant to be an icon and a name, and forced the choice before you had said "open" at all;
        collapsing them to one still left a control that means "open" beside a name that already
        means it. A file manager asks AFTER: you open a thing, and if more than one program handles
        it, it asks which. See ClickingTheFileIsHowYouOpenIt for the whole contract."""
        self.assertIn("$$('.file-card[data-sha] > a', grid)", self.app,
                      "nothing binds the card's link, so no way to open a Files document")
        self.assertIn("_openWithSheet", self.app)

    def _code_ext(self):
        """The SHIPPED regex, compiled and run against real names. Splitting the alternation on `|`
        and looking for `yml` fails against `ya?ml`, which is a correct pattern — the test has to
        ask the same question the code does."""
        m = re.search(r"const _CODE_EXT = (/[^\n]*?/i);", self.app)
        self.assertTrue(m, "_CODE_EXT moved — re-point this test")
        body = m.group(1)[1:-2]                      # strip the / … /i
        return re.compile(body, re.I)

    def test_it_does_not_offer_to_edit_a_picture(self):
        """A .png opened as text is a screenful of mojibake and a corrupted file once saved."""
        rx = self._code_ext()
        for binary in ("shot.png", "a.jpg", "a.jpeg", "a.gif", "a.webp", "clip.mp4",
                       "song.mp3", "doc.pdf", "bundle.zip", "game.xdc"):
            self.assertIsNone(rx.search(binary), f"Code offers to edit {binary} as text")
        for text in ("notes.md", "a.json", "s.py", "a.js", "run.sh", "conf.yml",
                     "conf.yaml", "a.css", "a.txt"):
            self.assertIsNotNone(rx.search(text), f"Code will not open {text}")

    def test_a_file_with_no_dot_can_still_be_opened(self):
        """`_CODE_EXT` anchors on `\.`, so `Makefile`, `Dockerfile`, `README`, `LICENSE` and
        `.gitignore` could never match it however they were spelled in the alternation — and those
        are exactly the files somebody opens an editor for."""
        self.assertIn("_CODE_BARE", self.app, "a file with no extension can never be opened")
        m = re.search(r"const _CODE_BARE = (/[^\n]*?/i);", self.app)
        self.assertTrue(m, "_CODE_BARE moved — re-point this test")
        rx = re.compile(m.group(1)[1:-2], re.I)
        for name in ("Makefile", "Dockerfile", "README", "LICENSE", ".gitignore", ".env"):
            self.assertIsNotNone(rx.search(name), f"{name} is still unopenable")

    def test_the_server_s_mime_is_believed_when_the_name_says_nothing(self):
        """A file uploaded without an extension still has a type, and `text/*` is a better witness
        than a name somebody typed."""
        self.assertIn("_codeable", self.app)
        i = self.app.index("const _codeable")
        body = self.app[i:self.app.index(";", i)]
        self.assertIn("text\\/", body, "the MIME type is ignored entirely")

    def test_a_pdf_can_be_opened_in_office(self):
        """Collabora opens and annotates PDFs, and a PDF is the document people actually have. It
        was in NEITHER list, so the commonest case offered no button at all."""
        m = re.search(r"const _OFFICE_EXT = (/[^\n]*?/i);", self.app)
        rx = re.compile(m.group(1)[1:-2], re.I)
        self.assertIsNotNone(rx.search("statement.pdf"), "a PDF still offers no way to open it")

    def test_no_stylesheet_rule_outlives_the_buttons(self):
        """The Open buttons are gone (they became the card's own click). A rule for a class nothing
        draws is the debris that makes the next person believe the control still exists."""
        css = _read(os.path.join(ROOT, "static", "css", "client.css"))
        for cls_ in (".openbtn", ".opensync", ".openhost"):
            self.assertNotIn(cls_, css, f"{cls_} is styled but never drawn")

    def test_a_spreadsheet_belongs_to_office_not_to_code(self):
        self.assertIsNone(self._code_ext().search("sheet.csv"),
                          "csv is in _OFFICE_EXT; offering both makes the two buttons fight")

    def test_binary_is_refused_by_its_BYTES_not_its_name(self):
        """A .txt that is really a zip must not open, whatever it is called."""
        body = _decomment(_fn(self.app, "async function openCodeFile("))
        self.assertIn("bytes.indexOf(0)", body,
                      "nothing checks for a NUL byte, so a mislabelled binary opens as mojibake")
        self.assertIn("_CODE_MAX", body, "no size limit on a buffer held in localStorage")

    def test_an_encrypted_file_is_decrypted_first(self):
        body = _decomment(_fn(self.app, "async function openCodeFile("))
        self.assertIn("encFileUrl", body, "an encrypted drive file would open as ciphertext")

    # ---- the way back -----------------------------------------------------------------------
    def test_the_editor_does_not_know_about_blossom(self):
        """It holds text. The index, the encryption and the folder live in app.js, so the save does."""
        body = _decomment(_fn(self.code, "async function saveDoc("))
        self.assertIn("saveBlobDoc", body, "code.js does not route a Files document back to Files")
        for leak in ("FilesIdx", "uploadEncFile", "uploadBlob"):
            self.assertNotIn(leak, self.code, f"code.js reaches into {leak} — that belongs in app.js")

    def test_the_saver_is_on_the_pc_surface(self):
        """The recurring `PC.x is not a function`: defined in app.js, never exported, called from a
        sub-module."""
        i = self.app.index("window.__PC = {")
        j = self.app.index("\n  };", i)
        self.assertIn("saveBlobDoc", self.app[i:j])

    def test_saving_re_points_the_index_and_keeps_the_old_blob(self):
        body = _decomment(_fn(self.app, "async function saveBlobDoc("))
        self.assertIn("FilesIdx.forget(desc.sha)", body,
                      "the old index entry survives, so the file appears twice in Files")
        self.assertIn("newSha !== desc.sha", body,
                      "an unchanged hash would forget the entry it just wrote")
        self.assertIn("uploadEncFile", body, "an encrypted file would be saved back in the clear")

    def test_a_restored_blob_buffer_never_asks_the_workspace_for_it(self):
        """`/api/code/file?path=<a file name>` resolves against the jail and 400s — the tab would
        come back as an error about a file that was never on this node."""
        body = _decomment(_fn(self.code, "async function hydrate("))
        self.assertIn("d.blob", body)
        self.assertLess(body.index("d.blob"), body.index("api("),
                        "hydrate reaches the workspace fetch before it checks for a blob buffer")


class TheOpenersAreOnTheTileNotOnlyInDetails(unittest.TestCase):
    """The DEFAULT Files view is tiles, and the buttons were only in the details row.

    `_fxView()` defaults to 'tiles'. Both openers went into the `acts:` string that only
    `_fxDetailsRow` consumes, so on the view almost everybody is looking at there was no way to open
    anything in Code, and Office was reachable only by clicking the tile — which nothing says.
    Reported as "why no way to open any blossom file in posterchan code or office yet".
    """

    @classmethod
    def setUpClass(cls):
        cls.app = _read(APP)
        cls.grid = _decomment(_fn(cls.app, "function _renderFilesGrid("))

    def test_tiles_is_the_default_view(self):
        """If this stops being true, this whole class is measuring the less-used screen."""
        self.assertIn("ClientSettings.get('filesView','tiles')", self.app,
                      "the default Files view changed — re-read this test")

    def test_both_openers_render_on_a_tile(self):
        # The tile branch is the one that is NOT behind `if(details)`.
        tile = self.grid[self.grid.index("if(m.enc){"):]
        self.assertIn("openbtns", tile, "the tile has no opener buttons at all")
        self.assertEqual(tile.count("${openbtns}"), 2,
                         "only one of the two tile shapes (encrypted / plain) got the openers")

    def test_the_opener_is_built_once_for_both_shapes(self):
        """Two hand-written copies is how the encrypted tile ends up missing one."""
        self.assertEqual(self.grid.count("const openbtns"), 1)
        self.assertIn("openbtn", self.grid)

    def test_the_same_handler_binds_it(self):
        """A button drawn in a second place with no handler is worse than no button. The tile's
        own link keeps its Office shortcut (`.office-open`), which is separate."""
        self.assertIn("$$('.openbtn',grid)", self.app)
        self.assertIn("$$('.office-open',grid)", self.app)

    def test_a_synced_row_offers_it_in_both_view_modes(self):
        """The synced list builds ONE `act` string and uses it for the details row and the card, so
        it never had this split — assert that it stays that way."""
        body = _decomment(_fn(self.app, "function _syncPaneRender(")) if "_syncPaneRender(" in self.app else None
        src = body or self.app
        self.assertIn("opensync", src)


class OneFileCanBeSelected(unittest.TestCase):
    """A synced folder offered Select all and Select none and nothing between them.

    "The select choices are all or none, no way to select 1 file like a regular file manager." The
    drive already had a per-card checkbox; the synced view had none at all, so the smallest thing
    you could act on was the whole directory.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = _read(APP)
        cls.css = _read(os.path.join(ROOT, "static", "css", "client.css"))

    def test_a_synced_file_has_its_own_checkbox(self):
        self.assertIn("syncbox", self.app, "there is still no way to pick a single synced file")
        self.assertIn("$$('.syncbox', grid)", self.app, "the checkbox is drawn but not bound")

    def test_it_uses_the_drive_s_grammar(self):
        """One selection idiom for all of Files — same class, and having a selection IS the mode."""
        self.assertIn('class="selbox syncbox"', self.app)
        self.assertIn("grid.classList.toggle('selmode', _syncSel.size > 0)", self.app)

    def test_toggling_one_file_does_not_redraw_the_view(self):
        """A full re-render loses the scroll position on every click, which is what makes a picker
        feel broken."""
        i = self.app.index("$$('.syncbox', grid)")
        body = self.app[i:i + 900]
        self.assertNotIn("renderBlossom()", body,
                         "picking one file re-renders the whole folder")
        self.assertIn("_syncSel.add", body)
        self.assertIn("_syncSel.delete", body)

    def test_the_details_grid_keeps_its_columns(self):
        """The details view is a GRID: a row with one more (or fewer) cell than the header shifts
        every heading by a column. Adding the checkbox did exactly that, twice — once for files
        (the grid still said `nosel`) and once for folders (which have no checkbox)."""
        self.assertNotIn("' details nosel'", self.app,
                         "the synced grid still declares it has no select column while its rows "
                         "have one")
        self.assertIn("selbox-gap", self.app, "a folder row has no cell for the checkbox column")
        self.assertIn(".selbox-gap", self.css)

    def test_the_placeholder_is_invisible_in_the_tile_view(self):
        """It exists to hold a grid column open; in tiles there is no column to hold."""
        self.assertIn(".files-grid:not(.details) .selbox-gap", self.css)


class TheOpenWithChooser(unittest.TestCase):
    """Asked for: "an open file that lets you choose, open as office document or open in PosterChan
    code, a nice splash screen"."""

    @classmethod
    def setUpClass(cls):
        cls.app = _read(APP)
        cls.css = _read(os.path.join(ROOT, "static", "css", "client.css"))

    def test_one_handler_opens_straight_away(self):
        """A chooser with a single choice is a dialog that wastes a click."""
        body = _decomment(_fn(self.app, "function _openWithSheet("))
        self.assertIn("handlers.length === 1", body)
        self.assertIn("handlers[0].run()", body)

    def test_nothing_openable_says_so_rather_than_showing_an_empty_sheet(self):
        body = _decomment(_fn(self.app, "function _openWithSheet("))
        self.assertIn("if(!handlers.length)", body)

    def test_the_sheet_closes_before_the_handler_opens_its_own(self):
        """Both are modals in one #modal-root; opening the second under the first leaves a chooser
        stacked behind a document editor."""
        body = _decomment(_fn(self.app, "function _openWithSheet("))
        self.assertLess(body.index("closeModal()"), body.index("h.run()"))

    def test_both_sources_offer_the_same_menu_for_the_same_file(self):
        """The drive and a synced folder must not disagree about what can open a .docx."""
        body = _decomment(_fn(self.app, "function _handlersFor("))
        self.assertIn("opts.sync ? openSyncOfficeFile", body)
        self.assertIn("opts.sync ? openSyncCodeFile", body)
        self.assertIn("_officeable", body)
        self.assertIn("_codeable", body)

    def test_each_choice_explains_itself(self):
        """The point of asking is that the answer is obvious, so it is not a list of two words."""
        body = _fn(self.app, "function _openWithSheet(")
        self.assertIn("ow-t", body)
        self.assertIn("h.hint", body)
        self.assertIn(".ow-opt", self.css, "the chooser has no styling")


class TheOfficeEditorGetsTheScreen(unittest.TestCase):
    """`modal()` caps its box at min(720px,96vw).

    The office frame asked for `min(94vw,1400px)` INSIDE that, so the iframe was clipped to 720px
    and the modal scrolled: a small white rectangle showing the top-left corner of a document —
    "open in office on desktop was a tiny ass window that is white". A sheet that has to be bigger
    than the default has to SAY so, the way the composer does with `.cmp-modal`.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = _read(APP)
        cls.css = _read(os.path.join(ROOT, "static", "css", "client.css"))

    def test_the_sheet_asks_to_be_wide(self):
        self.assertIn("office-modal", self.app, "the office sheet never marks itself")
        # EVERY rule for that selector, not the last one — the last is the phone media query, which
        # is narrow on purpose. Keeping only it reported the desktop sheet as too small.
        rules = [body for sel, body in re.findall(r"([^{}]*)\{([^{}]*)\}",
                                                  re.sub(r"/\*.*?\*/", "", self.css, flags=re.S))
                 if sel.strip().endswith(".modal.office-modal")]
        self.assertTrue(rules, "no .modal.office-modal rule, so the 720px cap still applies")
        widths = [int(m.group(1)) for m in
                  (re.search(r"width:min\((\d+)px", b) for b in rules) if m]
        self.assertTrue(widths and max(widths) > 720,
                        "the office sheet is no wider than an ordinary modal")

    def test_the_frame_fills_the_sheet_instead_of_naming_its_own_size(self):
        """Two elements naming their own width is how they disagree again the next time one moves."""
        rule = None
        for sel, body in re.findall(r"([^{}]*)\{([^{}]*)\}",
                                    re.sub(r"/\*.*?\*/", "", self.css, flags=re.S)):
            if sel.strip() == ".office-frame":
                rule = body
        self.assertIsNotNone(rule)
        self.assertNotIn("vw", rule, "the frame still sizes itself against the viewport, not its box")
        self.assertIn("flex:1 1 auto", rule, "the frame does not grow to fill the sheet")

    def test_it_is_marked_before_the_editor_is_launched(self):
        """The form submits into the iframe; sizing it afterwards reloads the layout under a
        document that is already loading."""
        i = self.app.index("root.classList.add('office-modal')")
        j = self.app.index("$('.office-launch',root).submit()")
        self.assertLess(i, j)


class TheExplorerToolbarStaysLiftable(unittest.TestCase):
    """`_fxBarHTML` is pulled out of app.js BY NAME and evaluated on its own by
    scripts/check_files_explorer.py — that is what stops the check measuring a copy of the markup
    that has drifted from the real screen.

    So it must not close over runtime state. Reading a module variable from it made the whole check
    SKIP with "the page never rendered": not a failure anybody would notice in a green run, just a
    layout check that had quietly stopped checking. Everything it needs is an argument.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = _read(APP)
        cls.bar = _decomment(_fn(cls.app, "function _fxBarHTML("))

    def test_it_takes_what_it_needs_as_arguments(self):
        sig = self.bar[:self.bar.index(")") + 1]
        self.assertIn("crumbs", sig)
        self.assertIn("canBack", sig, "the Back state is read from module scope, not passed")

    def test_it_reads_no_navigation_state_of_its_own(self):
        for leak in ("_fxHist", "_syncRoot", "_syncPath", "_filesFolder", "_hostOn"):
            self.assertNotIn(leak, self.bar,
                             f"_fxBarHTML closes over {leak}, so check_files_explorer.py cannot "
                             "evaluate it and skips instead of measuring")

    def test_back_and_up_exist_and_are_disabled_rather_than_hidden(self):
        """A toolbar whose buttons appear and disappear reflows the breadcrumbs under the cursor."""
        self.assertIn('id="fx-back"', self.bar)
        self.assertIn('id="fx-up"', self.bar)
        self.assertEqual(self.bar.count("disabled"), 3,
                         "Back, Up and the last crumb are the three that disable in place")

    def test_every_move_is_remembered_so_back_means_something(self):
        """Back that only undoes crumb clicks is the half of browsing nobody uses."""
        self.assertIn("_fxRemember()", self.app)
        self.assertGreaterEqual(self.app.count("_fxRemember()"), 4,
                                "some navigation paths do not record where they came from")

    def test_one_router_serves_crumbs_back_and_up(self):
        """Two copies of 'where does this go' is how they start disagreeing about `up`."""
        self.assertIn("function _fxRoute(", self.app)
        self.assertIn("_fxRoute(to)", self.app)


class SyncedFoldersOpenInCodeToo(unittest.TestCase):
    """A file in a SYNCED folder opens in Code, and saving reaches every device.

    A synced folder's rows offered Download, Save-a-copy-to-drive, Rename and Delete-everywhere —
    everything except opening the thing. And the save has to go back to the FOLDER: a copy quietly
    landing on the drive instead would look like the edit worked and change nothing anywhere else,
    which is the worst of the three possible outcomes.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = _read(APP)

    def test_a_synced_file_can_be_opened(self):
        """Clicking the row is the door here too \u2014 there is no button. The row already had a click
        (it downloaded), so the sheet has to keep Download on it; that is asserted next door in
        ClickingTheFileIsHowYouOpenIt."""
        self.assertIn("$$('.file-card:not(.isdir)', grid)", self.app,
                      "nothing binds a synced row, so no way to open a synced file")
        self.assertIn("openSyncCodeFile", self.app)
        self.assertIn("openSyncOfficeFile", self.app)

    def test_it_reuses_the_same_fetch_the_download_uses(self):
        """Blossom by sha OR chunk list, decrypted with the drive key. A file over ~16 MB has no
        `sha` of its own, so a path that only knows about sha silently cannot open the common case."""
        body = _decomment(_fn(self.app, "async function openSyncCodeFile("))
        self.assertIn("_syncFileBlob", body)
        self.assertIn("chunks", body, "a chunked file would appear to have no bytes")

    def test_the_buffer_knows_it_came_from_a_folder(self):
        body = _decomment(_fn(self.app, "async function openSyncCodeFile("))
        self.assertIn("sync: { key:", body,
                      "without the descriptor the save falls through to the drive path and the "
                      "edit reaches no other device")

    def test_saving_writes_back_to_the_folder_not_the_drive(self):
        body = _decomment(_fn(self.app, "async function saveBlobDoc("))
        self.assertIn("desc.sync", body)
        self.assertIn("PCSync.edit.uploadMany", body,
                      "a synced file is saved through some other path than the folder's own writer")
        # …and it must take that branch BEFORE the drive upload.
        self.assertLess(body.index("desc.sync"), body.index("uploadBlob"),
                        "the drive upload runs first, so a synced edit lands on the drive")

    def test_it_invalidates_the_manifest_it_just_changed(self):
        """The view must never redraw from the copy the write invalidated."""
        body = _decomment(_fn(self.app, "async function saveBlobDoc("))
        self.assertIn("_syncManifests.delete", body)

    def test_binary_and_size_are_refused_here_too(self):
        body = _decomment(_fn(self.app, "async function openSyncCodeFile("))
        self.assertIn("bytes.indexOf(0)", body)
        self.assertIn("_CODE_MAX", body)


if __name__ == "__main__":
    unittest.main()
