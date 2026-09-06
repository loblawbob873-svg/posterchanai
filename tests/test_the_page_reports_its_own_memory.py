"""AN `oom` REPORT WITH NO NUMBER IN IT CANNOT BE ACTED ON.

"posterchan ran out of memory", at start, on Windows -- while the same bundle ran fine on the
PosterChanOS laptop. `render-process-gone` gives a reason and an exit code and nothing else, so the
only honest next step from here was to guess at a cause and change a boot path to fix a symptom
nobody had reproduced.

The page takes the reading instead, while it is still alive to take one, and main.js keeps the last
few. Three numbers, because one word covers three different failures: a JS heap against
`jsHeapSizeLimit` is a document or a cache or a leak; a small heap under a dead process is a working
set full of pixels; a node count in the hundreds of thousands is a runaway document, which is
invisible in both of the others.

The sampler is EXTRACTED AND RUN, because the questions are what it sends and what it does when the
reading is unavailable -- and the second one matters most: `performance.memory` is non-standard and
can be absent or bucketed, and a sampler that answered that with zeroes would report an empty heap,
which is a measurement, and the opposite of the one that was taken.
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(mode):
    out = subprocess.run(["node", str(ROOT / "tests/preload_mem_sampler_sim.mjs"), mode],
                         cwd=ROOT, text=True, capture_output=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_it_reports_the_three_numbers_in_megabytes():
    payload = _run("live")["sent"][0]["payload"]
    assert payload == {"up": 4, "used": 300, "heap": 400, "cap": 2048,
                       "nodes": 18432, "url": "/index.html"}, payload


def test_the_first_reading_is_taken_immediately():
    """A death during boot is the case being chased. A sampler whose first reading lands after the
    crash measures nothing."""
    assert len(_run("live")["sent"]) == 1


def test_it_samples_often_enough_to_catch_a_boot():
    assert _run("live")["timers"] == [2000]


def test_no_reading_is_reported_as_no_reading():
    r = _run("absent")
    assert r["sent"] == [], r["sent"]
    assert r["timers"] == [], "it kept polling something that will never answer"


def test_the_query_string_never_reaches_the_report():
    """This string is written to a file somebody is asked to send us, and a page's query carries the
    instance it was pointed at and a popup's arguments."""
    payload = _run("live")["sent"][0]["payload"]
    assert "secret.example" not in json.dumps(payload), payload
