"""Replacing a stored transcode with the user's original file.

Every track uploaded while the Opus transcode existed is a 96 kbps re-encode; the original bytes were
never sent, so nothing on the server can recover them. `_musicReplaceOriginals` is the one-pass
repair: the user points it at the files they still have and each one REPLACES its transcode.

Blossom is content-addressed, so an original has a different sha from its transcode — "replace" is
really "add the new, retire the old", and the ORDER is the whole safety property:

  1. upload the original and get its sha;
  2. only then swap that sha into every playlist holding the old one, IN PLACE;
  3. only then delete the old blob.

Get that order wrong and there is a window where the track exists nowhere, or playlists point at a
blob that has just been deleted. These assertions are on the ordering and the guards, because the
alternative is finding out from somebody's library.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = open(os.path.join(ROOT, "static", "js", "client", "app.js"), encoding="utf-8").read()


def _fn():
    i = APP.index("  async function _musicReplaceOriginals(")
    return APP[i:APP.index("\n  }", i)]


def test_nothing_is_deleted_before_the_replacement_exists():
    """The upload has to come first, and its sha has to be checked. Deleting on the strength of a
    call that might have failed is how a track goes missing entirely."""
    b = _fn()
    up = b.index("await uploadMusicTrack(f)")
    dele = b.index("deleteBlobQuiet(oldSha)")
    assert up < dele, "the old blob must not be deleted before the new one is stored"
    assert "if(!newSha) throw new Error" in b, "an upload with no hash must abort, not proceed to delete"


def test_playlists_are_repaired_before_the_delete():
    """A failure between the two must leave both blobs and a working library — never playlists
    pointing at something that is already gone."""
    b = _fn()
    assert b.index("PCPlaylists.replaceTrack(oldSha, newSha)") < b.index("deleteBlobQuiet(oldSha)")


def test_identical_bytes_do_not_delete_the_track():
    """Re-picking a file that is already what is stored dedups to the SAME blob. Deleting `oldSha`
    there would delete the very track just uploaded."""
    b = _fn()
    assert "if(newSha === oldSha){ replaced++; continue; }" in b


def test_an_unmatched_file_is_added_and_counted_separately():
    """Pointing this at a music folder means "make the library match these files"; silently ignoring
    the ones it did not recognise is the answer nobody wants. Counted apart so the report is honest."""
    b = _fn()
    assert "if(!oldSha){ added++; continue; }" in b
    assert "added" in b and "replaced" in b and "failed" in b


def test_a_failure_deletes_nothing():
    b = _fn()
    assert "catch(e){ failed++;" in b, "a throw must be counted, not swallowed into the success path"
    # the delete is inside the same try, after every earlier step — so any throw skips it
    assert b.index("try{") < b.index("deleteBlobQuiet(oldSha)")


def test_playback_moves_with_the_track():
    """Replacing the blob that is playing would otherwise stop it mid-track, and leave the queue
    pointing at something deleted."""
    b = _fn()
    assert "MusicPlayer.cur === oldSha" in b
    assert "MusicPlayer.queue" in b and "x === oldSha ? newSha : x" in b


def test_the_offline_copy_of_the_old_blob_is_dropped():
    """Otherwise the device keeps caching bytes for a track that no longer exists."""
    assert "MusicOffline.drop(oldSha)" in _fn()


def test_matching_is_on_the_uploaded_name_case_folded():
    """srcName is the only thing the library kept about the original. Basename and extension are
    stripped so a re-pick from another folder, or an .mp3 replacing an .ogg, still matches."""
    i = APP.index("  function _musicBySrcName(){")
    fn = APP[i:APP.index("\n  }", i)]
    assert "m.srcName" in fn, "the uploaded name is what a stored track remembers"
    assert ".toLowerCase()" in fn and "split(/[/" in fn, "match on a case-folded basename"
    assert re.search(r"replace\(/\\\\\.\[\^\.\]\+\$/", fn) or ".replace(/\\.[^.]+$/,'')" in fn, \
        "the extension must be stripped — the replacement is a different format by definition"


def test_an_ambiguous_name_is_never_guessed_at():
    """THE TOOL CANNOT KNOW WHAT THE ORIGINAL IS — the user supplies it, and the only link back to a
    library entry is the filename it was uploaded under. That makes collisions the real hazard:
    "01 - Intro.mp3" exists on half the albums ever made, and first-one-wins would delete one album's
    track, replace it with another album's audio, and update every playlist to point at the wrong
    song. A name that identifies more than one track identifies none of them."""
    i = APP.index("  function _musicBySrcName(){")
    fn = APP[i:APP.index("\n  }", i)]
    assert "l.length === 1" in fn, "a name matching several tracks must not resolve to one of them"
    assert "by.set(k, null)" in fn, "an ambiguous name must be marked, not dropped (which would look unique)"
    b = _fn()
    assert "if(hit === null){ ambiguous++; }" in b, "an ambiguous file must be counted, not silently replaced"
    assert "ambiguous" in APP[APP.index("if(r.replaced)"):APP.index("if(r.replaced)") + 600], \
        "the report must say how many were ambiguous — otherwise 'added' hides them"


def test_it_is_asked_for_rather_than_automatic():
    """It deletes blobs. Anything that deletes has to be asked for."""
    assert "id=\"ma-orig\"" in APP, "no Originals control in the Music app"
    assert "Replace the stored copies of" in APP, "no confirmation before a destructive pass"


def test_upload_returns_the_sha_it_stored():
    """The whole tool hangs off this; without it there is nothing to swap playlists to."""
    i = APP.index("  async function uploadMusicTrack(")
    assert "return sha;" in APP[i:APP.index("\n  }", i)]
