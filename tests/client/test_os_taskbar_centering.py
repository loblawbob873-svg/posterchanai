from pathlib import Path


CSS = (Path(__file__).parents[2] / "static/css/client.css").read_text()


def test_task_group_is_centered_on_screen_not_in_asymmetric_leftover_space():
    rule = CSS[CSS.index(".os-tasks{"):CSS.index(".os-tasks::-webkit-scrollbar")]
    assert "position:absolute" in rule
    assert "inset-inline-start:50%" in rule
    assert "transform:translateX(-50%)" in rule


def test_centered_task_group_is_bounded_and_scrolls_when_many_apps_are_open():
    rule = CSS[CSS.index(".os-tasks{"):CSS.index(".os-tasks::-webkit-scrollbar")]
    assert "width:max-content" in rule
    assert "max-width:calc(100% - min(780px,64vw))" in rule
    assert "overflow-x:auto" in rule
