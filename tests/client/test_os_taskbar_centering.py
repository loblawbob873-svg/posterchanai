from pathlib import Path


CSS = (Path(__file__).parents[2] / "static/css/client.css").read_text()


def test_task_group_follows_start_from_the_left_without_recentering_open_apps():
    rule = CSS[CSS.index(".os-tasks{"):CSS.index(".os-tasks::-webkit-scrollbar")]
    assert "position:static" in rule
    assert "flex:1 1 auto" in rule
    assert "justify-content:flex-start" in rule
    assert "inset-inline-start:50%" not in rule
    assert "translateX(-50%)" not in rule


def test_left_aligned_task_group_scrolls_when_many_apps_are_open():
    rule = CSS[CSS.index(".os-tasks{"):CSS.index(".os-tasks::-webkit-scrollbar")]
    assert "width:auto" in rule
    assert "min-width:0" in rule
    assert "overflow-x:auto" in rule
