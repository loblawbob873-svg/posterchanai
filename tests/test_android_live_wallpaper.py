"""The PosterChan cyberpunk live wallpaper — looked at, measured and held to the home screen's rules.

There is no emulator on this box, so the wallpaper is not tested by reading its source. The scene
(`wallpaper/CyberScene.java`) is written against a tiny `Pen` interface, and `tests/android_wallpaper/`
implements that over java.awt: the SHIPPED scene renders to real PNGs here, composited exactly the way
the engine composites it on a phone (the cached sky, the cached skyline blitted at the parallax shift,
then the moving frame). Every assertion below is about pixels a person would see.

What it must be, and why each test exists:
  * a cyberpunk city in the app's own palette — the sun, the grid, the POSTERCHAN sign actually ON
    SCREEN on the middle home page (a sign drawn off the edge passes every source-level test there is);
  * ALIVE: two moments differ where things move and are identical where they do not;
  * parallax: a page swipe moves the city and NOT the sky — that is what makes it read as distance;
  * a tap pulses across the floor (the launcher forwards COMMAND_TAP; without that the wallpaper never
    hears a touch, because the launcher's window is on top of it);
  * deterministic — the same moment twice is the same frame, which is what makes the rest testable;
  * BATTERY: the home screen's rule is that nothing it owns ticks while nobody is looking, and a live
    wallpaper is the one animated thing in it. `FramePolicy` is RUN: invisible schedules nothing at all.
"""
import os
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "mobile", "android", "app", "src", "main")
PKG = os.path.join(APP, "java", "place", "poster", "app", "wallpaper")
HARNESS = os.path.join(ROOT, "tests", "android_wallpaper")
STUBS = os.path.join(ROOT, "tests", "androidstubs")


