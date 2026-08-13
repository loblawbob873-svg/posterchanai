"""The chat popout's chat FILLS its window, and grows when the window is dragged bigger.

"🗔 Window" pops the whole stream; Chat now pops the chat on its own, for a second monitor. The
sizing is the entire feature, and it is the one part that cannot be asserted from the markup: the
chat column in the ordinary popout is `flex:0 0 340px !important` with `max-width:38vw`, which is
correct BESIDE A VIDEO and wrong when the chat IS the window. Left alone it would sit at 340px in a
420px window with dead space to the right, and stay at 340px after the user drags the window to
1200px — "it opened but it doesn't resize", with nothing broken to point at.

So this measures real widths in a real browser against the real stylesheet, at two window sizes:

  fills-a-narrow-window   at 420px the chat is the window, not 340px of it
  grows-with-the-window   at 1200px it is the window too — the fixed width and the 38vw cap are gone
  composer-stays-visible  a long backlog must not push the input off the bottom (the flex-item
                          min-height:0 rule) — it is the one control this window exists to reach
  player-is-not-drawn     .stream-main is gone, so the second decode of the same broadcast cannot
                          happen in a window opened to read chat
  ordinary-popout-intact  without `popout-chat` the column is still the fixed one beside the video,
                          or this change has quietly rewritten the stream popout too
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CSS = os.path.join(REPO, "static", "css", "client.css")
CHROME = shutil.which("google-chrome-stable") or shutil.which("chromium") or shutil.which("chrome")

# The stream view as renderStream builds it, reduced to the structure the layout rules select on.
PAGE = """<!doctype html><meta charset="utf-8">
<link rel="stylesheet" href="client.css">
<!-- ANIMATION OFF, because this measures LAYOUT and headless --dump-dom never presents a frame: an
     entry animation stays frozen at its first keyframe for the whole run. `.stream-view` carries
     `animation:fade`, whose `from` is `transform:translateY(6px)` — which arrives here as the
     composer sitting exactly 6px below the window and reads as a real overflow. It is not; it cost
     two wrong CSS "fixes" before the keyframes were read. `animation:none` resolves every element
     to its resting style (the same reason `body.anim-off` uses `none` rather than `paused`). -->
<style>*{animation:none !important}</style>
<body class="popout %(extra)s">
<div class="app"><div class="main"><div id="feed">
  <div class="stream-view">
    <div class="row"><button>Streams</button></div>
    <h1 class="av-title">A stream</h1>
    <div class="av-by"><span class="name">host</span></div>
    <div class="stream-layout">
      <div class="stream-main"><video class="stream-player"></video><div class="muted small" id="st-note"></div>
        <div class="row"><button>x</button></div></div>
      <div class="stream-chat" id="st-chat">
        <div class="st-chat-hd">&#128172; Live chat</div>
        <div class="scm-list" id="st-chat-msgs">%(msgs)s</div>
        <div class="scm-input"><input class="input"><button>Send</button></div>
      </div>
    </div>
  </div>
</div></div></div>
<pre id="out"></pre>
<script>
setTimeout(() => {
  const chat = document.getElementById('st-chat');
  const main = document.querySelector('.stream-main');
  const inp  = document.querySelector('.scm-input');
  const r = e => { const b = e.getBoundingClientRect(); return {w: Math.round(b.width), h: Math.round(b.height),
                                                               bottom: Math.round(b.bottom)}; };
  document.getElementById('out').textContent = JSON.stringify({
    win:  {w: window.innerWidth, h: window.innerHeight},
    chat: r(chat),
    input: r(inp),
    mainShown: getComputedStyle(main).display !== 'none',
  });
}, 400);
</script>
"""


@unittest.skipIf(not CHROME, "no chrome on this host")
class ChatPopoutFluid(unittest.TestCase):
    def render(self, *, width, height=800, chat_only=True, msgs=1):
        body = "".join('<div class="scm-msg">line %d</div>' % i for i in range(msgs))
        page_src = PAGE % {"extra": "popout-chat" if chat_only else "", "msgs": body}
        with tempfile.TemporaryDirectory(prefix="pc-chatpop-") as tmp:
            shutil.copy(CSS, os.path.join(tmp, "client.css"))
            page = os.path.join(tmp, "s.html")
            with open(page, "w", encoding="utf-8") as fh:
                fh.write(page_src)
            res = subprocess.run(
                [CHROME, "--headless", "--disable-gpu", "--no-sandbox", "--virtual-time-budget=2000",
                 "--window-size=%d,%d" % (width, height),
                 "--user-data-dir=" + os.path.join(tmp, "profile"), "--dump-dom", "file://" + page],
                capture_output=True, text=True, timeout=90)
            assert res.returncode == 0, res.stderr[-2000:]
        m = re.search(r'<pre id="out">(\{.*?\})</pre>', res.stdout, re.S)
        assert m, "the page never reported — see the dump:\n" + res.stdout[:2000]
        return json.loads(m.group(1))

    def test_fills_a_narrow_window(self):
        o = self.render(width=420)
        self.assertGreaterEqual(o["chat"]["w"], o["win"]["w"] - 4,
                                "the chat is %dpx in a %dpx window — the fixed 340px column beside "
                                "the video is still applying" % (o["chat"]["w"], o["win"]["w"]))

    def test_grows_with_the_window(self):
        narrow = self.render(width=420)
        wide = self.render(width=1200)
        self.assertGreaterEqual(wide["chat"]["w"], wide["win"]["w"] - 4,
                                "the chat did not grow with the window (%dpx in %dpx) — max-width:38vw "
                                "or the fixed flex-basis survived" % (wide["chat"]["w"], wide["win"]["w"]))
        self.assertGreater(wide["chat"]["w"], narrow["chat"]["w"],
                           "the chat is the same width at 420px and 1200px — it is not fluid at all")

    def test_composer_stays_visible(self):
        """A long backlog must not push the input off the bottom: `min-height:0` on the list is what
        lets a flex item shrink below its content, and without it the one control this window exists
        to reach is the thing that leaves the screen."""
        o = self.render(width=420, height=600, msgs=200)
        self.assertLessEqual(o["input"]["bottom"], o["win"]["h"] + 2,
                             "the message input is at y=%d in a %dpx window — a long backlog pushed "
                             "the composer out of view" % (o["input"]["bottom"], o["win"]["h"]))

    def test_player_is_not_drawn(self):
        o = self.render(width=420)
        self.assertFalse(o["mainShown"],
                         "the player pane is still displayed in a chat-only popout — that is a "
                         "second decode of the same broadcast in a window opened to read chat")

    def test_ordinary_popout_intact(self):
        """The stream popout must keep its fixed chat column beside the video."""
        o = self.render(width=1200, chat_only=False)
        self.assertTrue(o["mainShown"], "the ordinary stream popout lost its player")
        self.assertLess(o["chat"]["w"], o["win"]["w"] / 2,
                        "the ordinary popout's chat is now filling the window — the chat-only rules "
                        "are leaking into it")


if __name__ == "__main__":
    unittest.main()
