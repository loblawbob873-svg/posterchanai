"""A <video> in the feed is a live decoder, not markup — only the ones on screen may hold one.

Run: venv-unified/bin/python -m unittest tests.client.test_video_lazy_mount

The report: on the Android tablet, three videos in the Notifications tab "kept reloading over and over,
never showing a preview", then the app toasted "PosterChan hit a display error — reloading". That toast
is MainActivity.onRenderProcessGone(didCrash=true): the WebView's RENDER PROCESS had died. Two causes,
both asserted here.

1. Every video was emitted with `src` + `preload="metadata"`, so the WebView allocated a media player
   for each one the moment it was parsed — however many were on the page, on screen or not. Android's
   codec pool is small and process-wide.

2. renderNotifications() is a whole-innerHTML rebuild whose rows embed the FULL referenced post
   (quotedDiv → media gallery), and it ran once per arriving event — the subscription burst alone
   rebuilt it ~150 times, discarding and re-creating every <video> mid-fetch. The right rail
   (loadNotifs) renders the same rows again, so on a tablet each video existed twice over.
"""
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "static" / "js" / "client" / "app.js"


def _body(src, sig):
    """The {...} body of the function whose signature text is `sig`."""
    i = src.index(sig)
    return _block(src, src.index("{", i + len(sig)))


def _block(src, start):
    """The balanced {...} or (...) run beginning at the first opener at/after `start`."""
    opener = min((i for i in (src.find("{", start), src.find("(", start)) if i != -1), default=-1)
    assert opener != -1
    pairs = {"{": "}", "(": ")"}
    close = pairs[src[opener]]
    depth = 0
    for i in range(opener, len(src)):
        if src[i] == src[opener]:
            depth += 1
        elif src[i] == close:
            depth -= 1
            if depth == 0:
                return src[opener : i + 1]
    raise AssertionError("unbalanced")


class VideoLazyMount(unittest.TestCase):
    def setUp(self):
        self.src = APP.read_text()

    def test_media_emits_no_eager_video_src(self):
        """_media() is the one funnel every note video comes through — it must hand out data-vsrc."""
        body = _body(self.src, "function _media(encUrl, kind, cls, onerr)")
        video = [ln for ln in body.splitlines() if "`<video" in ln]   # emitted markup, not prose
        self.assertTrue(video, "_media no longer emits a <video> — has the funnel moved?")
        for ln in video:
            self.assertIn("data-vsrc=", ln, "feed video must carry its URL in data-vsrc, not src")
            self.assertNotRegex(ln, r"<video[^>]*\ssrc=", "feed video must not be given a src at parse time")
            self.assertIn('preload="none"', ln, "an unmounted video must not preload")

    def test_detached_video_releases_its_decoder(self):
        """A re-render drops the node; removeAttribute('src') + load() is what frees the media player."""
        mount = _block(self.src, self.src.index("const VideoMount = (function()") + len("const VideoMount = "))
        self.assertIn("removeAttribute('src')", mount)
        self.assertIn(".load()", mount, "only load() after clearing src actually releases the decoder")
        self.assertIn("MutationObserver", mount, "no render path should have to opt in")
        self.assertRegex(mount, r"MAX_MOUNTED\s*=\s*[1-9]", "there must be a cap on live media players")
        # Both directions. Mounting on entry with nothing releasing on exit is how you end up back at
        # "every video that has ever scrolled past holds a decoder" — the state this fix removes.
        # Matched loosely on purpose: these used to be single-expression arms and are now blocks (the
        # observer also records visibility for the first-frame grace), and a regex pinned to the old
        # spelling failed a change that kept the behaviour exactly. What matters is that the entering
        # arm mounts and the leaving arm unmounts, not how the arm is punctuated.
        io = mount[mount.index("new IntersectionObserver"):]
        enter = io[:io.index("else")]
        self.assertIn("isIntersecting", enter)
        self.assertRegex(enter, r"mount\(e\.target\)", "entering the viewport must mount")
        self.assertRegex(io, r"else\s*\{[^}]*unmount\(e\.target\)", "leaving the viewport must release")
        # The release must be conditional on there being nothing to lose — see test_video_mount_grace.py.
        self.assertIn("FIRST_FRAME_GRACE", mount,
                      "a video still fetching its first frame must not be aborted when it scrolls "
                      "away; on a slow link (tor) the preview then never appears at all")

    def test_notification_subs_do_not_redraw_per_event(self):
        """The live subscriptions must coalesce their redraws, not rebuild the view per event."""
        for marker in ("Relay.subscribe([{ '#p':[ME.pubkey], kinds:[3]",
                       "Relay.subscribe([{ '#p':[ME.pubkey], kinds:[1,6,7,9735"):
            i = self.src.find(marker)
            self.assertNotEqual(i, -1, f"notification subscription moved: {marker}")
            block = _block(self.src, i + len(marker))
            self.assertNotIn(
                "renderNotifications()", block,
                "a notification subscription must call renderNotificationsSoon(), not rebuild per event",
            )

    def test_rail_redraw_is_coalesced_too(self):
        """bumpNotif fires per arriving event and the rail renders the same media-bearing rows."""
        body = _body(self.src, "function bumpNotif()")
        self.assertIn("loadNotifsSoon()", body)
        self.assertNotIn("loadNotifs()", body.replace("loadNotifsSoon()", ""))

    def test_lightbox_reads_the_unmounted_url(self):
        """Stepping to a video that has not been mounted yet must not open an empty src."""
        body = _body(self.src, "function _lbGroup(im)")
        self.assertIn("VideoMount.url(el)", body)


if __name__ == "__main__":
    unittest.main()
