"""The ⋯ on a notification is a small plain "more" icon, the same one every other More button uses.

Reported 2026-10-13: "the hamburger menu is very ugly in notifications". It was the hamburger (#i-menu) where every
other More button in the app is ⋯ (#i-more), and its svg carried class `ic` -- so `.notif .ic` (the kind icon's
26px box, 20px font) and `.notif.like .ic` / `.rt` / `.reply` / `.zap` / `.mention` (the kind's colour) styled it too:
a stretched hamburger tinted pink, green, cyan or amber by the kind of notification beside it.

Real Chrome, the SHIPPED client.css and sprite.js, and the button markup taken from the SHIPPED notifview.js.
"""
import json
import re
import shutil
import subprocess
import tempfile
from html import unescape
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHROME = shutil.which("google-chrome-stable") or shutil.which("chromium") or "/opt/google/chrome/chrome"
KINDS = ["like", "rt", "reply", "zap", "mention"]


def _button_markup():
    src = (ROOT / "static/js/client/notifview.js").read_text()
    m = re.search(r'<button type="button" class="notif-more".*?</button>', src, re.S)
    assert m, "the notification More button markup is gone from notifview.js"
    # the ${...} holes are attributes (event id / pubkeys) -- irrelevant to how it looks
    return re.sub(r"\$\{[^}]*\}", "", m.group(0)).replace("`", "")


@pytest.mark.skipif(not Path(CHROME).exists(), reason="Chrome required")
def test_the_notification_more_button_is_the_same_small_plain_dots_as_everywhere_else():
    btn = _button_markup()
    rows = "".join(
        f'<div class="notif notif-grid {k}" id="r-{k}"><span class="ic">♥</span><span class="av"></span>'
        f'<div class="notif-main"><div class="notif-hd"><b>alice</b> did a thing</div>'
        f'<div class="muted small notif-ts">2m</div></div>{btn}</div>' for k in KINDS)
    js = """(()=>{const out=[];for(const k of %s){const b=document.querySelector('#r-'+k+' .notif-more'),s=b.querySelector('svg'),
      u=s.querySelector('use'),r=s.getBoundingClientRect(),cs=getComputedStyle(s);
      out.push({k,href:u.getAttribute('href'),w:Math.round(r.width),h:Math.round(r.height),
        color:cs.color,btnColor:getComputedStyle(b).color,kindColor:getComputedStyle(document.querySelector('#r-'+k+' > .ic')).color});}
      document.getElementById('out').textContent=JSON.stringify(out);})()""" % json.dumps(KINDS)
    html = ('<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="%s"></head><body>'
            '<div id="feed" style="width:600px">%s</div><pre id="out"></pre>'
            '<script src="%s"></script><script>%s</script></body></html>'
            % ((ROOT / "static/css/client.css").as_uri(), rows, (ROOT / "static/js/client/sprite.js").as_uri(), js))
    with tempfile.TemporaryDirectory() as td:
        page = Path(td) / "n.html"
        page.write_text(html)
        done = subprocess.run([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--allow-file-access-from-files",
                               "--window-size=800,600", "--virtual-time-budget=1500", "--dump-dom", page.as_uri()],
                              text=True, capture_output=True, timeout=60)
    m = re.search(r'<pre id="out">(.*?)</pre>', done.stdout, re.S)
    assert m and m.group(1), done.stderr[-800:]
    got = json.loads(unescape(m.group(1)))
    for r in got:
        assert r["href"] == "#i-more", ("the notification More button is not the ⋯ every other More button uses", r)
        assert r["w"] <= 20 and r["h"] <= 20, ("the More icon is stretched like the kind icon beside it", r)
        assert r["color"] == r["btnColor"], ("the More icon is tinted by the notification's kind", r)
    assert len({r["color"] for r in got}) == 1, ("the More icon changes colour with the kind of notification", got)
