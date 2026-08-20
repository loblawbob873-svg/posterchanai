"""COMPILE THE APP'S JAVA AGAINST THE REAL ANDROID SDK, on a box with no Gradle.

The Gradle daemon will not stay up on this machine, so until now the only compile coverage for
anything native was `javac` against tests/androidstubs — a hand-written skeleton of the platform.
That catches a typo and nothing else: a stub of `PackageManager` with one method cannot tell you that
`queryIntentActivities` takes different arguments than you thought, and a stub that is WRONG is worse
than none.

There is an `android.jar` on this box. This module finds it, synthesises the `R` class from the real
res/ directory (aapt generates it during a build, and there is no build here), and compiles whatever
list of sources it is given against the genuine platform API. It is the difference between "the text
I grepped for is present" and "this code compiles against Android 35".

Used by tests/test_android_home_compiles.py and by the SMS and dialer packages' own floors.
"""
import os
import re
import shutil
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "mobile", "android", "app", "src", "main")
JAVA = os.path.join(APP, "java")
RES = os.path.join(APP, "res")
STUBS = os.path.join(ROOT, "tests", "androidstubs")

_CANDIDATES = [
    os.environ.get("ANDROID_HOME", ""),
    os.environ.get("ANDROID_SDK_ROOT", ""),
    os.path.expanduser("~/android-sdk"),
    os.path.expanduser("~/Android/Sdk"),
    "/opt/android-sdk",
    "/usr/lib/android-sdk",
]


def android_jar():
    """The newest platform android.jar on this machine, or None."""
    best = None
    for root in _CANDIDATES:
        if not root:
            continue
        plat = os.path.join(root, "platforms")
        if not os.path.isdir(plat):
            continue
        for name in os.listdir(plat):
            jar = os.path.join(plat, name, "android.jar")
            if not os.path.exists(jar):
                continue
            m = re.search(r"(\d+)", name)
            level = int(m.group(1)) if m else 0
            if best is None or level > best[0]:
                best = (level, jar)
    return best[1] if best else None


def _res_names():
    """{type: {name}} read from the real res/ tree — what aapt would put in R."""
    out = {"id": set(), "layout": set(), "drawable": set(), "mipmap": set(),
           "string": set(), "style": set(), "xml": set(), "color": set(), "array": set(),
           "dimen": set(), "raw": set(), "menu": set(), "anim": set(), "plurals": set(),
           "integer": set(), "bool": set(), "attr": set()}
    if not os.path.isdir(RES):
        return out
    for d in sorted(os.listdir(RES)):
        path = os.path.join(RES, d)
        if not os.path.isdir(path):
            continue
        kind = d.split("-")[0]
        for f in os.listdir(path):
            base = f.split(".")[0]
            if kind == "values":
                try:
                    body = open(os.path.join(path, f), encoding="utf-8").read()
                except OSError:
                    continue
                for m in re.finditer(r"<(string|style|color|dimen|integer|bool|string-array|"
                                     r"integer-array|plurals|attr)\s[^>]*name=\"([^\"]+)\"", body):
                    t = {"string-array": "array", "integer-array": "array"}.get(m.group(1), m.group(1))
                    out.setdefault(t, set()).add(m.group(2).replace(".", "_"))
                continue
            out.setdefault(kind, set()).add(base)
            # `@+id/x` declarations live inside layouts, drawables and xml resources.
            if f.endswith(".xml"):
                try:
                    body = open(os.path.join(path, f), encoding="utf-8").read()
                except OSError:
                    continue
                for m in re.finditer(r'@\+id/([A-Za-z0-9_]+)', body):
                    out["id"].add(m.group(1))
    return out


