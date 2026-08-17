"""The Java reconciler and the JavaScript one must decide identically. Generated inputs, both RUN.

A phone has to sync with its screen off, and a hidden WebView's JavaScript is throttled to about one
timer a minute — so the sweep that runs while the phone is asleep cannot be the JS one. That is why
the rules exist twice, and two implementations of a rule that decides whether files are deleted is
exactly the kind of duplication that drifts silently.

So this does not compare source, or grep for anything: it builds hundreds of folder states, runs BOTH
engines over each one, and compares the plans decision for decision. A single disagreement is a bug
in whichever one is wrong, and it fails here rather than on somebody's Pictures folder.

Android only builds in CI; this needs nothing but javac and node.
"""
import json
import os
import random
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(ROOT, "static", "js", "client")
ANDROID = os.path.join(ROOT, "mobile", "android", "app")
JAVA = os.path.join(ANDROID, "src", "main", "java", "place", "poster", "app", "sync")
STUBS = os.path.join(ROOT, "tests", "androidstubs")

SRC = [os.path.join(JAVA, f + ".java") for f in ("SyncReconcile", "SyncDiff", "Json")]

DRIVER = r"""
package place.poster.app.sync;

import java.io.InputStreamReader;
import java.io.BufferedReader;
import java.util.*;

public class Parity {
  @SuppressWarnings("unchecked")
  public static void main(String[] a) throws Exception {
    StringBuilder sb = new StringBuilder();
    BufferedReader r = new BufferedReader(new InputStreamReader(System.in, "UTF-8"));
    String line;
    while ((line = r.readLine()) != null) sb.append(line);
    List<Object> cases = Json.arr(Json.parse(sb.toString()));
    List<Object> out = new ArrayList<Object>();
    for (Object c : cases) {
      Map<String, Object> o = Json.obj(c);
      Map<String, Map<String, Map<String, Object>>> views =
          new LinkedHashMap<String, Map<String, Map<String, Object>>>();
      for (Map.Entry<String, Object> e : Json.obj(o.get("views")).entrySet()) {
        Map<String, Map<String, Object>> v = new LinkedHashMap<String, Map<String, Object>>();
        for (Map.Entry<String, Object> p : Json.obj(e.getValue()).entrySet())
          v.put(p.getKey(), Json.obj(p.getValue()));
        views.put(e.getKey(), v);
      }
      Map<String, Map<String, Object>> disk = new LinkedHashMap<String, Map<String, Object>>();
      for (Map.Entry<String, Object> e : Json.obj(o.get("disk")).entrySet())
        disk.put(e.getKey(), Json.obj(e.getValue()));
      Map<String, Map<String, Object>> index = new LinkedHashMap<String, Map<String, Object>>();
      for (Map.Entry<String, Object> e : Json.obj(o.get("index")).entrySet())
        index.put(e.getKey(), Json.obj(e.getValue()));
      List<String> ex = new ArrayList<String>();
      for (Object x : Json.arr(o.get("excludes"))) ex.add(String.valueOf(x));

      SyncReconcile.Merged m = SyncReconcile.merge(views);
      SyncReconcile.Plan p = SyncReconcile.reconcile(disk, m, index, ex, "me", 9000L);
      Map<String, Object> res = new LinkedHashMap<String, Object>();
      res.put("fetch", names(p.fetch));
      res.put("send", names(p.send));
      res.put("trash", names(p.trash));
      res.put("tombstone", names(p.tombstone));
      res.put("keepBoth", names(p.keepBoth));
      res.put("settle", names(p.settle));
      res.put("unchanged", (long) p.unchanged);
      res.put("excluded", (long) p.excluded);
      res.put("verdicts", kinds(SyncReconcile.check(p, (int) num(o.get("missing")))));
      out.add(res);
    }
    System.out.println(Json.write(out));
  }
  static long num(Object v){ return v instanceof Number ? ((Number) v).longValue() : 0L; }
  static List<Object> names(List<Map<String, Object>> xs) {
    List<Object> out = new ArrayList<Object>();
    for (Map<String, Object> x : xs) {
      out.add(String.valueOf(x.get("path")) + "|" + String.valueOf(x.get("v"))
              + "|" + String.valueOf(x.get("why")));
    }
    return out;
  }
  static List<Object> kinds(List<Map<String, Object>> xs) {
    List<Object> out = new ArrayList<Object>();
    for (Map<String, Object> x : xs) out.add(String.valueOf(x.get("kind")));
    return out;
  }
}
"""

