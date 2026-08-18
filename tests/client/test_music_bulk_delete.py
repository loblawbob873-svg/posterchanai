"""Deleting a music library, which there was no way to do at all.

Encrypted music is hidden from the Files grid on purpose — it is ciphertext with no useful name — and
the Music screen only ever deleted ONE track at a time. For a few hundred songs that is not a feature.
Reported as "i need to delete all my music but no way to do it in Files → Blossom".

Three things have to hold, and each of them is a way this could quietly go wrong:

  1. it deletes what is ON SCREEN (a search narrows it) and the button says which;
  2. it is NOT offered inside a playlist, where the same words mean something else entirely — the
     songs, not the list — beside a Delete-playlist control that deliberately does not touch them;
  3. the index is saved in BATCHES and the verdict comes from the save, not from having asked. A
     tidy that never reached the server once reported success and every entry was back on the next
     load — twice, over two days, for 2422 tracks.

`pruneTracks` is RUN, because a playlist left naming deleted tracks draws as a gap and "delete my
music" that leaves the names behind is not what anybody meant.
"""
import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CLIENT = os.path.join(ROOT, "static", "js", "client")
NODE = shutil.which("node")


def _src(name):
    with open(os.path.join(CLIENT, name), encoding="utf-8") as fh:
        return fh.read()


class TestTheButton(unittest.TestCase):
    def setUp(self):
        self.app = _src("app.js")
        at = self.app.index("function _renderMusicList(")
        self.body = self.app[at:self.app.index("\n  function ", at + 10)]

    def test_it_exists_at_all(self):
        self.assertIn("mus-delall", self.body, "there is still no way to delete a music library")

    def test_it_is_not_offered_inside_a_playlist(self):
        m = re.search(r"\$\{\(tracks\.length && !only\) \? `<button[^`]*mus-delall", self.body)
        self.assertIsNotNone(
            m, "the bulk delete is drawn inside a playlist view, where it reads as 'empty this "
               "playlist' and would delete the songs instead")

    def test_it_deletes_what_is_on_screen_and_says_so(self):
        """A search narrows the list, so the count in the label has to be the filtered one."""
        self.assertIn("const doomed = tracks.map(t=>t.sha);", self.body)
        self.assertIn("these ${tracks.length}", self.body,
                      "with a search active the button does not say how many it would delete")

    def test_the_index_write_is_batched_and_checkpointed(self):
        """forget() re-uploads the whole encrypted index per track otherwise, and a crash halfway
        through a few hundred songs would lose the progress rather than keep it."""
        h = self.body[self.body.index("const da=$('#mus-delall',grid);"):]
        self.assertIn("FilesIdx.beginBatch();", h)
        self.assertIn("done % 25 === 0", h)
        self.assertIn("const saved = await FilesIdx.endBatch();", h)

    def test_it_never_says_the_library_is_unchanged_after_deleting_the_bytes(self):
        """The loop has already sent a Blossom DELETE for every track by the time the index is
        saved. `saved` is only about whether the LIBRARY RECORD was written back — so "your library
        on the server is unchanged" at that moment is the opposite of the truth, and the songs are
        gone while the list still shows them."""
        # Scoped to the delete handler alone. The TIDY handler that follows it may legitimately say
        # "unchanged" — it only clears index entries for bytes the server already lost.
        h = self.body[self.body.index("const da=$('#mus-delall',grid);"):
                      self.body.index("const td=$('#mus-tidy',grid);")]
        self.assertIn("const saved = await FilesIdx.endBatch();", h,
                      "it reports success without checking that the library was actually saved")
        self.assertNotIn("your library on the server is unchanged", h)
        self.assertIn("could not be saved", h, "a failed index save is not reported at all")

    def test_the_confirmation_does_not_pretend_it_is_reversible(self):
        h = self.body[self.body.index("const da=$('#mus-delall',grid);"):]
        self.assertIn("cannot be", h)
        self.assertIn("uiConfirm", h)

    def test_playlists_are_pruned(self):
        h = self.body[self.body.index("const da=$('#mus-delall',grid);"):]
        self.assertIn("PL().pruneTracks(doomed)", h,
                      "playlists keep naming tracks that no longer exist")


@unittest.skipIf(not NODE, "no node on this node")
class TestPruneTracks(unittest.TestCase):
    """The prune itself, RUN against the shipped playlists.js.

    One save per playlist that actually changes — deleting a library one removeTrack() at a time is
    a save per (playlist, track) pair, which for a few hundred songs is thousands of encrypted
    writes. And a playlist whose save is REFUSED keeps its old order, the same rule the single-track
    path uses: a list that silently loses its order is worse than one naming a track that has gone.
    """

    def _run(self, playlists, drop, refuse=None):
        src = _src("playlists.js")
        js = """
        %s
        (async () => {
          const PL = window.PCPlaylists;
          const lists = %s, refuse = %s;
          // Drive the shipped pruneTracks against a stubbed library and save.
          const saves = [];
          const shim = new Function('all', '_save', '_stuck', '_changed', 'return ' +
            %s + ';')(
              () => lists,
              async (pl) => { saves.push(pl.id); return refuse.includes(pl.id) ? {ok:false} : {ok:true}; },
              (r) => !(r && r.ok),
              () => {});
          const touched = await shim(%s);
          process.stdout.write(JSON.stringify({ touched, saves,
            after: lists.map(p => ({id: p.id, tracks: p.tracks})) }));
        })();
        """
        fn = re.search(r"async function pruneTracks\(shas\)\{[\s\S]*?\n  \}", src)
        self.assertIsNotNone(fn, "pruneTracks moved in playlists.js")
        body = "(" + fn.group(0).replace("async function pruneTracks", "async function") + ")"
        js = js % ("", json.dumps(playlists), json.dumps(refuse or []), json.dumps(body),
                   json.dumps(drop))
        # The window shim the file expects, minus everything pruneTracks does not touch.
        js = "global.window = {};\n" + js
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        return json.loads(r.stdout)

    def test_it_removes_the_tracks_from_every_playlist_that_has_them(self):
        out = self._run(
            [{"id": "a", "tracks": ["11", "22", "33"]}, {"id": "b", "tracks": ["44"]}],
            ["11", "33"])
        self.assertEqual(out["after"][0]["tracks"], ["22"])
        self.assertEqual(out["after"][1]["tracks"], ["44"])
        self.assertEqual(out["touched"], 1)

    def test_a_playlist_that_changes_nothing_is_not_saved(self):
        out = self._run([{"id": "a", "tracks": ["11"]}, {"id": "b", "tracks": ["22"]}], ["22"])
        self.assertEqual(out["saves"], ["b"], "it re-saved a playlist it did not change")

    def test_a_refused_save_keeps_the_old_order(self):
        out = self._run([{"id": "a", "tracks": ["11", "22"]}], ["11"], refuse=["a"])
        self.assertEqual(out["after"][0]["tracks"], ["11", "22"],
                         "a playlist whose save was refused silently lost its order")
        self.assertEqual(out["touched"], 0)

    def test_deleting_nothing_saves_nothing(self):
        out = self._run([{"id": "a", "tracks": ["11"]}], [])
        self.assertEqual(out["saves"], [])
