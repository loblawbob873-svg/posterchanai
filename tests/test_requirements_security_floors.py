"""The requirement FLOORS sit at or above the release that fixed each known advisory.

A floor is what a fresh install, the Docker build and `install.sh` option 6 resolve against, so a
floor below the fixed release lets a node install (or keep) the vulnerable one with nothing said.
Audited 2026-10-21 with pip-audit against venv-unified:

  aiohttp   < 3.14.4      8 advisories (3.14.5 is the current release)
  Pillow    < 13.0.0      8 advisories, every one in an image PARSER (TIFF/PDF/GIF/EPS/PPM) — and
                          this app decodes images strangers upload
  certifi   < 2026.10.19  a revoked CA still trusted
  werkzeug  < 3.1.9       Windows device-name path handling (via Flask, i.e. bundled SearXNG)
  radicale  < 3.8.3       CalDAV/CardDAV server mounted at /caldav; kept on the current patch release

Not here on purpose: transformers is held <5 because ACE-Step cannot load on 5 (see CLAUDE.md), and
python-jose/ecdsa/diskcache have no fixed release.
"""
import re
from pathlib import Path

from packaging.version import Version

ROOT = Path(__file__).resolve().parent.parent

FLOORS = {
    "aiohttp": "3.14.5",
    "pillow": "13.0.0",
    "certifi": "2026.10.19",
    "werkzeug": "3.1.9",
    "radicale": "3.8.3",
}

# Which files must declare which package. requirements-nostr.txt carries no Flask: the SearXNG
# block (werkzeug) is installed from requirements.txt's markers on every build.
EXPECT = {
    "requirements.txt": ["aiohttp", "pillow", "certifi", "werkzeug", "radicale"],
    "requirements-nostr.txt": ["aiohttp", "pillow", "certifi", "radicale"],
}


def _specs(path):
    out = {}
    for raw in (ROOT / path).read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        m = re.match(r"^([A-Za-z0-9_.\-]+)(\[[^\]]*\])?\s*(.*)$", line)
        if m:
            out.setdefault(m.group(1).lower().replace("_", "-"), []).append(m.group(3))
    return out


def _floor(spec):
    m = re.search(r">=\s*([0-9][^,;\s]*)", spec)
    return Version(m.group(1)) if m else None


def test_every_floor_is_at_or_above_the_fixed_release():
    problems = []
    for path, names in EXPECT.items():
        specs = _specs(path)
        for name in names:
            got = specs.get(name)
            if not got:
                problems.append(f"{path}: {name} is not declared")
                continue
            floors = [_floor(s) for s in got]
            if any(f is None or f < Version(FLOORS[name]) for f in floors):
                problems.append(f"{path}: {name} {got} — needs >={FLOORS[name]}")
    assert not problems, "\n".join(problems)
