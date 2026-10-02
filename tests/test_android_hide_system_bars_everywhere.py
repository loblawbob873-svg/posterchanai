"""'Hide system bars' holds on every native screen, not only inside the app.

'on samsung tablet, i chose hide system bars but i still see both': the choice was applied in
MainActivity alone, so the LAUNCHER (HomeActivity -- the screen a tablet sits on), Texts, the dialer
and the call screen all kept both bars. Android drops immersive mode after any dialog or app switch,
so each screen must re-apply it on resume AND when its window regains focus.

The Gradle build runs only in CI; this reads the shipped sources: every screen in the manifest is
either MainActivity, HomeActivity or a PcActivity, those three apply the choice in both callbacks, and
a subclass overriding either callback still calls super (or it would silently undo the base's call).
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
JAVA = ROOT / "mobile/android/app/src/main/java/place/poster/app"


def _method(src, signature):
    i = src.find(signature)
    if i < 0:
        return ""
    j = src.index("{", i)
    depth = 0
    for k in range(j, len(src)):
        depth += src[k] == "{"
        depth -= src[k] == "}"
        if depth == 0:
            return src[j:k + 1]
    return ""


def test_every_full_screen_activity_applies_the_choice_on_resume_and_focus():
    for rel in ("MainActivity.java", "home/HomeActivity.java", "ui/PcActivity.java"):
        src = (JAVA / rel).read_text()
        resume = _method(src, "void onResume()")
        focus = _method(src, "void onWindowFocusChanged(boolean hasFocus)")
        assert "SystemBars.apply(this)" in resume, f"{rel}: onResume does not apply 'Hide system bars'"
        assert "SystemBars.apply(this)" in focus, f"{rel}: regaining focus does not re-apply 'Hide system bars'"


def test_subclasses_keep_the_base_call():
    for f in JAVA.rglob("*.java"):
        src = f.read_text()
        if "extends PcActivity" not in src:
            continue
        for sig, sup in (("void onResume()", "super.onResume()"),
                         ("void onWindowFocusChanged(boolean hasFocus)", "super.onWindowFocusChanged(hasFocus)")):
            body = _method(src, sig)
            if body:
                assert sup in body, f"{f.name} overrides {sig} without calling super -- the bars come back there"


def test_the_native_screens_are_pc_activities():
    for rel in ("sms/ThreadListActivity.java", "sms/ThreadActivity.java",
                "phone/DialerActivity.java", "phone/InCallActivity.java"):
        assert re.search(r"class \w+ extends PcActivity", (JAVA / rel).read_text()), rel
