"""Admin → Custom Emoji shows the built-in PosterChan pack as built-in, not as something it can rename or delete.

The PosterChan pack ships in assets/emoji-builtin/ (part of the code), and the service refuses to rename or
delete it -- so its grid cells must not offer ✏️/🗑️ buttons that can only answer with an error. The listing route
carries the flag; the SHIPPED renderer is run under node against that answer.
"""
import asyncio
import json
import os
import subprocess
import tempfile
from unittest import mock

from app.services import emoji_service

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = os.path.join(ROOT, "static", "js", "admin-emoji.js")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _listing():
    from app.routers.admin_emoji import list_emoji
    root = tempfile.mkdtemp()
    os.makedirs(os.path.join(root, "mine"))
    with open(os.path.join(root, "mine", "myown.png"), "wb") as fh:
        fh.write(PNG)
    with mock.patch.object(emoji_service, "emoji_dir", return_value=root):
        emoji_service._invalidate()
        try:
            r = asyncio.run(list_emoji(q="", pack="", offset=0, limit=1000, admin=None))
        finally:
            emoji_service._invalidate()
    return json.loads(r.body)


def test_the_listing_marks_built_in_emoji_and_packs():
    j = _listing()
    by = {e["s"]: e for e in j["emojis"]}
    assert by["pc_happy"]["b"] is True and by["myown"]["b"] is False
    packs = {p["name"]: p for p in j["packs"]}
    assert packs["posterchan"]["builtin"] is True and packs["mine"]["builtin"] is False


def test_the_grid_offers_no_rename_or_delete_on_a_built_in_emoji():
    j = _listing()
    rows = [e for e in j["emojis"] if e["s"] in ("pc_happy", "myown")]
    script = """
      const vm = require('vm'), fs = require('fs');
      const grid = { innerHTML: '' }, sel = { innerHTML: '', dataset: {}, value: '' };
      const ctx = { document: { getElementById: id => id === 'emojiGrid' ? grid : id === 'emojiPack' ? sel : null,
                                addEventListener() {} },
                    escapeHtml: s => String(s).replace(/[&<>"']/g, c => '&#' + c.charCodeAt(0) + ';'),
                    window: {}, console };
      vm.createContext(ctx);
      vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
      const data = JSON.parse(process.argv[2]);
      vm.runInContext('emojiRenderGrid(' + JSON.stringify(data.rows) + ', false); emojiRenderPacks(' + JSON.stringify({packs: data.packs}) + ')', ctx);
      console.log(JSON.stringify({ grid: grid.innerHTML, packs: sel.innerHTML }));
    """
    r = subprocess.run(["node", "-e", script, JS, json.dumps({"rows": rows, "packs": j["packs"]})],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    cells = out["grid"].split('class="emoji-cell"')[1:]
    builtin = next(c for c in cells if 'data-sc="pc_happy"' in c)
    own = next(c for c in cells if 'data-sc="myown"' in c)
    assert 'data-act="delete"' not in builtin and 'data-act="rename"' not in builtin and "built-in" in builtin
    assert 'data-act="delete"' in own and 'data-act="rename"' in own
    assert "posterchan (built-in)" in out["packs"]
