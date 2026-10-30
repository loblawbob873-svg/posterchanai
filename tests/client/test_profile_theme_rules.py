"""The profile theme reader (static/js/client/proftheme.js), RUN under node: anybody can publish a kind 16767, so what
it lets through decides what a stranger's theme can do to every visitor's page.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which("node")


def run(js):
    code = "require(%s); const T = globalThis.PCProfileTheme; const out = (%s); console.log(JSON.stringify(out));" % (
        json.dumps(str(ROOT / "static/js/client/proftheme.js")), js)
    r = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


pytestmark = pytest.mark.skipif(not NODE, reason="node required")

BASE = [["c", "#10131d", "background"], ["c", "#e5f0f5", "text"], ["c", "#91b250", "primary"]]


def theme(tags):
    return run("T.parse({kind:16767, tags:%s})" % json.dumps(BASE + tags))


def test_a_ditto_theme_reads_as_ditto_wrote_it():
    """The real event Armada published (relay.ditto.pub, 2026-10-29)."""
    t = theme([["f", "Silkscreen", "https://cdn.jsdelivr.net/fontsource/fonts/silkscreen@latest/latin-400-normal.woff2", "body"],
               ["bg", "url https://blossom.ditto.pub/6f8c.jpg", "mode cover", "m image/jpeg"], ["client", "Armada"]])
    assert t["colors"] == {"background": "#10131d", "text": "#e5f0f5", "primary": "#91b250"}
    assert t["fonts"]["body"]["name"] == "Silkscreen" and t["fonts"]["body"]["url"].startswith("https://cdn.jsdelivr.net/")
    assert t["bg"] == {"url": "https://blossom.ditto.pub/6f8c.jpg", "mode": "cover", "m": "image/jpeg"}


@pytest.mark.parametrize("bad", ["red", "#fff", "#12345g", "#123456;background:red", "url(x)"])
def test_a_colour_must_be_a_colour(bad):
    t = theme([["c", bad, "name"], ["c", bad, "primary"]])
    assert t["colors"]["primary"] == "#91b250" and not (t.get("parts") or {}).get("name"), t


@pytest.mark.parametrize("url", ["http://blossom.ditto.pub/x.jpg", "javascript:alert(1)", "data:image/png;base64,AAAA", "//x.example/a.jpg"])
def test_a_background_must_be_https(url):
    assert theme([["bg", "url " + url, "mode cover"]])["bg"] is None


def test_quotes_in_an_address_cannot_close_the_css_url():
    t = theme([["bg", 'url https://x.example/a.jpg");background:url("https://evil.example/', "mode cover"]])
    assert t["bg"] and '"' not in t["bg"]["url"] and ")" not in t["bg"]["url"].split("example", 1)[1].split("/", 1)[0], t


def test_fonts_load_only_from_font_hosts():
    t = theme([["f", "Spy", "https://tracker.example/f.woff2", "body"], ["f", "Fine", "https://fonts.gstatic.com/s/x.woff2", "title"]])
    assert t["fonts"]["body"]["url"] == "", "a font from any host would make every visitor call it"
    assert t["fonts"]["title"]["url"].startswith("https://fonts.gstatic.com/")


def test_a_font_name_cannot_carry_css():
    t = theme([["f", 'X", serif; } body{display:none} /*', "https://fonts.gstatic.com/s/x.woff2", "body"]])
    assert set(t["fonts"]["body"]["name"]) <= set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _-"), t


def test_unreadable_text_is_made_readable_and_the_accent_keeps_its_colour():
    v = run("T.vars(T.parse({kind:16767, tags:[['c','#1c253f','background'],['c','#1d2640','text'],['c','#c03838','primary']]}))")
    assert v["--text"] in ("#ffffff", "#000000"), "near-invisible text was kept"
    assert v["--accent"] == "#c03838", "the theme's own colour was thrown away"
    c = run("T.contrast('%s', '#1c253f')" % v["--cyan"])
    assert c >= 3, ("link-coloured text is unreadable", v["--cyan"], c)


def test_vyrams_parts_round_trip_and_other_clients_keep_the_base_three():
    t = theme([["c", "#ff3366", "name"], ["c", "#00ff66", "border"], ["border", "none"], ["c", "#000000", "nonsense-role"]])
    assert t["parts"] == {"name": "#ff3366", "border": "#00ff66"} and t["border"] == "none"
    tags = run("T.tags(T.parse({kind:16767, tags:%s}))" % json.dumps(BASE + [["c", "#ff3366", "name"], ["border", "none"]]))
    assert tags[:3] == BASE, "the three Ditto/Armada colours must come first and unchanged"
    assert ["c", "#ff3366", "name"] in tags and ["border", "none"] in tags
    again = run("T.parse({kind:16767, tags:T.tags(T.parse({kind:16767, tags:%s}))})" % json.dumps(BASE + [["c", "#ff3366", "name"]]))
    assert again["parts"] == {"name": "#ff3366"}


def test_another_kind_is_not_a_theme():
    assert run("T.parse({kind:0, tags:%s})" % json.dumps(BASE)) is None


def test_every_theme_to_start_from_is_whole_readable_and_travels():
    """'maybe add more profile themes that people can choose' (2026-10-29): each preset is a whole look -- colours, a
    text font and/or a title font from FONTS, part colours, borders -- and must (1) survive the trip through the
    kind-16767 tags exactly as Ditto/Armada would read it, and (2) need no rescue: its text is readable on its own
    background, so vars() keeps the colour the theme chose instead of swapping in black or white."""
    got = run("""(() => { const out = {};
      for (const n of Object.keys(T.PRESETS)) { const t = T.presetTheme(n), back = T.parse({kind:16767, tags:T.tags(t)});
        out[n] = { t, back, text: T.vars(back)['--text'], fonts: T.FONTS }; }
      return out; })()""")
    assert len(got) >= 20, ("more themes to choose from", len(got))
    for name, g in got.items():
        t, back = g["t"], g["back"]
        assert back["colors"] == t["colors"], (name, back["colors"])
        assert back.get("parts", {}) == t["parts"], (name, back.get("parts"))
        assert (back.get("border") or "") == t["border"], name
        for role, f in t["fonts"].items():
            assert f["name"] in g["fonts"], (name, "a font outside FONTS", f["name"])
            assert back["fonts"][role]["name"] == f["name"] and back["fonts"][role]["url"] == f["url"], (name, role)
        assert g["text"] == t["colors"]["text"], (name, "its text is unreadable on its background and got replaced")
    assert len({n.lower() for n in got}) == len(got)
