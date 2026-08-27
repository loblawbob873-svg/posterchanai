from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text()
MEME = (ROOT / "static/js/client/meme.js").read_text()


def test_save_actions_use_the_file_manager_name():
    for source in (APP, MEME):
        assert not re.search(r">Save to (?:my )?Blossom(?: drive)?<", source)
    assert APP.count(">Save to Files</button>") >= 2
    assert "Save to Files  (B)" in APP
    assert ">Save to Files</button>" in MEME


def test_save_results_also_speak_in_file_manager_terms():
    assert "Files save failed:" in APP
    assert "saved to Files" in MEME
    assert "no saved projects found in Files" in MEME


def test_file_actions_use_folder_icon_not_blossom_flower():
    user_markup = APP.replace('<symbol id="i-flower"', '<symbol id="technical-flower"')
    assert '<use href="#i-flower"></use>' not in user_markup
    assert "mkBtn('❀" not in APP
    assert "📁 Files" in APP
