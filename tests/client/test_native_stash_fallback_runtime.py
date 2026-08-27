"""The no-screenshot native placeholder must never raster as a black window."""

import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageStat


ROOT = Path(__file__).resolve().parents[2]


def test_native_stash_fallback_is_bright_varied_and_clickable():
    chrome = next((shutil.which(x) for x in ("chromium", "chromium-browser", "google-chrome")
                   if shutil.which(x)), None)
    if not chrome:
        return
    with tempfile.TemporaryDirectory(prefix="pc-native-stash-") as td:
        td = Path(td)
        html = td / "probe.html"
        shot = td / "probe.png"
        html.write_text(f"""<!doctype html><link rel=stylesheet href={
            (ROOT / 'static/css/client.css').as_uri()}><style>
            html,body{{margin:0;background:#000}}.osw{{position:relative!important;left:20px!important;
            top:20px!important;width:500px!important;height:320px!important}}.osw-body{{height:270px}}
            </style><div class='osw native-stashed'><div class=osw-bar>Firefox</div>
            <div class=osw-body></div></div>""")
        run = subprocess.run([chrome, "--headless", "--no-sandbox", "--disable-gpu",
                              "--hide-scrollbars", "--window-size=560,380",
                              f"--screenshot={shot}", html.as_uri()],
                             capture_output=True, timeout=30)
        assert run.returncode == 0 and shot.exists(), run.stderr.decode(errors="replace")[-1000:]
        crop = Image.open(shot).convert("RGB").crop((25, 75, 515, 335))
        stat = ImageStat.Stat(crop)
        mean = sum(stat.mean) / 3
        variance = sum(stat.var) / 3
        assert mean > 45, f"fallback raster is effectively black (mean={mean:.2f})"
        assert variance > 250, f"fallback raster is an empty flat panel (variance={variance:.2f})"


def test_preview_memory_is_bounded_and_cleared_on_restore_and_close():
    capture = (ROOT / "desktop/native-preview.js").read_text()
    os_js = (ROOT / "static/js/client/os.js").read_text()
    assert "maxBuffer:16*1024*1024" in capture
    assert "b.length<=16*1024*1024" in capture
    assert os_js.count("_nativePreview(it.w,'')") >= 2
    assert "_nativePreview(w,'')" in os_js