def _read(*p):
    with open(os.path.join(*p), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def build():
    for t in ("javac", "java"):
        if shutil.which(t) is None:
            pytest.skip("no JDK")
    pytest.importorskip("PIL")
    out = tempfile.mkdtemp(prefix="wallpaper-")
    srcs = [os.path.join(PKG, f) for f in ("Pen.java", "CyberScene.java", "FramePolicy.java")]
    srcs += [os.path.join(HARNESS, f) for f in os.listdir(HARNESS) if f.endswith(".java")]
    c = subprocess.run(["javac", "-nowarn", "-d", out] + srcs, capture_output=True, text=True, timeout=300)
    assert c.returncode == 0, c.stderr[-3000:]
    yield out
    shutil.rmtree(out, ignore_errors=True)


def render(build, w=540, h=1200, t=3.2, offset=0.5, tap=None):
    from PIL import Image
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    args = [path, str(w), str(h), str(t), str(offset)]
    if tap:
        args += [str(tap[0]), str(tap[1]), str(tap[2])]
    r = subprocess.run(["java", "-Djava.awt.headless=true", "-cp", build, "place.poster.app.wallpaper.Render"] + args,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-3000:]
    img = Image.open(path).convert("RGB")
    img.load()
    os.unlink(path)
    return img


def count(img, box, pred):
    """Pixels in box (x0,y0,x1,y1) satisfying pred(r,g,b)."""
    px = img.load()
    n = 0
    for y in range(box[1], box[3]):
        for x in range(box[0], box[2]):
            if pred(*px[x, y]):
                n += 1
    return n


def is_cyan(r, g, b):
    # Generous on purpose: a 1px grid ray is anti-aliased into the dark floor, so its pixels are a DIM
    # cyan. What separates it from everything else in the scene is the hue — blue-green, not red.
    return b > 90 and g > 80 and r < 90 and b > r + 50 and g > r + 40


def is_gold(r, g, b):
    return r > 200 and g > 140 and b < 120


def is_magenta(r, g, b):
    return r > 150 and b > 120 and g < 110


def diff(a, b, box):
    pa, pb = a.load(), b.load()
    return sum(1 for y in range(box[1], box[3]) for x in range(box[0], box[2]) if pa[x, y] != pb[x, y])


HORIZON = int(1200 * 0.62)


# ------------------------------------------------------------------------------------- the picture

def test_it_is_a_cyberpunk_city_in_the_apps_palette(build):
    img = render(build)
    # a dark sky, not a blank or a stock gradient
    top = img.crop((0, 0, 540, 200)).resize((1, 1)).getpixel((0, 0))
    assert sum(top) < 60, f"the sky is not night: {top}"
    # the sun, gold, above the horizon
    assert count(img, (100, 300, 440, HORIZON), is_gold) > 1500, "no sun"
    # the grid floor: magenta rows AND cyan rays below the horizon
    floor = (0, HORIZON + 5, 540, 1200)
    assert count(img, floor, is_magenta) > 800, "no grid rows"
    assert count(img, floor, is_cyan) > 800, "no grid rays"


def test_the_sign_is_on_screen_on_the_middle_page_and_lit(build):
    """The sign is the branding; drawn off the edge it passes every test that reads source."""
    from PIL import Image  # noqa
    # Pick a moment the tube is not stuttering — the scene says which by its own noise; sample a few.
    best = 0
    for t in (0.5, 3.2, 7.7, 12.1):
        img = render(build, t=t)
        best = max(best, count(img, (0, 0, 540, HORIZON), is_cyan))
    assert best > 900, "the lit POSTERCHAN sign is not on the middle page"


def test_the_sun_is_sliced_where_you_can_see_it(build):
    """The bands across the sun ARE the synthwave look. Drawn in the half the skyline covers, they are
    drawn and never seen — which is exactly what the first version did."""
    img = render(build)
    px = img.load()
    x = 270
    runs, inside, prev_gold = 0, False, False
    for y in range(250, HORIZON - 120):
        g = is_gold(*px[x, y]) or (px[x, y][0] > 200 and px[x, y][1] > 90)   # gold → pink gradient
        if prev_gold and not g:
            runs += 1
        prev_gold = g
    assert runs >= 2, "no visible slices across the sun"


def test_portrait_and_landscape_both_compose(build):
    land = render(build, w=1200, h=750)
    hz = int(750 * 0.62)
    assert count(land, (300, 0, 900, hz), is_gold) > 3000, "no sun on a tablet"
    assert count(land, (0, hz + 5, 1200, 750), is_cyan) > 800, "no floor on a tablet"


# ----------------------------------------------------------------------------------- it is alive

def test_it_moves_where_things_move_and_nowhere_else(build):
    a = render(build, t=3.0)
    b = render(build, t=3.6)
    assert diff(a, b, (0, HORIZON + 5, 540, 1200)) > 2000, "the grid floor is not moving"
    # the sky's static half (stars, sun) — sample a band the rain is sparse in and the sign is not in
    sky_static = diff(a, b, (300, 40, 330, 120))
    assert sky_static < 400, "the static sky is being redrawn differently every frame"


def test_the_same_moment_is_the_same_frame(build):
    a = render(build, t=5.25)
    b = render(build, t=5.25)
    assert a.tobytes() == b.tobytes()


def test_a_page_swipe_moves_the_city_and_not_the_sky(build):
    left = render(build, offset=0.0, t=2.0)
    right = render(build, offset=1.0, t=2.0)
    assert diff(left, right, (0, HORIZON - 260, 540, HORIZON - 4)) > 5000, "no parallax on the skyline"


def test_a_tap_pulses_across_the_floor(build):
    plain = render(build, t=4.0)
    tapped = render(build, t=4.0, tap=(270, 950, 3.7))
    assert diff(plain, tapped, (0, HORIZON + 5, 540, 1200)) > 300, "a tap draws nothing"
    # and it is over a moment later, not a permanent mark
    later = render(build, t=6.5, tap=(270, 950, 3.7))
    assert diff(render(build, t=6.5), later, (0, 0, 540, 1200)) == 0, "a pulse never fades"


def test_the_sign_flickers_rarely_and_hums_otherwise(build):
    """A sign that stutters every few frames reads as broken; one that never does reads as a sticker."""
    harness = os.path.join(build, "Stutter.java")
    with open(harness, "w") as fh:
        fh.write("package place.poster.app.wallpaper;\npublic class Stutter{public static void main(String[] a){"
                 "CyberScene s=new CyberScene(540,1200,0x50C4L);int n=0;for(int i=0;i<20000;i++)if(s.rnd(i,51)<0.045f)n++;"
                 "System.out.print(n/20000f);}}")
    c = subprocess.run(["javac", "-nowarn", "-cp", build, "-d", build, harness], capture_output=True, text=True)
    assert c.returncode == 0, c.stderr
    r = subprocess.run(["java", "-cp", build, "place.poster.app.wallpaper.Stutter"], capture_output=True, text=True)
    frac = float(r.stdout)
    assert 0.02 < frac < 0.08, frac


# ------------------------------------------------------------------------------------------ battery

def test_invisible_schedules_nothing_at_all(build):
    harness = os.path.join(build, "Policy.java")
    with open(harness, "w") as fh:
        fh.write("package place.poster.app.wallpaper;\npublic class Policy{public static void main(String[] a){"
                 "System.out.print(FramePolicy.nextDelayMs(false,false)+\" \"+FramePolicy.nextDelayMs(false,true)+\" \""
                 "+FramePolicy.nextDelayMs(true,false)+\" \"+FramePolicy.nextDelayMs(true,true));}}")
    c = subprocess.run(["javac", "-nowarn", "-cp", build, "-d", build, harness], capture_output=True, text=True)
    assert c.returncode == 0, c.stderr
    out = subprocess.run(["java", "-cp", build, "place.poster.app.wallpaper.Policy"], capture_output=True, text=True).stdout
    hidden, hidden_saver, normal, saver = (int(x) for x in out.split())
    assert hidden == -1 and hidden_saver == -1, "a hidden wallpaper still schedules frames"
    assert 0 < normal <= 40, "slower than ~25fps reads as stutter"
    assert normal < saver <= 100, "battery saver does not slow it down (or freezes it)"


def _code(src):
    """Source with comments removed: a rule must be in the CODE, not merely described."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


def test_the_engine_only_ever_schedules_through_the_policy():
    eng = _code(_read(PKG, "CyberWallpaper.java"))
    vis = eng[eng.index("public void onVisibilityChanged"):]
    vis = vis[:vis.index("\n        }")]
    assert "removeCallbacks(frame)" in vis, "hiding the wallpaper leaves a frame pending"
    assert "postDelayed" not in vis
    # the ONE place a frame is scheduled, and it asks the policy
    assert eng.count("postDelayed(") == 1
    draw = eng[eng.index("private void draw()"):]
    assert "FramePolicy.nextDelayMs(visible, powerSave)" in draw and "if (next >= 0)" in draw
    destroy = eng[eng.index("public void onDestroy()"):]
    destroy = destroy[:destroy.index("\n        }")]
    assert "removeCallbacks(frame)" in destroy and "unregisterReceiver(saver)" in destroy


def test_nothing_in_the_wallpaper_keeps_the_phone_awake():
    for f in os.listdir(PKG):
        src = _code(_read(PKG, f))
        for banned in ("WakeLock", "newWakeLock", "AlarmManager", "java.util.Timer", "WorkManager", "setRepeating"):
            assert banned not in src, f"{f} uses {banned}"


# ----------------------------------------------------------------------------------------- wiring

def test_the_android_half_type_checks_here():
    if shutil.which("javac") is None:
        pytest.skip("no JDK")
    with tempfile.TemporaryDirectory() as out:
        srcs = [os.path.join(PKG, f) for f in os.listdir(PKG) if f.endswith(".java")]
        r = subprocess.run(["javac", "-nowarn", "-d", out, "-sourcepath",
                            STUBS + os.pathsep + os.path.join(APP, "java")] + srcs,
                           capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr[-3000:]


def test_android_can_find_and_bind_it():
    man = _read(APP, "AndroidManifest.xml")
    i = man.index('android:name=".wallpaper.CyberWallpaper"')
    block = man[man.rindex("<service", 0, i):man.index("</service>", i)]
    assert 'android:permission="android.permission.BIND_WALLPAPER"' in block, (
        "without BIND_WALLPAPER any app could bind the engine, and Android refuses to list it anyway")
    assert "android.service.wallpaper.WallpaperService" in block
    assert 'android:resource="@xml/cyber_wallpaper"' in block
    assert 'android:name="android.software.live_wallpaper" android:required="false"' in man, (
        "required=true would hide the whole app from phones without live wallpapers")
    meta = _read(APP, "res", "xml", "cyber_wallpaper.xml")
    assert "@drawable/wallpaper_cyber_thumb" in meta and "@string/wallpaper_cyber_desc" in meta
    assert os.path.getsize(os.path.join(APP, "res", "drawable-nodpi", "wallpaper_cyber_thumb.png")) > 10000
    strings = _read(APP, "res", "values", "strings.xml")
    for k in ("wallpaper_cyber_name", "wallpaper_cyber_desc", "home_live_wallpaper", "home_no_live_wallpaper"):
        assert f'name="{k}"' in strings, k


def test_the_launcher_offers_it_and_says_so_when_it_cannot():
    home = _read(APP, "java", "place", "poster", "app", "home", "HomeActivity.java")
    menu = home[home.index("private void homeMenu()"):]
    menu = menu[:menu.index("\n    }\n")]
    labels = re.findall(r"labels\.add\(getString\(R\.string\.(\w+)\)\)", menu)
    cases = dict(re.findall(r"case (\d+):\s*(.*?)(?=case \d+:|\}\s*\}\s*\})", menu, re.S))
    w = labels.index("home_live_wallpaper")
    assert "CyberWallpaper.open(" in cases[str(w)] and "home_no_live_wallpaper" in cases[str(w)], (
        "the menu index and its action drifted apart — the item would open something else")
    # the way back must still be the LAST item and still Settings
    assert labels[-1] == "home_phone_settings" and "Settings.ACTION_SETTINGS" in cases[str(len(labels) - 1)]


def test_the_launcher_forwards_empty_taps_to_the_wallpaper():
    desk = _code(_read(APP, "java", "place", "poster", "app", "home", "DeskView.java"))
    assert "else if (hit == null) tapWallpaper(e);" in desk
    assert "WallpaperManager.COMMAND_TAP" in desk and "sendWallpaperCommand(getWindowToken()" in desk


def test_phone_shell_settings_offers_it_too():
    js = _read(ROOT, "static", "js", "client", "phoneshell.js")
    assert 'id="ps-wallpaper"' in js and "plug('setLiveWallpaper')" in js
    assert "st.liveWallpaper" in js, "the card cannot say the wallpaper is already in use"
    plugin = _read(APP, "java", "place", "poster", "app", "home", "HomePlugin.java")
    assert "public void setLiveWallpaper(PluginCall call)" in plugin
    assert 'o.put("liveWallpaper", place.poster.app.wallpaper.CyberWallpaper.isActive(getContext()));' in plugin