JS_DRIVER = r"""
const path = require('path');
require(path.join(process.argv[2], 'foldersync.js'));
const E = require(path.join(process.argv[2], 'syncengine.js'));
let raw = '';
process.stdin.on('data', d => raw += d);
process.stdin.on('end', () => {
  const cases = JSON.parse(raw);
  const names = (xs) => xs.map(x => x.path + '|' + x.v + '|' + x.why);
  const out = cases.map(c => {
    const m = E.merge(c.views || {});
    const p = E.reconcile({ disk: c.disk || {}, global: m.global, rivals: m.rivals, by: m.by,
                            index: c.index || {}, device: 'me', now: 9000,
                            excludes: c.excludes || [] });
    return { fetch: names(p.fetch), send: names(p.send), trash: names(p.trash),
             tombstone: names(p.tombstone), keepBoth: names(p.keepBoth), settle: names(p.settle),
             unchanged: p.unchanged, excluded: p.excluded,
             verdicts: E.check(p, { missingViews: c.missing || 0 }).map(v => v.kind) };
  });
  process.stdout.write(JSON.stringify(out));
});
"""


def _need(*tools):
    for t in tools:
        if not shutil.which(t):
            pytest.skip("%s is not installed here" % t)


def _cases(n=250, seed=20260817):
    """Folder states, generated to hit the awkward combinations rather than the average one."""
    rnd = random.Random(seed)
    devices = ["laptop", "phone", "tablet"]
    out = []
    for _ in range(n):
        views, disk, index = {}, {}, {}
        paths = ["a.txt", "b/c.txt", "d.bin", "Old/e.jpg"]
        for dev in rnd.sample(devices, rnd.randint(1, 3)):
            v = {}
            for p in paths:
                if rnd.random() < 0.35:
                    continue
                ver = rnd.randint(0, 3)
                if rnd.random() < 0.3:
                    v[p] = {"v": ver, "by": dev, "deletedAt": rnd.choice([4000, 6000])}
                else:
                    v[p] = {"v": ver, "by": dev, "csum": rnd.choice(["A", "B", "C"]),
                            "sha": "blob", "size": rnd.choice([100, 200]), "mtime": rnd.choice([1000, 3000])}
            views[dev] = v
        for p in paths:
            if rnd.random() < 0.5:
                disk[p] = {"csum": rnd.choice(["A", "B", "C"]), "size": rnd.choice([100, 200]),
                           "mtime": rnd.choice([1000, 3000])}
            if rnd.random() < 0.5:
                e = {"v": rnd.randint(0, 3), "by": rnd.choice(devices)}
                if rnd.random() < 0.3:
                    e["deletedAt"] = 4000
                else:
                    e.update({"csum": rnd.choice(["A", "B", "C"]), "size": 100, "mtime": 1000})
                if rnd.random() < 0.8:
                    e["local"] = {"size": rnd.choice([100, 200]), "mtime": rnd.choice([1000, 3000])}
                    if rnd.random() < 0.5:
                        e["local"]["csum"] = rnd.choice(["A", "B", "C"])
                index[p] = e
        out.append({"views": views, "disk": disk, "index": index,
                    "excludes": rnd.choice([[], ["Old"], ["*.bin"]]),
                    "missing": rnd.choice([0, 0, 0, 1])})
    return out


def _run_java(cases):
    with tempfile.TemporaryDirectory() as tmp:
        drv = os.path.join(tmp, "Parity.java")
        with open(drv, "w", encoding="utf-8") as fh:
            fh.write(DRIVER)
        out = os.path.join(tmp, "out")
        os.makedirs(out)
        c = subprocess.run(["javac", "-nowarn", "-d", out, "-sourcepath",
                            STUBS + os.pathsep + os.path.join(ANDROID, "src", "main", "java")]
                           + SRC + [drv], capture_output=True, text=True, timeout=300)
        assert c.returncode == 0, c.stderr[-4000:]
        r = subprocess.run(["java", "-cp", out, "place.poster.app.sync.Parity"],
                           input=json.dumps(cases), capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr[-4000:]
        return json.loads(r.stdout)


def _run_js(cases):
    with tempfile.TemporaryDirectory() as tmp:
        drv = os.path.join(tmp, "parity.js")
        with open(drv, "w", encoding="utf-8") as fh:
            fh.write(JS_DRIVER)
        r = subprocess.run(["node", drv, CLIENT], input=json.dumps(cases),
                           capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr[-4000:]
        return json.loads(r.stdout)


def test_the_two_engines_decide_identically():
    _need("javac", "java", "node")
    cases = _cases()
    a, b = _run_js(cases), _run_java(cases)
    assert len(a) == len(b) == len(cases)
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            diff = {k: (x[k], y[k]) for k in x if x[k] != y.get(k)}
            raise AssertionError(
                "case %d: the engines disagree\n  input: %s\n  js vs java: %s"
                % (i, json.dumps(cases[i])[:900], json.dumps(diff)[:1200]))


def test_the_generated_cases_actually_exercise_every_decision():
    """A parity test that compares two empty plans passes for the wrong reason."""
    _need("node")
    seen = set()
    for plan in _run_js(_cases()):
        for k in ("fetch", "send", "trash", "tombstone", "keepBoth", "settle"):
            if plan[k]:
                seen.add(k)
        for v in plan["verdicts"]:
            seen.add(v)
    for want in ("fetch", "send", "trash", "tombstone", "keepBoth", "settle"):
        assert want in seen, "no generated case ever produced a %s — the parity test proves nothing" % want
