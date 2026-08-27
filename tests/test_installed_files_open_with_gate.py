"""The installed File Manager gate must execute the immutable ASAR payload."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts/check_installed_files_open_with.sh"


def test_gate_extracts_routing_and_preview_runtime_from_the_installed_asar():
    src = GATE.read_text(encoding="utf-8")
    assert "PC_INSTALLED_ASAR:-/opt/posterchan/resources/app.asar" in src
    assert "www/static/js/client/app.js" in src
    assert "www/static/js/client/hostfiles.js" in src
    assert "www/static/js/client/preview.js" in src
    assert "PC_INSTALLED_APP_JS=" in src
    assert "PC_INSTALLED_HOSTFILES_JS=" in src
    assert "PC_INSTALLED_PREVIEW_JS=" in src
    assert "folder_drop_paths_sim.js" in src
    assert "preview_sim.js" in src


def test_simulations_accept_an_installed_payload_override():
    open_with = (ROOT / "tests/client/open_with_selector_sim.js").read_text(encoding="utf-8")
    folder_drop = (ROOT / "tests/client/folder_drop_paths_sim.js").read_text(encoding="utf-8")
    hostfiles = (ROOT / "tests/client/hostfiles_click_sim.js").read_text(encoding="utf-8")
    preview = (ROOT / "tests/client/preview_sim.js").read_text(encoding="utf-8")
    assert "process.env.PC_INSTALLED_APP_JS" in open_with
    assert "process.env.PC_INSTALLED_APP_JS" in folder_drop
    assert "process.env.PC_INSTALLED_HOSTFILES_JS" in hostfiles
    assert "process.env.PC_INSTALLED_PREVIEW_JS" in preview


def test_installed_selector_sim_covers_document_media_and_cancel_lifecycle():
    src = (ROOT / "tests/client/open_with_selector_sim.js").read_text(encoding="utf-8")
    for fixture in ("manual.pdf", "server.conf", "sheet.csv", "photo.jpg", "movie.mp4",
                    "recording.ogg", "letter.docx", "book.ods", "slides.pptx"):
        assert fixture in src
    assert "cancel launched a handler" in src
    assert "close before launching the selected handler" in src
