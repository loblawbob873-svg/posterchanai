from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_custom_scheme_desktop_does_not_probe_remote_images_with_cors():
    """Remote media still displays through its opaque request without a noisy doomed CORS probe."""
    sw = (ROOT / "static/js/client/sw.js").read_text(encoding="utf-8")
    media = sw[sw.index("async function cacheFirstMedia"):sw.index("async function mediaBudgetBytes")]
    assert "const webOrigin = /^https?:$/.test(self.location.protocol)" in media
    assert "if (webOrigin && req.destination === 'image'" in media
    assert media.index("if (webOrigin && req.destination === 'image'") < media.index("fetch(req.url, { mode: 'cors'")
    assert "if (!res) { try { res = await fetch(req);" in media
