"""Image text that Tesseract cannot read is read by RapidOCR -- and only then.

2026-10-05: a photo of a whiteboard riddle in marker ("What is a witch's favorite school subject?") came
back from Tesseract as an EMPTY string, so AI Chat told the person "I can't see the image". RapidOCR read
every line. It is a fallback, never the first reader: Tesseract's multi-language pass is what the
translate feature depends on for Thai/Arabic/Hindi, and RapidOCR's model is Chinese+English.
"""
import base64
import io

import pytest
from PIL import Image, ImageDraw, ImageFont

from app.services import document_service as ds


def _image_b64(text="FAVORITE SUBJECT"):
    im = Image.new("RGB", (1400, 360), (236, 238, 240))           # a whiteboard is never pure white
    d = ImageDraw.Draw(im)
    d.text((60, 110), text, fill=(40, 30, 120), font=ImageFont.load_default(size=110))
    buf = io.BytesIO(); im.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def test_when_tesseract_reads_nothing_the_fallback_reads_the_image(monkeypatch):
    pytest.importorskip("rapidocr_onnxruntime")
    pytesseract = pytest.importorskip("pytesseract")
    monkeypatch.setattr(pytesseract, "image_to_string", lambda *a, **k: "")
    out = ds.extract_image_text(_image_b64()) or ""
    assert "FAVORITE" in out.upper() and "SUBJECT" in out.upper(), out


def test_when_tesseract_reads_text_the_fallback_is_never_asked(monkeypatch):
    """The translate path: Tesseract's answer (Thai, Arabic, ...) must reach the caller untouched."""
    pytesseract = pytest.importorskip("pytesseract")
    monkeypatch.setattr(pytesseract, "image_to_string", lambda *a, **k: "สวัสดี")
    asked = []
    monkeypatch.setattr(ds, "_rapidocr_text", lambda image: asked.append(1) or "WRONG")
    if not ds.shutil_which_tesseract():
        pytest.skip("tesseract binary not installed here")
    assert ds.extract_image_text(_image_b64()) == "สวัสดี"
    assert not asked, "RapidOCR overrode a reading Tesseract had already made"


def test_without_the_fallback_installed_nothing_changes(monkeypatch):
    pytesseract = pytest.importorskip("pytesseract")
    monkeypatch.setattr(pytesseract, "image_to_string", lambda *a, **k: "")
    monkeypatch.setattr(ds, "_rapidocr_text", lambda image: None)
    assert ds.extract_image_text(_image_b64()) is None
