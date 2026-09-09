"""THE PHONE ENFORCES ITS OWN ANSWER, even when the server has the wrong one.

The server filter is the primary gate, and it is enough while the mirror to it succeeds. It can
fail — offline, a refused signature, a reinstall that leaves a stale row — and that failure is
SILENT and looks exactly like the bug the feature exists to fix: the tab says likes are off and the
phone keeps buzzing for likes. So the device keeps a copy of the toggles natively (localStorage
cannot serve here: the WebView is not running when a push arrives) and re-checks.

`allowsType(String, String)` is deliberately a pure function with no Context so it can be RUN here
rather than described. A source assertion would have passed just as happily against a gate that
reads a preference and ignores it — that is how the Android sweep spent a whole rewrite untested.
"""
from pathlib import Path
import re
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
PUSH = ROOT / "mobile/android/app/src/main/java/place/poster/app/push"
STORE = (PUSH / "DirectPushStore.java").read_text()
EVENTS = (PUSH / "PushEventService.java").read_text()
PLUGIN = (PUSH / "PushPlugin.java").read_text()

pytestmark = pytest.mark.skipif(not (shutil.which("javac") and shutil.which("java")),
                                reason="a JDK is required to RUN the decision")

# org.json is not in the JDK, so the harness carries a minimal stand-in with the two calls the
# decision makes. It is the SHIPPED method body that runs — only its JSON dependency is local.
HARNESS = """
import java.util.*;

public class PrefGate {
%s

    public static void main(String[] a) throws Exception {
        // unset / empty / corrupt / unknown -> SHOW (fail open, like the server)
        check(allowsType(null, "likes"), true, "unset");
        check(allowsType("", "likes"), true, "empty");
        check(allowsType("{ not json", "likes"), true, "corrupt");
        check(allowsType("{\\"replies\\":false}", "likes"), true, "a different type is off");
        check(allowsType("{\\"likes\\":null}", "likes"), true, "an explicit null is not a no");
        // an explicit false, and only that, silences
        check(allowsType("{\\"likes\\":false}", "likes"), false, "explicit false");
        check(allowsType("{\\"likes\\":true}", "likes"), true, "explicit true");
        check(allowsType("{\\"likes\\":false,\\"zaps\\":false}", "replies"), true, "untouched type");
        // a call has no toggle and must ring whatever else is off
        check(allowsType("{\\"likes\\":false}", "call"), true, "a call is never gated");
        check(allowsType("{\\"call\\":false}", "call"), true, "…not even by a forged key");
        // no type at all is not a licence to drop it
        check(allowsType("{\\"likes\\":false}", ""), true, "typeless payload still shows");
        check(allowsType("{\\"likes\\":false}", null), true, "null type still shows");
        System.out.println("OK");
    }

    static void check(boolean got, boolean want, String what) {
        if (got != want) throw new AssertionError(what + ": expected " + want + " got " + got);
    }
}
"""

JSON_STUB = """
class JSONObject {
    private final Map<String,Object> m = new HashMap<>();
    JSONObject(String src) {
        String s = src.trim();
        if (!s.startsWith("{") || !s.endsWith("}")) throw new RuntimeException("bad json");
        s = s.substring(1, s.length() - 1).trim();
        if (s.isEmpty()) return;
        for (String part : s.split(",")) {
            String[] kv = part.split(":");
            if (kv.length != 2) throw new RuntimeException("bad json");
            String k = kv[0].trim().replace("\\"", ""), v = kv[1].trim();
            m.put(k, "null".equals(v) ? null : Boolean.valueOf(v));
        }
    }
    boolean isNull(String k) { return !m.containsKey(k) || m.get(k) == null; }
    boolean optBoolean(String k, boolean d) {
        Object v = m.get(k); return v instanceof Boolean ? (Boolean) v : d;
    }
}
"""


def _decision_source():
    """The SHIPPED allowsType(String, String), lifted verbatim."""
    i = STORE.index("    static boolean allowsType(String storedJson, String type) {")
    j = STORE.index("\n    }", i) + len("\n    }")
    return STORE[i:j]


def test_the_native_gate_runs_and_fails_open(tmp_path):
    src = tmp_path / "PrefGate.java"
    body = _decision_source().replace("    static boolean", "    public static boolean", 1)
    src.write_text(HARNESS % body + JSON_STUB)
    build = subprocess.run(["javac", "-d", str(tmp_path), str(src)],
                           capture_output=True, text=True, timeout=120)
    assert build.returncode == 0, build.stderr
    run = subprocess.run(["java", "-cp", str(tmp_path), "PrefGate"],
                         capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "OK" in run.stdout


def test_the_gate_is_actually_consulted_before_a_notification_is_drawn():
    """A decision nothing calls is the failure this whole file is about, one level up."""
    deliver = EVENTS[EVENTS.index("public static boolean deliver("):EVENTS.index("/** A blocked channel")]
    assert "DirectPushStore.allowsType(ctx, type)" in deliver
    assert deliver.index("allowsType") < deliver.index("show(ctx, title, body, type, eventTag, route)"), \
        "the check must come BEFORE the notification is drawn"


def test_a_silenced_push_is_acknowledged_rather_than_retried_for_ever():
    """`deliver` returning false means "not handled", and a durable transport re-delivers it. A
    notification the user asked not to see is handled — dropping it is the point."""
    deliver = EVENTS[EVENTS.index("public static boolean deliver("):EVENTS.index("/** A blocked channel")]
    gate = deliver[deliver.index("DirectPushStore.allowsType(ctx, type)"):]
    assert re.match(r"DirectPushStore\.allowsType\(ctx, type\)\)\s*return true;", gate), gate[:120]


def test_the_phone_can_be_told_the_preferences_at_all():
    """Without the plugin method the native copy is never written, and the local gate would be a
    permanent no-op that reads as working."""
    assert "public void setPrefs(PluginCall call)" in PLUGIN
    assert "DirectPushStore.setTypePrefs(getContext()" in PLUGIN
    app = (ROOT / "static/js/client/app.js").read_text()
    fn = app[app.index("  async function mirrorPushPrefs("):app.index("  function notificationPreference(")]
    assert "setPrefs({prefs:" in fn, "the client never sends them to the phone"
    # And it happens on the SAME path as the server mirror, so the two cannot drift apart.
    assert fn.index("setPrefs({prefs:") < fn.index("fetch('/api/push/prefs'")
