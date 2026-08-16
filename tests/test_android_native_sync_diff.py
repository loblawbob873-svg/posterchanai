"""Two implementations of the code that decides whether your files get deleted, held to one answer.

The background sweep on Android has to run without the WebView, so `foldersync.js` — the three-way
merge, the conflict rule, delete-loses-to-edit, the mass-delete and mass-resurrect guards — now
exists a second time, in Java. Every one of those rules is in the JavaScript because it once cost
somebody data, and a port that gets any of them subtly wrong does not crash: it produces a plan that
looks reasonable, and the phone and the laptop quietly stop agreeing about what a folder contains.

So this does not test the Java against a description of the rules. It drives the SAME scenarios
through node running the shipped `static/js/client/foldersync.js` and through the Java, and compares
the plans — action for action, reason for reason, in order. Anything the JavaScript decides, the Java
must decide identically, including the parts nobody would think to assert:

  * `sha` is omitted from a download when the manifest entry has none (JSON.stringify drops it);
  * a tombstone dated after the local copy is a delete, and one dated before it is a resurrection;
  * `undefined === undefined` is true, so two entries with no size at all compare equal;
  * an empty chunk list is truthy in JS and compares equal at the same chunk size;
  * the paths are visited in UTF-16 code-unit order, which is what decides the order of the plan.

The fixed scenarios below are the named cases. The fuzz that follows is what actually protects the
port: a few hundred generated three-snapshot worlds, from a fixed seed, over the shapes that make
these rules disagree.
"""
import json
import os
import random
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANDROID = os.path.join(ROOT, "mobile", "android", "app")
JAVA = os.path.join(ANDROID, "src", "main", "java", "place", "poster", "app")
STUBS = os.path.join(ROOT, "tests", "androidstubs")
FSJS = os.path.join(ROOT, "static", "js", "client", "foldersync.js")

SRC = [os.path.join(JAVA, "sync", "Json.java"), os.path.join(JAVA, "sync", "SyncDiff.java")]

DAY = 86400000

JAVA_DRIVER = r"""
    String text = new String(java.nio.file.Files.readAllBytes(
        java.nio.file.Paths.get(argv[0])), "UTF-8");
    for (Object c : Json.arr(Json.parse(text))) {
      java.util.Map<String,Object> m = Json.obj(c);
      java.util.List<String> ex = new java.util.ArrayList<String>();
      for (Object e : Json.arr(m.get("excludes"))) ex.add(String.valueOf(e));
      SyncDiff.Plan p = SyncDiff.diff(snap(m.get("local")), snap(m.get("remote")), snap(m.get("base")),
                                      ex, Json.str(m.get("device"), "this device"),
                                      Json.num(m.get("now"), 0));
      java.util.Map<String,Object> out = new java.util.LinkedHashMap<String,Object>();
      out.put("plan", p.toMap());
      out.put("massDelete", SyncDiff.massDelete(p));
      out.put("massResurrect", SyncDiff.massResurrect(p));
      System.out.println(Json.write(out));
    }
  }

  static java.util.Map<String, java.util.Map<String,Object>> snap(Object v) {
    java.util.Map<String, java.util.Map<String,Object>> out =
        new java.util.LinkedHashMap<String, java.util.Map<String,Object>>();
    for (java.util.Map.Entry<String,Object> e : Json.obj(v).entrySet()) {
      out.put(e.getKey(), Json.obj(e.getValue()));
    }
    return out;
"""

NODE_DRIVER = r"""
const S = require(%s);
const fs = require('fs');
const cases = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
for (const c of cases) {
  const plan = S.diff({ local: c.local, remote: c.remote, base: c.base,
                        excludes: c.excludes, device: c.device, now: c.now });
  console.log(JSON.stringify({ plan, massDelete: S.massDelete(plan),
                               massResurrect: S.massResurrect(plan) }));
}
"""


