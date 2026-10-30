"""Communities' Discover draws each community icon as a THUMBNAIL, and the first rows do not wait to be scrolled to.

Vyram, "Discover Community Loading Jerk Screen" (f6493e34, 2026-10-30): "Definitely better ... loading when scrolling to
the bottom can be slightly faster". Each ~48px icon was fetched as its full-size original (often a multi-MB GIF), and
only once it neared the screen. Runs the shipped publicRoomIcon (lifted out of concord.js, as the scroll tests do) with
the client's own PCThumb rule: the src is the resizer at the drawn size, the original stays as the fallback, the first
eight rows load at once, and a text icon or a blob: icon is untouched.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / 'static/js/client/concord.js').read_text()
APP = (ROOT / 'static/js/client/app.js').read_text()


def _fn(src, name):
    start = src.index('  function %s(' % name)
    end = src.index('\n  }\n', start) + 4
    return src[start:end]


@pytest.mark.skipif(not shutil.which('node'), reason='node required')
def test_discover_icons_are_resized_thumbnails_with_the_original_as_fallback():
    thumb = APP[APP.index('window.PCThumb = (function(){'):APP.index('return { src, err, metered };\n  })();') + len('return { src, err, metered };\n  })();')]
    js = """
const window={__PC_API_BASE__:'https://poster.place',devicePixelRatio:2};
const navigator={};
%s
const PCThumb=window.PCThumb;
const normalizeIcon=x=>String(x||'');
const listingIcon=()=>'';
%s
const p={enc:s=>String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;')};
console.log(JSON.stringify({
  first: publicRoomIcon(p,{icon:'https://img.example/big.gif'},0),
  later: publicRoomIcon(p,{icon:'https://img.example/big.gif'},12),
  text: publicRoomIcon(p,{icon:'🎮',name:'Games'},0),
  blob: publicRoomIcon(p,{icon:'blob:https://poster.place/x'},0),
}));
""" % (thumb, _fn(SRC, 'publicRoomIcon'))
    out = json.loads(subprocess.run(['node', '-e', js], capture_output=True, text=True, check=True).stdout)
    assert 'src="https://poster.place/client/thumb?w=96&amp;u=https%3A%2F%2Fimg.example%2Fbig.gif"' in out['first'], out
    assert 'data-osrc="https://img.example/big.gif"' in out['first'] and 'PCThumb.err(this)' in out['first'], out
    assert 'loading="lazy"' not in out['first'] and 'loading="lazy"' in out['later'], ('the first rows must not wait', out)
    assert '<img' not in out['text'] and 'blob:https://poster.place/x' in out['blob'] and 'thumb?' not in out['blob'], out
