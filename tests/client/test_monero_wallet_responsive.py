"""Real-browser phone-width checks for the Monero wallet and its send/confirm sheet."""
from html import unescape
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "static/css/client.css").read_text() + "\n" + (ROOT / "static/css/monero-wallet.css").read_text()
CHROME = shutil.which("google-chrome-stable") or shutil.which("chromium") or shutil.which("chrome")
ADDRESS = "5" + "A" * 94


def run_page(width, height, body):
    html = f'''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
    <style>html,body{{margin:0;width:100%;height:100%}}{CSS}</style>{body}<pre id="out"></pre>
    <script>
    requestAnimationFrame(()=>{{
      const all=[...document.querySelectorAll('body *:not(#out):not(script)')];
      const boxes=all.map(x=>x.getBoundingClientRect());
      const modal=document.querySelector('.modal'),qr=document.querySelector('.mw-qr img');
      const fixed=all.filter(x=>getComputedStyle(x).position==='fixed').map(x=>{{const r=x.getBoundingClientRect();return {{tag:x.tagName,id:x.id,cls:x.className,left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height}}}});
      out.textContent=JSON.stringify({{
        iw:innerWidth,ih:innerHeight,scrollWidth:document.documentElement.scrollWidth,
        left:Math.min(...boxes.map(r=>r.left)),right:Math.max(...boxes.map(r=>r.right)),
        modal:modal?{{top:modal.getBoundingClientRect().top,bottom:modal.getBoundingClientRect().bottom,
          clientHeight:modal.clientHeight,scrollHeight:modal.scrollHeight}}:null,
        qr:qr?{{width:qr.getBoundingClientRect().width,right:qr.getBoundingClientRect().right}}:null,fixed
      }});
    }});
    </script>'''
    with tempfile.TemporaryDirectory() as td:
        page = Path(td) / "wallet.html"
        page.write_text(html)
        done = subprocess.run([
            CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
            f"--window-size={width},{height}", "--force-device-scale-factor=1",
            f"--screenshot={Path(td) / 'wallet.png'}", "--dump-dom", page.as_uri()], text=True, capture_output=True, timeout=30)
        shot = Path(td) / "wallet.png"
        assert shot.exists() and shot.stat().st_size > 1000
    assert done.returncode == 0, done.stderr[-1200:]
    match = re.search(r'<pre id="out">(.*?)</pre>', done.stdout, re.S)
    assert match, done.stdout[-1200:]
    return json.loads(unescape(match.group(1)))


@pytest.mark.skipif(not CHROME, reason="Chrome unavailable")
@pytest.mark.parametrize("width", [320, 360, 412, 1280])
def test_wallet_balance_address_and_history_never_widen_phone(width):
    body = f'''<main class="feed"><div class="mw-wrap">
      <header class="mw-head"><span class="mw-logo">ɱ</span><div><h2>Monero Wallet</h2><span class="mw-net">STAGENET · testing only</span></div><button class="btn">Refresh</button></header>
      <div class="mw-warning"><b>Small tips only.</b> Keep substantial Monero elsewhere.</div>
      <section class="mw-balance"><span>Available balance</span><strong>1,234,567,890,123.456789012345 <small>XMR</small></strong></section>
      <section class="mw-card mw-address"><h3>Receive address</h3><code>{ADDRESS}</code><button class="btn">Copy</button></section>
      <section class="mw-card"><div class="mw-history"><div class="mw-tx"><span class="mw-dir out">↑</span><span><b>Sent</b><small>11/14/2023, 10:13:20 PM</small></span><strong>−9,007,199.254740993123 XMR</strong></div></div></section>
    </div></main>'''
    got = run_page(width, 760, body)
    assert got["scrollWidth"] <= got["iw"]
    assert got["left"] >= -1 and got["right"] <= got["iw"] + 1
    # Regression for the reported square/arrow control in the lower-left: the wallet owns no fixed
    # corner control. Every fixed node in this isolated render must be absent.
    assert got["fixed"] == []


@pytest.mark.skipif(not CHROME, reason="Chrome unavailable")
@pytest.mark.parametrize("width", [320, 360, 412])
@pytest.mark.parametrize("kind", ["send", "receive", "confirm"])
def test_wallet_sheets_fit_narrow_and_keyboard_height_viewports(width, kind):
    if kind == "receive":
        content = f'''<div class="mw-modal"><h3>Receive Monero</h3><span class="mw-net">STAGENET</span><div class="mw-qr"><img alt="QR" src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='208' height='208'/%3E"></div><code>{ADDRESS}</code><div class="mw-actions"><button class="btn">Copy address</button><a class="btn">Open wallet</a></div></div>'''
    elif kind == "confirm":
        content = f'''<div class="mw-modal"><h3>Confirm payment</h3><div class="mw-confirm"><span>Send</span><strong>9,007,199.254740993123 XMR</strong><span>To</span><code>{ADDRESS}</code></div><label class="mw-check"><input type="checkbox"> I understand this transaction cannot be reversed.</label><button class="btn full">Send now</button><a class="btn full">Open external wallet instead</a></div>'''
    else:
        content = f'''<div class="mw-modal"><h3>Send Monero</h3><div class="mw-warning">Small tips only.</div><button class="btn full mw-scan">Scan wallet QR</button><div class="mw-scan-stage"><video></video><span>Point at a Monero payment QR…</span><button class="btn">Cancel scan</button></div><label>Recipient address<input class="input" value="{ADDRESS}"></label><label>Amount<input class="input" inputmode="decimal" value="0.001"></label><label>Note<input class="input" value="Thanks"></label><button class="btn full">Review payment</button></div>'''
    got = run_page(width, 480, f'<div class="modal-bg"><div class="modal glass">{content}</div></div>')
    assert got["scrollWidth"] <= got["iw"]
    assert got["left"] >= -1 and got["right"] <= got["iw"] + 1
    assert got["modal"]["top"] >= -1 and got["modal"]["bottom"] <= got["ih"] + 1
    if got["qr"]:
        assert got["qr"]["width"] <= 208 and got["qr"]["right"] <= got["iw"] + 1


def test_mobile_rules_include_safe_area_keyboard_and_long_token_guards():
    wallet_css = (ROOT / "static/css/monero-wallet.css").read_text()
    assert "100dvh" in wallet_css
    assert "safe-area-inset-bottom" in wallet_css
    assert "scroll-padding-block" in wallet_css
    assert "font-size:16px" in wallet_css  # prevents iOS zoom from moving the sheet off-screen
    assert "word-break:break-all" in wallet_css
    assert "minmax(0,1fr)" in wallet_css
    assert "max-height:min(42dvh,320px)" in wallet_css
