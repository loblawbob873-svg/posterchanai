"""PosterChanOS launcher geometry remains readable after the desktop zoom is applied."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "static/css/client.css").read_text()
JS = (ROOT / "static/js/client/os.js").read_text()


def _rule(selector: str) -> str:
    start = CSS.index(selector + "{")
    return CSS[start:CSS.index("}", start) + 1]


def test_desktop_icon_css_and_drag_geometry_agree():
    icon = _rule(".os-icon")
    values = re.search(r"width:(\d+)px;height:(\d+)px", icon)
    assert values
    constants = re.search(
        r"const ICON_W = (\d+), ICON_H = (\d+), ICON_GAP = (\d+)", JS
    )
    assert constants
    assert values.groups() == constants.groups()[:2]
    assert int(values.group(1)) >= 120
    assert int(values.group(2)) >= 100


def test_launcher_labels_survive_high_resolution_shell_scaling():
    icon_label = _rule(".os-icon span")
    app = _rule(".os-app")
    stats = _rule(".os-stat")
    assert "font-size:14px" in icon_label
    assert "font-size:17px" in app
    assert "font-size:14px" in stats


def test_start_menu_has_room_for_larger_rows():
    menu = _rule(".os-startmenu")
    assert "width:min(780px" in menu
    assert "height:min(920px,calc(100vh - 78px))" in menu
    assert "max-height:calc(100vh - 78px)" in menu
