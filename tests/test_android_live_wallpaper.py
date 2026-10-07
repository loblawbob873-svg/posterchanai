"""The PosterChan cyberpunk live wallpaper — looked at, measured and held to the home screen's rules.

There is no emulator on this box, so the wallpaper is not tested by reading its source. The scene
(`wallpaper/CyberScene.java`) is written against a tiny `Pen` interface, and `tests/android_wallpaper/`
implements that over java.awt: the SHIPPED scene renders to real PNGs here, composited exactly the way
the engine composites it on a phone (the cached sky, the cached skyline blitted at the parallax shift,
then the moving frame). Every assertion below is about pixels a person would see.

What it must be, and why each test exists:
  * POSTERCHAN IN A CYBERPUNK CITY ("kinda cringe, must include posterchan in a cyberpunk city"): she
    dances on a wet rooftop in front of a neon night city, with a POSTERCHAN rooftop sign and a hologram
    billboard of her -- all actually ON SCREEN on the middle home page (art drawn off the edge, or a
    frame the engine never loaded, passes every source-level test there is);
  * ALIVE: she dances, and the same moment twice is the same frame;
  * parallax: a page swipe moves the city, and never moves her off the screen;
  * a tap makes her hop and ripples the roof (the launcher forwards COMMAND_TAP; without that the
    wallpaper never hears a touch, because the launcher's window is on top of it);
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
    # Her frames, exactly as the APK ships them (res/drawable-nodpi/pc_dance_N.webp); AWT reads PNG.
    from PIL import Image
    frames = os.path.join(out, "frames")
    os.makedirs(frames)
    for i in range(1, 9):
        Image.open(os.path.join(APP, "res", "drawable-nodpi", f"pc_dance_{i}.webp")).convert("RGBA").save(
            os.path.join(frames, f"dance-{i}.png"))
    yield out
    shutil.rmtree(out, ignore_errors=True)


def render(build, w=540, h=1200, t=3.2, offset=0.5, tap=None):
    from PIL import Image
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    args = [path, str(w), str(h), str(t), str(offset)]
    if tap:
        args += [str(tap[0]), str(tap[1]), str(tap[2])]
    r = subprocess.run(["java", "-Djava.awt.headless=true", "-Dpc.frames=" + os.path.join(build, "frames"), "-cp", build,
                        "place.poster.app.wallpaper.Render"] + args,
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


def is_magenta(r, g, b):
    return r > 150 and b > 120 and g < 110


def diff(a, b, box):
    pa, pb = a.load(), b.load()
    return sum(1 for y in range(box[1], box[3]) for x in range(box[0], box[2]) if pa[x, y] != pb[x, y])


# ------------------------------------------------------------------------------------- the picture
# The scene at 540x1200 (portrait): the roof she stands on is at 0.80h, she is 0.36h tall, centred.
ROOF = int(1200 * 0.80)
HER = (270 - 160, ROOF - 440, 270 + 160, ROOF)          # the box she dances in, on the middle page


def is_hair(r, g, b):
    """Her orange hair: the one colour nothing else in the city is (a gold-to-magenta sun is never
    this orange with this little blue -- the old wallpaper's sun had to fail this)."""
    return r > 200 and 80 < g < 190 and b < 60


def hair_moved(a, b, box):
    """Pixels that are her hair in one picture and not the other: SHE moved, not the rain or a sign."""
    pa, pb = a.load(), b.load()
    return sum(1 for y in range(box[1], box[3]) for x in range(box[0], box[2]) if is_hair(*pa[x, y]) != is_hair(*pb[x, y]))


def test_it_is_posterchan_in_a_cyberpunk_city(build):
    """'must include posterchan in a cyberpunk city': she is THERE, on the middle page, in the app's art."""
    img = render(build)
    top = img.crop((0, 0, 540, 150)).resize((1, 1)).getpixel((0, 0))
    assert max(top) < 50, f"the sky is not night: {top}"
    assert count(img, HER, is_hair) > 2500, "PosterChan is not on the home screen"
    city = (0, 250, 540, ROOF - 4)
    assert count(img, city, is_magenta) > 600 and count(img, city, is_cyan) > 300, "no neon in the city"
    warm = count(img, city, lambda r, g, b: r > 150 and g > 100 and b < 120)
    assert warm > 400, "the towers have no lit windows"


def test_the_posterchan_sign_and_her_hologram_are_lit_on_the_middle_page(build):
    """The rooftop sign above her head and the hologram billboard of her, both actually on screen."""
    best_sign = best_holo = 0
    for t in (0.5, 3.2, 7.7, 12.1):          # some moment where the tube is not stuttering
        img = render(build, t=t)
        best_sign = max(best_sign, count(img, (40, 230, 500, HER[1] - 20), is_magenta))
        best_holo = max(best_holo, count(img, (330, 250, 540, ROOF - 120), is_cyan))
    assert best_sign > 1500, "the lit POSTERCHAN sign is not above her on the middle page"
    assert best_holo > 1500, "the hologram billboard is not on the middle page"


def test_she_is_reflected_in_the_wet_roof(build):
    img = render(build)
    lit = lambda r, g, b: r + g + b > 75                   # anything not wet black
    under = count(img, (190, ROOF + 6, 350, 1200), lit)    # beneath her feet
    beside = count(img, (0, ROOF + 6, 160, 1200), lit)     # the same-size patch of roof off to the side
    assert under > beside + 1500, f"no reflection under her feet ({under} lit vs {beside} beside)"


def test_portrait_and_landscape_both_compose(build):
    land = render(build, w=1200, h=750)
    roof = int(750 * 0.83)
    assert count(land, (380, 0, 820, roof), is_hair) > 2000, "PosterChan is not on a tablet"
    assert count(land, (0, 0, 1200, roof), is_magenta) > 1500, "no city on a tablet"
    small = render(build, w=360, h=640, offset=0.0)
    assert count(small, (0, 0, 360, 640), is_hair) > 600, "she is lost on a small phone's first page"


# ----------------------------------------------------------------------------------- it is alive

def test_she_dances(build):
    a = render(build, t=3.0)
    b = render(build, t=3.45)
    assert hair_moved(a, b, HER) > 1500, "PosterChan is standing still"


def test_the_same_moment_is_the_same_frame(build):
    a = render(build, t=5.25)
    b = render(build, t=5.25)
    assert a.tobytes() == b.tobytes()


def test_a_page_swipe_moves_the_city_and_keeps_her_on_screen(build):
    left = render(build, offset=0.0, t=2.0)
    right = render(build, offset=1.0, t=2.0)
    assert diff(left, right, (0, 250, 540, ROOF - 4)) > 8000, "no parallax on the skyline"
    for img in (left, right):
        assert count(img, HER, is_hair) > 2000, "a page swipe moved her off the screen"


def test_a_tap_makes_her_hop_and_ripples_the_roof(build):
    plain = render(build, t=4.0)
    tapped = render(build, t=4.0, tap=(270, 1100, 3.65))
    assert hair_moved(plain, tapped, HER) > 1500, "a tap does not move her"
    assert diff(plain, tapped, (0, ROOF + 4, 540, 1200)) > 300, "a tap draws no ripple"
    later = render(build, t=6.5, tap=(270, 1100, 3.65))
    assert diff(render(build, t=6.5), later, (0, 0, 540, 1200)) == 0, "a tap never wears off"


def test_the_app_ships_her_frames_and_the_engine_loads_them_by_name():
    """The scene draws nothing for a frame the engine could not load -- so missing art is silent."""
    for i in range(1, 9):
        f = os.path.join(APP, "res", "drawable-nodpi", f"pc_dance_{i}.webp")
        assert os.path.getsize(f) > 10000, f
    eng = _read(PKG, "CyberWallpaper.java")
    assert '"pc_dance_" + (i + 1), "drawable"' in eng and "pen.frames = dance;" in eng


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


def test_the_launcher_offers_it_as_the_default_once(tmp_path):
    """'if you enable the poster chan launcher, can you make that live wallpaper the default?' Android lets
    no ordinary app set a live wallpaper, so the default is the system's own preview of it, opened the first
    time the launcher is the home screen -- and only that once."""
    if shutil.which("javac") is None:
        pytest.skip("no JDK")
    home = os.path.join(APP, "java", "place", "poster", "app", "home")
    drv = tmp_path / "Drv.java"
    drv.write_text("package place.poster.app.home;\npublic class Drv{public static void main(String[] a){"
                   "boolean[] t={true,false};StringBuilder s=new StringBuilder();"
                   "for(boolean d:t)for(boolean o:t)for(boolean f:t)s.append(WallpaperOffer.shouldOffer(d,o,f)?'1':'0');"
                   "System.out.print(s);}}")
    c = subprocess.run(["javac", "-nowarn", "-d", str(tmp_path), os.path.join(home, "WallpaperOffer.java"), str(drv)],
                       capture_output=True, text=True)
    assert c.returncode == 0, c.stderr
    out = subprocess.run(["java", "-cp", str(tmp_path), "place.poster.app.home.Drv"], capture_output=True, text=True).stdout
    # (home, ours active, offered) in TT..FF order: only "home, not ours, never offered" offers.
    assert out == "00010000", out
    act = _read(home, "HomeActivity.java")
    body = act[act.index("private void offerWallpaperOnce()"):act.index("private void dropWallpaperBanner()")]
    # OFFERED, never opened by itself: the system preview opened on resume took the screen away from the
    # launcher the first time it appeared (the emulator's "HOME did not show the independent native launcher").
    assert "CyberWallpaper.open(" not in body.split("onClick(View v)")[0], "the preview opens without being asked for"
    assert "onClick(View v)" in body and "CyberWallpaper.open(HomeActivity.this)" in body, "tapping the offer does not open the preview"
    assert body.count("prefs.setWallpaperOffered()") >= 2, "tapping or hiding the offer does not answer it for good"
    assert "offerWallpaperOnce();" in act[act.index("protected void onResume()"):][:400]
    assert 'name="home_wallpaper_offer"' in _read(APP, "res", "values", "strings.xml")