def write_r(dest_dir, package="place.poster.app"):
    """Synthesise R.java for `package` into dest_dir, returning the file path.

    Values are sequential rather than real resource ids. Nothing here is executed against a device —
    the point is only that every `R.layout.x` and `R.id.y` the code names actually EXISTS, which is
    exactly the mistake that otherwise reaches a phone as a blank screen.
    """
    names = _res_names()
    pkg_dir = os.path.join(dest_dir, *package.split("."))
    os.makedirs(pkg_dir, exist_ok=True)
    body = ["package %s;" % package, "", "public final class R {"]
    n = 0x7F010000
    for kind in sorted(names):
        if not names[kind]:
            continue
        body.append("  public static final class %s {" % kind)
        for name in sorted(names[kind]):
            n += 1
            body.append("    public static final int %s = 0x%08x;" % (name, n))
        body.append("  }")
    body.append("}")
    path = os.path.join(pkg_dir, "R.java")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(body) + "\n")
    return path


def _framework_free_stubs(out_dir):
    """tests/androidstubs with `android/` and `place/` REMOVED.

    Those subtrees exist for the stub-only compiles, and here they are actively harmful: javac
    prefers a source on the -sourcepath over a class on the -classpath, so a one-method stub of
    `android.content.Intent` SHADOWS the real android.jar and every genuine API call fails to
    resolve. The same goes for the placeholder `place.poster.app.*` sources, which would shadow the
    real ones being compiled. What is left — com.getcapacitor and androidx — is what android.jar
    genuinely does not carry.
    """
    dst = os.path.join(out_dir, "stubs")
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(STUBS, dst)
    # `org` too: android.jar carries the real org.json, and the stub of it here is a two-method
    # skeleton that shadows it — which reads as "JSONArray has no optString(int, String)".
    for gone in ("android", "place", "org"):
        shutil.rmtree(os.path.join(dst, gone), ignore_errors=True)
    return dst


def compile_sources(sources, out_dir, extra_sourcepath=(), shims=None):
    """javac the given sources against the real android.jar. Returns CompletedProcess.

    `shims` is {relative/path/Klass.java: source} written into a directory placed FIRST on the
    sourcepath, so those classes are used INSTEAD of the real ones. It exists for one narrow reason:
    a file under test may import something that itself needs a library this box does not have (the
    music service needs androidx.media). Shimming it keeps the compile honest about the file being
    tested rather than dragging half the app in.

    A SHIM MEANS THAT CLASS IS NOT COMPILE-CHECKED HERE. Say so at every call site, and keep the shim
    to the members actually referenced — a shim that grows to cover a whole class is a second copy of
    it, which is exactly the shape this repo has been bitten by.
    """
    javac = shutil.which("javac")
    jar = android_jar()
    gen = os.path.join(out_dir, "gen")
    os.makedirs(gen, exist_ok=True)
    write_r(gen)
    shim_dir = os.path.join(out_dir, "shims")
    for rel, body in (shims or {}).items():
        path = os.path.join(shim_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
    sp = os.pathsep.join([gen, shim_dir, _framework_free_stubs(out_dir), JAVA]
                         + list(extra_sourcepath))
    # NOTHING IS SHARED BETWEEN CONCURRENT RUNS. The generated R, the shims, the copied stubs and
    # the class output all live under the caller's own temp directory — this was checked with four
    # copies of the compile test running at once, because a test that fails when the machine is busy
    # is one people learn to re-run rather than believe, and this repo has already paid for a test
    # nobody believed.
    #
    # The ONE thing that genuinely breaks it is the source tree being EDITED while it runs: javac
    # reads mobile/android/.../java and res/ live, so a half-written file is a compile error that
    # has nothing to do with the test. If this fails during a full run and passes alone, that is
    # what happened.
    return subprocess.run(
        # `--release` would pin us to the JDK's own platform classes, which is the opposite of what
        # is wanted: the point is to compile against ANDROID's java.*, not the desktop JVM's. So
        # -source/-target with an explicit -bootclasspath, and the "bootstrap class path not set"
        # warning that comes with it is suppressed rather than fixed — it is telling us we are doing
        # deliberately the thing we came here to do.
        [javac, "-nowarn", "-Xlint:-options", "-source", "8", "-target", "8",
         "-bootclasspath", jar, "-classpath", jar,
         "-d", os.path.join(out_dir, "classes"), "-sourcepath", sp] + list(sources),
        capture_output=True, text=True, timeout=600)
