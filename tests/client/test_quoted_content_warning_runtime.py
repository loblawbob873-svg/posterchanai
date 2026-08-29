"""Quoted media inherits only the quoted event's sensitive-content gate."""

import html as html_lib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import pytest


ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")
CHROME = (shutil.which("google-chrome-stable") or shutil.which("google-chrome") or
          shutil.which("chromium"))


def _function(name):
    start = APP.index(f"function {name}(")
    brace = APP.index("{", start)
    depth = 0
    quote = None
    escaped = False
    for pos in range(brace, len(APP)):
        char = APP[pos]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "'\"`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return APP[start:pos + 1]
    raise AssertionError(f"unterminated {name}")


@pytest.mark.skipif(not CHROME, reason="Chrome is unavailable")
def test_warned_quote_is_blurred_until_reveal_while_safe_quote_is_not_gated():
    script = f"""
      const BLUR_NSFW=true, NO_IMAGES=false, LOGO='logo.png';
      const profOf=()=>({{name:'Alice',picture:'avatar.png'}}),needProfile=()=>{{}},npubOf=()=> 'npub1alice';
      const niceNip05=()=>'',enc=x=>String(x),emojiName=(p,n)=>n,timeAgo=()=> 'now';
      const mediaParts=()=>({{mediaFirst:true,gallery:'<div class="media-row"><img src="media.png"></div>',text:'caption'}});
      const applyEmojis=x=>x,linkify=x=>x,stripQuoteRef=x=>x;
      const isSensitive=e=>(e.tags||[]).some(t=>t[0]==='content-warning'||(t[0]==='t'&&t[1]==='nsfw'));
      {_function('_cwRevealInner')}
      {_function('quotedDiv')}
      const warned={{id:'warned',pubkey:'a',created_at:1,content:'photo',tags:[['content-warning','nudity']]}};
      const safe={{id:'safe',pubkey:'b',created_at:1,content:'photo',tags:[]}};
      document.body.innerHTML=quotedDiv(warned)+quotedDiv(safe);
      const before=document.querySelector('[data-open="warned"] .cw-wrap');
      const safeGate=document.querySelector('[data-open="safe"] .cw-wrap');
      const blur=getComputedStyle(before.querySelector('.cw-inner')).filter;
      const warning=document.body.textContent.includes('nudity');
      before.querySelector('.cw-reveal').click();
      document.title=JSON.stringify({{warned:!!before,blur,safe:!!safeGate,revealed:!before.classList.contains('cw-on'),
        warning}});
    """
    page = f'<!doctype html><style>{CSS}</style><body></body><script>{script}</script>'
    with tempfile.TemporaryDirectory(prefix="pc-quote-cw-") as tmp:
        path = Path(tmp) / "quote.html"
        path.write_text(page, encoding="utf-8")
        run = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
                              "--blink-settings=imagesEnabled=false", "--virtual-time-budget=1000",
                              "--dump-dom", path.as_uri()], capture_output=True, text=True,
                             timeout=60, check=True)
    titles = re.findall(r"<title>(.*?)</title>", run.stdout, re.S)
    assert titles, run.stdout[-1000:]
    got = json.loads(html_lib.unescape(titles[-1]))
    assert got == {"warned": True, "blur": "blur(26px)", "safe": False,
                   "revealed": True, "warning": True}