def _both(cases):
    """Run the scenario list through the shipped JS and through the Java, returning both answers."""
    for t in ("javac", "java", "node"):
        if shutil.which(t) is None:
            pytest.skip("no " + t)
    with tempfile.TemporaryDirectory() as tmp:
        data = os.path.join(tmp, "cases.json")
        with open(data, "w", encoding="utf-8") as fh:
            json.dump(cases, fh)

        js = os.path.join(tmp, "diff.js")
        with open(js, "w", encoding="utf-8") as fh:
            fh.write(NODE_DRIVER % json.dumps(FSJS))
        r = subprocess.run(["node", js, data], capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr[-3000:]
        from_js = [json.loads(x) for x in r.stdout.strip().splitlines()]

        src = os.path.join(tmp, "DiffDriver.java")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write("package place.poster.app.sync;\npublic class DiffDriver {\n"
                     "  public static void main(String[] argv) throws Exception {\n%s\n  }\n}\n"
                     % JAVA_DRIVER)
        out = os.path.join(tmp, "out")
        os.makedirs(out)
        c = subprocess.run(["javac", "-nowarn", "-d", out, "-sourcepath",
                            STUBS + os.pathsep + os.path.join(ANDROID, "src", "main", "java")]
                           + SRC + [src], capture_output=True, text=True, timeout=300)
        assert c.returncode == 0, c.stderr[-4000:]
        r = subprocess.run(["java", "-cp", out, "place.poster.app.sync.DiffDriver", data],
                           capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr[-4000:]
        from_java = [json.loads(x) for x in r.stdout.strip().splitlines()]

    assert len(from_js) == len(cases) and len(from_java) == len(cases)
    return from_js, from_java


def _agree(cases):
    js, java = _both(cases)
    for i, (a, b) in enumerate(zip(js, java)):
        assert a == b, ("the phone and the browser disagree about scenario %d\n%s\n\nJS:   %s\n\nJava: %s"
                        % (i, json.dumps(cases[i], indent=2, sort_keys=True),
                           json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True)))
    return js


def f(csum, size=10, mtime=1000):
    return {"csum": csum, "size": size, "mtime": mtime}


def m(sha, csum=None, size=10, mtime=1000, device=None):
    e = {"sha": sha, "size": size, "mtime": mtime}
    if csum:
        e["csum"] = csum
    if device:
        e["device"] = device
    return e


NAMED = [
    # nothing anywhere
    {"local": {}, "remote": {}, "base": {}},
    # new here / new elsewhere / unchanged
    {"local": {"a.txt": f("A")}, "remote": {}, "base": {}},
    {"local": {}, "remote": {"a.txt": m("s1", "A")}, "base": {}},
    {"local": {"a.txt": f("A")}, "remote": {"a.txt": m("s1", "A")}, "base": {"a.txt": m("s1", "A")}},
    # a download whose manifest entry has NO sha at all — the key must be absent, not null
    {"local": {}, "remote": {"a.txt": {"csum": "A", "size": 10, "mtime": 1000}}, "base": {}},
    # deleted here, deleted there
    {"local": {}, "remote": {"a.txt": m("s1", "A")}, "base": {"a.txt": m("s1", "A")}},
    {"local": {"a.txt": f("A")}, "remote": {"a.txt": {"deletedAt": 4 * DAY}}, "base": {"a.txt": m("s1", "A")}},
    # the already-deleted path that used to re-propose its own deletion for ever
    {"local": {}, "remote": {"a.txt": {"deletedAt": 2 * DAY}}, "base": {"a.txt": {"deletedAt": 2 * DAY}}},
    # a device that lost its agreement must not resurrect a delete…
    {"local": {"a.txt": f("A", mtime=1000)}, "remote": {"a.txt": {"deletedAt": 4 * DAY}}, "base": {}},
    # …but a genuine post-delete edit still wins
    {"local": {"a.txt": f("A", mtime=5 * DAY)}, "remote": {"a.txt": {"deletedAt": 4 * DAY}}, "base": {}},
    # both edited → keep both, and the conflict name carries the other device and its date
    {"local": {"dir/report.pdf": f("MINE", mtime=3 * DAY)},
     "remote": {"dir/report.pdf": m("s2", "THEIRS", mtime=4 * DAY, device="laptop")}, "base": {}},
    # a conflict on a name with no extension, and one with a leading dot
    {"local": {"README": f("MINE")}, "remote": {"README": m("s2", "THEIRS", device="phone")}, "base": {}},
    {"local": {".env": f("MINE")}, "remote": {".env": m("s2", "THEIRS", device="phone")}, "base": {}},
    # the same bytes arriving independently is NOT a conflict
    {"local": {"img.jpg": f("SAME")}, "remote": {"img.jpg": m("s3", "SAME")}, "base": {}},
    # …and neither is it when neither side has hashed: size+mtime inside the slop
    {"local": {"img.jpg": {"size": 10, "mtime": 1000}},
     "remote": {"img.jpg": {"sha": "s3", "size": 10, "mtime": 1001}}, "base": {}},
    # two entries with no size at all — undefined === undefined
    {"local": {"x": {"mtime": 1000}}, "remote": {"x": {"sha": "s", "mtime": 1000}}, "base": {}},
    # chunked files: same list at the same chunk size, different list, and same list at different sizes
    {"local": {"big": {"chunks": ["c1", "c2"], "cs": 16, "size": 99, "mtime": 1}},
     "remote": {"big": {"sha": "s", "chunks": ["c1", "c2"], "cs": 16, "size": 99, "mtime": 9}}, "base": {}},
    {"local": {"big": {"chunks": ["c1", "c2"], "cs": 16, "size": 99, "mtime": 1}},
     "remote": {"big": {"sha": "s", "chunks": ["c1", "c9"], "cs": 16, "size": 99, "mtime": 9}}, "base": {}},
    {"local": {"big": {"chunks": ["c1"], "cs": 4, "size": 99, "mtime": 1}},
     "remote": {"big": {"sha": "s", "chunks": ["c1"], "cs": 16, "size": 99, "mtime": 9}}, "base": {}},
    {"local": {"big": {"chunks": [], "cs": 16, "size": 99, "mtime": 1}},
     "remote": {"big": {"sha": "s", "chunks": [], "cs": 16, "size": 99, "mtime": 9}}, "base": {}},
    # exclusions, including the ones people actually type
    {"local": {"Old/2019/a.jpg": f("A"), "b.jpg": f("B")},
     "remote": {"Old/2019/a.jpg": m("s1", "A")}, "base": {"Old/2019/a.jpg": m("s1", "A")},
     "excludes": ["Old"]},
    {"local": {"a/cache/x": f("A"), "cache/y": f("B")}, "remote": {}, "base": {},
     "excludes": ["**/cache"]},
    {"local": {"Trips/Old/x": f("A"), "Old/y": f("B")}, "remote": {}, "base": {},
     "excludes": ["/Old"]},
    {"local": {"a.tmp": f("A"), "a.txt": f("B")}, "remote": {}, "base": {}, "excludes": ["*.tmp"]},
    {"local": {"OLD/x": f("A")}, "remote": {}, "base": {}, "excludes": ["old"]},
    # a pattern with regex punctuation in it, which must be matched literally
    {"local": {"a+b(1).txt": f("A"), "axb1.txt": f("B")}, "remote": {}, "base": {},
     "excludes": ["a+b(1).txt"]},
    # UTF-16 ordering, which decides the order of the plan
    {"local": {"Z": f("A"), "a": f("B"), "É": f("C"), "0": f("D"), "☃": f("E")},
     "remote": {}, "base": {}},
]


def test_the_named_scenarios_get_the_same_plan_from_both_engines():
    _agree(NAMED)


def test_the_mass_delete_guard_answers_the_same_on_both_sides():
    """A short list is a delete order. The floor, the keep count and what counts as kept all have to
    match, or the phone waves through a sweep the browser would refuse."""
    big_delete = {"local": {}, "remote": {}, "base": {}}
    for i in range(40):
        big_delete["local"]["f%02d.txt" % i] = f("C%d" % i)
        big_delete["remote"]["f%02d.txt" % i] = {"deletedAt": 4 * DAY}
        big_delete["base"]["f%02d.txt" % i] = m("s%d" % i, "C%d" % i)

    # …and one just under the floor, which must never ask.
    small = {"local": {}, "remote": {}, "base": {}}
    for i in range(19):
        small["local"]["f%02d.txt" % i] = f("C%d" % i)
        small["remote"]["f%02d.txt" % i] = {"deletedAt": 4 * DAY}
        small["base"]["f%02d.txt" % i] = m("s%d" % i, "C%d" % i)

    # …and one over the floor where MORE is kept than trashed, which must also never ask.
    kept = json.loads(json.dumps(big_delete))
    for i in range(100):
        kept["local"]["keep%03d.txt" % i] = f("K%d" % i)
        kept["remote"]["keep%03d.txt" % i] = m("k%d" % i, "K%d" % i)
        kept["base"]["keep%03d.txt" % i] = m("k%d" % i, "K%d" % i)

    out = _agree([big_delete, small, kept])
    assert out[0]["massDelete"] and out[0]["massDelete"]["n"] == 40
    assert out[1]["massDelete"] is None, "a 19-file delete asked a question"
    assert out[2]["massDelete"] is None, "a delete smaller than what it keeps asked a question"


def test_the_mass_resurrect_guard_answers_the_same_on_both_sides():
    """The mirror: a restored machine republishing other devices' deletions. An absolute floor, not a
    ratio — the resurrections arrive beside thousands of ordinary uploads."""
    world = {"local": {}, "remote": {}, "base": {}}
    for i in range(25):
        world["local"]["r%02d.txt" % i] = f("R%d" % i, mtime=9 * DAY)
        world["remote"]["r%02d.txt" % i] = {"deletedAt": 4 * DAY}
    for i in range(500):
        world["local"]["plain%03d.txt" % i] = f("P%d" % i)
    out = _agree([world])
    assert out[0]["massResurrect"] and out[0]["massResurrect"]["n"] == 25


def _random_entry(rnd, remote):
    kind = rnd.choice(["live", "live", "live", "tomb", "nohash", "chunked"])
    if kind == "tomb":
        return {"deletedAt": rnd.choice([1 * DAY, 4 * DAY, 9 * DAY])}
    e = {"size": rnd.choice([10, 10, 11, 4096]), "mtime": rnd.choice([1000, 1001, 3000, 9 * DAY])}
    if kind == "live":
        e["csum"] = rnd.choice(["A", "B", "C"])
    elif kind == "chunked":
        e["chunks"] = rnd.choice([["c1"], ["c1", "c2"], ["c9"]])
        e["cs"] = rnd.choice([0, 4, 16])
    if remote:
        if rnd.random() < 0.9:
            e["sha"] = rnd.choice(["s1", "s2"])
        if rnd.random() < 0.4:
            e["device"] = rnd.choice(["laptop", "phone", "the tablet"])
    return e


def test_a_few_hundred_generated_worlds_get_the_same_plan():
    """The fixed cases above are the ones somebody thought of. This is the part that protects the
    port: three snapshots filled independently, so every combination of present / absent / tombstone /
    hashed / unhashed / chunked turns up, including the ones that only matter together."""
    rnd = random.Random(20260815)
    cases = []
    for _ in range(300):
        paths = ["a.txt", "b/c.txt", "b/d.jpg", "Old/e.txt", "f.tmp", "É.txt"]
        case = {"local": {}, "remote": {}, "base": {}, "device": rnd.choice(["laptop", "phone"]),
                "now": rnd.choice([0, 5 * DAY]),
                "excludes": rnd.choice([[], ["Old"], ["*.tmp"], ["**/b"], ["/Old", "*.tmp"]])}
        for p in paths:
            if rnd.random() < 0.7:
                case["local"][p] = _random_entry(rnd, False)
            if rnd.random() < 0.7:
                case["remote"][p] = _random_entry(rnd, True)
            if rnd.random() < 0.5:
                case["base"][p] = _random_entry(rnd, True)
        cases.append(case)
    _agree(cases)


# ---- the battery policy -------------------------------------------------------------------------

POLICY_DRIVER = r"""
    String text = new String(java.nio.file.Files.readAllBytes(
        java.nio.file.Paths.get(argv[0])), "UTF-8");
    for (Object c : Json.arr(Json.parse(text))) {
      java.util.Map<String,Object> m = Json.obj(c);
      System.out.println(Json.write(SyncDiff.shouldSync(Json.obj(m.get("state")),
                                                        Json.obj(m.get("prefs")))));
    }
"""

POLICY_NODE = r"""
const S = require(%s);
const fs = require('fs');
for (const c of JSON.parse(fs.readFileSync(process.argv[2], 'utf8'))) {
  console.log(JSON.stringify(S.shouldSync(c.state, c.prefs)));
}
"""


def _policy(cases):
    for t in ("javac", "java", "node"):
        if shutil.which(t) is None:
            pytest.skip("no " + t)
    with tempfile.TemporaryDirectory() as tmp:
        data = os.path.join(tmp, "cases.json")
        with open(data, "w", encoding="utf-8") as fh:
            json.dump(cases, fh)
        js = os.path.join(tmp, "pol.js")
        with open(js, "w", encoding="utf-8") as fh:
            fh.write(POLICY_NODE % json.dumps(FSJS))
        r = subprocess.run(["node", js, data], capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr[-3000:]
        from_js = [json.loads(x) for x in r.stdout.strip().splitlines()]

        src = os.path.join(tmp, "PolDriver.java")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write("package place.poster.app.sync;\npublic class PolDriver {\n"
                     "  public static void main(String[] argv) throws Exception {\n%s\n  }\n}\n"
                     % POLICY_DRIVER)
        out = os.path.join(tmp, "out")
        os.makedirs(out)
        c = subprocess.run(["javac", "-nowarn", "-d", out, "-sourcepath",
                            STUBS + os.pathsep + os.path.join(ANDROID, "src", "main", "java")]
                           + SRC + [src], capture_output=True, text=True, timeout=300)
        assert c.returncode == 0, c.stderr[-4000:]
        r = subprocess.run(["java", "-cp", out, "place.poster.app.sync.PolDriver", data],
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr[-4000:]
        from_java = [json.loads(x) for x in r.stdout.strip().splitlines()]

    for i, (a, b) in enumerate(zip(from_js, from_java)):
        assert a == b, ("the phone and the browser disagree about when to sync (case %d)\n%s\nJS:   %s\nJava: %s"
                        % (i, json.dumps(cases[i], sort_keys=True), a, b))
    return from_js


def test_when_to_sync_is_the_same_answer_on_both_sides():
    """"Only when plugged in" and "Wi-Fi only" are switches the user can see, and the background
    sweep is the one nobody is watching. A native policy that read them differently would either
    spend somebody's data plan or stop syncing for a reason nothing reports."""
    HOUR = 3600000
    cases = []
    for charging in (True, False, None):
        for metered in (True, False):
            for online in (True, False):
                for battery in (5, 19, 20, 100):
                    for prefs in ({}, {"onlyWhenCharging": True}, {"wifiOnly": False},
                                  {"paused": True}, {"enabled": False}, {"minBattery": 50}):
                        st = {"metered": metered, "online": online, "battery": battery,
                              "now": 100 * HOUR, "lastSyncAt": 99 * HOUR,
                              "lastFullScanAt": 50 * HOUR, "dirty": True}
                        if charging is not None:
                            st["charging"] = charging
                        cases.append({"state": st, "prefs": prefs})
    # the interval and the full-scan cases, which the grid above always makes dirty
    cases += [
        {"state": {"charging": True, "now": 100 * HOUR, "lastSyncAt": 100 * HOUR - 60000,
                   "lastFullScanAt": 100 * HOUR}, "prefs": {}},
        {"state": {"charging": True, "now": 100 * HOUR, "lastSyncAt": 90 * HOUR,
                   "lastFullScanAt": 50 * HOUR}, "prefs": {}},
        {"state": {"charging": False, "now": 100 * HOUR, "lastSyncAt": 90 * HOUR,
                   "lastFullScanAt": 50 * HOUR}, "prefs": {}},
        {"state": {"manual": True, "battery": 3, "metered": True}, "prefs": {"onlyWhenCharging": True}},
        {"state": {"manual": True, "deep": True}, "prefs": {}},
        {"state": {}, "prefs": {}},
    ]
    out = _policy(cases)
    assert any(o["mode"] == "full" for o in out) and any(o["mode"] == "metadata" for o in out)
    assert any(o["why"] == "waiting for Wi-Fi" for o in out)
    assert any(o["why"] == "waiting until you plug in" for o in out)
